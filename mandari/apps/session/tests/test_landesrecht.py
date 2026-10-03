# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Landesprofil als Sitzungsrecht (Issue #757, Teil L2a).

- Fassungen mit Stichtag: Für eine Sitzung am 31.10.2026 gilt in Niedersachsen die Fassung ab 07.05.2026, für eine
  am 01.11.2026 die Fassung ab 01.11.2026 (Sainte-Laguë/Schepers, Sitzungsleitung, Öffentlichkeit per Video)
- Jeder Eintrag nennt Norm, Quelle und Stand; Profildatei, Datenbank und Doku stimmen überein
- Ergebnisregel: Bei Stimmengleichheit ist ein Antrag abgelehnt (§ 66 Abs. 1 NKomVG)
- Hybridteil: Sitzungsleitung, örtliche Regel je Gremium, Zuschaltung nur in öffentlichen Sitzungen, Vermerke in
  der Ladung, Störung im Verantwortungsbereich der Kommune, Notlagenbeschluss mit Ablauf (§ 182), Nachweise für
  Aufnahmen und Öffentlichkeit per Video, Widerspruch je Person
- Ortsrecht (Hauptsatzung und Geschäftsordnung) je Körperschaft mit Prüfprotokoll
- Datenmigration und Rückfall per Image
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.session.models import (
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionAuditLog,
    SessionBody,
    SessionMeeting,
    SessionOrganization,
    SessionStateProfile,
    SessionStateProfileVersion,
)
from apps.session.services import (
    body_service,
    cockpit_service,
    meeting_format_service,
    participation_service,
    state_law_service,
)
from apps.session.services.state_law_service import LAW_FIELDS, LawDataError, LocalRules
from apps.session.tests._niederschrift import Welt, base, client, nutzer, welt

pytestmark = pytest.mark.django_db

DOKU = Path(__file__).resolve().parents[4] / "docs" / "SESSION_SITZUNGSFORMAT_LANDESRECHT.md"
#: Themen der Erweiterung „vom Sitzungsformat zum Sitzungsrecht“ (Konzept Abschnitt 5.2)
THEMEN = {
    "body_types",
    "designations",
    "publicity",
    "non_public_committee_kinds",
    "convocation",
    "agenda",
    "quorum",
    "majority",
    "tie_vote",
    "abstentions",
    "voting_methods",
    "elections",
    "bias",
    "minutes",
    "motions",
    "urgent_decisions",
    "objection",
    "term_years",
    "seat_allocation",
    "committee_seats",
}


def _am(tag: date, uhrzeit: time = time(18, 0)) -> datetime:
    return timezone.make_aware(datetime.combine(tag, uhrzeit))


def _ni(w: Welt, *, tag: date | None = None, sitzungsformat: str = SessionMeeting.FORMAT_PRESENCE) -> Welt:
    meeting_format_service.sync_profiles()
    w.tenant.state_profile = SessionStateProfile.objects.get(code="NI")
    w.tenant.hybrid_basis_kind = SessionStateProfile.BASIS_HAUPTSATZUNG
    w.tenant.hybrid_basis_date = date(2024, 3, 12)
    w.tenant.hybrid_basis_reference = "§ 7 Hauptsatzung"
    w.tenant.save()
    felder: dict[str, Any] = {"format": sitzungsformat}
    if tag is not None:
        felder["start"] = _am(tag)
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(**felder)
    w.sitzung.refresh_from_db()
    return w


def _ortsrecht(w: Welt, **werte: Any) -> SessionBody:
    body = body_service.default_body(w.tenant)
    body.local_rules = LocalRules.from_json(werte).to_json()
    body.save(update_fields=["local_rules"])
    w.gremium.refresh_from_db()
    w.sitzung = SessionMeeting.objects.get(pk=w.sitzung.pk)
    return body


# =============================================================================
# Fassungen mit Stichtag
# =============================================================================


def test_sitzungen_am_31_10_und_01_11_2026_folgen_verschiedenen_fassungen() -> None:
    w = _ni(welt(status="draft"), tag=date(2026, 10, 31))
    vorher = state_law_service.for_meeting(w.sitzung)
    assert vorher is not None and vorher.version is not None
    assert vorher.version.valid_from == date(2026, 5, 7)
    assert vorher.value("seat_allocation") == "dhondt"
    assert vorher.value("session_lead") == "chair"
    assert vorher.value("residents_questions") == "all"
    assert vorher.value("video_public") is None  # ungeklärt
    assert "youth_body" not in vorher.entries

    w.sitzung.start = _am(date(2026, 11, 1))
    nachher = state_law_service.for_meeting(w.sitzung)
    assert nachher is not None and nachher.version is not None
    assert nachher.version.valid_from == date(2026, 11, 1)
    assert nachher.value("seat_allocation") == "sainte_lague_schepers"
    assert nachher.value("session_lead") == "session_lead"
    assert nachher.value("video_public") == "local_basis"
    assert nachher.value("hvb_deputies_max") == 5
    # Geerbt aus der früheren Fassung, mit deren Stichtag
    assert nachher.value("tie_vote") == "rejected"
    assert nachher.entries["tie_vote"].since == date(2026, 5, 7)
    assert nachher.entries["seat_allocation"].since == date(2026, 11, 1)

    # Vor der ersten Fassung: Landesprofil ohne Sitzungsrecht
    frueh = state_law_service.effective(w.tenant.state_profile, date(2026, 5, 6))
    assert frueh.version is None and not frueh.entries


def test_jeder_eintrag_nennt_norm_quelle_und_stand_und_deckt_die_themen_ab() -> None:
    meeting_format_service.sync_profiles()
    ni = SessionStateProfile.objects.get(code="NI")
    fassungen = list(ni.versions.order_by("valid_from"))
    assert [f.valid_from for f in fassungen] == [date(2026, 5, 7), date(2026, 11, 1)]
    for fassung in fassungen:
        assert fassung.amendment and fassung.sources
        for key, eintrag in fassung.law.items():
            assert key in LAW_FIELDS, key
            assert eintrag["norm"] or eintrag["unclear"], key
            assert eintrag["source"]["url"].startswith("https://"), key
            assert date.fromisoformat(eintrag["as_of"]), key
    assert set(fassungen[0].law) >= THEMEN
    # Abweichungen im Bestand sind ausgewiesen
    assert ni.digital_council == ni.digital_committees == "emergency"


def test_profildatei_wird_geprueft() -> None:
    known = {f.name for f in SessionStateProfile._meta.get_fields() if getattr(f, "concrete", False)}
    roh = {
        "valid_from": "2026-11-01",
        "title": "Test",
        "as_of": "2026-10-02",
        "sources": [{"title": "Quelle", "url": "https://example.org/gesetz"}],
        "law": {"tie_vote": {"value": "rejected", "norm": "§ 1"}},
    }
    assert (
        state_law_service.normalize_version("NI", roh, profile_fields=known)["law"]["tie_vote"]["value"] == "rejected"
    )
    for kaputt in (
        {"tie_vote": {"value": "vielleicht", "norm": "§ 1"}},
        {"tie_vote": {"value": "rejected"}},
        {"gibt_es_nicht": {"norm": "§ 1"}},
        {"term_years": {"value": "fünf", "norm": "§ 1"}},
        {"quorum": {"value": True, "norm": "§ 1"}},
    ):
        with pytest.raises(LawDataError):
            state_law_service.normalize_version("NI", {**roh, "law": kaputt}, profile_fields=known)
    with pytest.raises(LawDataError):
        state_law_service.normalize_version("NI", {**roh, "overrides": {"unbekannt": 1}}, profile_fields=known)


def test_sync_uebernimmt_fassungen_und_check_meldet_abweichungen() -> None:
    meeting_format_service.sync_profiles()
    assert meeting_format_service.profile_differences() == []
    assert SessionStateProfileVersion.objects.filter(profile_id="NI").count() == 2

    SessionStateProfileVersion.objects.filter(profile_id="NI", valid_from=date(2026, 11, 1)).update(law={})
    SessionStateProfileVersion.objects.create(
        profile_id="NI", valid_from=date(2030, 1, 1), title="Alt", as_of=date(2026, 1, 1), verification="wortlaut"
    )
    unterschiede = meeting_format_service.profile_differences()
    assert "NI, Fassung ab 01.11.2026: abweichend (law)" in unterschiede
    assert "NI, Fassung ab 01.01.2030: nicht mehr in der Profildatei" in unterschiede
    meeting_format_service.sync_profiles()
    assert meeting_format_service.profile_differences() == []
    assert not SessionStateProfileVersion.objects.filter(valid_from=date(2030, 1, 1)).exists()


def test_doku_nennt_die_quellen_der_fassungen() -> None:
    text = DOKU.read_text(encoding="utf-8")
    for row in meeting_format_service.profile_rows():
        for fassung in row.get("versions") or []:
            for source in fassung["sources"]:
                assert source["url"] in text, (row["code"], source["url"])
    assert "Fassungen mit Stichtag" in text and "Ortsrecht" in text


def test_rechtsuebersicht_zeigt_die_fassung_zum_stichtag() -> None:
    w = _ni(welt(status="draft"))
    einstellungen = client(nutzer(w.tenant, "einstellungen", "view_meetings", "manage_settings"))
    seite = einstellungen.get(f"{base(w)}/settings/meeting-formats/?stichtag=2026-11-01").content.decode()
    assert 'data-testid="sitzungsrecht"' in seite
    assert "Fassung ab 01.11.2026" in seite and "Sainte-Laguë/Schepers" in seite
    assert "§ 71 Abs. 2 und 8 NKomVG" in seite and "Stand 02.10.2026" in seite
    seite = einstellungen.get(f"{base(w)}/settings/meeting-formats/?stichtag=2026-10-31").content.decode()
    assert "Fassung ab 07.05.2026" in seite and "d’Hondt" in seite
    # Ungültiger Stichtag: heute
    assert einstellungen.get(f"{base(w)}/settings/meeting-formats/?stichtag=morgen").status_code == 200


# =============================================================================
# Ergebnisregel (§ 66 Abs. 1 NKomVG)
# =============================================================================


def test_stimmengleichheit_ist_abgelehnt() -> None:
    w = _ni(welt(status="draft"), tag=date(2026, 10, 20))
    erfassung = client(nutzer(w.tenant, "abstimmung", "view_meetings", "edit_protocols", "view_non_public_meetings"))
    url = f"{base(w)}/agenda/{w.top.pk}/voting/"
    daten = {"voting_method": "summary", "votes_yes": "1", "votes_no": "1", "votes_abstain": "1"}

    antwort = erfassung.post(url, {**daten, "vote_result": "approved"})
    meldungen = " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert "Bei Stimmengleichheit ist er abgelehnt (§ 66 Abs. 1 NKomVG)" in meldungen
    assert "Enthaltungen zählen nicht mit" in meldungen
    w.top.refresh_from_db()
    assert (w.top.votes_yes, w.top.votes_no) == (2, 1)  # unverändert

    erfassung.post(url, {**daten, "vote_result": "rejected"})
    w.top.refresh_from_db()
    assert (w.top.vote_result, w.top.votes_yes, w.top.votes_no) == ("rejected", 1, 1)


def test_ohne_regel_im_landesprofil_bleibt_die_erfassung_frei() -> None:
    w = welt(status="draft")
    meeting_format_service.sync_profiles()
    w.tenant.state_profile = SessionStateProfile.objects.get(code="NW")
    w.tenant.save()
    w.top.votes_yes, w.top.votes_no = 1, 1
    assert w.top.vote_result == "approved"
    from apps.session.services import voting_service

    assert voting_service.result_rule_problem(w.top) == ""


# =============================================================================
# Hybridteil
# =============================================================================


def _rolle(w: Welt, name: str, rolle: str, *, remote: bool) -> SessionAttendance:
    zeile = SessionAttendance.objects.get(meeting=w.sitzung, person__family_name=name)
    zeile.role = rolle
    zeile.participation_mode = (
        SessionAttendance.PARTICIPATION_REMOTE if remote else SessionAttendance.PARTICIPATION_IN_PERSON
    )
    zeile.save()
    return zeile


def test_sitzungsleitung_ab_01_11_2026_auch_fuer_die_leitende_stellvertretung() -> None:
    w = _ni(welt(status="draft"), tag=date(2026, 10, 20), sitzungsformat=SessionMeeting.FORMAT_HYBRID)
    _rolle(w, "Amsel", "deputy_chair", remote=True)
    zeilen = list(w.sitzung.attendances.select_related("person"))
    # Vor dem 01.11.2026: Nur der Vorsitz muss im Raum sein
    assert participation_service.chair_hint(w.sitzung, zeilen) == ""

    w.sitzung.start = _am(date(2026, 11, 5))
    hinweis = participation_service.chair_hint(w.sitzung, zeilen)
    assert "Fassung ab 01.11.2026" in hinweis and "§ 64 Abs. 3 Satz 1 NKomVG" in hinweis
    assert "auch eine Stellvertretung, die die Sitzung leitet" in hinweis and "P Amsel" in hinweis

    # Leitet der Vorsitz im Raum, darf die Stellvertretung zugeschaltet sein
    _rolle(w, "Buche", "chair", remote=False)
    zeilen = list(w.sitzung.attendances.select_related("person"))
    assert participation_service.chair_hint(w.sitzung, zeilen) == ""


def test_oertliche_regel_schliesst_ein_gremium_aus() -> None:
    w = _ni(welt(status="draft"))
    w.gremium.remote_local_rule = SessionOrganization.REMOTE_LOCAL_EXCLUDED
    w.gremium.save()
    ergebnis = meeting_format_service.check(w.tenant, [w.gremium], SessionMeeting.FORMAT_HYBRID)
    assert any("nach der Hauptsatzung ausgeschlossen" in fehler for fehler in ergebnis.errors)


def test_zuschaltung_nur_in_oeffentlichen_sitzungen() -> None:
    w = _ni(welt(status="draft"))
    _ortsrecht(w, remote_public_only=True)
    oeffentlich = meeting_format_service.check(w.tenant, [w.gremium], SessionMeeting.FORMAT_HYBRID, is_public=True)
    assert oeffentlich.ok
    nicht = meeting_format_service.check(w.tenant, [w.gremium], SessionMeeting.FORMAT_HYBRID, is_public=False)
    assert any("nur in öffentlichen Sitzungen" in fehler and "§ 64 Abs. 3 Satz 3" in fehler for fehler in nicht.errors)


def test_ladung_vermerkt_zulassung_und_pflichthinweis_fuer_zugeschaltete() -> None:
    w = _ni(welt(status="draft"), tag=date(2026, 10, 20), sitzungsformat=SessionMeeting.FORMAT_HYBRID)
    _ortsrecht(w, remote_per_invitation=True)
    info = meeting_format_service.describe(w.sitzung, for_members=True)
    assert any("mit dieser Ladung zugelassen (§ 64 Abs. 3 Satz 2 NKomVG" in v for v in info.notices)
    assert any("§ 64 Abs. 6 NKomVG" in v for v in info.notices)
    # Öffentliche Fassung einer öffentlichen Sitzung: ohne den Hinweis zum nichtöffentlichen Teil
    oeffentlich = meeting_format_service.describe(w.sitzung, for_members=False)
    assert not any("§ 64 Abs. 6" in v for v in oeffentlich.notices)
    # Präsenzsitzung: keine Vermerke
    w.sitzung.format = SessionMeeting.FORMAT_PRESENCE
    assert meeting_format_service.describe(w.sitzung, for_members=True).notices == ()


def test_stoerung_im_verantwortungsbereich_der_kommune_fordert_unterbrechung() -> None:
    w = _ni(welt(status="draft"), tag=date(2026, 10, 20), sitzungsformat=SessionMeeting.FORMAT_HYBRID)
    zeile = _rolle(w, "Carl", "member", remote=True)
    steuern = {"view_meetings", "conduct_meetings", "view_non_public_meetings"}
    cockpit_service.start_disruption(w.sitzung, {"attendance": str(zeile.pk), "responsibility": "other"})
    assert cockpit_service.build_state(w.sitzung, steuern).interruption_hint == ""
    SessionAttendanceDisruption.objects.filter(attendance=zeile).update(responsibility="municipality")
    hinweis = cockpit_service.build_state(w.sitzung, steuern).interruption_hint
    assert "Verantwortungsbereich der Kommune (§ 64 Abs. 5 NKomVG)" in hinweis
    stoerung = SessionAttendanceDisruption.objects.get(attendance=zeile)
    assert stoerung.get_responsibility_display() == "Im Verantwortungsbereich der Kommune"
    # Unbekannter Wert aus dem Formular: nicht festgestellt
    assert cockpit_service._responsibility("irgendwas") == ""


def test_notlage_verlangt_beschluss_mit_ablauf() -> None:
    w = _ni(welt(status="draft"))
    tag = date(2026, 12, 1)
    ergebnis = meeting_format_service.check(w.tenant, [w.gremium], SessionMeeting.FORMAT_DIGITAL, "Hochwasser", day=tag)
    assert any("§ 182 NKomVG" in fehler and "Beschluss der Vertretung" in fehler for fehler in ergebnis.errors)

    _ortsrecht(w, emergency_date="2026-11-20", emergency_until="2027-02-19", emergency_reference="Rat, TOP 3")
    w.gremium.refresh_from_db()
    assert meeting_format_service.check(w.tenant, [w.gremium], SessionMeeting.FORMAT_DIGITAL, "Hochwasser", day=tag).ok
    spaet = meeting_format_service.check(
        w.tenant, [w.gremium], SessionMeeting.FORMAT_DIGITAL, "Hochwasser", day=date(2027, 3, 1)
    )
    assert any("deckt die Sitzung am 01.03.2027 nicht ab" in fehler for fehler in spaet.errors)

    _ortsrecht(w, emergency_date="2026-11-20", emergency_until="2027-03-20")
    w.gremium.refresh_from_db()
    zu_lang = meeting_format_service.check(w.tenant, [w.gremium], SessionMeeting.FORMAT_DIGITAL, "Hochwasser", day=tag)
    assert any("höchstens 3 Monate" in fehler for fehler in zu_lang.errors)

    regel = LocalRules(emergency_date=date(2026, 11, 20), emergency_until=date(2027, 2, 19))
    assert "läuft am 19.02.2027 ab" in state_law_service.emergency_warning(regel, today=date(2027, 2, 10))
    assert state_law_service.emergency_warning(regel, today=date(2027, 1, 1)) == ""


def test_uebertragung_braucht_nachweis_und_nennt_widersprueche() -> None:
    w = _ni(welt(status="draft"), tag=date(2026, 10, 20))
    SessionMeeting.objects.filter(pk=w.sitzung.pk).update(public_access_url="https://stream.example.org/rat")
    w.sitzung.refresh_from_db()
    amsel = w.stimmberechtigt[0]
    amsel.recording_objection = True
    amsel.save()

    hinweise = meeting_format_service.media_hints(w.sitzung)
    assert any("Bild- und Tonaufnahmen von Mitgliedern" in h and "§ 64 Abs. 2 NKomVG" in h for h in hinweise)
    assert any("Widerspruch gegen Bild- und Tonaufnahmen" in h and "P Amsel" in h for h in hinweise)

    # Ab 01.11.2026: Öffentlichkeit per Video laut Hauptsatzung (§ 64 Abs. 9)
    w.sitzung.start = _am(date(2026, 11, 5))
    hinweise = meeting_format_service.media_hints(w.sitzung)
    assert any("per Video nur verfolgen" in h and "§ 64 Abs. 9 NKomVG" in h for h in hinweise)
    _ortsrecht(w, video_public_date="2026-10-15", video_public_reference="§ 8a Hauptsatzung")
    w.sitzung.start = _am(date(2026, 11, 5))
    hinweise = meeting_format_service.media_hints(w.sitzung)
    assert not any("per Video nur verfolgen" in h for h in hinweise)
    # Detailseite zeigt die Hinweise intern
    info = meeting_format_service.describe(w.sitzung)
    assert any("P Amsel" in h for h in info.hints)


# =============================================================================
# Ortsrecht je Körperschaft
# =============================================================================


def test_ortsrecht_pflegen_mit_pruefprotokoll() -> None:
    w = _ni(welt(status="draft"))
    body = body_service.default_body(w.tenant)
    url = f"{base(w)}/settings/meeting-formats/ortsrecht/{body.pk}/"
    leser = client(nutzer(w.tenant, "leser2", "view_meetings"))
    assert leser.get(url).status_code == 403

    einstellungen = client(nutzer(w.tenant, "einstellungen", "view_meetings", "manage_settings"))
    seite = einstellungen.get(url).content.decode()
    assert 'data-testid="ortsrecht"' in seite
    uebersicht = einstellungen.get(f"{base(w)}/settings/meeting-formats/").content.decode()
    assert 'data-testid="ortsrecht-liste"' in uebersicht and url in uebersicht

    # Unvollständige Nachweise und zu lange Notlagenbeschlüsse werden abgewiesen
    antwort = einstellungen.post(url, {"recording_date": "2024-01-10", "emergency_date": "2026-11-20"})
    assert antwort.status_code == 200
    inhalt = antwort.content.decode()
    assert "Für den Nachweis bitte Datum und Fundstelle angeben." in inhalt
    assert "bitte Datum und Ablauf angeben" in inhalt
    antwort = einstellungen.post(url, {"emergency_date": "2026-11-20", "emergency_until": "2027-06-01"})
    assert "höchstens 3 Monate (§ 182 NKomVG)" in antwort.content.decode()

    daten = {
        "remote_per_invitation": "on",
        "recording_date": "2024-01-10",
        "recording_reference": "§ 8 Hauptsatzung",
        "rules_date": "2022-03-24",
        "rules_reference": "Amtsblatt 2022 Nr. 4",
        "invitation_days": "7",
        "invitation_day_kind": "calendar",
        "deadline_start": "provision",
        "urgent_days": "3",
        "urgent_notice": "Die Ladungsfrist ist wegen Eilbedürftigkeit abgekürzt.",
        "motion_days": "10",
        "question_days": "5",
        "minutes_signers": "Vorsitz, Bürgermeister/in, Protokollführung",
        "committees_public": "public",
        "residents_questions_minutes": "30",
    }
    assert einstellungen.post(url, daten).status_code == 302
    body.refresh_from_db()
    regeln = LocalRules.of(body)
    assert regeln.remote_per_invitation and regeln.recording_basis == "Hauptsatzung vom 10.01.2024, § 8 Hauptsatzung"
    assert (regeln.invitation_days, regeln.deadline_start, regeln.residents_questions_minutes) == (7, "provision", 30)
    assert regeln.rules_label == "Geschäftsordnung vom 24.03.2022 (Amtsblatt 2022 Nr. 4)"
    eintrag = SessionAuditLog.objects.filter(tenant=w.tenant, action="update").order_by("-seq").first()
    assert eintrag is not None and "ortsrecht" in eintrag.changes
    assert eintrag.changes["ortsrecht"]["invitation_days"] == {"alt": "", "neu": 7}


def test_ortsrecht_liest_fremde_oder_kaputte_werte_als_nicht_geregelt() -> None:
    regeln = LocalRules.from_json(
        {"remote_per_invitation": "ja", "invitation_days": 999, "emergency_until": "kein Datum", "x": 1}
    )
    assert not regeln.remote_per_invitation and regeln.invitation_days is None and regeln.emergency_until is None
    assert LocalRules.from_json(None).to_json() == {}
    assert LocalRules.of(None) == LocalRules()


def test_person_und_gremium_formular_kennen_die_neuen_felder() -> None:
    w = _ni(welt(status="draft"))
    pflege = client(nutzer(w.tenant, "stammdaten", "view_meetings", "manage_organizations"))
    person = w.stimmberechtigt[1]
    person.recording_objection, person.recording_objection_date = True, date(2026, 9, 1)
    person.save()
    seite = pflege.get(f"{base(w)}/persons/{person.pk}/edit/").content.decode()
    assert 'data-testid="aufnahme-widerspruch"' in seite
    # Datum als ISO-Text, damit das Datumsfeld es anzeigt und beim Speichern nicht verliert
    assert 'value="2026-09-01"' in seite
    seite = pflege.get(f"{base(w)}/organizations/{w.gremium.pk}/edit/").content.decode()
    assert 'name="remote_local_rule"' in seite


# =============================================================================
# Datenmigration und Rückfall per Image
# =============================================================================

VORHER = ("session", "0057_koerperschaften_zuordnen")
NACHHER = ("session", "0058_landesprofil_sitzungsrecht")


@pytest.mark.django_db(transaction=True)
def test_migration_laedt_fassungen_und_altes_image_legt_weiter_an() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Profile = alt.get_model("session", "SessionStateProfile")
        meeting_format_service.sync_profiles(model=Profile)
        Profile.objects.filter(code="NI").update(digital_council="unclear")
        Tenant = alt.get_model("session", "SessionTenant")
        Body = alt.get_model("session", "SessionBody")
        Organization = alt.get_model("session", "SessionOrganization")
        Person = alt.get_model("session", "SessionPerson")
        tenant = Tenant.objects.create(name="Gemeinde Alt", slug="alt-ni", state_profile_id="NI")

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        assert neu.get_model("session", "SessionStateProfile").objects.get(code="NI").digital_council == "emergency"
        fassungen = neu.get_model("session", "SessionStateProfileVersion").objects.filter(profile_id="NI")
        assert sorted(f.valid_from for f in fassungen) == [date(2026, 5, 7), date(2026, 11, 1)]

        # Rückfall per Image: Das alte Modell kennt die neuen Spalten nicht und legt trotzdem an
        body = Body.objects.create(tenant=tenant, name="Gemeinde Alt", slug="alt", is_default=True)
        Organization.objects.create(tenant=tenant, body=body, name="Rat", organization_type="council")
        Person.objects.create(tenant=tenant, given_name="A", family_name="Alt")
        assert neu.get_model("session", "SessionBody").objects.get(pk=body.pk).local_rules is None
        assert neu.get_model("session", "SessionOrganization").objects.get(name="Rat").remote_local_rule == ""
        assert neu.get_model("session", "SessionPerson").objects.get(family_name="Alt").recording_objection is False
        assert LocalRules.from_json(None) == LocalRules()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
