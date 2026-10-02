# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hybridregel im Landesprofil (Issue #754).

Nach § 64 Abs. 3 Satz 6 NKomVG dürfen in einer Sitzung, an der Abgeordnete zugeschaltet teilnehmen, geheime
Wahlen, geheime Abstimmungen und Beratungen geheimhaltungspflichtiger Angelegenheiten nicht durchgeführt
werden – in der ganzen Sitzung, nicht nur für die Zugeschalteten (für Videositzungen § 182 Abs. 2 Satz 6).

- Sperre ab der ersten Zuschaltung bis zum Sitzungsende, auch in digitalen Sitzungen; Präsenz unverändert
- Abstimmungserfassung und Sitzungscockpit nennen Grund und Norm und bieten die Vertagung an
- Zugeschaltete zählen bei der Beschlussfähigkeit immer; offene Wahlen erfassen ihre Stimmen
- Sachsen-Anhalt und Saarland nach dem Wortlaut; Brandenburg schließt nur die Zugeschalteten aus
- Datenmigration übernimmt die Profile; ein älteres Image legt weiter TOPs und Profile an
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionMeeting,
    SessionStateProfile,
)
from apps.session.services import (
    attendance_service,
    cockpit_service,
    meeting_format_service,
    participation_service,
    protocol_lock,
    voting_service,
)
from apps.session.services.cockpit_service import CockpitError
from apps.session.tests._niederschrift import Welt, base, client, nutzer, welt

pytestmark = pytest.mark.django_db

HYBRID = SessionMeeting.FORMAT_HYBRID
DIGITAL = SessionMeeting.FORMAT_DIGITAL
STEUERN = {"view_meetings", "conduct_meetings", "view_non_public_meetings"}


def _sitzung(w: Welt, profil: str = "NI", sitzungsformat: str = HYBRID) -> Welt:
    # Landesprofile aus der Datei: Tests mit transaction=True leeren die Tabelle der Datenmigration
    meeting_format_service.sync_profiles()
    w.tenant.state_profile = SessionStateProfile.objects.get(code=profil)
    w.tenant.save(update_fields=["state_profile"])
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=sitzungsformat)
    w.sitzung.refresh_from_db()
    return w


def _zuschalten(w: Welt, name: str = "Buche", *, status: str = "present", von: time | None = None) -> SessionAttendance:
    zeile = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name=name)
    zeile.participation_mode = SessionAttendance.PARTICIPATION_REMOTE
    zeile.status = status
    zeile.arrival_time = von
    zeile.save()
    return zeile


def _top(w: Welt, **felder: Any) -> SessionAgendaItem:
    return SessionAgendaItem.objects.create(meeting=w.sitzung, number="2", order=3, name="Wahl Vorsitz", **felder)


def _erfassung(w: Welt) -> Any:
    return client(nutzer(w.tenant, "abstimmung", "view_meetings", "edit_protocols", "view_non_public_meetings"))


def _url(w: Welt, top: SessionAgendaItem) -> str:
    return f"{base(w)}/agenda/{top.pk}/voting/"


def _gesperrt(w: Welt, top: SessionAgendaItem) -> bool:
    regel = participation_service.remote_vote_rule(w.sitzung, top)
    return regel is not None and regel.barred


def _um(uhrzeit: time) -> datetime:
    """Zeitpunkt heute zur Uhrzeit (Ortszeit) – wie das Cockpit Abstimmungen festhält."""
    return timezone.make_aware(datetime.combine(timezone.localdate(), uhrzeit))


# =============================================================================
# Niedersachsen: Sperre in der ganzen Sitzung
# =============================================================================


def test_geheime_abstimmung_mit_zugeschalteten_in_der_ganzen_sitzung_gesperrt() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w)
    top = _top(w)
    erfassung = _erfassung(w)

    daten = {"voting_method": "secret", "votes_yes": "2", "votes_no": "0", "votes_abstain": "0"}
    inhalt = erfassung.post(_url(w, top), {**daten, "vote_result": "approved"}, follow=True).content.decode()
    assert "Geheime Abstimmungen sind nach dem Landesprofil Niedersachsen unzulässig" in inhalt
    assert "§ 64 Abs. 3 Satz 6 NKomVG" in inhalt
    assert "Die Zugeschalteten abzuschalten genügt nicht" in inhalt
    top.refresh_from_db()
    assert (top.voting_method, top.vote_result, top.votes_yes) == ("summary", "pending", 0)

    # Nicht nur die Zugeschalteten sind betroffen: Auch die Anwesenden im Raum stimmen nicht geheim ab
    top.voting_method = "secret"
    top.save()
    beurteilt = voting_service.eligibility(w.sitzung, top)
    assert beurteilt.remote_rule.barred and not beurteilt.remote_excluded
    amsel = beurteilt.voting[0].person
    with pytest.raises(voting_service.VotingRightsError, match="unzulässig"):
        voting_service.capture_votes(top, {amsel: "excluded"}, recorded_by=None)

    # Die Seite nennt Grund und Norm und bietet die Vertagung an
    seite = erfassung.get(_url(w, top)).content.decode()
    assert 'data-testid="abstimmung-unzulaessig"' in seite and 'data-testid="top-vertagen"' in seite


def test_geheime_wahl_gesperrt_offene_wahl_erfasst_die_zugeschalteten() -> None:
    w = _sitzung(welt(status="draft"))
    zeile = _zuschalten(w)
    top = _top(w)
    erfassung = _erfassung(w)

    inhalt = erfassung.post(_url(w, top), {"voting_method": "secret", "is_election": "1"}, follow=True).content.decode()
    assert "Geheime Wahlen und geheime Abstimmungen sind nach dem Landesprofil Niedersachsen unzulässig" in inhalt
    top.refresh_from_db()
    assert not top.is_election

    # Offene Wahl (§ 67 Satz 1 NKomVG): Die Zugeschalteten wählen mit
    erfassung.post(_url(w, top), {"voting_method": "open", "is_election": "1", f"vote_{zeile.person_id}": "yes"})
    top.refresh_from_db()
    assert top.is_election and top.votes_yes == 1
    assert list(top.votes.values_list("person_id", flat=True)) == [zeile.person_id]
    assert participation_service.remote_vote_rule(w.sitzung, top) is None


def test_beschlussfaehigkeit_zaehlt_zugeschaltete_bei_jedem_top() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w)
    top = _top(w, voting_method="secret", is_election=True)
    # Vier Stimmberechtigte (Fink entschuldigt): nötig 3, anwesend 3, davon 1 zugeschaltet
    status = attendance_service.quorum_status(w.sitzung, top)
    assert (status["voting_present"], status["remote_present"], status["met"]) == (3, 1, True)
    assert status["remote_rule"].barred
    assert "remote_excluded" not in status


def test_videositzung_nach_paragraf_182_ebenso_gesperrt() -> None:
    w = _sitzung(welt(status="draft"), sitzungsformat=DIGITAL)
    for name in ("Amsel", "Buche", "Carl"):
        _zuschalten(w, name)
    top = _top(w, voting_method="secret")
    regel = participation_service.remote_vote_rule(w.sitzung, top)
    assert regel is not None and regel.barred
    assert "§ 182 Abs. 2 Satz 6" in regel.message


def test_praesenzsitzung_laesst_geheime_wahl_unveraendert_zu() -> None:
    w = _sitzung(welt(status="draft"), sitzungsformat=SessionMeeting.FORMAT_PRESENCE)
    top = _top(w)
    daten = {"voting_method": "secret", "is_election": "1", "votes_yes": "2", "votes_no": "1", "votes_abstain": "0"}
    _erfassung(w).post(_url(w, top), {**daten, "vote_result": "approved"})
    top.refresh_from_db()
    assert (top.voting_method, top.is_election, top.votes_yes, top.vote_result) == ("secret", True, 2, "approved")
    assert participation_service.remote_vote_rule(w.sitzung, top) is None


def test_hybride_sitzung_ohne_zuschaltung_bleibt_offen_mit_hinweis() -> None:
    # Maßgeblich ist die Zuschaltung, nicht das Format; Zusage oder Absage als Zugeschaltete zählt nicht
    w = _sitzung(welt(status="draft"))
    _zuschalten(w, "Buche", status="confirmed")
    _zuschalten(w, "Fink", status="excused")
    top = _top(w)
    erfassung = _erfassung(w)

    seite = erfassung.get(_url(w, top)).content.decode()
    assert "Bisher nimmt niemand zugeschaltet teil." in seite
    daten = {"voting_method": "secret", "votes_yes": "2", "votes_no": "0", "votes_abstain": "0"}
    erfassung.post(_url(w, top), {**daten, "vote_result": "approved"})
    top.refresh_from_db()
    assert (top.voting_method, top.vote_result) == ("secret", "approved")


def test_beendete_zuschaltung_sperrt_bis_sitzungsende() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w, status="left_early")
    top = _top(w, voting_method="secret")
    assert _gesperrt(w, top)


def test_sperre_gilt_ab_der_ersten_zuschaltung() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w, von=time(18, 30))
    top = _top(w, voting_method="secret", vote_opened_at=_um(time(18, 5)), vote_closed_at=_um(time(18, 10)))

    # Laut Sitzungscockpit vor der ersten Zuschaltung abgeschlossen: zulässig
    regel = participation_service.remote_vote_rule(w.sitzung, top)
    assert regel is not None and not regel.barred
    assert "vor der ersten Zuschaltung (18:30 Uhr)" in regel.message
    assert not voting_service.eligibility(w.sitzung, top).remote_rule.barred

    # Danach gesperrt; ohne bekannte Zeit ebenso (sicherer Standard)
    top.vote_closed_at = _um(time(18, 45))
    assert _gesperrt(w, top)
    top.vote_opened_at = top.vote_closed_at = None
    assert _gesperrt(w, top)

    # Zuschaltung ohne Uhrzeit: Sperre ab Sitzungsbeginn
    top.vote_opened_at, top.vote_closed_at = _um(time(18, 5)), _um(time(18, 10))
    _zuschalten(w, von=None)
    assert _gesperrt(w, top)


def test_vertagen_statt_abstimmen() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w)
    top = _top(w, voting_method="secret", is_election=True)
    erfassung = _erfassung(w)

    antwort = erfassung.post(
        _url(w, top), {"vertagen": "1", "voting_method": "secret", "is_election": "1"}, follow=True
    )
    assert "TOP 2 wurde vertagt" in antwort.content.decode()
    top.refresh_from_db()
    assert (top.vote_result, top.votes_yes, top.votes.count()) == ("deferred", 0, 0)
    assert SessionAuditLog.objects.filter(action="vote", object_id=top.pk).exists()
    # Vertagt: kein weiterer Knopf
    assert 'data-testid="top-vertagen"' not in erfassung.get(_url(w, top)).content.decode()


def test_niederschrift_uebernimmt_kein_ergebnis_einer_gesperrten_abstimmung() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w)
    top = _top(w, voting_method="secret")
    niederschrift = client(nutzer(w.tenant, "niederschrift", "view_meetings", "view_protocols", "edit_protocols"))
    url = f"{base(w)}/meetings/{w.sitzung.pk}/protocol/edit/"
    felder = {"content": "Neu", f"protocol_note_{top.pk}": "Aussprache"}
    zahlen = {f"votes_yes_{top.pk}": "2", f"votes_no_{top.pk}": "0", f"votes_abstain_{top.pk}": "0"}

    antwort = niederschrift.post(url, {**felder, **zahlen, f"vote_result_{top.pk}": "approved"}, follow=True)
    assert "TOP 2: Geheime Abstimmungen sind nach dem Landesprofil Niedersachsen unzulässig" in antwort.content.decode()
    top.refresh_from_db()
    assert (top.protocol_note, top.vote_result, top.votes_yes) == ("Aussprache", "pending", 0)
    # „Vertagt“ bleibt möglich
    niederschrift.post(url, {**felder, f"vote_result_{top.pk}": "deferred"})
    top.refresh_from_db()
    assert top.vote_result == "deferred"


# =============================================================================
# Geheimhaltungspflichtige Angelegenheiten
# =============================================================================


def test_geheimhaltungspflichtiger_top_mit_zugeschalteten_nicht_beraten() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w)
    top = _top(w, requires_secrecy=True)

    # Auch eine offene Abstimmung über den TOP ist gesperrt – die Beratung selbst ist unzulässig
    inhalt = _erfassung(w).post(_url(w, top), {"voting_method": "open"}, follow=True).content.decode()
    assert "Beratungen geheimhaltungspflichtiger Angelegenheiten sind nach dem Landesprofil Niedersachsen" in inhalt
    with pytest.raises(CockpitError, match="geheimhaltungspflichtiger"):
        cockpit_service.perform(w.sitzung, "top_aufrufen", {"item": str(top.pk)}, permissions=STEUERN)
    top.refresh_from_db()
    assert top.start_time is None

    # In der Präsenzsitzung kein Thema
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(format=SessionMeeting.FORMAT_PRESENCE)
    w.sitzung.refresh_from_db()
    cockpit_service.perform(w.sitzung, "top_aufrufen", {"item": str(top.pk)}, permissions=STEUERN)
    top.refresh_from_db()
    assert top.start_time is not None


def test_merkmal_geheimhaltungspflichtig_in_der_top_bearbeitung() -> None:
    w = _sitzung(welt(status="draft"))
    top = _top(w)
    bearbeitung = client(nutzer(w.tenant, "tagesordnung", "view_meetings", "edit_meetings", "view_non_public_meetings"))
    url = f"{base(w)}/agenda/{top.pk}/edit/"
    assert 'name="requires_secrecy"' in bearbeitung.get(url).content.decode()
    bearbeitung.post(url, {"name": top.name, "is_public": "on", "requires_secrecy": "on"})
    top.refresh_from_db()
    assert top.requires_secrecy and top.is_public  # getrennt von öffentlich/nichtöffentlich


def test_merkmal_nach_genehmigung_gesperrt() -> None:
    w = welt()  # genehmigte Niederschrift
    w.top.requires_secrecy = True
    with pytest.raises(protocol_lock.ProtocolLockedError):
        w.top.save()


# =============================================================================
# Sitzungscockpit
# =============================================================================


def test_cockpit_lehnt_geheime_abstimmung_ab_und_vertagt() -> None:
    w = _sitzung(welt(status="draft"))
    _zuschalten(w)
    top = _top(w)
    cockpit_service.perform(w.sitzung, "top_aufrufen", {"item": str(top.pk)}, permissions=STEUERN)

    with pytest.raises(CockpitError, match="Geheime Abstimmungen sind nach dem Landesprofil"):
        cockpit_service.perform(
            w.sitzung, "abstimmung_oeffnen", {"item": str(top.pk), "voting_method": "secret"}, permissions=STEUERN
        )
    top.refresh_from_db()
    assert top.vote_opened_at is None and top.voting_method == "summary"

    # Offene Abstimmung bleibt möglich
    cockpit_service.perform(
        w.sitzung, "abstimmung_oeffnen", {"item": str(top.pk), "voting_method": "summary"}, permissions=STEUERN
    )
    cockpit_service.perform(w.sitzung, "abstimmung_abbrechen", {"item": str(top.pk)}, permissions=STEUERN)

    # Geplante geheime Wahl: Hinweis mit Vertagung, die den TOP beendet
    SessionAgendaItem.objects.filter(pk=top.pk).update(voting_method="secret", is_election=True)
    stand = cockpit_service.build_state(w.sitzung, STEUERN)
    assert stand.item_quorum is not None and stand.item_quorum["remote_rule"].barred
    cockpit_service.perform(w.sitzung, "top_vertagen", {"item": str(top.pk)}, permissions=STEUERN)
    top.refresh_from_db()
    assert top.vote_result == "deferred" and top.end_time is not None
    with pytest.raises(CockpitError, match="bereits ein Ergebnis"):
        cockpit_service.perform(w.sitzung, "top_vertagen", {"item": str(top.pk)}, permissions=STEUERN)


# =============================================================================
# Andere Länder nach dem Wortlaut
# =============================================================================


def test_sachsen_anhalt_sperrt_geheime_wahlen_in_der_sitzung() -> None:
    w = _sitzung(welt(status="draft"), "ST")
    _zuschalten(w)
    geheim = participation_service.remote_vote_rule(w.sitzung, _top(w, voting_method="secret", is_election=True))
    assert geheim is not None and geheim.barred and "§ 56b Abs. 1 Satz 8 KVG LSA" in geheim.message
    # Offene Wahl: zulässig; geheime Abstimmung ohne Wahl: nur Hinweis (ungeklärt)
    offen = SessionAgendaItem(meeting=w.sitzung, voting_method="open", is_election=True)
    assert participation_service.remote_vote_rule(w.sitzung, offen) is None
    abstimmung = SessionAgendaItem(meeting=w.sitzung, voting_method="secret")
    regel = participation_service.remote_vote_rule(w.sitzung, abstimmung)
    assert regel is not None and regel.rule == "unclear" and not regel.barred


def test_saarland_sperrt_alle_wahlen_und_geheime_abstimmungen_in_der_sitzung() -> None:
    w = _sitzung(welt(status="draft"), "SL")
    _zuschalten(w)
    for item in (
        SessionAgendaItem(meeting=w.sitzung, voting_method="open", is_election=True),
        SessionAgendaItem(meeting=w.sitzung, voting_method="secret"),
    ):
        regel = participation_service.remote_vote_rule(w.sitzung, item)
        assert regel is not None and regel.barred and "§ 51a Abs. 4 KSVG" in regel.message


def test_brandenburg_schliesst_nur_zugeschaltete_von_geheimen_wahlen_aus() -> None:
    w = _sitzung(welt(status="draft"), "BB")
    _zuschalten(w)
    top = _top(w, voting_method="secret", is_election=True)
    beurteilt = voting_service.eligibility(w.sitzung, top)
    assert not beurteilt.remote_rule.barred
    assert [a.person.family_name for a in beurteilt.remote_excluded] == ["Buche"]
    offen = SessionAgendaItem(meeting=w.sitzung, voting_method="open", is_election=True)
    assert participation_service.remote_vote_rule(w.sitzung, offen) is None


def test_profildatei_kennt_die_neuen_regeln() -> None:
    werte = {key for key, _ in SessionStateProfile.REMOTE_VOTE_CHOICES}
    umfang = {key for key, _ in SessionStateProfile.ELECTION_SCOPE_CHOICES}
    zeilen = {row["code"]: row for row in meeting_format_service.profile_rows()}
    for row in zeilen.values():
        for key in ("remote_elections", "remote_secret_votes", "remote_secrecy_matters"):
            assert row[key] in werte, (row["code"], key)
        assert row["remote_elections_scope"] in umfang, row["code"]
        if "meeting" in (row["remote_elections"], row["remote_secret_votes"], row["remote_secrecy_matters"]):
            assert row["remote_vote_norm"], row["code"]
    ni = zeilen["NI"]
    assert (ni["remote_elections"], ni["remote_secret_votes"], ni["remote_secrecy_matters"]) == ("meeting",) * 3
    assert ni["remote_elections_scope"] == "secret_only"
    assert "Mehrheitsbeschluss" in ni["notes"]
    meeting_format_service.sync_profiles()
    assert meeting_format_service.profile_differences() == []


def test_einstellungen_zeigen_die_regel() -> None:
    w = _sitzung(welt(status="draft"))
    einstellungen = client(nutzer(w.tenant, "einstellungen", "view_meetings", "manage_settings"))
    seite = einstellungen.get(f"{base(w)}/settings/meeting-formats/").content.decode()
    assert "in der Sitzung unzulässig, sobald jemand zugeschaltet ist" in seite
    assert "Geheime Wahlen mit Zugeschalteten" in seite


# =============================================================================
# Datenmigration und Rückfall per Image
# =============================================================================

VORHER = ("session", "0053_dcat_kennung")
NACHHER = ("session", "0055_landesprofile_hybridregel")


@pytest.mark.django_db(transaction=True)
def test_migration_uebernimmt_hybridregel_und_altes_image_legt_weiter_an() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Profile = alt.get_model("session", "SessionStateProfile")
        Tenant = alt.get_model("session", "SessionTenant")
        Organization = alt.get_model("session", "SessionOrganization")
        Meeting = alt.get_model("session", "SessionMeeting")
        Item = alt.get_model("session", "SessionAgendaItem")
        meeting_format_service.sync_profiles(model=Profile)
        # Stand vor Issue #754: Niedersachsen schließt nur die Zugeschalteten aus
        Profile.objects.filter(code="NI").update(remote_elections="excluded", remote_secret_votes="excluded")
        tenant = Tenant.objects.create(name="Stadt Alt", slug="alt", state_profile_id="NI")
        rat = Organization.objects.create(tenant=tenant, name="Rat")
        sitzung = Meeting.objects.create(tenant=tenant, organization=rat, name="Rat", start="2030-01-01T17:00:00Z")
        bestand = Item.objects.create(meeting=sitzung, number="1", order=1, name="Bestand")

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        ni = neu.get_model("session", "SessionStateProfile").objects.get(code="NI")
        assert (ni.remote_elections, ni.remote_secret_votes, ni.remote_secrecy_matters) == ("meeting",) * 3
        assert ni.remote_elections_scope == "secret_only" and "§ 64 Abs. 3 Satz 6" in ni.remote_vote_norm
        assert neu.get_model("session", "SessionTenant").objects.get(pk=tenant.pk).state_profile_id == "NI"
        assert not neu.get_model("session", "SessionAgendaItem").objects.get(pk=bestand.pk).requires_secrecy

        # Rückfall per Image: Das alte Modell kennt die neuen Spalten nicht und legt trotzdem an
        spaeter = Item.objects.create(meeting=sitzung, number="2", order=2, name="Später")
        Profile.objects.create(
            code="XX",
            name="Testland",
            law="Testgesetz",
            hybrid_council="regular",
            hybrid_committees="regular",
            digital_council="none",
            digital_committees="none",
            legal_basis="hauptsatzung",
            chair_present="required",
            remote_elections="allowed",
            remote_secret_votes="allowed",
            as_of="2026-10-02",
            verification="wortlaut",
        )
        assert not neu.get_model("session", "SessionAgendaItem").objects.get(pk=spaeter.pk).requires_secrecy
        xx = neu.get_model("session", "SessionStateProfile").objects.get(code="XX")
        assert (xx.remote_elections_scope, xx.remote_secrecy_matters, xx.remote_vote_norm) == ("all", "unclear", "")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
