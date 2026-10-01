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
    assert "abwesend 18:30–18:50 Uhr" in buche.notes
    assert cockpit_service.build_state(s.meeting, LEITUNG_RECHTE).quorum["voting_present"] == 3

    with pytest.raises(CockpitError, match="bereits als anwesend"):
        _tun(s, "anwesenheit", attendance=buche.pk, wechsel="anwesend")
    with pytest.raises(CockpitError, match="geht"):
        _tun(s, "anwesenheit", attendance=buche.pk, wechsel="abwesend")


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

    fehler = _post(c, s, "top_aufrufen", item=s.tops["1"].pk)
    assert json.loads(fehler["HX-Trigger"])["showToast"]["type"] == "error"

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


async def _verbinden(user: Any, s: Sitzung) -> WebsocketCommunicator:
    pfad = f"/ws/session/{s.tenant.slug}/cockpit/{s.meeting.pk}/"
    communicator = WebsocketCommunicator(CockpitConsumer.as_asgi(), pfad)
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"tenant_slug": s.tenant.slug, "meeting_id": str(s.meeting.pk)}}
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

    async def code(user: Any) -> Any:
        communicator = await _verbinden(user, s)
        verbunden, schliesscode = await communicator.connect()
        await communicator.disconnect()
        return None if verbunden else schliesscode

    async def lauf() -> list[Any]:
        return [
            await code(AnonymousUser()),
            await code(ohne_noe.user),
            await code(fremd.user),
            await code(s.leitung.user),
        ]

    assert asyncio.run(lauf()) == [4401, 4403, 4403, None]


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

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        Role = neu.get_model("session", "SessionRole")
        rechte = dict(Role.objects.filter(tenant__slug="bestand").values_list("name", "can_conduct_meetings"))
        assert rechte == {"Protokoll": True, "Nur Anwesenheit": False, "Nur Protokoll": False}
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
