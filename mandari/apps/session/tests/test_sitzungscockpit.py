# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungscockpit (Issue #140): Sitzungsleitung und Protokollführung steuern die laufende Sitzung live.

- TOP aufrufen, beenden, weiter: Nach der Sitzung sind alle TOP-Zeiten ohne Nacharbeit gefüllt.
- Ohne Steuerungsrecht ist keine Aktion möglich; die Mitlese-Ansicht zeigt den Stand.
- Anwesenheitswechsel, Beschlussfähigkeit live, Störungsprotokoll, Abstimmung öffnen und schließen.
- Nichtöffentliche TOPs und interne Vermerke bleiben Berechtigten vorbehalten; Audit-Log je Schritt.
- Polling: unveränderter Stand ohne Inhalt (204); Aktionen benachrichtigen offene Ansichten (Channels).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import time
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import Client
from django.utils import timezone

from apps.session.consumers import CockpitConsumer
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionProtocol,
    SessionTenant,
    SessionUser,
)
from apps.session.services import cockpit_service, meeting_format_service
from apps.session.services.cockpit_service import CockpitError
from apps.session.tests._niederschrift import client, nutzer, person

pytestmark = pytest.mark.django_db

LEITUNG_RECHTE = ("view_meetings", "conduct_meetings", "view_non_public_meetings", "edit_protocols")


@dataclass
class Sitzung:
    tenant: SessionTenant
    meeting: SessionMeeting
    tops: dict[str, SessionAgendaItem]
    personen: dict[str, SessionPerson]
    leitung: SessionUser
    leser: SessionUser

    @property
    def url(self) -> str:
        return f"/session/{self.tenant.slug}/meetings/{self.meeting.pk}/cockpit/"

    def zeile(self, name: str) -> SessionAttendance:
        return SessionAttendance.objects.get(meeting=self.meeting, person=self.personen[name])

    def top(self, nummer: str) -> SessionAgendaItem:
        return SessionAgendaItem.objects.get(pk=self.tops[nummer].pk)


def _sitzung(slug: str = "cockpit", *, is_public: bool = True) -> Sitzung:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=gremium, start=timezone.now(), is_public=is_public
    )
    tops = {}
    for order, (nummer, name, public) in enumerate(
        [("1", "Eröffnung", True), ("2", "Radweg", True), ("3", "Verschiedenes", True), ("N1", "GEHEIMTOP", False)]
    ):
        tops[nummer] = SessionAgendaItem.objects.create(
            meeting=meeting, number=nummer, name=name, order=order, is_public=public
        )
    personen = {name: person(tenant, name) for name in ("Amsel", "Buche", "Carl")}
    for p in personen.values():
        SessionAttendance.objects.create(meeting=meeting, person=p, status="invited")
    return Sitzung(
        tenant=tenant,
        meeting=meeting,
        tops=tops,
        personen=personen,
        leitung=nutzer(tenant, "leitung", *LEITUNG_RECHTE),
        leser=nutzer(tenant, "leser", "view_meetings"),
    )


@pytest.fixture
def s() -> Sitzung:
    return _sitzung()


@pytest.fixture
def uhr(monkeypatch: pytest.MonkeyPatch) -> list[time]:
    """Gestellte Uhr für TOP-, Anwesenheits- und Störungszeiten: ``uhr[0]`` ist „jetzt“."""
    jetzt = [time(18, 0)]
    monkeypatch.setattr(cockpit_service, "_now", lambda: jetzt[0])
    return jetzt


def _tun(s: Sitzung, aktion: str, **daten: Any) -> cockpit_service.Outcome:
    s.meeting.refresh_from_db()
    return cockpit_service.perform(
        s.meeting,
        aktion,
        {key: str(value) for key, value in daten.items()},
        permissions={"view_meetings", "conduct_meetings", "view_non_public_meetings"},
        session_user=s.leitung,
    )


def _post(c: Client, s: Sitzung, aktion: str, *, htmx: bool = True, **daten: Any) -> Any:
    felder = {"aktion": aktion, **{k: str(v) for k, v in daten.items()}}
    if htmx:
        return c.post(f"{s.url}aktion/", felder, headers={"HX-Request": "true"})
    return c.post(f"{s.url}aktion/", felder)


# =============================================================================
# Zeiten der TOPs (Akzeptanzkriterium: ohne Nacharbeit gefüllt)
# =============================================================================


def test_sitzungsablauf_fuellt_alle_top_zeiten(s: Sitzung, uhr: list[time]) -> None:
    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    s.meeting.refresh_from_db()
    assert s.meeting.meeting_state == "in_progress" and s.meeting.actual_start is not None

    uhr[0] = time(18, 12)
    _tun(s, "naechster_top")
    uhr[0] = time(18, 40)
    _tun(s, "naechster_top")
    uhr[0] = time(19, 5)
    _tun(s, "naechster_top")  # N1 (nichtöffentlicher Teil)
    uhr[0] = time(19, 20)
    _tun(s, "sitzung_schliessen")

    zeiten = {nr: (s.top(nr).start_time, s.top(nr).end_time) for nr in s.tops}
    assert zeiten == {
        "1": (time(18, 0), time(18, 12)),
        "2": (time(18, 12), time(18, 40)),
        "3": (time(18, 40), time(19, 5)),
        "N1": (time(19, 5), time(19, 20)),
    }
    s.meeting.refresh_from_db()
    assert s.meeting.meeting_state == "completed" and s.meeting.actual_end is not None


def test_naechster_top_ueberspringt_abgesetzte_und_behandelte(s: Sitzung, uhr: list[time]) -> None:
    SessionAgendaItem.objects.filter(pk=s.tops["2"].pk).update(is_withdrawn=True, withdrawn_reason="vertagt")
    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    _tun(s, "naechster_top")
    assert s.top("3").start_time == time(18, 0) and s.top("2").start_time is None
    with pytest.raises(CockpitError, match="abgesetzt"):
        _tun(s, "top_aufrufen", item=s.tops["2"].pk)


def test_wiederaufruf_behaelt_beginn(s: Sitzung, uhr: list[time]) -> None:
    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    uhr[0] = time(18, 10)
    _tun(s, "top_aufrufen", item=s.tops["2"].pk)
    uhr[0] = time(18, 30)
    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    eins, zwei = s.top("1"), s.top("2")
    assert (eins.start_time, eins.end_time) == (time(18, 0), None)
    assert (zwei.start_time, zwei.end_time) == (time(18, 10), time(18, 30))
    with pytest.raises(CockpitError, match="bereits aufgerufen"):
        _tun(s, "top_aufrufen", item=s.tops["1"].pk)


def test_geschlossene_sitzung_erst_fortsetzen(s: Sitzung, uhr: list[time]) -> None:
    _tun(s, "sitzung_eroeffnen")
    with pytest.raises(CockpitError, match="läuft bereits"):
        _tun(s, "sitzung_eroeffnen")
    _tun(s, "sitzung_schliessen")
    with pytest.raises(CockpitError, match="geschlossen"):
        _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    _tun(s, "sitzung_eroeffnen")
    s.meeting.refresh_from_db()
    assert s.meeting.meeting_state == "in_progress" and s.meeting.actual_end is None


def test_abgesagte_sitzung_und_genehmigte_niederschrift_sperren(s: Sitzung) -> None:
    SessionProtocol.objects.create(meeting=s.meeting, status="approved")
    with pytest.raises(CockpitError, match="genehmigt"):
        _tun(s, "sitzung_eroeffnen")
    SessionProtocol.objects.filter(meeting=s.meeting).update(status="draft")
    SessionMeeting.objects.filter(pk=s.meeting.pk).update(cancelled=True)
    with pytest.raises(CockpitError, match="abgesagt"):
        _tun(s, "sitzung_eroeffnen")


# =============================================================================
# Rechte (Akzeptanzkriterium: ohne Steuerungsrecht keine Aktion)
# =============================================================================

AKTIONEN = [
    ("sitzung_eroeffnen", {}),
    ("top_aufrufen", {"item": "1"}),
    ("naechster_top", {}),
    ("anwesenheit", {"attendance": "Amsel", "wechsel": "anwesend"}),
    ("stoerung_beginn", {"attendance": "Amsel"}),
    ("abstimmung_oeffnen", {"item": "1", "voting_method": "summary"}),
    ("sitzung_schliessen", {}),
]


def _daten(s: Sitzung, daten: dict[str, str]) -> dict[str, Any]:
    ersetzt: dict[str, Any] = dict(daten)
    if "item" in ersetzt:
        ersetzt["item"] = s.tops[ersetzt["item"]].pk
    if "attendance" in ersetzt:
        ersetzt["attendance"] = s.zeile(ersetzt["attendance"]).pk
    return ersetzt


@pytest.mark.parametrize(("aktion", "daten"), AKTIONEN, ids=[a for a, _ in AKTIONEN])
def test_ohne_steuerungsrecht_keine_aktion(s: Sitzung, aktion: str, daten: dict[str, str]) -> None:
    sitzungsdaten = ("SessionMeeting", "SessionAgendaItem", "SessionAttendance", "SessionAttendanceDisruption")
    vorher = SessionAuditLog.objects.filter(model_name__in=sitzungsdaten).count()
    antwort = _post(client(s.leser), s, aktion, **_daten(s, daten))
    assert antwort.status_code == 403
    s.meeting.refresh_from_db()
    assert s.meeting.meeting_state == "draft"
    assert not SessionAgendaItem.objects.filter(meeting=s.meeting, start_time__isnull=False).exists()
    assert not SessionAttendance.objects.filter(meeting=s.meeting).exclude(status="invited").exists()
    assert SessionAuditLog.objects.filter(model_name__in=sitzungsdaten).count() == vorher


def test_steuerungsrecht_ohne_sichtrecht_reicht_nicht(s: Sitzung) -> None:
    nur_steuern = nutzer(s.tenant, "nur-steuern", "conduct_meetings")
    # Rollen sehen Sitzungen standardmäßig; hier ausdrücklich nicht
    nur_steuern.roles.update(can_view_meetings=False)
    assert _post(client(nur_steuern), s, "sitzung_eroeffnen").status_code == 403
    assert client(nur_steuern).get(s.url).status_code == 403


def test_service_prueft_das_recht_selbst(s: Sitzung) -> None:
    with pytest.raises(CockpitError, match="Sitzungsleitung"):
        cockpit_service.perform(s.meeting, "sitzung_eroeffnen", {}, permissions={"view_meetings"})


def test_mitlese_ansicht_ohne_bedienelemente(s: Sitzung) -> None:
    _tun(s, "top_aufrufen", item=s.tops["2"].pk)
    seite = client(s.leser).get(s.url)
    assert seite.status_code == 200
    inhalt = seite.content.decode()
    assert 'data-testid="mitlesen"' in inhalt
    assert "TOP 2: Radweg" in inhalt
    assert 'name="aktion"' not in inhalt
    # Nichtöffentliche TOPs sieht nur, wer das NÖ-Recht hat
    assert "GEHEIMTOP" not in inhalt

    steuern = client(s.leitung).get(s.url).content.decode()
    assert 'name="aktion"' in steuern and "GEHEIMTOP" in steuern and 'data-testid="mitlesen"' not in steuern


def test_nichtoeffentlicher_top_bleibt_in_der_mitlese_ansicht_verborgen(s: Sitzung) -> None:
    _tun(s, "top_aufrufen", item=s.tops["N1"].pk)
    stand = cockpit_service.build_state(s.meeting, {"view_meetings"})
    assert stand.current is None and stand.current_hidden
    assert all(item.is_public for item in stand.items)
    inhalt = client(s.leser).get(s.url).content.decode()
    assert "Nichtöffentlicher Teil" in inhalt and "GEHEIMTOP" not in inhalt
    # Aufrufen eines unsichtbaren TOP ist auch mit Steuerungsrecht nicht möglich
    with pytest.raises(CockpitError, match="nicht gefunden"):
        cockpit_service.perform(
            s.meeting, "top_aufrufen", {"item": str(s.tops["N1"].pk)}, permissions={"view_meetings", "conduct_meetings"}
        )


def test_nichtoeffentliche_sitzung_ohne_noe_recht_404() -> None:
    s = _sitzung("noe", is_public=False)
    assert client(s.leser).get(s.url).status_code == 404
    assert client(s.leser).get(f"{s.url}stand/").status_code == 404
    steuern_ohne_noe = nutzer(s.tenant, "steuern", "view_meetings", "conduct_meetings")
    assert _post(client(steuern_ohne_noe), s, "sitzung_eroeffnen").status_code == 404


def test_ohne_noe_recht_kein_eingriff_in_den_nichtoeffentlichen_teil(s: Sitzung, uhr: list[time]) -> None:
    """Wer den NÖ-Teil nicht sieht, beendet dort nichts – weder per Aufruf noch per Schließen."""
    steuern_ohne_noe = {"view_meetings", "conduct_meetings"}

    def ohne_noe(aktion: str, **daten: Any) -> cockpit_service.Outcome:
        s.meeting.refresh_from_db()
        felder = {key: str(value) for key, value in daten.items()}
        return cockpit_service.perform(s.meeting, aktion, felder, permissions=steuern_ohne_noe)

    _tun(s, "top_aufrufen", item=s.tops["N1"].pk)
    for aktion, daten in (
        ("top_aufrufen", {"item": s.tops["1"].pk}),
        ("naechster_top", {}),
        ("sitzung_schliessen", {}),
    ):
        with pytest.raises(CockpitError, match="nichtöffentlichen Teil"):
            ohne_noe(aktion, **daten)
    noe = s.top("N1")
    assert noe.start_time == time(18, 0) and noe.end_time is None

    # Offene Abstimmung im NÖ-Teil: Hinweis ohne Nummer des TOP
    _tun(s, "abstimmung_oeffnen", item=s.tops["N1"].pk)
    with pytest.raises(CockpitError) as fehler:
        ohne_noe("sitzung_schliessen")
    assert "N1" not in fehler.value.user_message and "nichtöffentlichen Teil" in fehler.value.user_message
    # Mit NÖ-Recht nennt die Meldung den TOP
    with pytest.raises(CockpitError, match="TOP N1"):
        _tun(s, "sitzung_schliessen")


# =============================================================================
# Anwesenheit und Beschlussfähigkeit
# =============================================================================


def test_anwesenheitswechsel_und_beschlussfaehigkeit(s: Sitzung, uhr: list[time]) -> None:
    for name in ("Amsel", "Buche"):
        _tun(s, "anwesenheit", attendance=s.zeile(name).pk, wechsel="anwesend")
    _tun(s, "anwesenheit", attendance=s.zeile("Carl").pk, wechsel="abwesend")
    assert cockpit_service.build_state(s.meeting, LEITUNG_RECHTE).quorum["met"] is True

    _tun(s, "sitzung_eroeffnen")
    uhr[0] = time(18, 30)
    _tun(s, "anwesenheit", attendance=s.zeile("Buche").pk, wechsel="geht")
    buche = s.zeile("Buche")
    assert (buche.status, buche.departure_time) == ("left_early", time(18, 30))
    assert cockpit_service.build_state(s.meeting, LEITUNG_RECHTE).quorum["met"] is False

    uhr[0] = time(18, 45)
    _tun(s, "anwesenheit", attendance=s.zeile("Carl").pk, wechsel="kommt")
    carl = s.zeile("Carl")
    assert (carl.status, carl.arrival_time) == ("joined_late", time(18, 45))
    uhr[0] = time(18, 50)
    _tun(s, "anwesenheit", attendance=buche.pk, wechsel="zurueck")
    buche = s.zeile("Buche")
    assert buche.status == "present" and buche.departure_time is None
    assert buche.interruptions == [{"left": "18:30", "returned": "18:50"}]
    assert cockpit_service.build_state(s.meeting, LEITUNG_RECHTE).quorum["voting_present"] == 3

    with pytest.raises(CockpitError, match="bereits als anwesend"):
        _tun(s, "anwesenheit", attendance=buche.pk, wechsel="anwesend")
    with pytest.raises(CockpitError, match="geht"):
        _tun(s, "anwesenheit", attendance=buche.pk, wechsel="abwesend")


def test_unterbrechungen_stehen_in_der_niederschrift(s: Sitzung, uhr: list[time]) -> None:
    """Gegangen und zurück – auch mehrfach – steht im Teilnahmevermerk: Ansicht, PDF, öffentliche Fassung."""
    from apps.session.services import protocol_publication, protocol_service
    from apps.session.tests._niederschrift import pdf_text

    _anwesend(s, "Amsel", "Buche", "Carl")
    _tun(s, "sitzung_eroeffnen")
    buche = s.zeile("Buche").pk
    for weg, zurueck in ((time(18, 30), time(18, 50)), (time(19, 10), time(19, 15))):
        uhr[0] = weg
        _tun(s, "anwesenheit", attendance=buche, wechsel="geht")
        uhr[0] = zurueck
        _tun(s, "anwesenheit", attendance=buche, wechsel="zurueck")
    uhr[0] = time(19, 40)
    _tun(s, "anwesenheit", attendance=buche, wechsel="geht")
    _tun(s, "sitzung_schliessen")
    vermerk = "abwesend 18:30–18:50 Uhr; abwesend 19:10–19:15 Uhr"

    zeile: Any = cockpit_service.build_state(s.meeting, LEITUNG_RECHTE).attendances[1]
    assert vermerk in zeile.participation_note
    protokoll = SessionProtocol.objects.create(meeting=s.meeting, status="draft")
    oeffentlich = protocol_publication.public_text(protokoll)
    assert f"vorzeitig gegangen, bis 19:40 Uhr; {vermerk}" in oeffentlich
    # Zeilenumbrüche des PDF fallen beim Vergleich weg
    pdf = "".join(pdf_text(protocol_service.build_protocol_pdf(protokoll, internal=False)).split())
    assert "".join(vermerk.split()) in pdf
    leser = nutzer(s.tenant, "vermerk-leser", "view_meetings", "view_protocols")
    seite = client(leser).get(f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/protocol/").content.decode()
    assert vermerk in seite


def test_unterbrechung_zugeschalteter_heisst_getrennt(s: Sitzung, uhr: list[time]) -> None:
    from apps.session.services import participation_service

    _hybrid(s)
    carl = s.zeile("Carl").pk
    uhr[0] = time(18, 5)
    _tun(s, "anwesenheit", attendance=carl, wechsel="geht")
    uhr[0] = time(18, 25)
    _tun(s, "anwesenheit", attendance=carl, wechsel="zurueck")
    zeile = s.zeile("Carl")
    assert participation_service.participation_note(zeile) == "zugeschaltet; getrennt 18:05–18:25 Uhr"
    # Bestand ohne Feld (älteres Image) bzw. unbrauchbare Einträge: kein Vermerk, kein Fehler
    zeile.interruptions = None
    assert participation_service.interruptions(zeile) == []
    zeile.interruptions = [{"left": "kaputt"}, "18:00", {"left": "18:00", "returned": "18:10"}]
    assert participation_service.interruption_labels(zeile) == ["getrennt 18:00–18:10 Uhr"]


def test_fremde_anwesenheitszeile_wird_nicht_gefunden(s: Sitzung) -> None:
    andere = _sitzung("andere")
    with pytest.raises(CockpitError, match="Anwesenheitsliste"):
        _tun(s, "anwesenheit", attendance=andere.zeile("Amsel").pk, wechsel="anwesend")


# =============================================================================
# Störungsprotokoll
# =============================================================================


def _hybrid(s: Sitzung) -> None:
    meeting_format_service.sync_profiles()
    SessionMeeting.objects.filter(pk=s.meeting.pk).update(format=SessionMeeting.FORMAT_HYBRID)
    SessionAttendance.objects.filter(meeting=s.meeting).update(status="present")
    SessionAttendance.objects.filter(pk=s.zeile("Carl").pk).update(participation_mode="remote")
    s.meeting.refresh_from_db()


def test_stoerung_nimmt_aus_der_beschlussfaehigkeit_und_endet_mit_der_sitzung(s: Sitzung, uhr: list[time]) -> None:
    _hybrid(s)
    with pytest.raises(CockpitError, match="zugeschaltete"):
        _tun(s, "stoerung_beginn", attendance=s.zeile("Amsel").pk)
    _tun(s, "sitzung_eroeffnen")
    uhr[0] = time(18, 40)
    _tun(s, "stoerung_beginn", attendance=s.zeile("Carl").pk, cause="audio", note="telefonisch gemeldet")
    stoerung = SessionAttendanceDisruption.objects.get(attendance=s.zeile("Carl"))
    assert (stoerung.started_at, stoerung.cause, stoerung.note) == (time(18, 40), "audio", "telefonisch gemeldet")
    stand = cockpit_service.build_state(s.meeting, LEITUNG_RECHTE)
    assert stand.quorum["voting_present"] == 2 and stand.quorum["disrupted"]
    with pytest.raises(CockpitError, match="bereits eine andauernde"):
        _tun(s, "stoerung_beginn", attendance=s.zeile("Carl").pk)

    uhr[0] = time(18, 44)
    _tun(s, "stoerung_ende", disruption=stoerung.pk)
    stoerung.refresh_from_db()
    assert stoerung.ended_at == time(18, 44)
    assert cockpit_service.build_state(s.meeting, LEITUNG_RECHTE).quorum["voting_present"] == 3

    uhr[0] = time(19, 0)
    _tun(s, "stoerung_beginn", attendance=s.zeile("Carl").pk)
    uhr[0] = time(19, 30)
    _tun(s, "sitzung_schliessen")
    offen = SessionAttendanceDisruption.objects.filter(attendance=s.zeile("Carl"), started_at=time(19, 0)).get()
    assert offen.ended_at == time(19, 30)


def test_interner_vermerk_nur_fuer_steuernde(s: Sitzung, uhr: list[time]) -> None:
    _hybrid(s)
    _tun(s, "stoerung_beginn", attendance=s.zeile("Carl").pk, note="INTERNER-VERMERK")
    assert "INTERNER-VERMERK" in client(s.leitung).get(s.url).content.decode()
    lesend = client(s.leser).get(s.url).content.decode()
    assert "INTERNER-VERMERK" not in lesend and "dauert an" in lesend


# =============================================================================
# Abstimmung
# =============================================================================


def _anwesend(s: Sitzung, *namen: str) -> None:
    SessionAttendance.objects.filter(meeting=s.meeting, person__family_name__in=namen).update(status="present")


def test_abstimmung_oeffnen_und_schliessen(s: Sitzung, uhr: list[time]) -> None:
    _anwesend(s, "Amsel", "Buche", "Carl")
    with pytest.raises(CockpitError, match="aufgerufenen"):
        _tun(s, "abstimmung_oeffnen", item=s.tops["2"].pk)
    _tun(s, "top_aufrufen", item=s.tops["2"].pk)
    _tun(s, "abstimmung_oeffnen", item=s.tops["2"].pk, voting_method="summary")
    assert s.top("2").vote_open

    # Solange abgestimmt wird, kein Wechsel und kein Schluss
    for aktion, daten in (("top_beenden", {"item": s.tops["2"].pk}), ("naechster_top", {}), ("sitzung_schliessen", {})):
        with pytest.raises(CockpitError, match="Abstimmung"):
            _tun(s, aktion, **daten)
    with pytest.raises(CockpitError, match="Ergebnis"):
        _tun(s, "abstimmung_schliessen", item=s.tops["2"].pk, votes_yes=2, votes_no=1)

    _tun(
        s,
        "abstimmung_schliessen",
        item=s.tops["2"].pk,
        votes_yes=2,
        votes_no=1,
        votes_abstain=0,
        vote_result="approved",
    )
    top = s.top("2")
    assert (top.vote_result, top.votes_yes, top.votes_no, top.votes_abstain) == ("approved", 2, 1, 0)
    assert not top.vote_open and top.vote_closed_at is not None
    eintrag = SessionAuditLog.objects.filter(action="vote_result", object_id=top.pk).get()
    assert eintrag.user_id == s.leitung.pk and eintrag.changes["quelle"] == "Sitzungscockpit"
    with pytest.raises(CockpitError, match="bereits ein Ergebnis"):
        _tun(s, "abstimmung_oeffnen", item=s.tops["2"].pk)


def test_zu_viele_stimmen_werden_abgewiesen(s: Sitzung) -> None:
    _anwesend(s, "Amsel", "Buche")
    SessionAttendance.objects.filter(pk=s.zeile("Carl").pk).update(status="absent")
    from apps.session.models import SessionOrganizationMembership

    for p in s.personen.values():
        SessionOrganizationMembership.objects.create(organization=s.meeting.organization, person=p)
    _tun(s, "top_aufrufen", item=s.tops["2"].pk)
    _tun(s, "abstimmung_oeffnen", item=s.tops["2"].pk, voting_method="secret")
    with pytest.raises(CockpitError, match="übersteigen"):
        _tun(s, "abstimmung_schliessen", item=s.tops["2"].pk, votes_yes=3, vote_result="approved")
    assert s.top("2").vote_open and s.top("2").vote_result == "pending"


def test_ergebnis_ohne_stimmen_wird_abgewiesen(s: Sitzung) -> None:
    """Kamen die Zahlen nicht an (0/0/0), stellt das Cockpit kein Ergebnis fest."""
    _anwesend(s, "Amsel", "Buche", "Carl")
    _tun(s, "top_aufrufen", item=s.tops["2"].pk)
    _tun(s, "abstimmung_oeffnen", item=s.tops["2"].pk, voting_method="summary")
    with pytest.raises(CockpitError, match="Stimmen eintragen"):
        _tun(s, "abstimmung_schliessen", item=s.tops["2"].pk, votes_yes=0, votes_no=0, vote_result="approved")
    top = s.top("2")
    assert top.vote_open and top.vote_result == "pending"


def test_einzelstimmen_bestimmen_die_summen(s: Sitzung) -> None:
    from apps.session.models import SessionVote

    _anwesend(s, "Amsel", "Buche", "Carl")
    _tun(s, "top_aufrufen", item=s.tops["2"].pk)
    _tun(s, "abstimmung_oeffnen", item=s.tops["2"].pk, voting_method="roll_call")
    for name, stimme in (("Amsel", "yes"), ("Buche", "yes"), ("Carl", "no")):
        SessionVote.objects.create(agenda_item=s.top("2"), person=s.personen[name], vote=stimme)
    # Summen aus dem Formular zählen bei namentlicher Abstimmung nicht
    _tun(s, "abstimmung_schliessen", item=s.tops["2"].pk, votes_yes=9, vote_result="approved")
    top = s.top("2")
    assert (top.votes_yes, top.votes_no, top.vote_result) == (2, 1, "approved")


def test_abstimmung_abbrechen(s: Sitzung) -> None:
    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    _tun(s, "abstimmung_oeffnen", item=s.tops["1"].pk)
    _tun(s, "abstimmung_abbrechen", item=s.tops["1"].pk)
    top = s.top("1")
    assert not top.vote_open and top.vote_result == "pending"
    _tun(s, "top_beenden", item=s.tops["1"].pk)


# =============================================================================
# Abstimmungserfassung und Niederschrift neben dem Cockpit
# =============================================================================


def _formular_stand(html: str) -> dict[str, str]:
    """Versteckte Felder „geladen_…“ einer Seite: der Stand beim Laden."""
    return dict(re.findall(r'name="(geladen_[^"]+)" value="([^"]*)"', html))


def test_erfassung_mit_altem_formular_behaelt_das_cockpit_ergebnis(s: Sitzung, uhr: list[time]) -> None:
    """Leitung schließt im Cockpit, die Protokollführung speichert danach eine Korrektur: Das Ergebnis bleibt."""
    _anwesend(s, "Amsel", "Buche", "Carl")
    zwei = s.tops["2"].pk
    _tun(s, "top_aufrufen", item=zwei)
    _tun(s, "abstimmung_oeffnen", item=zwei, voting_method="open")
    vorher = s.top("2")
    c = client(s.leitung)
    url = f"/session/{s.tenant.slug}/agenda/{zwei}/voting/"
    stimme = {name: f"vote_{p.pk}" for name, p in s.personen.items()}

    erste = {"voting_method": "open", "vote_result": "pending", stimme["Amsel"]: "yes", stimme["Buche"]: "yes"}
    c.post(url, {**_formular_stand(c.get(url).content.decode()), **erste, stimme["Carl"]: ""})
    stand = _formular_stand(c.get(url).content.decode())
    assert stand["geladen_vote_result"] == "pending" and stand["geladen_voting_method"] == "open"

    # Inzwischen stellt die Sitzungsleitung das Ergebnis im Cockpit fest
    _tun(s, "abstimmung_schliessen", item=zwei, vote_result="approved")
    geschlossen = s.top("2").vote_closed_at
    assert geschlossen is not None

    # Korrektur mit dem Formular vom Laden: Ergebnis-Auswahl unverändert „ausstehend“
    antwort = c.post(url, {**stand, **erste, stimme["Carl"]: "no"})
    assert antwort.status_code == 302
    top = s.top("2")
    assert (top.vote_result, top.votes_yes, top.votes_no) == ("approved", 2, 1)
    assert (top.start_time, top.vote_opened_at, top.vote_closed_at) == (
        vorher.start_time,
        vorher.vote_opened_at,
        geschlossen,
    )
    with pytest.raises(CockpitError, match="bereits ein Ergebnis"):
        _tun(s, "abstimmung_oeffnen", item=zwei)

    # Was die Protokollführung selbst ändert, gilt
    c.post(url, {**_formular_stand(c.get(url).content.decode()), "voting_method": "open", "vote_result": "rejected"})
    assert s.top("2").vote_result == "rejected"


def test_niederschrift_mit_altem_formular_behaelt_das_cockpit_ergebnis(s: Sitzung, uhr: list[time]) -> None:
    """Die Niederschrift ist offen, während die Leitung abstimmen lässt: Ergebnis und Summen bleiben."""
    _anwesend(s, "Amsel", "Buche", "Carl")
    SessionProtocol.objects.create(meeting=s.meeting, status="draft")
    c = client(s.leitung)
    url = f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/protocol/edit/"
    seite = c.get(url)
    assert seite.status_code == 200
    stand = _formular_stand(seite.content.decode())
    zwei = s.tops["2"].pk
    assert stand[f"geladen_vote_result_{zwei}"] == "pending" and stand[f"geladen_votes_yes_{zwei}"] == "0"

    _tun(s, "top_aufrufen", item=zwei)
    _tun(s, "abstimmung_oeffnen", item=zwei, voting_method="summary")
    _tun(s, "abstimmung_schliessen", item=zwei, votes_yes=2, votes_no=1, votes_abstain=0, vote_result="approved")

    daten = {
        **stand,
        "content": "",
        f"protocol_note_{zwei}": "Wortbeiträge",
        f"resolution_text_{zwei}": "",
        f"vote_result_{zwei}": "pending",
        f"votes_yes_{zwei}": "0",
        f"votes_no_{zwei}": "0",
        f"votes_abstain_{zwei}": "0",
    }
    assert c.post(url, daten).status_code == 302
    top = s.top("2")
    assert (top.vote_result, top.votes_yes, top.votes_no, top.protocol_note) == ("approved", 2, 1, "Wortbeiträge")
    assert top.start_time == time(18, 0) and top.vote_closed_at is not None

    # Eigene Änderungen gelten weiterhin
    daten.update(_formular_stand(c.get(url).content.decode()))
    daten.update({f"vote_result_{zwei}": "approved", f"votes_no_{zwei}": "0", f"votes_abstain_{zwei}": "1"})
    daten[f"votes_yes_{zwei}"] = "2"
    c.post(url, daten)
    top = s.top("2")
    assert (top.vote_result, top.votes_yes, top.votes_no, top.votes_abstain) == ("approved", 2, 0, 1)


def test_erfassung_und_niederschrift_nehmen_die_sperre_der_sitzung(s: Sitzung, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wie jede Cockpit-Aktion sperren beide die Sitzungszeile, bevor sie einen TOP schreiben."""
    from django.db import connection

    gesperrt: list[Any] = []
    original = cockpit_service.lock_meeting

    def sperre(meeting_id: Any) -> None:
        assert connection.in_atomic_block
        gesperrt.append(meeting_id)
        original(meeting_id)

    monkeypatch.setattr(cockpit_service, "lock_meeting", sperre)
    SessionProtocol.objects.create(meeting=s.meeting, status="draft")
    c = client(s.leitung)
    c.post(f"/session/{s.tenant.slug}/agenda/{s.tops['1'].pk}/voting/", {"voting_method": "summary"})
    c.post(f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/protocol/edit/", {"content": ""})
    assert gesperrt == [s.meeting.pk, s.meeting.pk]


# =============================================================================
# Ansicht, Polling, Audit
# =============================================================================


def test_aktion_per_htmx_liefert_den_neuen_stand(s: Sitzung, uhr: list[time]) -> None:
    c = client(s.leitung)
    antwort = _post(c, s, "top_aufrufen", item=s.tops["1"].pk)
    assert antwort.status_code == 200
    assert 'id="cockpit-stand"' in antwort.content.decode()
    assert "TOP 1: Eröffnung" in antwort.content.decode()
    toast = json.loads(antwort["HX-Trigger"])["showToast"]
    assert toast["type"] == "success" and "TOP 1 aufgerufen" in toast["message"]

    # Abgewiesen: nur die Meldung, der Stand bleibt (Eingaben gehen nicht verloren), die Ansicht lädt nach
    fehler = _post(c, s, "top_aufrufen", item=s.tops["1"].pk)
    ausloeser = json.loads(fehler["HX-Trigger"])
    assert ausloeser["showToast"]["type"] == "error" and "cockpit:nachladen" in ausloeser
    assert fehler["HX-Reswap"] == "none" and fehler.content == b""

    ohne_js = _post(c, s, "top_beenden", htmx=False, item=s.tops["1"].pk)
    assert ohne_js.status_code == 302 and ohne_js["Location"].endswith("/cockpit/")


def test_polling_antwortet_ohne_inhalt_solange_nichts_passiert(s: Sitzung) -> None:
    c = client(s.leser)
    erste = c.get(f"{s.url}stand/")
    assert erste.status_code == 200
    version = cockpit_service.state_version(s.meeting)
    assert f"?v={version}" in erste.content.decode()
    assert c.get(f"{s.url}stand/", {"v": version}).status_code == 204

    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    neu = c.get(f"{s.url}stand/", {"v": version})
    assert neu.status_code == 200 and "TOP 1: Eröffnung" in neu.content.decode()


#: Obergrenze für einen Abruf des Stands (Polling alle zwei Sekunden je offener Ansicht)
STAND_ABFRAGEN = 20


def _abfragen_stand(s: Sitzung) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    c = client(s.leser)
    c.get(f"{s.url}stand/")  # Aufwärmen
    with CaptureQueriesContext(connection) as erfasst:
        assert c.get(f"{s.url}stand/").status_code == 200
    return len(erfasst)


def test_stand_abfragen_unabhaengig_von_tops_und_anwesenden(uhr: list[time]) -> None:
    """Der häufigste Abruf des Cockpits wächst nicht mit Tagesordnung und Anwesenheitsliste."""
    klein, gross = _sitzung("klein"), _sitzung("gross")
    for nummer in range(4, 31):
        SessionAgendaItem.objects.create(
            meeting=gross.meeting, number=str(nummer), name=f"Punkt {nummer}", order=nummer
        )
        neu = person(gross.tenant, f"Person {nummer:02d}")
        SessionAttendance.objects.create(meeting=gross.meeting, person=neu, status="present")
    for sitzung in (klein, gross):
        _tun(sitzung, "top_aufrufen", item=sitzung.tops["1"].pk)
    wenige = _abfragen_stand(klein)
    assert _abfragen_stand(gross) == wenige <= STAND_ABFRAGEN


def test_jeder_schritt_steht_im_audit_log(s: Sitzung, uhr: list[time]) -> None:
    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    _tun(s, "anwesenheit", attendance=s.zeile("Amsel").pk, wechsel="anwesend")
    eintraege = SessionAuditLog.objects.filter(tenant=s.tenant, action="update")
    modelle = set(eintraege.values_list("model_name", flat=True))
    assert {"SessionMeeting", "SessionAgendaItem", "SessionAttendance"} <= modelle
    top_eintrag = eintraege.filter(model_name="SessionAgendaItem", object_id=s.tops["1"].pk).get()
    assert "start_time" in top_eintrag.changes


def test_sitzungsseite_verlinkt_das_cockpit(s: Sitzung) -> None:
    seite = client(s.leser).get(f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/").content.decode()
    assert 'data-testid="cockpit-link"' in seite and "Live-Ansicht" in seite
    steuern = client(s.leitung).get(f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/").content.decode()
    assert "Cockpit" in steuern


def test_standardrollen_steuern_das_cockpit() -> None:
    from apps.session.models import SessionRole

    tenant = SessionTenant.objects.create(name="Stadt Rollen", slug="rollen")
    rollen = SessionRole.create_default_roles(tenant)
    assert rollen["clerk"].has_permission("conduct_meetings")
    assert rollen["recorder"].has_permission("conduct_meetings")
    assert rollen["admin"].has_permission("conduct_meetings")
    assert not rollen["viewer"].has_permission("conduct_meetings")


# =============================================================================
# Echtzeit über Channels
# =============================================================================


async def _verbinden(user: Any, s: Sitzung, meeting_id: str | None = None) -> WebsocketCommunicator:
    sitzung = meeting_id if meeting_id is not None else str(s.meeting.pk)
    pfad = f"/ws/session/{s.tenant.slug}/cockpit/{sitzung}/"
    communicator = WebsocketCommunicator(CockpitConsumer.as_asgi(), pfad)
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"tenant_slug": s.tenant.slug, "meeting_id": sitzung}}
    return communicator


@pytest.mark.django_db(transaction=True)
def test_aktion_benachrichtigt_offene_ansichten() -> None:
    s = _sitzung("live")

    async def lauf() -> dict[str, Any]:
        lesend = await _verbinden(s.leser.user, s)
        verbunden, _ = await lesend.connect()
        assert verbunden
        assert await lesend.receive_json_from() == {"type": "connected"}
        await sync_to_async(_tun)(s, "top_aufrufen", item=s.tops["1"].pk)
        hinweis: dict[str, Any] = await lesend.receive_json_from(timeout=2)
        await lesend.disconnect()
        return hinweis

    assert asyncio.run(lauf()) == {"type": "stand"}


@pytest.mark.django_db(transaction=True)
def test_socket_nur_mit_sichtrecht() -> None:
    from django.contrib.auth.models import AnonymousUser

    s = _sitzung("socket", is_public=False)
    ohne_noe = nutzer(s.tenant, "ohne-noe", "view_meetings")
    fremd = _sitzung("fremd").leitung

    async def code(user: Any, meeting_id: str | None = None) -> Any:
        communicator = await _verbinden(user, s, meeting_id)
        verbunden, schliesscode = await communicator.connect()
        await communicator.disconnect()
        return None if verbunden else schliesscode

    async def lauf() -> list[Any]:
        return [
            await code(AnonymousUser()),
            await code(ohne_noe.user),
            await code(fremd.user),
            await code(s.leitung.user),
            # Die Route lässt Hex-Ziffern und Bindestriche zu; keine UUID findet keine Sitzung
            await code(s.leitung.user, "abc"),
            await code(s.leitung.user, "--"),
        ]

    assert asyncio.run(lauf()) == [4401, 4403, 4403, None, 4403, 4403]


@pytest.mark.django_db(transaction=True)
def test_aenderungen_ausserhalb_des_cockpits_benachrichtigen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sitzungsseite, Abstimmungserfassung, Admin: Jede gespeicherte Änderung erreicht offene Ansichten."""
    import contextlib

    from django.db import transaction

    s = _sitzung("signale")
    gemeldet: list[Any] = []
    monkeypatch.setattr(cockpit_service, "notify", gemeldet.append)

    # Mehrere Änderungen in einer Transaktion: ein Hinweis, erst nach dem Commit
    with transaction.atomic():
        zeile = s.zeile("Amsel")
        zeile.status = "present"
        zeile.save()
        top = s.top("3")
        top.is_withdrawn = True
        top.save()
        assert gemeldet == []
    assert gemeldet == [s.meeting.pk]

    # Zurückgerollt: kein Hinweis; die nächste Transaktion meldet wieder (auch nach einem Savepoint-Rollback)
    gemeldet.clear()
    with contextlib.suppress(RuntimeError), transaction.atomic():
        top.save()
        raise RuntimeError
    assert gemeldet == []
    with transaction.atomic():
        with contextlib.suppress(RuntimeError), transaction.atomic():
            top.save()
            raise RuntimeError
        zeile.save()
    assert gemeldet == [s.meeting.pk]

    # Ohne Transaktion sofort; Löschen einer Störung meldet ebenfalls
    gemeldet.clear()
    stoerung = SessionAttendanceDisruption.objects.create(attendance=zeile, started_at=time(18, 0))
    stoerung.delete()
    assert gemeldet == [s.meeting.pk, s.meeting.pk]


def test_benachrichtigung_scheitert_nie_an_der_aktion(s: Sitzung, monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.session import consumers

    class KaputterLayer:
        async def group_send(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Layer weg")

    monkeypatch.setattr(consumers, "get_channel_layer", lambda: KaputterLayer())
    consumers.broadcast_cockpit(s.meeting.pk)  # wirft nicht


# =============================================================================
# Migration: Bestandsrollen erhalten das Recht
# =============================================================================

VORHER = ("session", "0049_oparl_lizenz")
NACHHER = ("session", "0050_sitzungscockpit")


@pytest.mark.django_db(transaction=True)
def test_migration_berechtigt_protokollfuehrung_im_bestand() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Tenant = alt.get_model("session", "SessionTenant")
        Role = alt.get_model("session", "SessionRole")
        tenant = Tenant.objects.create(name="Bestand", slug="bestand")
        Role.objects.create(tenant=tenant, name="Protokoll", can_manage_attendance=True, can_edit_protocols=True)
        Role.objects.create(tenant=tenant, name="Nur Anwesenheit", can_manage_attendance=True)
        Role.objects.create(tenant=tenant, name="Nur Protokoll", can_edit_protocols=True)
        Organization = alt.get_model("session", "SessionOrganization")
        Meeting = alt.get_model("session", "SessionMeeting")
        Protocol = alt.get_model("session", "SessionProtocol")
        gremium = Organization.objects.create(tenant=tenant, name="Rat")
        for status in ("draft", "review", "approved", "published"):
            sitzung = Meeting.objects.create(tenant=tenant, organization=gremium, name=status, start=timezone.now())
            Protocol.objects.create(meeting=sitzung, status=status)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        Role = neu.get_model("session", "SessionRole")
        rechte = dict(Role.objects.filter(tenant__slug="bestand").values_list("name", "can_conduct_meetings"))
        assert rechte == {"Protokoll": True, "Nur Anwesenheit": False, "Nur Protokoll": False}
        # Offene Niederschriften weisen den Verlauf aus, genehmigte bleiben, wie sie genehmigt wurden
        Protocol = neu.get_model("session", "SessionProtocol")
        verlauf = dict(Protocol.objects.filter(meeting__tenant__slug="bestand").values_list("status", "show_timings"))
        assert verlauf == {"draft": True, "review": True, "approved": False, "published": False}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


# =============================================================================
# Vermerke in der Niederschrift
# =============================================================================


def test_niederschrift_nennt_verlauf_und_top_zeiten(s: Sitzung, uhr: list[time]) -> None:
    from apps.session.services import protocol_lock, protocol_publication, protocol_service
    from apps.session.tests._niederschrift import pdf_text

    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    uhr[0] = time(18, 12)
    _tun(s, "naechster_top")
    uhr[0] = time(18, 40)
    _tun(s, "sitzung_schliessen")
    s.meeting.refresh_from_db()
    protokoll = SessionProtocol.objects.create(meeting=s.meeting, status="draft")

    oeffentlich = protocol_publication.public_text(protokoll)
    assert "Verlauf: eröffnet" in oeffentlich and "geschlossen" in oeffentlich
    assert "Behandelt 18:00–18:12 Uhr" in oeffentlich and "Behandelt 18:12–18:40 Uhr" in oeffentlich
    text = " ".join(pdf_text(protocol_service.build_protocol_pdf(protokoll, internal=True)).split())
    assert "Behandelt 18:00–18:12 Uhr" in text and "Verlauf:" in text
    leser = nutzer(s.tenant, "protokoll-leser", "view_meetings", "view_protocols")
    seite = client(leser).get(f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/protocol/").content.decode()
    assert "Behandelt 18:00–18:12 Uhr" in seite

    # Nach der Genehmigung stehen die Zeiten fest
    SessionProtocol.objects.filter(pk=protokoll.pk).update(status="approved")
    top = s.top("1")
    top.start_time = time(17, 0)
    with pytest.raises(protocol_lock.ProtocolLockedError):
        top.save()


def test_bei_einfuehrung_genehmigte_niederschrift_bleibt_ohne_verlauf(s: Sitzung, uhr: list[time]) -> None:
    """Genehmigte Niederschriften aus dem Bestand ändern ihren Inhalt nicht nachträglich."""
    from apps.session.services import protocol_lock, protocol_publication, protocol_service
    from apps.session.tests._niederschrift import pdf_text

    _tun(s, "top_aufrufen", item=s.tops["1"].pk)
    _tun(s, "sitzung_schliessen")
    protokoll = SessionProtocol.objects.create(meeting=s.meeting, status="draft")
    assert protokoll.show_timings is True
    # Stand nach der Migration: genehmigt, ohne Verlauf
    SessionProtocol.objects.filter(pk=protokoll.pk).update(status="approved", show_timings=False)
    protokoll.refresh_from_db()

    assert "Verlauf:" not in protocol_publication.public_text(protokoll)
    assert "Behandelt" not in protocol_publication.public_text(protokoll)
    pdf = pdf_text(protocol_service.build_protocol_pdf(protokoll, internal=True))
    assert "Verlauf" not in pdf and "Behandelt" not in pdf
    leser = nutzer(s.tenant, "bestand-leser", "view_meetings", "view_protocols")
    seite = client(leser).get(f"/session/{s.tenant.slug}/meetings/{s.meeting.pk}/protocol/").content.decode()
    assert "Behandelt" not in seite
    # Der Schalter gehört zum gesperrten Inhalt
    protokoll.show_timings = True
    with pytest.raises(protocol_lock.ProtocolLockedError):
        protokoll.save()
