# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Störungen Zugeschalteter zum Zeitpunkt der Abstimmung (Issue #1045).

Hybride Sitzung, Buche ist zugeschaltet und hat eine Störung von 18:10 bis 18:20 Uhr. Die Abstimmung zu TOP 1 lief im
Sitzungscockpit und wurde um 18:15 Uhr geschlossen. Erfasst, nacherfasst oder berichtigt wird später, wenn die
Störung längst beendet ist.

- Abstimmungserfassung, Niederschrift und Berichtigung: Buche war bei der Abstimmung nicht erreichbar und stimmt
  nicht ab.
- Abstimmung um 18:25 Uhr, nach der Störung: Buche ist stimmberechtigt.
- Eine vor Beginn der Störung erfasste Stimme bleibt, auch wenn die Person beim Schließen gestört war: Die
  Nacherfassung entfernt sie nicht, die Summen bleiben.
- Eine Störung, die erst nach dem Schließen beginnt und beim Nacherfassen andauert, nimmt niemandem die Stimme.
- Live-Anzeige (ohne Zeitpunkt, laufende Abstimmung) und Abstimmungen ohne gespeicherten Zeitpunkt: wie bisher zählt
  nur die andauernde Störung.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionMeeting,
    SessionProtocol,
    SessionProtocolCorrection,
    SessionVote,
)
from apps.session.services import participation_service, voting_service
from apps.session.services import protocol_correction_service as korrektur
from apps.session.tests._niederschrift import Welt, base, client, nutzer, welt

pytestmark = pytest.mark.django_db


def _um(w: Welt, uhrzeit: time) -> datetime:
    """Zeitpunkt am Sitzungstag (Ortszeit)."""
    return timezone.make_aware(datetime.combine(timezone.localtime(w.sitzung.start).date(), uhrzeit))


def _welt(
    status: str = "draft",
    *,
    geschlossen: time | None = time(18, 15),
    methode: str = "roll_call",
    stoerung: tuple[time, time | None] = (time(18, 10), time(18, 20)),
) -> tuple[Welt, SessionAttendance, SessionAgendaItem]:
    """Sitzung mit gestörter Zuschaltung (``stoerung``) und zu ``geschlossen`` abgeschlossener Abstimmung."""
    w = welt(status=status)
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=SessionMeeting.FORMAT_HYBRID)
    w.sitzung.refresh_from_db()
    zeile = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name="Buche")
    zeile.participation_mode = SessionAttendance.PARTICIPATION_REMOTE
    zeile.save()
    SessionAttendanceDisruption.objects.create(attendance=zeile, started_at=stoerung[0], ended_at=stoerung[1])
    top = SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=w.top.pk)
    top.voting_method = methode
    if geschlossen is not None:
        top.vote_opened_at = _um(w, time(18, 0))
        top.vote_closed_at = _um(w, geschlossen)
    top.save()
    return w, zeile, top


def _namen(zeilen: list[Any]) -> list[str]:
    return [zeile.person.family_name for zeile in zeilen]


def _meldungen(antwort: Any) -> list[str]:
    return [str(m) for m in get_messages(antwort.wsgi_request)]


def _abstimmung(w: Welt) -> Any:
    return client(nutzer(w.tenant, "abstimmung", "view_meetings", "edit_protocols", "view_non_public_meetings"))


GESTOERT = "Wegen einer Störung nicht erreichbar: P Buche."


class TestZeitpunktDerAbstimmung:
    def test_stoerung_zur_abstimmung_zaehlt_auch_nach_ihrem_ende(self) -> None:
        w, zeile, top = _welt()

        assert participation_service.vote_time(top) == time(18, 15)
        beurteilt = voting_service.eligibility(w.sitzung, top, at=participation_service.vote_time(top))
        assert _namen(beurteilt.unreachable) == ["Buche"]
        assert _namen(beurteilt.voting) == ["Amsel", "Carl"]
        # Erfassen ohne mitgegebene Beurteilung prüft ebenso zum Zeitpunkt der Abstimmung
        with pytest.raises(voting_service.VotingRightsError) as fehler:
            voting_service.capture_votes(top, {zeile.person: "yes"}, recorded_by=None)
        assert GESTOERT in fehler.value.user_message
        assert voting_service.check_counts(top, 3, 0, 0).exceeded
        assert not voting_service.check_counts(top, 2, 0, 0).exceeded

    def test_abstimmung_nach_der_stoerung(self) -> None:
        w, zeile, top = _welt(geschlossen=time(18, 25))

        beurteilt = voting_service.eligibility(w.sitzung, top, at=participation_service.vote_time(top))
        assert _namen(beurteilt.voting) == ["Amsel", "Buche", "Carl"]
        assert beurteilt.unreachable == []
        voting_service.capture_votes(top, {zeile.person: "yes"}, recorded_by=None)
        assert top.votes.get(person=zeile.person).vote == "yes"

    def test_abstimmungserfassung_nach_der_sitzung(self) -> None:
        w, zeile, top = _welt()
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        seite = erfassung.get(url)
        assert _namen(seite.context["unreachable"]) == ["Buche"]
        assert "bei der Abstimmung gestört" in seite.content.decode()
        # Beschlussfähigkeit zum selben Zeitpunkt wie die Stimmliste
        assert (seite.context["item_quorum"]["voting_present"], seite.context["item_quorum"]["disrupted"]) == (
            2,
            ["P Buche"],
        )
        antwort = erfassung.post(url, {"voting_method": "roll_call", f"vote_{zeile.person_id}": "yes"})
        assert antwort.status_code == 302
        assert any(GESTOERT in meldung for meldung in _meldungen(antwort))
        assert not top.votes.filter(person=zeile.person).exists()

    def test_summen_in_der_niederschrift(self) -> None:
        w, _zeile, top = _welt(methode="summary")

        antwort = client(w.protokollant).post(
            f"{base(w)}/meetings/{w.sitzung.pk}/protocol/edit/",
            {"content": "Allgemein", f"protocol_note_{top.pk}": "Aussprache", f"votes_yes_{top.pk}": "3"},
        )
        assert antwort.status_code == 302
        assert any("übersteigen die Zahl der stimmberechtigten Anwesenden (2)" in m for m in _meldungen(antwort))
        top.refresh_from_db()
        assert top.votes_yes == 2  # gespeicherter Stand

    def test_berichtigung(self) -> None:
        w, zeile, top = _welt(status="review")
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        protokoll.status, protokoll.approved_at = "approved", timezone.now()
        protokoll.save()

        seite = client(w.genehmiger).get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/berichtigung/?top={top.pk}")
        assert _namen(seite.context["voters"]) == ["Amsel", "Carl"]
        with pytest.raises(korrektur.CorrectionError, match="Buche"):
            korrektur.propose(
                SessionProtocol.objects.get(pk=w.protokoll.pk),
                target=SessionProtocolCorrection.TARGET_ITEM,
                item=SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=top.pk),
                data=cast(dict[str, Any], {f"vote_{zeile.person_id}": "yes"}),
                reason="Stimme nachgetragen",
                user=w.genehmiger,
                can_view_np=True,
            )

    @pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neues-design"])
    def test_vorher_erfasste_stimme_bleibt(self, neu: bool) -> None:
        # Stimme um 18:12 im offenen Fenster, Störung ab 18:14 bis über das Schließen um 18:15 hinaus
        w, zeile, top = _welt(stoerung=(time(18, 14), time(18, 20)))
        w.tenant.session_new_design = neu
        w.tenant.save(update_fields=["session_new_design"])
        amsel = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name="Amsel")
        SessionVote.objects.create(agenda_item=top, person=zeile.person, vote="yes")
        SessionVote.objects.create(agenda_item=top, person=amsel.person, vote="no")
        voting_service.recompute_sums(top)
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        seite = erfassung.get(url)
        assert seite.context["stray_votes"] == []
        assert "die vorher erfasste Stimme bleibt" in seite.content.decode()
        # Nacherfassung speichert, was die Seite schickt (ohne Feld für Buche)
        daten = {"voting_method": "roll_call", f"vote_{amsel.person_id}": "no"}
        daten.update({f"vote_{a.person_id}": "" for a in seite.context["stray_votes"]})
        assert erfassung.post(url, daten).status_code == 302

        top.refresh_from_db()
        assert top.votes.get(person=zeile.person).vote == "yes"
        assert (top.votes_yes, top.votes_no) == (1, 1)
        # Eine geänderte Stimme der zur Abstimmung gestörten Person bleibt gesperrt
        with pytest.raises(voting_service.VotingRightsError):
            voting_service.capture_votes(top, {zeile.person: "no"}, recorded_by=None)

    def test_stoerung_erst_nach_dem_schliessen(self) -> None:
        # Abstimmung 18:15 geschlossen, Störung ab 18:30 und beim Nacherfassen noch andauernd
        w, zeile, top = _welt(stoerung=(time(18, 30), None))

        assert _namen(voting_service.eligibility(w.sitzung, top).unreachable) == ["Buche"]  # live: gestört
        beurteilt = voting_service.eligibility(w.sitzung, top, at=participation_service.vote_time(top))
        assert "Buche" in _namen(beurteilt.voting)
        voting_service.capture_votes(top, {zeile.person: "yes"}, recorded_by=None)
        assert top.votes.get(person=zeile.person).vote == "yes"


class TestOhneZeitpunkt:
    def test_live_anzeige_unveraendert(self) -> None:
        w, zeile, top = _welt()

        # Ohne Zeitpunkt zählt die andauernde Störung: beendet → stimmberechtigt, andauernd → nicht erreichbar
        assert "Buche" in _namen(voting_service.eligibility(w.sitzung, top).voting)
        SessionAttendanceDisruption.objects.create(attendance=zeile, started_at=time(18, 30))
        assert _namen(voting_service.eligibility(w.sitzung, top).unreachable) == ["Buche"]

    def test_laufende_abstimmung_im_sitzungscockpit(self) -> None:
        w, _zeile, top = _welt()
        top.vote_closed_at = None
        top.save()

        assert participation_service.vote_time(top) is None
        assert "Buche" in _namen(voting_service.eligibility(w.sitzung, top, at=None).voting)

    def test_ohne_sitzungscockpit_kein_zeitpunkt(self) -> None:
        # Nur Beginn und Ende des TOP: Sie grenzen die Beratung ein, nicht die Abstimmung – es bleibt wie bisher
        w, zeile, top = _welt(geschlossen=None)
        top.start_time, top.end_time = time(18, 5), time(18, 15)
        top.save()

        assert participation_service.vote_time(top) is None
        voting_service.capture_votes(top, {zeile.person: "yes"}, recorded_by=None)
        assert top.votes.get(person=zeile.person).vote == "yes"
