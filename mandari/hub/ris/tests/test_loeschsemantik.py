# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Löschsemantik des RIS-Bestands (Issue #524, ADRs ``docs/adr/20260929-kanonisches-modell.md`` und
``docs/adr/20260929-fremdschluessel-ris-bestand.md``).

- Objekte werden markiert, nicht gelöscht: ``deleted`` mit Grund (``quelle_geloescht``) bzw. ``depublished``
  (``zurueckgenommen``, ``nichtoeffentlich``, ``datenschutz``).
- Kein Modell außerhalb des RIS-Bestands zeigt mit ``CASCADE`` auf ihn (Fitnessfunktion); die Ausnahmen stehen in einer
  Liste, die nur kürzer werden darf.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.apps import apps
from django.db import models

from hub.ris import retraction
from insight_core.models import (
    DELETION_REASON_CHOICES,
    REASON_DELETED_AT_SOURCE,
    REASON_NOT_PUBLIC,
    REASON_PRIVACY,
    REASON_WITHDRAWN,
    OParlBody,
    OParlMeeting,
    OParlSource,
)
from insight_sync.session_mirror import SessionMirror

#: Relationen mit ``CASCADE`` auf den RIS-Bestand, die (noch) erlaubt sind: Daten des Bürgerportals und abgeleitete
#: Geodaten in insight_core selbst. Über sie ist eine Grundsatzentscheidung offen (Kommentar in Issue #524); die Liste
#: darf nur kürzer werden, Ziel ist null.
AUSNAHMEN_CASCADE = frozenset(
    {
        "insight_core.Address.body",
        "insight_core.InsightSubscriber.body",
        "insight_core.LocationMapping.body",
        "insight_core.PageFeedback.body",
        "insight_core.PaperLocation.body",
        "insight_core.PaperLocation.paper",
        "insight_core.PaperPlanReference.body",
        "insight_core.PaperPlanReference.paper",
        "insight_core.PlanBoundary.body",
        "insight_core.PlanBoundarySource.body",
        "insight_core.PublicQuestion.body",
        "insight_core.PublicQuestion.recipient",
        "insight_core.Street.body",
    }
)

RIS = "https://ris.example.org/oparl"
SESSION = "https://mandari.example/session/nord/api/oparl/"


def _ris_bestand() -> set[type[models.Model]]:
    return {m for m in apps.get_app_config("insight_core").get_models() if m.__name__.startswith("OParl")}


def _verweise_auf_den_bestand() -> dict[str, str]:
    """``<app>.<Modell>.<Feld>`` -> ``on_delete`` für jede Relation von außerhalb auf den RIS-Bestand."""
    bestand = _ris_bestand()
    gefunden: dict[str, str] = {}
    for model in apps.get_models():
        if model in bestand:
            continue
        for feld in model._meta.get_fields():
            if not (feld.many_to_one or feld.one_to_one) or not feld.concrete:
                continue
            if feld.related_model in bestand:
                remote = cast(Any, feld).remote_field
                gefunden[f"{model._meta.label}.{feld.name}"] = remote.on_delete.__name__
    return gefunden


def test_kein_verweis_auf_den_ris_bestand_mit_cascade() -> None:
    verweise = _verweise_auf_den_bestand()
    cascade = {name for name, on_delete in verweise.items() if on_delete not in ("PROTECT", "SET_NULL")}

    assert cascade - AUSNAHMEN_CASCADE == set(), "Verweise auf den RIS-Bestand: PROTECT oder SET_NULL, nie CASCADE"
    # Abgebaute Ausnahmen aus der Liste streichen, damit sie nicht zurückkommen
    assert AUSNAHMEN_CASCADE - cascade == set()
    # Work und Mandanten zeigen auf den Bestand; keiner dieser Verweise ist CASCADE (Issue #420, #421, #524)
    assert any(name.startswith("work.") for name in verweise)
    assert not {name for name in cascade if not name.startswith("insight_core.")}


def test_gruende_wie_im_vertrag_der_ruecknahme() -> None:
    codes = {code for code, _ in DELETION_REASON_CHOICES}
    assert codes == {REASON_DELETED_AT_SOURCE, REASON_WITHDRAWN, REASON_NOT_PUBLIC, REASON_PRIVACY}
    # Die Rücknahme mit Ereignis kennt alle außer datenschutz (das verlangt redact, nicht nur eine Markierung)
    assert codes - {REASON_PRIVACY} == retraction.REASONS


@pytest.fixture
def body(db: None) -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt", slug="muster")


def _sitzung(body: OParlBody, external_id: str = f"{RIS}/meeting/1") -> OParlMeeting:
    return OParlMeeting.objects.create(external_id=external_id, body=body, name="Rat")


@pytest.mark.parametrize(
    ("grund", "zurueckgenommen", "hinweis"),
    [
        (REASON_DELETED_AT_SOURCE, False, "In der Quelle gelöscht"),
        (REASON_WITHDRAWN, True, "Zurückgezogen"),
        (REASON_NOT_PUBLIC, True, "Zurückgezogen"),
        (REASON_PRIVACY, True, "Zurückgezogen"),
    ],
)
def test_markierung_mit_grund(body: OParlBody, grund: str, zurueckgenommen: bool, hinweis: str) -> None:
    sitzung = _sitzung(body)
    assert (sitzung.depublished, sitzung.deletion_label) == (False, "")

    sitzung.mark_deleted(reason=grund)
    sitzung.refresh_from_db()

    assert (sitzung.deleted, sitzung.deletion_reason) == (True, grund)
    assert (sitzung.depublished, sitzung.deletion_label) == (zurueckgenommen, hinweis)
    # Eine zweite Markierung ändert den Grund nicht
    sitzung.mark_deleted(reason=REASON_DELETED_AT_SOURCE)
    sitzung.refresh_from_db()
    assert sitzung.deletion_reason == grund


def test_unbekannter_grund_wird_abgewiesen(body: OParlBody) -> None:
    sitzung = _sitzung(body)
    with pytest.raises(ValueError, match="Unbekannter Grund"):
        sitzung.mark_deleted(reason="irgendwas")
    sitzung.refresh_from_db()
    assert not sitzung.deleted


def test_markierungen_ohne_grund_aus_dem_bestand(body: OParlBody) -> None:
    """Vor #524 markierte Zeilen: Session-Objekte gelten als zurückgezogen, fremde als in der Quelle gelöscht."""
    fremd = _sitzung(body)
    eigen = _sitzung(body, f"{SESSION}meeting/1/")
    OParlMeeting.objects.filter(pk__in=[fremd.pk, eigen.pk]).update(deleted=True, deletion_reason=None)
    fremd.refresh_from_db()
    eigen.refresh_from_db()

    assert (fremd.depublished, fremd.deletion_label) == (False, "In der Quelle gelöscht")
    assert (eigen.depublished, eigen.deletion_label) == (True, "Zurückgezogen")


def test_ruecknahme_speichert_den_grund(body: OParlBody, settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = False
    sitzung = _sitzung(body)

    assert retraction.retract(sitzung, reason=REASON_NOT_PUBLIC)

    sitzung.refresh_from_db()
    assert (sitzung.deleted, sitzung.deletion_reason, sitzung.deletion_label) == (
        True,
        REASON_NOT_PUBLIC,
        "Zurückgezogen",
    )


def test_spiegel_markiert_mit_grund_und_hebt_ihn_wieder_auf(db: None) -> None:
    source = OParlSource.objects.create(name="Nord (Session)", url=SESSION)
    body = OParlBody.objects.create(external_id=f"{SESSION}body/", source=source, name="Nord", slug="nord")
    mirror = cast(Any, SessionMirror)(source, fetch=lambda url: {})
    daten = {"id": f"{SESSION}meeting/1/", "type": "https://schema.oparl.org/1.1/Meeting", "name": "Rat"}
    mirror._upsert_meeting(body, daten)

    mirror._handle_tombstone({**daten, "deleted": True})
    sitzung = OParlMeeting.objects.get(external_id=daten["id"])
    assert (sitzung.deleted, sitzung.deletion_reason) == (True, REASON_DELETED_AT_SOURCE)

    # Die Quelle liefert das Objekt wieder: Markierung und Grund sind aufgehoben
    mirror._upsert_meeting(body, daten)
    sitzung.refresh_from_db()
    assert (sitzung.deleted, sitzung.deletion_reason, sitzung.deletion_label) == (False, None, "")
