# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Störungen Zugeschalteter zum Zeitpunkt der Abstimmung (Issue #1045).

Hybride Sitzung, Buche ist zugeschaltet und hat eine Störung von 18:10 bis 18:20 Uhr. Die Abstimmung zu TOP 1 lief im
Sitzungscockpit und wurde um 18:15 Uhr geschlossen. Erfasst, nacherfasst oder berichtigt wird später, wenn die
Störung längst beendet ist.

- Abstimmungserfassung, Niederschrift und Berichtigung: Buche war bei der Abstimmung nicht erreichbar und stimmt
  nicht ab.
- Abstimmung um 18:25 Uhr, nach der Störung: Buche ist stimmberechtigt.
- Eine im Abstimmungsfenster vor Beginn der Störung erfasste Stimme bleibt, im offenen Fenster wie nach dem
  Schließen: Speichern entfernt sie nicht, die Summen bleiben, entfernen lässt sie sich nur ausdrücklich. Nicht dazu
  gehören Stimmen aus einer abgebrochenen Runde und Einzelstimmen einer geheimen Abstimmung.
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
    SessionUser,
    SessionVote,
)
from apps.session.services import cockpit_service, participation_service, voting_service
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
    stoerung: tuple[time, time | None] | None = (time(18, 10), time(18, 20)),
    offen: bool = False,
) -> tuple[Welt, SessionAttendance, SessionAgendaItem]:
    """
    Sitzung mit gestörter Zuschaltung (``stoerung``) und zu ``geschlossen`` abgeschlossener Abstimmung; ``offen``:
    Abstimmung seit 18:00 Uhr geöffnet und noch nicht geschlossen.
    """
    w = welt(status=status)
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=SessionMeeting.FORMAT_HYBRID)
    w.sitzung.refresh_from_db()
    zeile = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name="Buche")
    zeile.participation_mode = SessionAttendance.PARTICIPATION_REMOTE
    zeile.save()
    if stoerung is not None:
        _stoerung(zeile, *stoerung)
    top = SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=w.top.pk)
    top.voting_method = methode
    if geschlossen is not None or offen:
        top.vote_opened_at = _um(w, time(18, 0))
        top.vote_closed_at = None if offen else _um(w, cast(time, geschlossen))
    top.save()
    return w, zeile, top


def _stoerung(zeile: SessionAttendance, beginn: time, ende: time | None) -> None:
    SessionAttendanceDisruption.objects.create(attendance=zeile, started_at=beginn, ended_at=ende)


def _stimme(
    w: Welt, top: SessionAgendaItem, name: str, wert: str, um: time, von: SessionUser | None = None
) -> SessionAttendance:
    """Stimme mit dem Zeitpunkt ihrer Speicherung (``updated_at``) am Sitzungstag, erfasst von ``von``."""
    zeile = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name=name)
    stimme = SessionVote.objects.create(agenda_item=top, person=zeile.person, vote=wert, recorded_by=von)
    SessionVote.objects.filter(pk=stimme.pk).update(updated_at=_um(w, um))
    return zeile


def _seitendaten(seite: Any) -> dict[str, str]:
    """Was die geladene Seite abschickt: Abstimmungsart, Felder der Stimmberechtigten, versteckte Leerfelder."""
    daten = {"voting_method": seite.context["item"].voting_method}
    for zeile in seite.context["attendances"]:
        daten[f"vote_{zeile.person_id}"] = zeile.current_vote
    daten.update({f"vote_{a.person_id}": "" for a in seite.context["stray_votes"]})
    return daten


def _namen(zeilen: list[Any]) -> list[str]:
    return [zeile.person.family_name for zeile in zeilen]


def _meldungen(antwort: Any) -> list[str]:
    return [str(m) for m in get_messages(antwort.wsgi_request)]


def _erfasser(w: Welt, name: str = "abstimmung") -> SessionUser:
    return nutzer(w.tenant, name, "view_meetings", "edit_protocols", "view_non_public_meetings")


def _abstimmung(w: Welt) -> Any:
    return client(_erfasser(w))


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

    def test_stoerung_erst_nach_dem_schliessen(self) -> None:
        # Abstimmung 18:15 geschlossen, Störung ab 18:30 und beim Nacherfassen noch andauernd
        w, zeile, top = _welt(stoerung=(time(18, 30), None))

        assert _namen(voting_service.eligibility(w.sitzung, top).unreachable) == ["Buche"]  # live: gestört
        beurteilt = voting_service.eligibility(w.sitzung, top, at=participation_service.vote_time(top))
        assert "Buche" in _namen(beurteilt.voting)
        voting_service.capture_votes(top, {zeile.person: "yes"}, recorded_by=None)
        assert top.votes.get(person=zeile.person).vote == "yes"


STEUERN = {"view_meetings", "conduct_meetings", "view_non_public_meetings"}


def _schliessen(w: Welt, top: SessionAgendaItem) -> None:
    cockpit_service.perform(
        w.sitzung, "abstimmung_schliessen", {"item": str(top.pk), "vote_result": "approved"}, permissions=STEUERN
    )


class TestVorherErfassteStimme:
    """Stimme 18:12 Uhr im Abstimmungsfenster (offen ab 18:00 Uhr), Störung ab 18:14 Uhr."""

    @pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neues-design"])
    def test_nach_dem_schliessen(self, neu: bool) -> None:
        w, zeile, top = _welt(stoerung=(time(18, 14), time(18, 20)))  # geschlossen 18:15
        w.tenant.session_new_design = neu
        w.tenant.save(update_fields=["session_new_design"])
        _stimme(w, top, "Buche", "yes", time(18, 12))
        _stimme(w, top, "Amsel", "no", time(18, 12))
        voting_service.recompute_sums(top)
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        seite = erfassung.get(url)
        inhalt = seite.content.decode()
        assert seite.context["stray_votes"] == []
        assert "Vor Beginn der Störung abgestimmt: Ja. Die Stimme bleibt." in inhalt
        assert f'name="remove_vote_{zeile.person_id}"' in inhalt
        assert erfassung.post(url, _seitendaten(seite)).status_code == 302

        top.refresh_from_db()
        assert top.votes.get(person=zeile.person).vote == "yes"
        assert (top.votes_yes, top.votes_no) == (1, 1)
        # Ein leeres Feld (etwa aus einer älteren Seite) trifft sie nicht; eine Änderung bleibt gesperrt
        erfassung.post(url, {**_seitendaten(seite), f"vote_{zeile.person_id}": ""})
        assert top.votes.filter(person=zeile.person).exists()
        antwort = erfassung.post(url, {**_seitendaten(seite), f"vote_{zeile.person_id}": "no"})
        assert any(GESTOERT in meldung for meldung in _meldungen(antwort))
        assert top.votes.get(person=zeile.person).vote == "yes"

    def test_ausdruecklich_entfernen(self) -> None:
        w, zeile, top = _welt(stoerung=(time(18, 14), time(18, 20)))
        _stimme(w, top, "Buche", "yes", time(18, 12))
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        seite = erfassung.get(url)
        erfassung.post(url, {**_seitendaten(seite), f"remove_vote_{zeile.person_id}": "1"})
        assert not top.votes.filter(person=zeile.person).exists()

    def test_offenes_fenster_und_andauernde_stoerung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, zeile, top = _welt(offen=True, stoerung=(time(18, 14), None))
        _stimme(w, top, "Buche", "yes", time(18, 12))
        _stimme(w, top, "Amsel", "no", time(18, 12))
        jetzt = [_um(w, time(18, 16))]
        monkeypatch.setattr(timezone, "now", lambda: jetzt[0])
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        # Speichern vor dem Schließen: Die Stimme bleibt
        seite = erfassung.get(url)
        assert participation_service.vote_time(top) is None
        assert seite.context["stray_votes"] == []
        assert "Störung dauert an" in seite.content.decode()
        assert erfassung.post(url, _seitendaten(seite)).status_code == 302
        assert top.votes.get(person=zeile.person).vote == "yes"

        # Das Cockpit schließt mit den Summen aus den Einzelstimmen
        jetzt[0] = _um(w, time(18, 17))
        _schliessen(w, top)
        top.refresh_from_db()
        assert (top.votes_yes, top.votes_no, top.vote_result) == (1, 1, "approved")

    @pytest.mark.parametrize("geladen", [time(18, 13), time(18, 16)], ids=["vor-der-stoerung", "waehrend"])
    def test_seite_offen_geladen_nach_dem_schliessen_gesendet(
        self, monkeypatch: pytest.MonkeyPatch, geladen: time
    ) -> None:
        w, zeile, top = _welt(offen=True, stoerung=None)
        _stimme(w, top, "Buche", "yes", time(18, 12))
        _stimme(w, top, "Amsel", "no", time(18, 12))
        jetzt = [_um(w, geladen)]
        monkeypatch.setattr(timezone, "now", lambda: jetzt[0])
        if geladen > time(18, 14):
            _stoerung(zeile, time(18, 14), None)
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"
        seite = erfassung.get(url)
        if geladen < time(18, 14):
            _stoerung(zeile, time(18, 14), None)

        jetzt[0] = _um(w, time(18, 17))
        _schliessen(w, top)
        jetzt[0] = _um(w, time(18, 25))
        assert erfassung.post(url, _seitendaten(seite)).status_code == 302

        top.refresh_from_db()
        assert top.votes.get(person=zeile.person).vote == "yes"
        assert (top.votes_yes, top.votes_no) == (1, 1)
        # Unverändert abgeschickt, also nicht neu geschrieben: Die Stimme gilt auch beim nächsten Laden
        assert erfassung.get(url).context["stray_votes"] == []

    def test_stimme_aus_abgebrochener_runde_gilt_nicht(self) -> None:
        # Runde 1 vor 18:00 abgebrochen (Stimme 17:55 bleibt stehen), Runde 2 ab 18:00, geschlossen 18:15
        w, _zeile, top = _welt(stoerung=(time(18, 14), time(18, 20)))
        _stimme(w, top, "Buche", "yes", time(17, 55))

        seite = _abstimmung(w).get(f"{base(w)}/agenda/{top.pk}/voting/")
        assert _namen(seite.context["stray_votes"]) == ["Buche"]
        # Eigener Satz für Gestörte, nicht „ohne Stimmrecht oder ohne Anwesenheit“
        inhalt = seite.content.decode()
        assert 'data-testid="gestoert-entfernt"' in inhalt
        assert "ohne Stimmrecht oder ohne Anwesenheit" not in inhalt

    def test_anderer_erfasser_laesst_unveraenderte_stimmen_unberuehrt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A erfasst 18:12 Buche und Amsel; um 21:00 trägt B Carl nach; danach wird die Störung 18:14–18:20 nacherfasst
        w, zeile, top = _welt(stoerung=None)
        a = _erfasser(w, "erfasser-a")
        _stimme(w, top, "Buche", "yes", time(18, 12), von=a)
        _stimme(w, top, "Amsel", "no", time(18, 12), von=a)
        voting_service.recompute_sums(top)
        b = client(_erfasser(w, "erfasser-b"))
        url = f"{base(w)}/agenda/{top.pk}/voting/"
        monkeypatch.setattr(timezone, "now", lambda: _um(w, time(21, 0)))
        carl = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name="Carl")
        seite = b.get(url)
        assert b.post(url, {**_seitendaten(seite), f"vote_{carl.person_id}": "yes"}).status_code == 302

        buche = top.votes.get(person=zeile.person)
        assert (buche.recorded_by_id, buche.updated_at) == (a.pk, _um(w, time(18, 12)))
        _stoerung(zeile, time(18, 14), time(18, 20))
        seite = b.get(url)
        assert seite.context["stray_votes"] == []
        assert b.post(url, _seitendaten(seite)).status_code == 302
        top.refresh_from_db()
        assert top.votes.get(person=zeile.person).vote == "yes"
        assert (top.votes_yes, top.votes_no) == (2, 1)

    def test_unveraenderter_wert_einer_behaltenen_stimme_wird_nicht_neu_geschrieben(self) -> None:
        w, zeile, top = _welt(stoerung=(time(18, 14), time(18, 20)))
        a = _erfasser(w, "erfasser-a")
        _stimme(w, top, "Buche", "yes", time(18, 12), von=a)
        b = client(_erfasser(w, "erfasser-b"))
        url = f"{base(w)}/agenda/{top.pk}/voting/"
        carl = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name="Carl")

        # Ältere Seite: Buches Wert steht noch im Formular, dazu eine andere Änderung
        daten = {**_seitendaten(b.get(url)), f"vote_{zeile.person_id}": "yes", f"vote_{carl.person_id}": "no"}
        assert b.post(url, daten).status_code == 302
        buche = top.votes.get(person=zeile.person)
        assert (buche.vote, buche.recorded_by_id, buche.updated_at) == ("yes", a.pk, _um(w, time(18, 12)))

    @pytest.mark.parametrize("cockpit", [True, False], ids=["vor-dem-oeffnen", "ohne-cockpit"])
    def test_befangenheitsvermerk_bleibt(self, cockpit: bool) -> None:
        w, zeile, top = _welt(
            geschlossen=time(18, 15) if cockpit else None, stoerung=(time(18, 14), time(18, 20) if cockpit else None)
        )
        _stimme(w, top, "Buche", "excluded", time(17, 30))
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        seite = erfassung.get(url)
        assert seite.context["stray_votes"] == []
        assert "Befangenheitsvermerk: Er bleibt." in seite.content.decode()
        assert erfassung.post(url, _seitendaten(seite)).status_code == 302
        assert top.votes.get(person=zeile.person).vote == "excluded"

    def test_geheim_nach_dem_schliessen_ohne_einzelstimme(self) -> None:
        w, zeile, top = _welt(stoerung=(time(18, 14), time(18, 20)))
        _stimme(w, top, "Buche", "yes", time(18, 12))
        erfassung = _abstimmung(w)
        url = f"{base(w)}/agenda/{top.pk}/voting/"

        seite = erfassung.get(url)
        geheim = {
            **_seitendaten(seite),
            "voting_method": "secret",
            "votes_yes": "1",
            "votes_no": "1",
            "votes_abstain": "0",
        }
        antwort = erfassung.post(url, geheim)
        assert antwort.status_code == 302, _meldungen(antwort)
        assert not top.votes.filter(person=zeile.person).exists()

    def test_berichtigung_bietet_nur_entfernen(self) -> None:
        w, zeile, top = _welt(status="review", stoerung=(time(18, 14), time(18, 20)))
        _stimme(w, top, "Buche", "yes", time(18, 12))
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        protokoll.status, protokoll.approved_at = "approved", timezone.now()
        protokoll.save()

        seite = client(w.genehmiger).get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/berichtigung/?top={top.pk}")
        assert _namen(seite.context["voters"]) == ["Amsel", "Carl", "Buche"]
        auswahl = seite.content.decode().split(f'name="vote_{zeile.person_id}"')[1].split("</select>")[0]
        assert "Ja</option>" in auswahl and "Nein</option>" not in auswahl

        def berichtigen(wert: str) -> Any:
            return korrektur.propose(
                SessionProtocol.objects.get(pk=w.protokoll.pk),
                target=SessionProtocolCorrection.TARGET_ITEM,
                item=SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=top.pk),
                data=cast(dict[str, Any], {f"vote_{zeile.person_id}": wert}),
                reason="Stimme berichtigt",
                user=w.genehmiger,
                can_view_np=True,
            )

        with pytest.raises(korrektur.CorrectionError, match="Buche"):
            berichtigen("no")
        assert berichtigen("").applied
        assert not top.votes.filter(person=zeile.person).exists()


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
