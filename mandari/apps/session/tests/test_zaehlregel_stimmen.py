# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine Zählregel für Sitzungscockpit, Abstimmungserfassung und Niederschrift (Issue #1059).

Es zählen die Stimmen der Stimmberechtigten zum Zeitpunkt der Abstimmung und die vor einer Störung abgegebenen
(``voting_service.kept_votes``). Die drei Fälle aus der Prüfung von #1053 (#1045):

- Eine Stellvertretung stimmt ab, danach kommt das vertretene Mitglied.
- Eine Stimme aus einer abgebrochenen Runde bzw. aus der Minute, in der eine Störung beginnt.
- Mit einer Landesprofil-Regel ohne Zugeschaltete wird die Runde als Wahl neu geöffnet, die Stimme der zugeschalteten
  Person liegt noch.

In allen drei Fällen nennt das Cockpit vor dem Schließen die Stimme, die nicht zählt, das Schließen entfernt sie und
vermerkt sie im Prüfprotokoll; Cockpit-Ergebnis, Summen nach dem Speichern der Erfassung und die Namensliste der
Niederschrift stimmen überein. Gewöhnliche Abstimmungen bleiben, wie sie waren.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any

import pytest
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganizationMembership,
    SessionStateProfile,
    SessionVote,
)
from apps.session.services import (
    cockpit_service,
    meeting_format_service,
    protocol_publication,
    resolution_service,
    voting_service,
)
from apps.session.tests._niederschrift import Welt, base, client, nutzer, person, welt

pytestmark = pytest.mark.django_db

STEUERN = {"view_meetings", "conduct_meetings", "view_non_public_meetings"}


def _um(w: Welt, uhrzeit: time) -> datetime:
    """Zeitpunkt am Sitzungstag (Ortszeit)."""
    return timezone.make_aware(datetime.combine(timezone.localtime(w.sitzung.start).date(), uhrzeit))


def _welt(monkeypatch: pytest.MonkeyPatch, *, hybrid: bool = False, profil: str = "") -> tuple[Welt, SessionAgendaItem]:
    """Sitzung, Uhr auf 18:12 Uhr am Sitzungstag; TOP 1 aufgerufen, namentliche Abstimmung seit 18:00 Uhr offen."""
    w = welt(status="draft")
    if hybrid:
        SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=SessionMeeting.FORMAT_HYBRID)
        w.sitzung.refresh_from_db()
        SessionAttendance.objects.filter(meeting=w.sitzung, person__family_name="Buche").update(
            participation_mode=SessionAttendance.PARTICIPATION_REMOTE
        )
    if profil:
        # Landesprofile aus der Datei (wie test_teilnahmeart)
        meeting_format_service.sync_profiles()
        w.tenant.state_profile = SessionStateProfile.objects.get(code=profil)
        w.tenant.save(update_fields=["state_profile"])
    monkeypatch.setattr(timezone, "now", lambda: _um(w, time(18, 12)))
    top = SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=w.top.pk)
    top.voting_method = "roll_call"
    top.vote_result = "pending"
    top.votes_yes = top.votes_no = top.votes_abstain = 0
    top.start_time = time(18, 0)
    top.vote_opened_at = _um(w, time(18, 0))
    top.save()
    return w, top


def _zeile(w: Welt, name: str) -> SessionAttendance:
    return SessionAttendance.objects.get(meeting=w.sitzung, person__family_name=name)


def _stimme(w: Welt, top: SessionAgendaItem, name: str, wert: str, um: time) -> None:
    """Stimme mit dem Zeitpunkt ihrer Speicherung (``updated_at``), wie sie die Erfassung angelegt hätte."""
    zeile = _zeile(w, name)
    stimme = SessionVote.objects.create(agenda_item=top, person=zeile.person, vote=wert)
    SessionVote.objects.filter(pk=stimme.pk).update(updated_at=_um(w, um))
    voting_service.recompute_sums(top)


def _vor_dem_schliessen(w: Welt) -> voting_service.CountedVotes:
    state = cockpit_service.build_state(SessionMeeting.objects.get(pk=w.sitzung.pk), STEUERN)
    assert state.vote_counted is not None
    return state.vote_counted


def _schliessen(w: Welt, top: SessionAgendaItem, ergebnis: str = "rejected") -> cockpit_service.Outcome:
    return cockpit_service.perform(
        w.sitzung, "abstimmung_schliessen", {"item": str(top.pk), "vote_result": ergebnis}, permissions=STEUERN
    )


def _summen(top: SessionAgendaItem) -> tuple[int, int, int]:
    top.refresh_from_db()
    return top.votes_yes, top.votes_no, top.votes_abstain


def _erfassung_speichern(w: Welt, top: SessionAgendaItem) -> tuple[int, int, int]:
    """Erfassung laden und unverändert speichern wie im Browser (Ladestand, Stimmen, versteckte Leerfelder)."""
    c = client(nutzer(w.tenant, "erfassung", "view_meetings", "edit_protocols", "view_non_public_meetings"))
    url = f"{base(w)}/agenda/{top.pk}/voting/"
    seite = c.get(url)
    item = seite.context["item"]
    daten: dict[str, str] = dict(re.findall(r'name="(geladen_[^"]+)" value="([^"]*)"', seite.content.decode()))
    daten.update({"voting_method": item.voting_method, "vote_result": item.vote_result})
    if item.is_election:
        daten["is_election"] = "1"
    for zeile in seite.context["attendances"]:
        daten[f"vote_{zeile.person_id}"] = zeile.current_vote
    daten.update({f"vote_{a.person_id}": "" for a in seite.context["stray_votes"]})
    assert c.post(url, daten).status_code == 302
    return _summen(top)


def _namentlich(top: SessionAgendaItem) -> str:
    """Namensliste der öffentlichen Fassung der Niederschrift."""
    zeilen = protocol_publication._item_lines(SessionAgendaItem.objects.select_related("meeting").get(pk=top.pk))
    return next((zeile for zeile in zeilen if zeile.startswith("Namentlich: ")), "")


def _pruefprotokoll(top: SessionAgendaItem) -> dict[str, Any]:
    changes: dict[str, Any] = SessionAuditLog.objects.get(action="vote_result", object_id=top.pk).changes
    return changes


def _uebereinstimmung(w: Welt, top: SessionAgendaItem, *, entfernt: str, grund: str) -> None:
    """Cockpit vorher und nachher, Erfassung und Niederschrift: Ja 0, Nein 1 (Amsel bzw. Carl), ohne ``entfernt``."""
    vorher = _vor_dem_schliessen(w)
    assert vorher.sums == (0, 1, 0)
    assert [(v.person.family_name, v.vote, g) for v, g in vorher.uncounted] == [(entfernt, "yes", grund)]

    ausgang = _schliessen(w, top)
    assert ausgang.level == "warning" and f"P {entfernt} (Ja, {grund})" in ausgang.message
    assert _summen(top) == (0, 1, 0)
    assert not top.votes.filter(person__family_name=entfernt).exists()
    eintrag = _pruefprotokoll(top)
    assert (eintrag["ja"], eintrag["nein"]) == (0, 1)
    assert eintrag["nicht_gezaehlt"] == [{"person": f"P {entfernt}", "stimme": "Ja", "grund": grund}]

    assert _erfassung_speichern(w, top) == (0, 1, 0)
    namen = _namentlich(top)
    assert namen.count("(Nein)") == 1 and "(Ja)" not in namen and entfernt not in namen


class TestDreiFaelle:
    def test_stellvertretung_und_das_vertretene_mitglied_kommt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, top = _welt(monkeypatch)
        amsel = _zeile(w, "Amsel")
        amsel.status = "excused"
        amsel.save()
        stellv = person(w.tenant, "Stellv")
        SessionOrganizationMembership.objects.create(
            organization=w.gremium, person=stellv, has_voting_rights=True, substitute_for=amsel.person
        )
        SessionAttendance.objects.create(meeting=w.sitzung, person=stellv, status="present")
        _stimme(w, top, "Stellv", "yes", time(18, 5))
        _stimme(w, top, "Carl", "no", time(18, 5))
        assert _vor_dem_schliessen(w).uncounted == []

        # Das vertretene Mitglied kommt, die Stellvertretung wird zur Bereitschaft
        amsel.status = "joined_late"
        amsel.save()
        _uebereinstimmung(w, top, entfernt="Stellv", grund="Stellvertretung, das vertretene Mitglied ist anwesend")

    @pytest.mark.parametrize(
        "abgegeben", [time(17, 55), time(18, 10, 30)], ids=["abgebrochene-runde", "minute-der-stoerung"]
    )
    def test_stimme_vor_dieser_runde_oder_in_der_minute_der_stoerung(
        self, monkeypatch: pytest.MonkeyPatch, abgegeben: time
    ) -> None:
        # Runde 1 vor 18:00 Uhr abgebrochen (cancel_vote lässt die Stimme stehen), Runde 2 seit 18:00 Uhr,
        # Störung bei Buche seit 18:10 Uhr
        w, top = _welt(monkeypatch, hybrid=True)
        _stimme(w, top, "Buche", "yes", abgegeben)
        _stimme(w, top, "Amsel", "no", time(18, 5))
        SessionAttendanceDisruption.objects.create(attendance=_zeile(w, "Buche"), started_at=time(18, 10))

        _uebereinstimmung(w, top, entfernt="Buche", grund="wegen einer Störung nicht erreichbar")

    def test_runde_als_wahl_neu_geoeffnet_ohne_zugeschaltete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bayern: Zugeschaltete nehmen an Wahlen nicht teil. Runde 1 ohne Wahl, Buche stimmt zugeschaltet ab; nach dem
        # Abbruch wird die Runde als Wahl neu geöffnet
        w, top = _welt(monkeypatch, hybrid=True, profil="BY")
        _stimme(w, top, "Buche", "yes", time(17, 55))
        top.is_election = True
        top.save()
        _stimme(w, top, "Amsel", "no", time(18, 5))

        _uebereinstimmung(w, top, entfernt="Buche", grund="zugeschaltet und nach dem Landesprofil ausgeschlossen")

    def test_cockpit_nennt_die_stimme_vor_dem_schliessen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, top = _welt(monkeypatch, hybrid=True)
        _stimme(w, top, "Buche", "yes", time(17, 55))
        _stimme(w, top, "Amsel", "no", time(18, 5))
        SessionAttendanceDisruption.objects.create(attendance=_zeile(w, "Buche"), started_at=time(18, 10))
        leitung = client(nutzer(w.tenant, "leitung", *STEUERN))

        inhalt = leitung.get(f"{base(w)}/meetings/{w.sitzung.pk}/cockpit/").content.decode()
        assert 'data-testid="cockpit-nicht-gezaehlt"' in inhalt
        assert "P Buche (Ja, wegen einer Störung nicht erreichbar)" in inhalt
        assert "Erfasste Einzelstimmen: Ja 0 · Nein 1 · Enthaltung 0" in inhalt


class TestGewoehnlicheAbstimmung:
    @pytest.mark.parametrize("methode", ["roll_call", "open"])
    def test_alle_stimmen_zaehlen(self, monkeypatch: pytest.MonkeyPatch, methode: str) -> None:
        w, top = _welt(monkeypatch)
        top.voting_method = methode
        top.save()
        for name, wert in (("Amsel", "yes"), ("Buche", "yes"), ("Carl", "abstain")):
            _stimme(w, top, name, wert, time(18, 5))

        assert _vor_dem_schliessen(w).uncounted == []
        assert _schliessen(w, top, "approved").level == "success"
        assert _summen(top) == (2, 0, 1)
        assert "nicht_gezaehlt" not in _pruefprotokoll(top)
        assert top.votes.count() == 3
        assert _erfassung_speichern(w, top) == (2, 0, 1)

    def test_cockpit_ohne_hinweis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, top = _welt(monkeypatch)
        _stimme(w, top, "Amsel", "yes", time(18, 5))
        inhalt = client(nutzer(w.tenant, "leitung", *STEUERN)).get(f"{base(w)}/meetings/{w.sitzung.pk}/cockpit/")
        assert 'data-testid="cockpit-nicht-gezaehlt"' not in inhalt.content.decode()

    def test_mitlesende_sehen_keinen_zwischenstand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, _top = _welt(monkeypatch)
        state = cockpit_service.build_state(SessionMeeting.objects.get(pk=w.sitzung.pk), {"view_meetings"})
        assert state.vote_counted is None


class TestNiederschrift:
    def test_namensliste_nach_der_regel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bestand von vor der Regel: Das Cockpit hatte auch die Stimme der beratenden Person gezählt
        w, top = _welt(monkeypatch)
        _stimme(w, top, "Amsel", "yes", time(18, 5))
        _stimme(w, top, "Carl", "no", time(18, 5))
        _stimme(w, top, "Dachs", "yes", time(18, 5))
        _stimme(w, top, "Buche", "excluded", time(17, 0))

        assert _namentlich(top) == "Namentlich: P Amsel (Ja), P Carl (Nein)"
        votes = voting_service.protocol_votes(top)
        assert [p.family_name for p in votes.excluded] == ["Buche"]
        assert [v.person.family_name for v in votes.roll_call] == ["Amsel", "Carl"]
        # Beschlussauszug und Niederschrift-PDF über dieselbe Regel
        captured: dict[str, Any] = {}
        monkeypatch.setattr(resolution_service, "html_to_pdf", lambda html: b"")
        monkeypatch.setattr(
            resolution_service, "render_to_string", lambda name, context: captured.update(context) or ""
        )
        resolution_service.build_extract_pdf(
            [SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=top.pk)], internal=False
        )
        assert [v.person.family_name for v in captured["items"][0].roll_call_votes] == ["Amsel", "Carl"]

    def test_ohne_anwesenheitsliste_wie_bisher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, top = _welt(monkeypatch)
        SessionVote.objects.create(agenda_item=top, person=w.beratend, vote="yes")
        w.sitzung.attendances.all().delete()
        assert _namentlich(top) == "Namentlich: P Dachs (Ja)"

    def test_offene_abstimmung_ohne_namensliste(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, top = _welt(monkeypatch)
        top.voting_method = "open"
        top.save()
        _stimme(w, top, "Amsel", "yes", time(18, 5))
        assert _namentlich(top) == ""
