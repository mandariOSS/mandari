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

import importlib
from datetime import date, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from django.apps import apps as django_apps
from django.test import Client
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionProtocol, SessionTenant
from apps.session.services import portal_publication
from apps.session.tests import test_drehscheibe_beschluesse as beschluesse
from apps.session.tests.test_drehscheibe_beschluesse import BASIS, Welt
from hub.ris import selectors
from hub.ris.canonical import protocol_approval_extension
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource
from insight_sync.session_mirror import SessionMirror

pytestmark = pytest.mark.django_db

#: Gemeinsame Welt der Beschluss-Tests (Mandant, Rat, Sitzung, angemeldete Sitzungsdienst-Rolle)
welt = beschluesse.welt

GENEHMIGUNGSWEG = importlib.import_module("apps.session.migrations.0067_niederschrift_genehmigungsweg")

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
    assert item.implementation_public_note == OEFFENTLICH
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
    assert item.implementation_status is None and item.implementation_public_note is None
    # Die Abstimmung selbst ist öffentlich
    assert item.vote_result == "approved"


def test_freigabe_zurueckgenommen_leert_den_bestand(welt: Welt) -> None:
    welt.beschlusskontrolle_veroeffentlichen()
    top = _beschluss(welt)
    _spiegeln(_api(f"meeting/{welt.sitzung.pk}/"))

    SessionAgendaItem.objects.filter(pk=top.pk).update(implementation_public=False)
    item = OParlAgendaItem.objects.get(meeting=_spiegeln(_api(f"meeting/{welt.sitzung.pk}/")))

    assert item.implementation_status is None and item.implementation_public_note is None
    assert selectors.decision(item).implementation is None


# =============================================================================
# Freigabe am Mandanten: inkrementeller Abgleich des Bestands (modified_since)
# =============================================================================


def _abruf(url: str) -> dict[str, Any]:
    ziel = urlsplit(url)
    antwort = Client().get(f"{ziel.path}?{ziel.query}" if ziel.query else ziel.path)
    assert antwort.status_code == 200, (url, antwort.status_code)
    daten: dict[str, Any] = antwort.json()
    return daten


def _abgleichen(welt: Welt, *, full: bool = False) -> None:
    """Abgleich der registrierten Quelle wie Ingestor bzw. ``sync_session_insight`` (inkrementell: ab letztem Lauf)."""
    source = OParlSource.objects.get(sync_config__session_tenant=welt.tenant.slug)
    cast(Any, SessionMirror)(source, fetch=_abruf).sync(full=full)


def _im_bestand(top: SessionAgendaItem) -> OParlAgendaItem:
    return OParlAgendaItem.objects.get(external_id=f"{BASIS}agendaitem/{top.pk}/")


def _schalter(welt: Welt, *, an: bool) -> None:
    antwort = welt.client.post(welt.url("/settings/implementation-publish/"), {"publish": "1" if an else "0"})
    assert antwort.status_code == 302, antwort.status_code


def _geaendert(top: SessionAgendaItem) -> datetime:
    return SessionAgendaItem.objects.values_list("updated_at", flat=True).get(pk=top.pk)


def _zurueckdatieren(*tops: SessionAgendaItem) -> datetime:
    """Letzte Änderung der TOPs eine Stunde zurück: Ein Vorrücken ist unabhängig von der Uhrauflösung sichtbar."""
    frueher = timezone.now() - timedelta(hours=1)
    SessionAgendaItem.objects.filter(pk__in=[top.pk for top in tops]).update(updated_at=frueher)
    return frueher


def test_schalter_am_mandanten_aus_leert_den_bestand_beim_naechsten_abgleich(welt: Welt) -> None:
    """
    Nimmt die Verwaltung den Schalter „Umsetzungsstand veröffentlichen“ zurück, liest schon der inkrementelle
    Abgleich die Beschlüsse neu: Umsetzungsstand und Statusmeldung verschwinden sofort aus dem Bestand und der
    offenen Schnittstelle, nicht erst beim nächtlichen Vollabgleich. Wieder an: ebenso zurück.
    """
    welt.beschlusskontrolle_veroeffentlichen()
    top = _beschluss(welt)
    _abgleichen(welt, full=True)
    assert _im_bestand(top).implementation_status == "in_progress"

    _schalter(welt, an=False)
    _abgleichen(welt)

    item = _im_bestand(top)
    assert item.implementation_status is None and item.implementation_public_note is None
    assert selectors.decision(item).implementation is None
    # Die Abstimmung selbst bleibt öffentlich
    assert item.vote_result == "approved"

    _schalter(welt, an=True)
    _abgleichen(welt)

    item = _im_bestand(top)
    assert (item.implementation_status, item.implementation_public_note) == ("in_progress", OEFFENTLICH)


@pytest.mark.parametrize(
    "ende",
    [SessionTenant.PORTAL_END_PAUSED, SessionTenant.PORTAL_END_ARCHIVED, SessionTenant.PORTAL_END_WITHDRAWN],
)
def test_ende_und_wiederaufnahme_der_veroeffentlichung_melden_die_beschluesse_als_geaendert(
    welt: Welt, ende: str
) -> None:
    """
    Beendet die Verwaltung die Veröffentlichung im Bürgerportal, gibt die Schnittstelle den Umsetzungsstand nicht
    mehr aus (Regel der Beschlussseiten); mit „Wieder veröffentlichen“ wieder. Beides zählt als Änderung der
    Beschlüsse, damit Abgleiche mit ``modified_since`` sie neu lesen.
    """
    welt.beschlusskontrolle_veroeffentlichen()
    top = _beschluss(welt)
    vorher = _zurueckdatieren(top)

    portal_publication.end_publication(welt.tenant, ende)

    assert _geaendert(top) > vorher
    assert "mandari:implementation" not in _api(f"agendaitem/{top.pk}/")

    vorher = _zurueckdatieren(top)
    portal_publication.resume_publication(welt.tenant)

    assert _geaendert(top) > vorher
    assert _api(f"agendaitem/{top.pk}/")["mandari:implementation"]["status"] == "in_progress"


def test_nur_freigegebene_beschluesse_gelten_als_geaendert(welt: Welt) -> None:
    """Beschlüsse, deren Umsetzungsstand ohnehin nie öffentlich ist, behalten ihren Zeitpunkt; ebenso ohne Wechsel."""
    welt.beschlusskontrolle_veroeffentlichen()
    freigegeben = _beschluss(welt)
    nicht_freigegeben = welt.top(2)
    welt.erfassen(nicht_freigegeben, vote_result="approved")
    SessionAgendaItem.objects.filter(pk=nicht_freigegeben.pk).update(implementation_public=False)
    abgelehnt = welt.top(3)
    welt.erfassen(abgelehnt, vote_result="rejected")
    nichtoeffentlich = welt.top(4, is_public=False)
    welt.erfassen(nichtoeffentlich, vote_result="approved")
    vorher = _zurueckdatieren(freigegeben, nicht_freigegeben, abgelehnt, nichtoeffentlich)

    # Gespeichert ohne Wechsel der Freigabe: nichts rückt vor
    welt.tenant.save(update_fields=["implementation_publish", "updated_at"])
    assert _geaendert(freigegeben) == vorher

    _schalter(welt, an=False)

    assert _geaendert(freigegeben) > vorher
    for top in (nicht_freigegeben, abgelehnt, nichtoeffentlich):
        assert _geaendert(top) == vorher


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


def _frueher_veroeffentlicht(welt: Welt, tage: int = 3) -> date:
    """Genehmigung und Veröffentlichung um ``tage`` zurück: Die erneute Veröffentlichung fällt auf einen anderen Tag."""
    protokoll = SessionProtocol.objects.get(meeting=welt.sitzung)
    assert protokoll.approved_at is not None and protokoll.published_at is not None
    damals = protokoll.approved_at - timedelta(days=tage)
    SessionProtocol.objects.filter(pk=protokoll.pk).update(
        approved_at=damals, published_at=protokoll.published_at - timedelta(days=tage)
    )
    return timezone.localtime(damals).date()


def test_direkt_veroeffentlicht_bleibt_direkt_nach_ruecknahme_und_erneuter_veroeffentlichung(welt: Welt) -> None:
    """
    Berichtigung mit Rücknahme und erneuter Veröffentlichung: Nur ``published_at`` ändert sich. Die Schnittstelle
    meldet weiter „ohne Genehmigungsschritt“, nie eine Genehmigung in der Folgesitzung, die es nicht gab.
    """
    welt.tenant.protocol_approval_mode = SessionTenant.PROTOCOL_APPROVAL_DIRECT
    welt.tenant.save(update_fields=["protocol_approval_mode"])
    _niederschrift_veroeffentlichen(welt)
    tag = _frueher_veroeffentlicht(welt)

    welt.niederschrift("unpublish")
    welt.niederschrift("publish")

    protokoll = SessionProtocol.objects.get(meeting=welt.sitzung)
    assert protokoll.status == "published" and protokoll.published_at != protokoll.approved_at
    daten = _api(f"meeting/{welt.sitzung.pk}/")
    assert daten["mandari:protocolApproval"] == {"mode": "direct", "date": tag.isoformat()}
    sitzung = _spiegeln(daten)
    assert (sitzung.protocol_approval_mode, sitzung.protocol_approved_on) == ("direct", tag)


def test_folgesitzung_bleibt_nach_ruecknahme_und_erneuter_veroeffentlichung(welt: Welt) -> None:
    _niederschrift_veroeffentlichen(welt)
    tag = _frueher_veroeffentlicht(welt)

    welt.niederschrift("unpublish")
    welt.niederschrift("publish")

    assert _api(f"meeting/{welt.sitzung.pk}/")["mandari:protocolApproval"] == {
        "mode": "follow_up",
        "date": tag.isoformat(),
    }


def test_genehmigungsweg_steht_an_der_niederschrift(welt: Welt) -> None:
    _niederschrift_veroeffentlichen(welt)
    protokoll = SessionProtocol.objects.get(meeting=welt.sitzung)
    assert protokoll.approval_mode == SessionTenant.PROTOCOL_APPROVAL_FOLLOW_UP


def test_ohne_angabe_gelten_die_zeitpunkte(welt: Welt) -> None:
    """Niederschriften ohne festgehaltenen Weg (Admin, ältere Stände): gleiche Zeitpunkte = ohne Genehmigungsschritt."""
    welt.tenant.protocol_approval_mode = SessionTenant.PROTOCOL_APPROVAL_DIRECT
    welt.tenant.save(update_fields=["protocol_approval_mode"])
    _niederschrift_veroeffentlichen(welt)
    SessionProtocol.objects.filter(meeting=welt.sitzung).update(approval_mode="")

    assert _api(f"meeting/{welt.sitzung.pk}/")["mandari:protocolApproval"]["mode"] == "direct"


def test_datenmigration_befuellt_den_genehmigungsweg(welt: Welt) -> None:
    """
    0067 befüllt bestehende genehmigte Niederschriften: gleiche Zeitpunkte bzw. Audit „veröffentlicht aus der
    Prüfung“ = ohne Genehmigungsschritt (auch nach erneuter Veröffentlichung), sonst Folgesitzung; Gesetztes und
    nicht Genehmigtes bleibt, ein zweiter Lauf ändert nichts.
    """
    welt.tenant.protocol_approval_mode = SessionTenant.PROTOCOL_APPROVAL_DIRECT
    welt.tenant.save(update_fields=["protocol_approval_mode"])
    _niederschrift_veroeffentlichen(welt)
    direkt = SessionProtocol.objects.get(meeting=welt.sitzung)
    assert direkt.approved_at is not None
    # Vor diesem Stand erneut veröffentlicht: Zeitpunkte verschieden, nur das Audit kennt den Weg
    SessionProtocol.objects.filter(pk=direkt.pk).update(
        approval_mode="", published_at=direkt.approved_at + timedelta(days=2)
    )

    def niederschrift(name: str, **felder: Any) -> SessionProtocol:
        sitzung = SessionMeeting.objects.create(
            tenant=welt.tenant, name=name, organization=welt.rat, start=timezone.now() - timedelta(days=9)
        )
        return SessionProtocol.objects.create(meeting=sitzung, **felder)

    jetzt = timezone.now()
    gleich = niederschrift("Gleich", status="published", approved_at=jetzt, published_at=jetzt)
    folge = niederschrift("Folge", status="published", approved_at=jetzt - timedelta(days=5), published_at=jetzt)
    genehmigt = niederschrift("Genehmigt", status="approved", approved_at=jetzt)
    gesetzt = niederschrift(
        "Gesetzt", status="published", approved_at=jetzt, published_at=jetzt, approval_mode="follow_up"
    )
    entwurf = niederschrift("Entwurf")

    for _ in range(2):
        GENEHMIGUNGSWEG.befuellen(django_apps, None)
        weg = dict(SessionProtocol.objects.values_list("pk", "approval_mode"))
        assert weg == {
            direkt.pk: "direct",
            gleich.pk: "direct",
            folge.pk: "follow_up",
            genehmigt.pk: "follow_up",
            gesetzt.pk: "follow_up",
            entwurf.pk: "",
        }


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
