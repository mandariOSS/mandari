# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfassung im kanonischen Modell (Issue #525): von Session über die Abbildung in den RIS-Bestand.

- Die Session-Abbildung liefert Beschlussnummer, Abstimmung, Umsetzungsstand (nur mit Freigabe der Verwaltung, nur
  die öffentliche Statusmeldung) und die Genehmigung der veröffentlichten Niederschrift.
- Der Spiegel (wie der Ingestor, dieselbe Übersetzung ``mandari_oparl.extensions``) schreibt sie in die Spalten des
  RIS-Bestands.
- Lese-Fassade und offene Schnittstelle geben daraus dieselben Erweiterungen aus, die Session geliefert hat; das
  Bürgerportal liest die Abstimmung aus dem Bestand, nicht aus Session-Tabellen.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionTenant
from apps.session.tests import test_drehscheibe_beschluesse as beschluesse
from apps.session.tests.test_drehscheibe_beschluesse import BASIS, Welt
from hub.ris import selectors
from hub.ris.canonical import protocol_approval_extension
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource
from insight_sync.session_mirror import SessionMirror

pytestmark = pytest.mark.django_db

#: Gemeinsame Welt der Beschluss-Tests (Mandant, Rat, Sitzung, angemeldete Sitzungsdienst-Rolle)
welt = beschluesse.welt

INTERN = "Vermerk nur für die Verwaltung"
OEFFENTLICH = "Die Ausschreibung läuft."


def _api(pfad: str) -> dict[str, Any]:
    antwort = Client().get(f"/session/nord/api/oparl/{pfad}")
    assert antwort.status_code == 200, antwort.status_code
    daten: dict[str, Any] = antwort.json()
    return daten


def _spiegeln(sitzung_json: dict[str, Any]) -> OParlMeeting:
    """Sitzung samt eingebetteter Tagesordnung so in den Bestand schreiben, wie der Spiegel es tut."""
    source, _ = OParlSource.objects.get_or_create(url=BASIS, defaults={"name": "Nord (Session)"})
    body, _ = OParlBody.objects.get_or_create(
        external_id=f"{BASIS}body/", defaults={"source": source, "name": "Nord", "slug": "nord-spiegel"}
    )
    # Wie in insight_sync/tests/test_session_mirror_ort.py: Der Spiegel ist nicht typisiert
    mirror = cast(Any, SessionMirror)(source, fetch=lambda url: {})
    return cast(OParlMeeting, mirror._upsert_meeting(body, sitzung_json))


def _beschluss(welt: Welt, **werte: Any) -> SessionAgendaItem:
    top = welt.top()
    welt.erfassen(top, vote_result="approved", votes_yes="12", votes_no="3", votes_abstain="1")
    SessionAgendaItem.objects.filter(pk=top.pk).update(
        resolution_number="B/2026/7",
        implementation_status="in_progress",
        implementation_deadline=date(2026, 12, 31),
        implementation_note=INTERN,
        implementation_public_note=OEFFENTLICH,
        implementation_updated_at=timezone.now(),
        **werte,
    )
    return top


def test_umsetzungsstand_mit_freigabe_bis_in_den_bestand(welt: Welt) -> None:
    welt.beschlusskontrolle_veroeffentlichen()
    top = _beschluss(welt)

    punkt = _api(f"agendaitem/{top.pk}/")
    assert punkt["mandari:resolutionNumber"] == "B/2026/7"
    assert punkt["mandari:vote"] == {
        "method": "summary",
        "methodLabel": "Nur Summen",
        "result": "approved",
        "resultLabel": "Angenommen",
        "yes": 12,
        "no": 3,
        "abstain": 1,
    }
    umsetzung = punkt["mandari:implementation"]
    assert {k: umsetzung[k] for k in ("status", "statusLabel", "deadline", "note")} == {
        "status": "in_progress",
        "statusLabel": "In Umsetzung",
        "deadline": "2026-12-31",
        "note": OEFFENTLICH,
    }
    assert "modified" in umsetzung
    # Der interne Erledigungsvermerk verlässt Session nie
    assert INTERN not in str(punkt)

    sitzung = _spiegeln(_api(f"meeting/{welt.sitzung.pk}/"))
    item = OParlAgendaItem.objects.get(meeting=sitzung)
    assert (item.resolution_number, item.vote_method, item.vote_result) == ("B/2026/7", "summary", "approved")
    assert (item.votes_yes, item.votes_no, item.votes_abstain) == (12, 3, 1)
    assert (item.implementation_status, item.implementation_deadline) == ("in_progress", date(2026, 12, 31))
    assert item.implementation_note == OEFFENTLICH
    assert item.roll_call is None

    # Lese-Fassade: dieselben Erweiterungen, wie Session sie geliefert hat
    beschluss = selectors.decision(item)
    assert beschluss.resolution_number == punkt["mandari:resolutionNumber"]
    assert beschluss.vote == punkt["mandari:vote"]
    assert beschluss.implementation == umsetzung
    assert beschluss.roll_call is None


def test_umsetzungsstand_ohne_freigabe_bleibt_in_session(welt: Welt) -> None:
    top = _beschluss(welt)

    punkt = _api(f"agendaitem/{top.pk}/")
    assert "mandari:implementation" not in punkt
    assert OEFFENTLICH not in str(punkt)
    item = OParlAgendaItem.objects.get(meeting=_spiegeln(_api(f"meeting/{welt.sitzung.pk}/")))
    assert item.implementation_status is None and item.implementation_note is None
    # Die Abstimmung selbst ist öffentlich
    assert item.vote_result == "approved"


def test_freigabe_zurueckgenommen_leert_den_bestand(welt: Welt) -> None:
    welt.beschlusskontrolle_veroeffentlichen()
    top = _beschluss(welt)
    _spiegeln(_api(f"meeting/{welt.sitzung.pk}/"))

    SessionAgendaItem.objects.filter(pk=top.pk).update(implementation_public=False)
    item = OParlAgendaItem.objects.get(meeting=_spiegeln(_api(f"meeting/{welt.sitzung.pk}/")))

    assert item.implementation_status is None and item.implementation_note is None
    assert selectors.decision(item).implementation is None


def _niederschrift_veroeffentlichen(welt: Welt, **genehmigung: Any) -> None:
    welt.top()
    welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
    welt.niederschrift("submit")
    if welt.tenant.protocol_approval_mode != SessionTenant.PROTOCOL_APPROVAL_DIRECT:
        welt.niederschrift("approve", **genehmigung)
    welt.niederschrift("publish")


def test_genehmigung_in_der_folgesitzung(welt: Welt) -> None:
    folge = SessionMeeting.objects.create(
        tenant=welt.tenant,
        name="Folgesitzung",
        organization=welt.rat,
        start=(timezone.now() + timedelta(days=14)).replace(second=0, microsecond=0),
        is_public=True,
    )
    _niederschrift_veroeffentlichen(welt, approval_meeting=str(folge.pk))

    daten = _api(f"meeting/{welt.sitzung.pk}/")
    genehmigung = daten["mandari:protocolApproval"]
    assert genehmigung == {
        "mode": "follow_up",
        "date": timezone.localtime(folge.start).date().isoformat(),
        "meeting": f"{BASIS}meeting/{folge.pk}/",
    }

    # Erst die genehmigende Sitzung im Bestand, dann die genehmigte: Die Lese-Fassade findet sie
    folge_im_bestand = _spiegeln(_api(f"meeting/{folge.pk}/"))
    sitzung = _spiegeln(daten)
    assert (sitzung.protocol_approval_mode, sitzung.protocol_approved_on) == (
        "follow_up",
        date.fromisoformat(genehmigung["date"]),
    )
    assert sitzung.protocol_approved_in_external_id == genehmigung["meeting"]
    gelesen = selectors.protocol_approval(sitzung)
    assert gelesen is not None and gelesen.approved_in == folge_im_bestand
    assert protocol_approval_extension(sitzung, genehmigung["meeting"]) == genehmigung


def test_nichtoeffentliche_folgesitzung_wird_nicht_genannt(welt: Welt) -> None:
    folge = SessionMeeting.objects.create(
        tenant=welt.tenant,
        name="Folgesitzung",
        organization=welt.rat,
        start=timezone.now() + timedelta(days=14),
        is_public=False,
    )
    _niederschrift_veroeffentlichen(welt, approval_meeting=str(folge.pk))

    genehmigung = _api(f"meeting/{welt.sitzung.pk}/")["mandari:protocolApproval"]
    assert genehmigung["mode"] == "follow_up"
    assert "meeting" not in genehmigung
    assert genehmigung["date"] == timezone.localdate().isoformat()


def test_direkt_veroeffentlicht(welt: Welt) -> None:
    welt.tenant.protocol_approval_mode = SessionTenant.PROTOCOL_APPROVAL_DIRECT
    welt.tenant.save(update_fields=["protocol_approval_mode"])
    _niederschrift_veroeffentlichen(welt)

    daten = _api(f"meeting/{welt.sitzung.pk}/")
    assert daten["mandari:protocolApproval"] == {"mode": "direct", "date": timezone.localdate().isoformat()}
    sitzung = _spiegeln(daten)
    assert sitzung.protocol_approval_mode == "direct" and sitzung.protocol_approved_in_external_id is None


def test_ohne_veroeffentlichte_niederschrift_keine_genehmigung(welt: Welt) -> None:
    welt.top()
    welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
    welt.niederschrift("submit")
    welt.niederschrift("approve")

    daten = _api(f"meeting/{welt.sitzung.pk}/")
    assert "mandari:protocolApproval" not in daten
    sitzung = _spiegeln(daten)
    assert sitzung.protocol_approval_mode is None
    assert selectors.protocol_approval(sitzung) is None


def test_buergerportal_liest_die_abstimmung_aus_dem_bestand(welt: Welt) -> None:
    """Die Sitzungsseite zeigt die Summen aus den Spalten, auch wenn ``raw_json`` sie nicht (mehr) trägt."""
    _beschluss(welt)
    sitzung = _spiegeln(_api(f"meeting/{welt.sitzung.pk}/"))
    OParlAgendaItem.objects.filter(meeting=sitzung).update(raw_json={})
    OParlMeeting.objects.filter(pk=sitzung.pk).update(raw_json={})
    client = Client()
    session = client.session
    session["active_body_id"] = str(sitzung.body_id)
    session.save()

    antwort = client.get(f"/insight/termine/{sitzung.pk}/")

    assert antwort.status_code == 200
    inhalt = antwort.content.decode()
    assert "Ja 12" in inhalt and "Nein 3" in inhalt and "Enthaltung 1" in inhalt
