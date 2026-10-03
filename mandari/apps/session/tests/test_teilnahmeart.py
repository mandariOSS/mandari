# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teilnahmeart in der Anwesenheit (Issue #139): vor Ort oder zugeschaltet, Zuschalt- und Trennzeiten,
Störungen mit Dauer und Ursache.

- Anwesenheitsliste, Niederschrift und Protokoll-PDF weisen Zugeschaltete mit Zeiten und Störungen aus.
- Die Beschlussfähigkeit zählt Zugeschaltete, während einer Störung aber nicht – und nach ihrem Ende wieder.
- Zuschaltung nur in hybriden und digitalen Sitzungen (Sitzungsformat, Issue #138).
- Das Landesprofil schließt Zugeschaltete ggf. von Wahlen und geheimen Abstimmungen aus.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionAuditLog,
    SessionMeeting,
    SessionStateProfile,
)
from apps.session.services import (
    attendance_service,
    meeting_format_service,
    participation_service,
    privacy_service,
    protocol_lock,
    protocol_publication,
    protocol_service,
    voting_service,
)
from apps.session.tests._niederschrift import Welt, base, client, nutzer, pdf_text, welt

pytestmark = pytest.mark.django_db

HYBRID = SessionMeeting.FORMAT_HYBRID


def _hybrid(w: Welt, profil: str = "NI") -> Welt:
    # Landesprofile aus der Datei: Tests mit transaction=True leeren die Tabelle der Datenmigration
    meeting_format_service.sync_profiles()
    w.tenant.state_profile = SessionStateProfile.objects.get(code=profil)
    w.tenant.save(update_fields=["state_profile"])
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=HYBRID)
    w.sitzung.refresh_from_db()
    return w


def _zeile(w: Welt, name: str) -> SessionAttendance:
    return SessionAttendance.objects.get(meeting=w.sitzung, person__family_name=name)


def _zuschalten(w: Welt, name: str = "Buche", *, von: time | None = None, bis: time | None = None) -> SessionAttendance:
    zeile = _zeile(w, name)
    zeile.participation_mode = SessionAttendance.PARTICIPATION_REMOTE
    zeile.arrival_time, zeile.departure_time = von, bis
    zeile.save()
    return zeile


def _verwaltung(w: Welt) -> Any:
    return client(nutzer(w.tenant, "sitzungsdienst", "view_meetings", "manage_attendance", "view_non_public_meetings"))


def _seite(w: Welt, c: Any) -> str:
    antwort = c.get(f"{base(w)}/meetings/{w.sitzung.pk}/")
    assert antwort.status_code == 200
    return str(antwort.content.decode())


# =============================================================================
# Anwesenheitsliste, Niederschrift, Protokoll-PDF
# =============================================================================


def test_anwesenheitsliste_zeigt_zuschaltung_und_stoerung() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w, von=time(18, 3), bis=time(19, 10))
    SessionAttendanceDisruption.objects.create(
        attendance=zeile, started_at=time(18, 40), ended_at=time(18, 44), note="INTERNER-VERMERK"
    )
    erwartet = "zugeschaltet 18:03–19:10 Uhr; Störung 18:40–18:44 Uhr (Verbindung abgebrochen)"

    verwaltung = _seite(w, _verwaltung(w))
    assert erwartet in verwaltung
    assert 'name="participation_mode"' in verwaltung
    assert 'data-testid="stoerungen"' in verwaltung
    # Leseansicht ohne Erfassungsrecht zeigt denselben Vermerk, aber keine Störungsverwaltung
    lesend = _seite(w, client(w.leser))
    assert erwartet in lesend and 'data-testid="stoerungen"' not in lesend
    # In hybriden Sitzungen steht die Teilnahmeart bei allen
    assert "vor Ort" in lesend


def test_praesenzsitzung_ohne_teilnahmeart() -> None:
    w = welt(status="review")
    seite = _seite(w, _verwaltung(w))
    assert 'name="participation_mode"' not in seite
    assert "vor Ort" not in seite


def test_niederschrift_und_pdf_nennen_teilnahmeart_je_person() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w, von=time(18, 3), bis=time(19, 10))
    SessionAttendanceDisruption.objects.create(
        attendance=zeile,
        started_at=time(18, 40),
        ended_at=time(18, 44),
        cause=SessionAttendanceDisruption.CAUSE_AUDIO,
        note="INTERNER-VERMERK",
    )

    verzeichnis = protocol_service.participant_directory(w.sitzung)
    vermerke = {a.person.family_name: a.presence_note for a in verzeichnis["present"]}
    assert vermerke["Buche"] == "zugeschaltet 18:03–19:10 Uhr; Störung 18:40–18:44 Uhr (Ton gestört)"
    assert vermerke["Amsel"] == "vor Ort"

    for internal in (False, True):
        text = " ".join(pdf_text(protocol_service.build_protocol_pdf(w.protokoll, internal=internal)).split())
        assert "Amsel (Mitglied, vor Ort)" in text
        assert "zugeschaltet 18:03–19:10 Uhr; Störung 18:40–18:44 Uhr (Ton gestört)" in text
        assert "INTERNER-VERMERK" not in text

    oeffentlich = protocol_publication.public_text(w.protokoll)
    assert "zugeschaltet 18:03–19:10 Uhr" in oeffentlich and "INTERNER-VERMERK" not in oeffentlich


def test_verspaetet_und_vorzeitig_getrennt() -> None:
    w = _hybrid(welt(status="review"))
    spaet = _zuschalten(w, "Amsel", von=time(18, 20))
    spaet.status = "joined_late"
    spaet.save()
    frueh = _zuschalten(w, "Carl", bis=time(19, 0))
    frueh.status = "left_early"
    frueh.save()
    SessionAttendanceDisruption.objects.create(attendance=frueh, started_at=time(18, 50))

    assert protocol_service.presence_note(spaet) == "verspätet zugeschaltet ab 18:20 Uhr"
    assert protocol_service.presence_note(frueh) == (
        "zugeschaltet bis 19:00 Uhr, vorzeitig getrennt; Störung ab 18:50 Uhr (Verbindung abgebrochen)"
    )


# =============================================================================
# Beschlussfähigkeit
# =============================================================================


def test_stoerung_nimmt_zugeschaltete_aus_dem_quorum_und_ende_bringt_sie_zurueck() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w)
    verwaltung = _verwaltung(w)
    # Vier Stimmberechtigte (Fink entschuldigt; Gast Esche zählt nie): nötig 3, anwesend 3, davon 1 zugeschaltet
    status = attendance_service.quorum_status(w.sitzung)
    assert (status["voting_total"], status["voting_present"], status["remote_present"], status["met"]) == (
        4,
        3,
        1,
        True,
    )

    antwort = verwaltung.post(f"{base(w)}/meetings/{w.sitzung.pk}/disruptions/add/", {"attendance": str(zeile.pk)})
    assert antwort.status_code == 302
    stoerung = SessionAttendanceDisruption.objects.get(attendance=zeile)
    assert stoerung.ongoing
    status = attendance_service.quorum_status(w.sitzung)
    assert (status["voting_present"], status["met"], status["disrupted"]) == (2, False, ["P Buche"])
    seite = _seite(w, verwaltung)
    assert "Nicht beschlussfähig" in seite and "Wegen einer Störung nicht mitgezählt: P Buche" in seite

    antwort = verwaltung.post(f"{base(w)}/attendance/disruptions/{stoerung.pk}/", {"action": "end"})
    assert antwort.status_code == 302
    stoerung.refresh_from_db()
    assert stoerung.ended_at is not None and stoerung.duration_minutes is not None
    status = attendance_service.quorum_status(w.sitzung)
    assert (status["voting_present"], status["met"], status["disrupted"]) == (3, True, [])


def test_stoerung_zu_einem_zeitpunkt() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w)
    SessionAttendanceDisruption.objects.create(attendance=zeile, started_at=time(18, 40), ended_at=time(18, 44))
    zeile = SessionAttendance.objects.prefetch_related("disruptions").get(pk=zeile.pk)
    assert participation_service.is_disrupted(zeile, time(18, 42))
    assert not participation_service.is_disrupted(zeile, time(18, 44))
    assert not participation_service.is_disrupted(zeile)  # beendet: live wieder dabei


def test_stoerung_ende_vor_beginn_und_unlesbare_uhrzeit_abgelehnt() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w)
    verwaltung = _verwaltung(w)
    url = f"{base(w)}/meetings/{w.sitzung.pk}/disruptions/add/"

    # Tippfehler 18:40–18:04 wäre sonst eine Störung von rund 23 Stunden
    daten = {"attendance": str(zeile.pk), "started_at": "18:40", "ended_at": "18:04"}
    antwort = verwaltung.post(url, daten, follow=True)
    assert "Das Ende der Störung liegt vor ihrem Beginn." in antwort.content.decode()
    antwort = verwaltung.post(url, {"attendance": str(zeile.pk), "started_at": "18:4x"}, follow=True)
    assert "Die Uhrzeit ist nicht lesbar" in antwort.content.decode()
    assert not SessionAttendanceDisruption.objects.exists()

    # Sitzung bis in den Folgetag: über Mitternacht zulässig
    beginn = timezone.make_aware(datetime(2026, 10, 1, 22, 0))
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(start=beginn, end=beginn + timedelta(hours=3))
    verwaltung.post(url, {"attendance": str(zeile.pk), "started_at": "23:50", "ended_at": "00:10"})
    assert SessionAttendanceDisruption.objects.get(attendance=zeile).duration_minutes == 20


def test_stoerung_korrigieren_behaelt_bei_fehler_den_stand() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w)
    stoerung = SessionAttendanceDisruption.objects.create(
        attendance=zeile, started_at=time(18, 40), ended_at=time(18, 44)
    )
    verwaltung = _verwaltung(w)
    url = f"{base(w)}/attendance/disruptions/{stoerung.pk}/"

    # Nicht lesbares Ende: Die beendete Störung wird nicht wieder „andauernd“
    antwort = verwaltung.post(url, {"started_at": "18:40", "ended_at": "18:4x", "cause": "audio"}, follow=True)
    assert "Die Uhrzeit ist nicht lesbar" in antwort.content.decode()
    stoerung.refresh_from_db()
    assert (stoerung.ended_at, stoerung.cause) == (time(18, 44), SessionAttendanceDisruption.CAUSE_CONNECTION)
    assert attendance_service.quorum_status(w.sitzung)["disrupted"] == []

    # Ende vor Beginn beim Korrigieren und beim Beenden mit Uhrzeit
    for daten in ({"started_at": "18:40", "ended_at": "18:04"}, {"action": "end", "ended_at": "18:00"}):
        antwort = verwaltung.post(url, daten, follow=True)
        assert "Das Ende der Störung liegt vor ihrem Beginn." in antwort.content.decode()
        stoerung.refresh_from_db()
        assert (stoerung.started_at, stoerung.ended_at) == (time(18, 40), time(18, 44))

    # Leeres Ende beim Speichern ist gewollt: Die Störung dauert an
    verwaltung.post(url, {"started_at": "18:40", "ended_at": ""})
    stoerung.refresh_from_db()
    assert stoerung.ongoing


def test_jetzt_beenden_nach_mitternacht() -> None:
    w = _hybrid(welt(status="review"))
    # Laufende Sitzung seit gestern Abend, noch ohne Ende: „Jetzt beenden“ nach Mitternacht ist echt
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(start=timezone.now() - timedelta(days=1), end=None)
    w.sitzung.refresh_from_db()
    assert participation_service.period_error(w.sitzung, time(23, 50), time(0, 5), live=True) == ""
    # Nacherfassung derselben Zeiten ohne eingetragenes Sitzungsende: abgelehnt
    fehler = participation_service.period_error(w.sitzung, time(23, 50), time(0, 5))
    assert fehler == participation_service.END_BEFORE_START


def test_stoerung_nur_fuer_zugeschaltete() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zeile(w, "Amsel")
    antwort = _verwaltung(w).post(
        f"{base(w)}/meetings/{w.sitzung.pk}/disruptions/add/", {"attendance": str(zeile.pk)}, follow=True
    )
    assert "nur für zugeschaltete Personen" in antwort.content.decode()
    assert not SessionAttendanceDisruption.objects.exists()


def test_stoerung_fremder_sitzung_nicht_erreichbar() -> None:
    w = _hybrid(welt(status="review"))
    fremd = _hybrid(welt("sued", status="review"))
    zeile = _zuschalten(fremd)
    antwort = _verwaltung(w).post(f"{base(w)}/meetings/{w.sitzung.pk}/disruptions/add/", {"attendance": str(zeile.pk)})
    assert antwort.status_code == 404
    assert not SessionAttendanceDisruption.objects.exists()


# =============================================================================
# Zuschaltung nur in hybriden und digitalen Sitzungen
# =============================================================================


def test_zuschaltung_nur_in_hybrider_oder_digitaler_sitzung() -> None:
    w = welt(status="review")
    zeile = _zeile(w, "Buche")
    verwaltung = _verwaltung(w)
    daten = {"status": "present", "participation_mode": "remote", "arrival_time": "", "departure_time": "", "notes": ""}

    antwort = verwaltung.post(f"{base(w)}/attendance/{zeile.pk}/update/", daten, HTTP_HX_REQUEST="true")
    assert "nur in hybriden oder digitalen Sitzungen" in antwort.content.decode()
    zeile.refresh_from_db()
    assert not zeile.is_remote

    _hybrid(w)
    antwort = verwaltung.post(f"{base(w)}/attendance/{zeile.pk}/update/", daten, HTTP_HX_REQUEST="true")
    assert antwort.status_code == 200
    zeile.refresh_from_db()
    assert zeile.is_remote


def test_zugeschaltete_in_praesenzsitzung_zaehlen_und_lassen_sich_korrigieren() -> None:
    w = welt(status="review")  # Präsenzsitzung
    zeile = _zeile(w, "Buche")
    # Überholte Teilnahmeart, etwa nach einer Änderung des Sitzungsformats
    SessionAttendance.objects.filter(pk=zeile.pk).update(participation_mode=SessionAttendance.PARTICIPATION_REMOTE)
    status = attendance_service.quorum_status(w.sitzung)
    assert (status["voting_present"], status["remote_present"], status["met"]) == (3, 1, True)

    verwaltung = _verwaltung(w)
    seite = _seite(w, verwaltung)
    assert ">Teilnahme</th>" in seite and 'name="participation_mode"' in seite
    assert "In dieser Präsenzsitzung sind Personen als zugeschaltet erfasst" in seite

    # Die Zeile nach dem Speichern hat so viele Spalten wie die Kopfzeile (7 mit „Teilnahme“)
    url = f"{base(w)}/attendance/{zeile.pk}/update/"
    daten = {"status": "present", "arrival_time": "", "departure_time": "", "notes": ""}
    antwort = verwaltung.post(url, {**daten, "mode_column": "1"}, HTTP_HX_REQUEST="true")
    assert antwort.content.decode().count("<td") == 7
    wechsel = {**daten, "mode_column": "1", "participation_mode": "in_person"}
    antwort = verwaltung.post(url, wechsel, HTTP_HX_REQUEST="true")
    assert antwort.content.decode().count("<td") == 7
    zeile.refresh_from_db()
    assert not zeile.is_remote

    # Ohne Zugeschaltete keine Spalte – weder in der Tabelle noch in der Zeile
    seite = _seite(w, verwaltung)
    assert ">Teilnahme</th>" not in seite
    assert verwaltung.post(url, daten, HTTP_HX_REQUEST="true").content.decode().count("<td") == 6


def test_schnellerfassung_ohne_teilnahmeart_laesst_sie_unveraendert() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w)
    daten = {"status": "present", "arrival_time": "18:05", "departure_time": "", "notes": ""}
    _verwaltung(w).post(f"{base(w)}/attendance/{zeile.pk}/update/", daten, HTTP_HX_REQUEST="true")
    zeile.refresh_from_db()
    assert zeile.is_remote and zeile.arrival_time == time(18, 5)


def test_digitale_sitzung_erzeugt_zugeschaltete() -> None:
    w = welt(status="review")
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=SessionMeeting.FORMAT_DIGITAL)
    w.sitzung.refresh_from_db()
    w.sitzung.attendances.all().delete()
    attendance_service.generate_attendance(w.sitzung)
    modi = set(w.sitzung.attendances.values_list("participation_mode", flat=True))
    assert modi == {SessionAttendance.PARTICIPATION_REMOTE}


def test_zugeschalteter_vorsitz_ergibt_hinweis() -> None:
    w = _hybrid(welt(status="review"), "NI")
    zeile = _zuschalten(w)
    zeile.role = "chair"
    zeile.save()
    assert "muss die Sitzungsleitung im Sitzungsraum anwesend sein" in _seite(w, _verwaltung(w))


# =============================================================================
# Landesprofil: Wahlen und geheime Abstimmungen
# =============================================================================


def _abstimmung(w: Welt) -> Any:
    return client(nutzer(w.tenant, "abstimmung", "view_meetings", "edit_protocols", "view_non_public_meetings"))


def _offener_top(w: Welt) -> SessionAgendaItem:
    return SessionAgendaItem.objects.create(meeting=w.sitzung, number="2", order=3, name="Wahl Vorsitz")


def test_wahl_ohne_zugeschaltete_wo_das_landesprofil_sie_ausschliesst() -> None:
    # Bayern (Art. 47a GO): Zugeschaltete nehmen an Wahlen nicht teil – die Wahl selbst bleibt zulässig
    w = _hybrid(welt(status="review"), "BY")
    _zuschalten(w)
    top = _offener_top(w)
    top.is_election = True

    beurteilt = voting_service.eligibility(w.sitzung, top)
    assert [a.person.family_name for a in beurteilt.voting] == ["Amsel", "Carl"]
    assert [a.person.family_name for a in beurteilt.remote_excluded] == ["Buche"]
    assert beurteilt.remote_rule.message.startswith("Zugeschaltete nehmen nach dem Landesprofil Bayern an Wahlen")
    assert not beurteilt.remote_rule.barred
    # Summen höchstens so viele wie Stimmberechtigte im Raum
    assert voting_service.check_counts(top, 3, 0, 0, assessed=beurteilt).exceeded
    assert not voting_service.check_counts(top, 2, 0, 0, assessed=beurteilt).exceeded
    # Beschlussfähigkeit für diese Wahl ohne Zugeschaltete (anwesend und stimmberechtigt, Art. 47 Abs. 2 GO)
    status = attendance_service.quorum_status(w.sitzung, top)
    assert (status["voting_present"], status["met"], status["remote_excluded"]) == (2, False, ["P Buche"])
    # Ohne Wahl stimmen Zugeschaltete mit
    top.is_election = False
    assert len(voting_service.eligibility(w.sitzung, top).voting) == 3


def test_wahl_ohne_zugeschaltete_und_stimme_wird_abgelehnt() -> None:
    w = _hybrid(welt(status="draft"), "BY")
    zeile = _zuschalten(w)
    top = _offener_top(w)
    abstimmung = _abstimmung(w)

    antwort = abstimmung.post(
        f"{base(w)}/agenda/{top.pk}/voting/",
        {"voting_method": "open", "is_election": "1", f"vote_{zeile.person_id}": "yes"},
        follow=True,
    )
    inhalt = antwort.content.decode()
    assert "an Wahlen nicht teil" in inhalt and "P Buche" in inhalt
    assert not top.votes.exists()
    top.refresh_from_db()
    assert top.is_election is False  # alles oder nichts

    seite = abstimmung.get(f"{base(w)}/agenda/{top.pk}/voting/").content.decode()
    assert 'data-testid="hinweis-wahl"' in seite


def test_wahl_nach_genehmigung_gesperrt() -> None:
    w = _hybrid(welt(), "NI")  # genehmigte Niederschrift
    with protocol_lock.permit(w.sitzung.pk):  # Stand vor der Genehmigung
        top = _offener_top(w)
    top.is_election = True
    with pytest.raises(protocol_lock.ProtocolLockedError):
        top.save()

    # Über die Abstimmungserfassung mit unveränderten Stimmen: Fehlermeldung, kein Eintrag „Stimmabgabe“
    antwort = _abstimmung(w).post(
        f"{base(w)}/agenda/{top.pk}/voting/", {"voting_method": "open", "is_election": "1"}, follow=True
    )
    assert "Die Niederschrift dieser Sitzung ist genehmigt." in antwort.content.decode()
    top.refresh_from_db()
    assert top.is_election is False
    assert not SessionAuditLog.objects.filter(action="vote", object_id=top.pk).exists()

    # Neuer TOP als Wahl in einer genehmigten Niederschrift: ebenso gesperrt
    with pytest.raises(protocol_lock.ProtocolLockedError):
        SessionAgendaItem.objects.create(meeting=w.sitzung, number="3", order=4, name="Nachwahl", is_election=True)

    # Innerhalb einer Berichtigung (Freigabe der Sperre) geht es
    top.is_election = True
    with protocol_lock.permit(w.sitzung.pk):
        top.save()
    top.refresh_from_db()
    assert top.is_election is True


def test_gast_zaehlt_nicht_zur_beschlussfaehigkeit() -> None:
    w = welt(status="review")
    gast = _zeile(w, "Esche")
    assert gast.role == "guest" and gast.has_voting_rights  # Stimmrecht-Häkchen gesetzt, Funktion Gast
    status = attendance_service.quorum_status(w.sitzung)
    assert (status["voting_total"], status["voting_present"]) == (4, 3)


def test_bedingte_regel_ergibt_nur_hinweis() -> None:
    w = _hybrid(welt(status="review"), "NW")
    zeile = _zuschalten(w)
    top = _offener_top(w)
    top.voting_method = "secret"
    beurteilt = voting_service.eligibility(w.sitzung, top)
    assert zeile.person_id in beurteilt.voting_person_ids
    assert "nur unter Bedingungen" in beurteilt.remote_rule.message


def test_stimme_waehrend_stoerung_abgelehnt() -> None:
    w = _hybrid(welt(status="draft"))
    zeile = _zuschalten(w)
    SessionAttendanceDisruption.objects.create(attendance=zeile, started_at=time(18, 40))
    top = _offener_top(w)
    top.voting_method = "roll_call"
    top.save()
    with pytest.raises(voting_service.VotingRightsError) as fehler:
        voting_service.capture_votes(top, {zeile.person: "yes"}, recorded_by=None)
    assert "Wegen einer Störung nicht erreichbar: P Buche." in fehler.value.user_message


# =============================================================================
# Datenschutz: Auskunft und Anonymisierung
# =============================================================================


def test_auskunft_und_anonymisierung_der_stoerungsvermerke() -> None:
    w = _hybrid(welt(status="review"))
    zeile = _zuschalten(w)
    SessionAttendanceDisruption.objects.create(
        attendance=zeile, started_at=time(18, 40), ended_at=time(18, 44), note="PERSOENLICHER-VERMERK"
    )
    auskunft = privacy_service.subject_access_export(w.tenant, zeile.person)
    eintrag = next(a for a in auskunft["anwesenheiten"] if a["sitzung"] == w.sitzung.name)
    assert eintrag["teilnahmeart"] == "Zugeschaltet"
    assert eintrag["stoerungen"][0]["vermerk"] == "PERSOENLICHER-VERMERK"

    assert "Vermerke zu Störungen" in privacy_service._anonymize_person(zeile.person)
    stoerung = SessionAttendanceDisruption.objects.get(attendance=zeile)
    assert stoerung.note == "" and stoerung.started_at == time(18, 40)
