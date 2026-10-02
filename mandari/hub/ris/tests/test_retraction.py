# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rücknahme einzelner Objekte des RIS-Bestands mit Ereignis (``hub.ris.retraction``, Issue #707).

Der Ingestor meldet eine Löschmarkierung nur beim Übergang ``deleted = false → true``. Markiert die
Anwendung selbst (mandari Session nimmt ein Objekt sofort aus dem Bürgerportal), meldet sie die Rücknahme
deshalb selbst – mit denselben Ereignissen, wie sie der Ingestor bildete, und nie doppelt.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.utils import timezone

from apps.events.models import Event
from hub.ris import retraction
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

BASIS = "https://ris.example.org/oparl"
INGESTOR_DIR = Path(__file__).resolve().parents[4] / "ingestor"


def _ingestor_ris_events() -> ModuleType:
    """``ingestor/src/storage/ris_events.py`` (braucht nur die Standardbibliothek und ``mandari_oparl``)."""
    pfad = INGESTOR_DIR / "src" / "storage" / "ris_events.py"
    spec = importlib.util.spec_from_file_location("ingestor_ris_events_ruecknahme", pfad)
    assert spec is not None and spec.loader is not None, f"nicht gefunden: {pfad}"
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture(autouse=True)
def _eingeschaltet(settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = True


@pytest.fixture
def bestand() -> dict[str, Any]:
    quelle = OParlSource.objects.create(name="RIS", url=f"{BASIS}/system")
    body = OParlBody.objects.create(external_id=f"{BASIS}/body/1", source=quelle, name="Musterstadt")
    sitzung = OParlMeeting.objects.create(external_id=f"{BASIS}/meeting/1", body=body, name="Rat")
    gremium = OParlOrganization.objects.create(external_id=f"{BASIS}/organization/1", body=body, name="Rat")
    person = OParlPerson.objects.create(external_id=f"{BASIS}/person/1", body=body, name="Erika Muster")
    return {
        "quelle": quelle,
        "body": body,
        "sitzung": sitzung,
        "gremium": gremium,
        "vorlage": OParlPaper.objects.create(external_id=f"{BASIS}/paper/1", body=body, name="Radweg"),
        "punkt": OParlAgendaItem.objects.create(external_id=f"{BASIS}/agendaitem/1", meeting=sitzung, name="Radweg"),
        "mitgliedschaft": OParlMembership.objects.create(
            external_id=f"{BASIS}/membership/1", person=person, organization=gremium
        ),
    }


def test_markiert_und_meldet_in_einem_zug(bestand: dict[str, Any]) -> None:
    vorlage = bestand["vorlage"]

    assert retraction.retract(vorlage, reason="nichtoeffentlich") is True

    vorlage.refresh_from_db()
    assert vorlage.deleted is True and vorlage.deleted_at is not None
    (ereignis,) = Event.objects.all()
    assert ereignis.type == "ris.object.depublished"
    assert (ereignis.aggregate_type, ereignis.aggregate_id) == ("Paper", vorlage.pk)
    assert ereignis.tenant_ref == f"source:{bestand['quelle'].pk}"
    assert ereignis.body_id == bestand["body"].pk
    assert (ereignis.visibility, ereignis.operation) == ("oeffentlich", "delete")
    assert ereignis.payload == {"object_type": "Paper", "object": str(vorlage.pk), "reason": "nichtoeffentlich"}
    assert ereignis.occurred_at == vorlage.deleted_at
    assert ereignis.seq is None, "die Folgenummer vergibt der Sequenzierer nach dem Commit"


def test_schon_markiert_meldet_nichts(bestand: dict[str, Any]) -> None:
    """Gleich wer zuerst markiert (Anwendung oder Ingestor): eine Rücknahme, ein Ereignis."""
    vorlage = bestand["vorlage"]
    assert retraction.retract(vorlage, reason="quelle_geloescht") is True
    veraltet = OParlPaper.objects.get(pk=vorlage.pk)
    veraltet.deleted = False  # Stand eines Aufrufers, der die Markierung noch nicht kennt

    assert retraction.retract(veraltet, reason="quelle_geloescht") is False
    assert retraction.retract(vorlage, reason="quelle_geloescht") is False
    assert Event.objects.count() == 1


def test_ausgeschaltet_nur_markiert(bestand: dict[str, Any], settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = False

    assert retraction.retract(bestand["vorlage"], reason="nichtoeffentlich") is True

    assert OParlPaper.objects.get(pk=bestand["vorlage"].pk).deleted is True
    assert not Event.objects.exists()


def test_ausgenommene_quelle_meldet_nichts(bestand: dict[str, Any]) -> None:
    """Wie beim Ingestor: ``sync_config["events_enabled"] = false`` nimmt die Quelle aus."""
    quelle = bestand["quelle"]
    quelle.sync_config = {"events_enabled": False}
    quelle.save(update_fields=["sync_config"])

    assert retraction.retract(bestand["vorlage"], reason="nichtoeffentlich") is True
    assert not Event.objects.exists()


def test_tagesordnungspunkt_gehoert_zur_kommune_seiner_sitzung(bestand: dict[str, Any]) -> None:
    punkt = bestand["punkt"]

    retraction.retract(punkt, reason="nichtoeffentlich")

    (ereignis,) = Event.objects.all()
    assert (ereignis.type, ereignis.body_id) == ("ris.object.depublished", bestand["body"].pk)
    assert ereignis.payload["object_type"] == "AgendaItem"


def test_nichtoeffentlicher_punkt_nur_an_nichtoeffentliche_empfaenger(bestand: dict[str, Any]) -> None:
    """Öffentliche Empfänger haben ihn nie gesehen; eine öffentliche Rücknahme nennte erstmals seine Kennung."""
    punkt = bestand["punkt"]
    OParlAgendaItem.objects.filter(pk=punkt.pk).update(public=False)

    retraction.retract(punkt, reason="quelle_geloescht")

    (ereignis,) = Event.objects.all()
    assert ereignis.type == "ris.agendaitem.changed"
    assert (ereignis.visibility, ereignis.operation) == ("nichtoeffentlich", "delete")
    assert ereignis.payload == {
        "agenda_item": str(punkt.pk),
        "meeting": str(bestand["sitzung"].pk),
        "change": "deleted",
    }


def test_mitgliedschaft_gehoert_zur_kommune_ihres_gremiums(bestand: dict[str, Any]) -> None:
    retraction.retract(bestand["mitgliedschaft"], reason="quelle_geloescht")

    (ereignis,) = Event.objects.all()
    assert ereignis.body_id == bestand["body"].pk
    assert ereignis.payload["object_type"] == "Membership"


def test_ohne_kommune_kein_ereignis(bestand: dict[str, Any]) -> None:
    datei = OParlFile.objects.create(external_id=f"{BASIS}/file/1", name="Anlage")

    assert retraction.retract(datei, reason="quelle_geloescht") is True
    assert not Event.objects.exists()


def test_scheitert_die_meldung_bleibt_die_ruecknahme(
    bestand: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nichtöffentliches muss das Bürgerportal sofort verlassen, auch wenn das Journal nicht schreibt."""

    def kaputt(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Journal nicht erreichbar")

    monkeypatch.setattr(retraction, "publish", kaputt)

    with caplog.at_level(logging.ERROR, logger="hub.ris.retraction"):
        assert retraction.retract(bestand["vorlage"], reason="nichtoeffentlich") is True

    assert OParlPaper.objects.get(pk=bestand["vorlage"].pk).deleted is True
    assert not Event.objects.exists()
    assert "ohne Ereignis" in caplog.text


def test_unbekannter_grund_wird_abgelehnt(bestand: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        retraction.retract(bestand["vorlage"], reason="datenschutz")
    assert OParlPaper.objects.get(pk=bestand["vorlage"].pk).deleted is False


def test_zeitpunkt_der_ruecknahme(bestand: dict[str, Any]) -> None:
    zeitpunkt = timezone.now().replace(microsecond=0)

    retraction.retract(bestand["vorlage"], reason="quelle_geloescht", when=zeitpunkt)

    assert Event.objects.get().occurred_at == zeitpunkt
    assert bestand["vorlage"].deleted_at == zeitpunkt


# =============================================================================
# Gleich wie der Ingestor
# =============================================================================


@pytest.mark.parametrize("modell", list(retraction.AGGREGATE_TYPES))
@pytest.mark.parametrize("grund", sorted(retraction.REASONS))
def test_dieselben_ereignisse_wie_der_ingestor(modell: type, grund: str) -> None:
    """Typ, Aggregat, Sichtbarkeit, Operation und Nutzlast wie ``ris_events.depublished_draft``."""
    ris_events = _ingestor_ris_events()
    kennung = uuid.uuid4()
    art = next(art for art, name in ris_events.AGGREGATE_TYPES.items() if name == retraction.AGGREGATE_TYPES[modell])

    (unser,) = retraction.drafts(retraction.AGGREGATE_TYPES[modell], kennung, grund)
    ihrer = ris_events.depublished_draft(art, kennung, grund)

    assert (unser.type, unser.aggregate_type, unser.aggregate_id) == (ihrer.type, ihrer.aggregate_type, kennung)
    assert (unser.visibility, unser.operation, unser.payload) == (ihrer.visibility, ihrer.operation, ihrer.payload)


def test_nichtoeffentlicher_punkt_wie_der_ingestor() -> None:
    ris_events = _ingestor_ris_events()
    punkt, sitzung = uuid.uuid4(), uuid.uuid4()

    (unser,) = retraction.drafts("AgendaItem", punkt, "quelle_geloescht", public=False, meeting_id=sitzung)
    (ihrer,) = ris_events.depublished_events("agendaitem", punkt, public=False, meeting_id=sitzung)

    assert (unser.type, unser.visibility, unser.operation, unser.payload) == (
        ihrer.type,
        ihrer.visibility,
        ihrer.operation,
        ihrer.payload,
    )
    assert retraction.drafts("AgendaItem", punkt, "quelle_geloescht", public=False) == []
    assert ris_events.depublished_events("agendaitem", punkt, public=False) == []


def test_typen_wie_der_ingestor() -> None:
    assert set(retraction.AGGREGATE_TYPES.values()) == set(_ingestor_ris_events().AGGREGATE_TYPES.values())


def test_schluessel_der_ausnahme_wie_der_ingestor() -> None:
    quelltext = (INGESTOR_DIR / "src" / "storage" / "database.py").read_text(encoding="utf-8")
    assert f'SYNC_CONFIG_EVENTS_KEY: Final = "{retraction.SYNC_CONFIG_EVENTS_KEY}"' in quelltext
