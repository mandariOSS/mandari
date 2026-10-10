# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet Sitzungen, Tagesordnung und Ladung an die Datendrehscheibe (Issue #533).

Geprüft an den Fachfunktionen selbst (Ansichten und Dienste, wie die Oberfläche sie aufruft):

- je fachlicher Änderung genau ein Ereignis je Objekt und Empfängerkreis, keines ohne Änderung im kanonischen
  Modell und keines bei ausgeschaltetem Schalter;
- kanonische Kennungen, Mandant ``session:<uuid>`` und die Kommune der Session-Schnittstelle;
- Sichtbarkeit: öffentlich nur, was die Schnittstelle ausliefert; Rücknahmen als ``delete`` mit Grund;
- Reihenfolge: Sitzung vor ihren Tagesordnungspunkten, Punkte in der Reihenfolge der Tagesordnung;
- Schattenbetrieb: Ein Fehler beim Melden lässt die Änderung bestehen, im aktiven Betrieb nimmt er sie zurück.

Jedes Ereignis prüft ``publish()`` in den Tests gegen seinen Vertrag (``EVENTS_VALIDATE_CONTRACTS``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import transaction
from django.db.models import Max
from django.test import Client
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.common.models import IdentifierBase
from apps.common.tests.factories import UserFactory
from apps.events.models import Event
from apps.events.presence import required_roles
from apps.session import hub_events
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionStandardAgendaItem,
    SessionTenant,
    SessionUser,
)
from apps.session.services import invitation_service
from hub.ris.session_events import SessionEvents

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"


def kennung(kind: str, pk: Any) -> str:
    """Kanonische Kennung eines Session-Objekts (wie Schnittstelle und Bestand sie führen)."""
    return str(canonical_id(f"{BASIS}{kind}/{pk}/"))


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    user: SessionUser
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def sitzung(self, **werte: Any) -> SessionMeeting:
        daten = {
            "tenant": self.tenant,
            "name": "Ratssitzung",
            "organization": self.rat,
            "start": _zehn_uhr_in(14),
            "location": "Rathaus",
            "room": "Saal 1",
            "is_public": True,
        }
        daten.update(werte)
        return SessionMeeting.objects.create(**daten)

    def top(self, sitzung: SessionMeeting, nummer: int, **werte: Any) -> SessionAgendaItem:
        daten = {"meeting": sitzung, "name": f"Punkt {nummer}", "number": str(nummer), "order": nummer}
        daten.update(werte)
        return SessionAgendaItem.objects.create(**daten)


def _zehn_uhr_in(tage: int) -> datetime:
    """10 Uhr Ortszeit in ``tage`` Tagen: liegt nie in der doppelten oder fehlenden Stunde der Zeitumstellung,
    auch nicht nach weiteren ganzen Tagen (Formulare lesen Termine als Ortszeit)."""
    return timezone.localtime(timezone.now() + timedelta(days=tage)).replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture
def welt(settings: Any) -> Welt:
    cache.clear()
    settings.SITE_URL = SITE
    settings.SESSION_EVENTS = "aktiv"
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord", oparl_public_since=timezone.now())
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat")
    rolle = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_create_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_view_papers=True,
        can_edit_papers=True,
    )
    user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    user.roles.add(rolle)
    client = Client()
    client.force_login(user.user)
    return Welt(tenant=tenant, rat=rat, user=user, client=client)


def _start() -> int:
    """Höchste Zeile des Journals vor dem Schritt; ``_neu`` liest alles danach."""
    return int(Event.objects.aggregate(hoechste=Max("id"))["hoechste"] or 0)


def _neu(seit: int) -> list[Event]:
    return list(Event.objects.filter(id__gt=seit).order_by("id"))


def _kurz(events: list[Event]) -> list[tuple[str, str, str, str]]:
    """(Typ, Objekttyp, Sichtbarkeit, Operation) je Ereignis."""
    return [(e.type, e.aggregate_type, e.visibility, e.operation) for e in events]


def _formular(sitzung: SessionMeeting, **aenderungen: Any) -> dict[str, Any]:
    """Bearbeitungsformular der Sitzung mit den gespeicherten Werten und den gewünschten Änderungen."""
    daten: dict[str, Any] = {
        "name": sitzung.name,
        "organization": str(sitzung.organization_id),
        "start": timezone.localtime(sitzung.start).strftime("%Y-%m-%dT%H:%M"),
        "location": sitzung.location,
        "room": sitzung.room,
        "format": sitzung.format,
        "meeting_state": sitzung.meeting_state,
        "cancellation_reason": sitzung.cancellation_reason,
    }
    if sitzung.is_public:
        daten["is_public"] = "on"
    if sitzung.date_public:
        daten["date_public"] = "on"
    if sitzung.cancelled:
        daten["cancelled"] = "on"
    for name, wert in aenderungen.items():
        if wert is None:
            daten.pop(name, None)
        else:
            daten[name] = wert
    return daten


def _bearbeiten(welt: Welt, sitzung: SessionMeeting, **aenderungen: Any) -> None:
    antwort = welt.client.post(welt.url(f"/meetings/{sitzung.id}/edit/"), _formular(sitzung, **aenderungen))
    assert antwort.status_code == 302, antwort.content.decode()[:2000]


# =============================================================================
# Schalter
# =============================================================================


class TestSchalter:
    def test_ausgeschaltet_entsteht_nichts(self, welt: Welt, settings: Any) -> None:
        settings.SESSION_EVENTS = "aus"
        seit = _start()
        antwort = welt.client.post(
            welt.url("/meetings/create/"),
            {"name": "Ratssitzung", "organization": str(welt.rat.pk), "start": "2031-03-01T17:00", "is_public": "on"},
        )
        assert antwort.status_code == 302
        assert _neu(seit) == []

    def test_mandant_ueberschreibt_die_installation(self, welt: Welt, settings: Any) -> None:
        settings.SESSION_EVENTS = "aus"
        welt.tenant.hub_events = "schatten"
        welt.tenant.save(update_fields=["hub_events"])
        assert hub_events.mode(welt.tenant) == "schatten"
        seit = _start()
        welt.sitzung()  # direkt angelegt: kein Ereignis (keine Fachfunktion)
        assert _neu(seit) == []
        with hub_events.track(welt.tenant) as tracked:
            neu = welt.sitzung(name="Sondersitzung")
            tracked.meeting(neu, created=True)
        assert _kurz(_neu(seit)) == [("ris.meeting.scheduled", "Meeting", "oeffentlich", "upsert")]

        welt.tenant.hub_events = "aus"
        settings.SESSION_EVENTS = "aktiv"
        assert hub_events.mode(welt.tenant) == "aus"

    def test_worker_mit_sequenzierer_wird_verlangt(self, settings: Any) -> None:
        settings.EVENTS_WORKER_REQUIRED = ""
        settings.INGESTOR_EVENTS_ENABLED = False
        settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
        settings.SESSION_EVENTS = "aus"
        assert "sequencer" not in required_roles()
        settings.SESSION_EVENTS = "schatten"
        assert "sequencer" in required_roles()


# =============================================================================
# Sitzungen
# =============================================================================


class TestSitzungen:
    def test_anlegen_meldet_sitzung_dann_standard_tops(self, welt: Welt) -> None:
        SessionStandardAgendaItem.objects.create(tenant=welt.tenant, name="Eröffnung", placement="start", order=1)
        SessionStandardAgendaItem.objects.create(tenant=welt.tenant, name="Verschiedenes", placement="end", order=1)
        seit = _start()
        antwort = welt.client.post(
            welt.url("/meetings/create/"),
            {"name": "Ratssitzung", "organization": str(welt.rat.pk), "start": "2031-03-01T17:00", "is_public": "on"},
        )
        assert antwort.status_code == 302
        sitzung = SessionMeeting.objects.get(tenant=welt.tenant, name="Ratssitzung")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.meeting.scheduled", "Meeting", "oeffentlich", "upsert"),
            ("ris.agendaitem.changed", "AgendaItem", "oeffentlich", "upsert"),
            ("ris.agendaitem.changed", "AgendaItem", "oeffentlich", "upsert"),
        ]
        termin = events[0]
        assert str(termin.aggregate_id) == kennung("meeting", sitzung.pk)
        assert termin.payload == {
            "meeting": kennung("meeting", sitzung.pk),
            "organizations": [kennung("organization", welt.rat.pk)],
        }
        assert termin.tenant_ref == f"session:{welt.tenant.pk}"
        assert str(termin.body_id) == str(canonical_id(f"{BASIS}body/"))
        assert termin.actor_ref == f"user:{welt.user.user.pk}"
        tops = list(SessionAgendaItem.objects.filter(meeting=sitzung).order_by("order"))
        assert [e.payload["agenda_item"] for e in events[1:]] == [kennung("agendaitem", t.pk) for t in tops]
        assert {e.payload["change"] for e in events[1:]} == {"added"}
        # Alle in einer Transaktion: eine Korrelation
        assert len({e.correlation_id for e in events}) == 1

    def test_nichtoeffentliche_sitzung_nur_nichtoeffentlich(self, welt: Welt) -> None:
        seit = _start()
        antwort = welt.client.post(
            welt.url("/meetings/create/"),
            {"name": "Klausur", "organization": str(welt.rat.pk), "start": "2031-03-01T17:00"},
        )
        assert antwort.status_code == 302
        assert _kurz(_neu(seit)) == [("ris.meeting.scheduled", "Meeting", "nichtoeffentlich", "upsert")]

    def test_aendern_meldet_genau_die_geaenderten_felder(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        seit = _start()
        neu = timezone.localtime(sitzung.start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        _bearbeiten(welt, sitzung, start=neu, room="Saal 2")
        events = _neu(seit)
        assert _kurz(events) == [("ris.meeting.changed", "Meeting", "oeffentlich", "upsert")]
        assert events[0].payload == {"meeting": kennung("meeting", sitzung.pk), "changed": ["start", "location"]}

    def test_speichern_ohne_aenderung_und_interne_felder_melden_nichts(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        seit = _start()
        _bearbeiten(welt, sitzung)
        _bearbeiten(welt, sitzung, invitation_text="Bitte pünktlich erscheinen.")
        assert _neu(seit) == []

    def test_absagen(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        seit = _start()
        _bearbeiten(welt, sitzung, cancelled="on", cancellation_reason="Beschlussunfähig")
        events = _neu(seit)
        assert _kurz(events) == [("ris.meeting.changed", "Meeting", "oeffentlich", "upsert")]
        assert events[0].payload["cancelled"] is True
        assert set(events[0].payload["changed"]) == {"meetingState", "cancelled"}

    def test_nichtoeffentlich_stellen_nimmt_sitzung_ort_und_tops_zurueck(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        top = welt.top(sitzung, 1)
        seit = _start()
        _bearbeiten(welt, sitzung, is_public=None)
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "Meeting", "oeffentlich", "delete"),
            ("ris.object.depublished", "Location", "oeffentlich", "delete"),
            ("ris.meeting.changed", "Meeting", "nichtoeffentlich", "upsert"),
            ("ris.object.depublished", "AgendaItem", "oeffentlich", "delete"),
        ]
        assert {e.payload["reason"] for e in events if e.type == "ris.object.depublished"} == {"nichtoeffentlich"}
        assert events[1].payload["object"] == kennung("location", sitzung.pk)
        assert events[2].payload["changed"] == ["public"]
        assert events[3].payload["object"] == kennung("agendaitem", top.pk)

        # Wieder öffentlich: für die Öffentlichkeit neu, samt Tagesordnung
        sitzung.refresh_from_db()
        seit = _start()
        _bearbeiten(welt, sitzung, is_public="on")
        assert _kurz(_neu(seit)) == [
            ("ris.meeting.scheduled", "Meeting", "oeffentlich", "upsert"),
            ("ris.agendaitem.changed", "AgendaItem", "oeffentlich", "upsert"),
        ]

    def test_termin_einer_nichtoeffentlichen_sitzung(self, welt: Welt) -> None:
        sitzung = welt.sitzung(is_public=False, date_public=True)
        seit = _start()
        # Der Ort gehört nicht zum veröffentlichten Termin: nur die übrigen Empfänger erfahren davon
        _bearbeiten(welt, sitzung, location="Feuerwache")
        events = _neu(seit)
        assert _kurz(events) == [("ris.meeting.changed", "Meeting", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["location"]

        sitzung.refresh_from_db()
        seit = _start()
        neu = timezone.localtime(sitzung.start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        _bearbeiten(welt, sitzung, start=neu, location="Bauhof")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.meeting.changed", "Meeting", "oeffentlich", "upsert"),
            ("ris.meeting.changed", "Meeting", "nichtoeffentlich", "upsert"),
        ]
        assert events[0].payload["changed"] == ["start"]
        assert events[1].payload["changed"] == ["location"]


# =============================================================================
# Tagesordnung
# =============================================================================


class TestTagesordnung:
    def test_top_anlegen_und_neu_nummerieren(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        welt.top(sitzung, 1, name="Eröffnung")
        ende = welt.top(sitzung, 2, name="Verschiedenes", is_end_item=True)
        seit = _start()
        antwort = welt.client.post(
            welt.url(f"/meetings/{sitzung.id}/agenda/add/"), {"name": "Radweg", "is_public": "on"}
        )
        assert antwort.status_code == 302
        neu = SessionAgendaItem.objects.get(meeting=sitzung, name="Radweg")
        events = _neu(seit)
        # Neuer Punkt vor dem Ende-TOP, der dadurch Nummer und Platz wechselt – in der Reihenfolge der Tagesordnung
        assert [(e.payload["agenda_item"], e.payload["change"]) for e in events] == [
            (kennung("agendaitem", neu.pk), "added"),
            (kennung("agendaitem", ende.pk), "changed"),
        ]
        assert set(events[1].payload["changed"]) == {"number", "order"}
        assert all(e.visibility == "oeffentlich" for e in events)

    def test_top_nichtoeffentlich_und_absetzen(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        top = welt.top(sitzung, 1, name="Grundstück")
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/edit/"), {"name": "Grundstück"})
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events)[:2] == [
            ("ris.object.depublished", "AgendaItem", "oeffentlich", "delete"),
            ("ris.agendaitem.changed", "AgendaItem", "nichtoeffentlich", "upsert"),
        ]
        assert events[0].payload["reason"] == "nichtoeffentlich"
        assert "public" in events[1].payload["changed"]

        offen = welt.top(sitzung, 5, name="Spielplatz")
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{offen.id}/withdraw/"), {"reason": "Rückfragen"})
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.agendaitem.changed", "AgendaItem", "oeffentlich", "upsert")]
        assert events[0].payload["change"] == "withdrawn"

    def test_top_loeschen(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        oeffentlich = welt.top(sitzung, 1)
        geheim = welt.top(sitzung, 2, is_public=False)
        seit = _start()
        assert welt.client.post(welt.url(f"/agenda/{geheim.id}/delete/")).status_code == 302
        assert welt.client.post(welt.url(f"/agenda/{oeffentlich.id}/delete/")).status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.agendaitem.changed", "AgendaItem", "nichtoeffentlich", "delete"),
            ("ris.object.depublished", "AgendaItem", "oeffentlich", "delete"),
        ]
        assert events[0].payload["change"] == "deleted"
        assert events[1].payload == {
            "object_type": "AgendaItem",
            "object": kennung("agendaitem", oeffentlich.pk),
            "reason": "quelle_geloescht",
        }

    def test_top_verschieben_meldet_beide_punkte(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        erster = welt.top(sitzung, 1)
        zweiter = welt.top(sitzung, 2)
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{zweiter.id}/move/"), {"direction": "up"})
        assert antwort.status_code == 302
        events = _neu(seit)
        assert [e.payload["agenda_item"] for e in events] == [
            kennung("agendaitem", zweiter.pk),
            kennung("agendaitem", erster.pk),
        ]
        assert all(set(e.payload["changed"]) == {"number", "order"} for e in events)

    def test_vorlage_nur_wenn_veroeffentlicht(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        entwurf = SessionPaper.objects.create(tenant=welt.tenant, name="Entwurf", is_public=True, status="draft")
        events = SessionEvents(tenant_id=welt.tenant.pk, uris=hub_events.events_for(welt.tenant).uris)
        with hub_events.track(welt.tenant) as tracked:
            tracked.agenda(sitzung)
            welt.top(sitzung, 1, paper=entwurf)
        neu = Event.objects.order_by("-id").first()
        assert neu is not None and neu.visibility == "oeffentlich"
        assert "paper" not in neu.payload, "Die Vorlage im Entwurf ist nicht veröffentlicht"
        assert events.ref("paper", entwurf.pk)  # Kennung ließe sich bilden – sie steht nur nicht im Ereignis


# =============================================================================
# Ladung
# =============================================================================


class TestLadung:
    def test_ladung_meldet_versand_und_veroeffentlicht_die_tagesordnung(self, welt: Welt) -> None:
        sitzung = welt.sitzung(meeting_state="scheduled")
        seit = _start()
        dispatch = invitation_service.send_invitations(sitzung, sent_by=welt.user)
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.meeting.invited", "Meeting", "nichtoeffentlich", "upsert"),
            ("ris.meeting.changed", "Meeting", "oeffentlich", "upsert"),
        ]
        assert events[0].payload == {
            "meeting": kennung("meeting", sitzung.pk),
            "dispatch": str(dispatch.pk),
            "dispatch_type": "invitation",
        }
        assert events[1].payload["changed"] == ["meetingState"]

        # Nachtrag: nur der Versand, der Status bleibt
        seit = _start()
        welt.top(sitzung, 1, is_supplementary=True)
        invitation_service.send_invitations(sitzung, sent_by=welt.user, dispatch_type="supplementary")
        events = _neu(seit)
        assert _kurz(events) == [("ris.meeting.invited", "Meeting", "nichtoeffentlich", "upsert")]
        assert events[0].payload["dispatch_type"] == "supplementary"


# =============================================================================
# Schattenbetrieb und Atomarität
# =============================================================================


class TestBetrieb:
    def test_schattenbetrieb_laesst_die_aenderung_bestehen(
        self, welt: Welt, settings: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings.SESSION_EVENTS = "schatten"
        sitzung = welt.sitzung()

        def kaputt(self: Any, drafts: Any) -> int:
            raise RuntimeError("Zeile enthält Ratssitzung (verlegt)")

        monkeypatch.setattr(SessionEvents, "publish", kaputt)
        seit = _start()
        with caplog.at_level(logging.WARNING, logger="apps.session.hub_events"):
            _bearbeiten(welt, sitzung, name="Ratssitzung (verlegt)")
        sitzung.refresh_from_db()
        assert sitzung.name == "Ratssitzung (verlegt)"
        assert _neu(seit) == []
        assert "Schattenbetrieb" in caplog.text and "RuntimeError" in caplog.text
        assert "verlegt" not in caplog.text, "Keine Inhalte im Protokoll"

    def test_aktiv_nimmt_die_aenderung_mit_zurueck(self, welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
        sitzung = welt.sitzung()

        def kaputt(self: Any, drafts: Any) -> int:
            raise RuntimeError("Fehler beim Schreiben")

        monkeypatch.setattr(SessionEvents, "publish", kaputt)
        with pytest.raises(RuntimeError), hub_events.track(welt.tenant) as tracked:
            tracked.meeting(sitzung)
            sitzung.name = "Umbenannt"
            sitzung.save()
        sitzung.refresh_from_db()
        assert sitzung.name == "Ratssitzung"

    def test_verschachtelt_meldet_jedes_objekt_einmal(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        seit = _start()
        with hub_events.track(welt.tenant) as aussen:
            aussen.meeting(sitzung)
            aussen.agenda(sitzung)
            with hub_events.track(welt.tenant) as innen:
                assert innen is aussen
                innen.agenda(sitzung)
                welt.top(sitzung, 1)
            sitzung.name = "Neu"
            sitzung.save()
        assert _kurz(_neu(seit)) == [
            ("ris.meeting.changed", "Meeting", "oeffentlich", "upsert"),
            ("ris.agendaitem.changed", "AgendaItem", "oeffentlich", "upsert"),
        ]

    def test_ausgeschaltet_ohne_transaktion_und_abfrage(
        self, welt: Welt, settings: Any, django_assert_num_queries: Any
    ) -> None:
        settings.SESSION_EVENTS = "aus"
        sitzung = welt.sitzung()
        with django_assert_num_queries(0), hub_events.track(welt.tenant) as tracked:
            tracked.meeting(sitzung)
            tracked.agenda(sitzung)
            assert tracked.active is False


# =============================================================================
# Rückrufe nach dem Commit, Freischaltung, Mandantengrenze
# =============================================================================


@pytest.mark.django_db(transaction=True)
def test_rueckruf_nach_dem_commit_meldet_seine_eigene_aenderung(welt: Welt) -> None:
    """
    Ein ``on_commit``-Rückruf mit eigener Erfassung schließt sich nicht der schon geschriebenen an.

    Ist die Transaktion von ``track`` die äußerste (Ansichten ohne eigenes ``atomic``), laufen die Rückrufe beim
    Verlassen von ``track``. Hinge sich ihre Erfassung an die abgeschlossene, blieben ihre Änderungen ungemeldet.
    """
    sitzung = welt.sitzung()
    gelaufen: list[bool] = []

    def rueckruf() -> None:
        with hub_events.track(welt.tenant) as tracked:
            tracked.meeting(sitzung)
            SessionMeeting.objects.filter(pk=sitzung.pk).update(name="Ratssitzung (verlegt)")
        gelaufen.append(True)

    seit = _start()
    with hub_events.track(welt.tenant) as tracked:
        tracked.meeting(sitzung)
        SessionMeeting.objects.filter(pk=sitzung.pk).update(room="Saal 2")
        transaction.on_commit(rueckruf)
    assert gelaufen == [True]
    events = _neu(seit)
    assert [(e.type, e.payload["changed"]) for e in events] == [
        ("ris.meeting.changed", ["location"]),
        ("ris.meeting.changed", ["name"]),
    ]
    # Zwei Transaktionen, zwei Korrelationen
    assert events[0].correlation_id != events[1].correlation_id


class TestFreischaltung:
    """Vor der Freischaltung der Schnittstelle (Issue #319) ist nichts öffentlich – auch keine Rücknahme."""

    @pytest.fixture(autouse=True)
    def gesperrt(self, welt: Welt) -> None:
        welt.tenant.oparl_public_since = None
        welt.tenant.save(update_fields=["oparl_public_since"])
        assert hub_events.interface_open(welt.tenant) is False

    def test_oeffentliche_sitzung_und_tops_nur_nichtoeffentlich(self, welt: Welt) -> None:
        SessionStandardAgendaItem.objects.create(tenant=welt.tenant, name="Eröffnung", placement="start", order=1)
        seit = _start()
        antwort = welt.client.post(
            welt.url("/meetings/create/"),
            {"name": "Ratssitzung", "organization": str(welt.rat.pk), "start": "2031-03-01T17:00", "is_public": "on"},
        )
        assert antwort.status_code == 302
        assert _kurz(_neu(seit)) == [
            ("ris.meeting.scheduled", "Meeting", "nichtoeffentlich", "upsert"),
            ("ris.agendaitem.changed", "AgendaItem", "nichtoeffentlich", "upsert"),
        ]

    def test_aenderung_und_rueckzug_ohne_oeffentliche_ruecknahme(self, welt: Welt) -> None:
        sitzung = welt.sitzung()
        top = welt.top(sitzung, 1)
        seit = _start()
        _bearbeiten(welt, sitzung, room="Saal 2")
        _bearbeiten(welt, SessionMeeting.objects.get(pk=sitzung.pk), is_public=None)
        assert welt.client.post(welt.url(f"/agenda/{top.id}/delete/")).status_code == 302
        events = _neu(seit)
        assert events, "Die übrigen Empfänger erfahren die Änderungen"
        assert {e.visibility for e in events} == {"nichtoeffentlich"}
        assert "ris.object.depublished" not in {e.type for e in events}

    def test_deaktivierter_mandant_ist_nicht_oeffentlich(self, welt: Welt) -> None:
        welt.tenant.oparl_public_since = timezone.now()
        welt.tenant.is_active = False
        assert hub_events.interface_open(welt.tenant) is False
        welt.tenant.is_active = True
        assert hub_events.interface_open(welt.tenant) is True


def test_objekte_eines_anderen_mandanten_werden_nicht_gemeldet(welt: Welt) -> None:
    """Die Erfassung liest nur Objekte ihres Mandanten – ein fremdes Objekt ergäbe Kennungen auf falscher Basis."""
    fremd = SessionTenant.objects.create(name="Bezirk Süd", slug="sued", oparl_public_since=timezone.now())
    gremium = SessionOrganization.objects.create(tenant=fremd, name="Rat")
    sitzung = SessionMeeting.objects.create(
        tenant=fremd, name="Fremd", organization=gremium, start=timezone.now(), is_public=True
    )
    seit = _start()
    with hub_events.track(welt.tenant) as tracked:
        tracked.meeting(sitzung)
        tracked.agenda(sitzung)
        welt.top(sitzung, 1)
        SessionMeeting.objects.filter(pk=sitzung.pk).update(name="Fremd (verlegt)")
    assert _neu(seit) == []


def test_anwesenheit_im_cockpit_liest_keine_tagesordnung(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anwesenheit und Störungen ändern nichts im kanonischen Modell: keine Erfassung, kein Lesen vorher/nachher."""
    from apps.session.models import SessionAttendance, SessionPerson
    from apps.session.services import cockpit_service

    sitzung = welt.sitzung()
    welt.top(sitzung, 1)
    person = SessionPerson.objects.create(tenant=welt.tenant, given_name="Ada", family_name="Amsel")
    zeile = SessionAttendance.objects.create(meeting=sitzung, person=person, status="invited")

    def nicht_erfassen(tenant: Any) -> Any:
        raise AssertionError("Anwesenheit wird nicht erfasst")

    seit = _start()
    with monkeypatch.context() as m:
        m.setattr(hub_events, "track", nicht_erfassen)
        cockpit_service.perform(
            sitzung,
            "anwesenheit",
            {"attendance": str(zeile.pk), "wechsel": "anwesend"},
            permissions={"view_meetings", "conduct_meetings"},
        )
    zeile.refresh_from_db()
    assert zeile.status == "present"
    assert _neu(seit) == []
    # Eröffnen ändert den Sitzungsstatus und wird gemeldet
    cockpit_service.perform(sitzung, "sitzung_eroeffnen", {}, permissions={"view_meetings", "conduct_meetings"})
    assert [e.type for e in _neu(seit)] == ["ris.meeting.changed"]


def test_admin_weist_auf_den_sequenzierer_hin(welt: Welt, settings: Any, rf: Any) -> None:
    """Nur ein Mandant eingeschaltet, die Installation aus: Der Admin verlangt ``EVENTS_WORKER_REQUIRED``."""
    from django.contrib.messages import get_messages
    from django.contrib.messages.storage.fallback import FallbackStorage

    from apps.session.admin import SessionTenantAdmin

    settings.SESSION_EVENTS = "aus"
    settings.EVENTS_WORKER_REQUIRED = ""
    settings.INGESTOR_EVENTS_ENABLED = False
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}

    def hinweise() -> list[str]:
        request = rf.post("/")
        request.session = {}
        request._messages = FallbackStorage(request)
        SessionTenantAdmin._warn_events_without_worker(request, welt.tenant)
        return [str(m) for m in get_messages(request)]

    assert hinweise() == []
    welt.tenant.hub_events = "schatten"
    assert any("EVENTS_WORKER_REQUIRED=true" in text for text in hinweise())
    settings.EVENTS_WORKER_REQUIRED = "true"
    assert hinweise() == []


def test_kennungen_wie_die_session_schnittstelle(welt: Welt) -> None:
    """Die Kennungen sind die, die die Session-Schnittstelle im Änderungsfeed sucht (``SessionUris``)."""
    events = hub_events.events_for(welt.tenant)
    pk = uuid.uuid4()
    assert str(events.ref("meeting", pk)) == kennung("meeting", pk)
    assert str(events.body_id) == str(canonical_id(f"{BASIS}body/"))


def test_ereignisse_erscheinen_im_feed_der_session_schnittstelle(welt: Welt, settings: Any) -> None:
    """Ohne Ingestor: Was Session meldet, nennt der Änderungsfeed ihrer Schnittstelle (dieselbe Kommune)."""
    settings.OPARL_CHANGES_ENABLED = True
    settings.OPARL_API_RATE_LIMIT = 0
    settings.OPARL_API_CACHE_SECONDS = 0
    sitzung = welt.sitzung()
    top = welt.top(sitzung, 1)
    _bearbeiten(welt, sitzung, name="Ratssitzung (verlegt)")
    assert welt.client.post(welt.url(f"/agenda/{top.id}/delete/")).status_code == 302
    for pk in Event.objects.filter(seq__isnull=True).order_by("id").values_list("pk", flat=True):
        hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
        Event.objects.filter(pk=pk).update(seq=hoechste + 1)

    antwort = Client().get("/session/nord/api/oparl/body/changes/")
    assert antwort.status_code == 200, antwort.content
    eintraege = [(e["operation"], e["id"], e.get("reason")) for e in antwort.json()["data"]]
    assert eintraege == [
        ("upsert", f"{BASIS}meeting/{sitzung.id}/", None),
        ("delete", f"{BASIS}agendaitem/{top.id}/", "quelle_geloescht"),
    ]
