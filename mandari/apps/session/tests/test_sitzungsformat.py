# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsformat (präsent, hybrid, digital) und Landesprofil je Mandant (Issue #138).

- Landesprofile für alle 16 Länder mit Normen, Quellen und Stand; Doku nennt dieselben Quellen
- Prüfung gegen das Landesprofil: ausgeschlossene Gremientypen werden mit Begründung verhindert,
  Notlagen und digitale Sitzungen brauchen eine Begründung, der Regelbetrieb den örtlichen Nachweis
- Sitzungsformular, Einstellungsseite (mit Audit) und verschlüsselter Zugangsweg
- Datenmigration lädt die Profile; ein älteres Image legt weiter Sitzungen an
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionRole,
    SessionStateProfile,
    SessionTenant,
    SessionUser,
)
from apps.session.services import meeting_format_service as mfs

LAENDER = {"BW", "BY", "BE", "BB", "HB", "HH", "HE", "MV", "NI", "NW", "RP", "SL", "SN", "ST", "SH", "TH"}
DOKU = Path(__file__).resolve().parents[4] / "docs" / "SESSION_SITZUNGSFORMAT_LANDESRECHT.md"


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    bau: SessionOrganization
    haupt: SessionOrganization
    fraktion: SessionOrganization
    client: Client


def _profile(code: str) -> SessionStateProfile:
    mfs.sync_profiles()  # Tests mit transaction=True leeren die Tabelle; Übernahme ist idempotent
    return SessionStateProfile.objects.get(code=code)


def _nachweis(tenant: SessionTenant) -> None:
    tenant.hybrid_basis_kind = "hauptsatzung"
    tenant.hybrid_basis_date = date(2024, 3, 12)
    tenant.hybrid_basis_reference = "§ 7 Hauptsatzung, Amtsblatt 2024 Nr. 5"
    tenant.save()


@pytest.fixture
def welt(db: Any) -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="muster", state_profile=_profile("NW"))
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    haupt = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", committee_kind="main")
    fraktion = SessionOrganization.objects.create(tenant=tenant, name="Fraktion A", organization_type="faction")
    role = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_create_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_manage_settings=True,
        can_manage_organizations=True,
    )
    user = cast(Any, UserFactory)(email="sitzungsdienst@muster.example")
    staff = SessionUser.objects.create(user=user, tenant=tenant)
    staff.roles.add(role)
    client = Client()
    client.force_login(user)
    return Welt(tenant, rat, bau, haupt, fraktion, client)


# =============================================================================
# Landesprofile: Datei, Datenbank, Doku
# =============================================================================


def test_profildatei_deckt_alle_16_laender_mit_quellen_und_gueltigen_werten_ab() -> None:
    rows = mfs.profile_rows()
    assert {row["code"] for row in rows} == LAENDER
    rule_values = {key for key, _ in SessionStateProfile.RULE_CHOICES}
    kinds = {key for key, _ in SessionOrganization.COMMITTEE_KIND_CHOICES}
    for row in rows:
        for key in ("hybrid_council", "hybrid_committees", "digital_council", "digital_committees"):
            assert row[key] in rule_values, (row["code"], key)
        assert row["excluded_committee_rule"] in rule_values
        assert row["legal_basis"] in {key for key, _ in SessionStateProfile.BASIS_CHOICES}
        assert row["chair_present"] in {key for key, _ in SessionStateProfile.CHAIR_CHOICES}
        assert row["verification"] in {key for key, _ in SessionStateProfile.VERIFICATION_CHOICES}
        assert set(row["excluded_committee_kinds"]) <= kinds
        assert row["sources"], row["code"]
        assert all(source["url"].startswith("https://") and source["title"] for source in row["sources"])
        assert isinstance(row["as_of"], date)
        # Regelbetrieb nennt eine Norm, Notlage ebenso
        if "regular" in (row["hybrid_council"], row["hybrid_committees"]):
            assert row["norm_regular"], row["code"]
        if "emergency" in (row["hybrid_council"], row["digital_council"], row["digital_committees"]):
            assert row["norm_emergency"], row["code"]


def test_doku_nennt_stand_hinweis_und_alle_quellen() -> None:
    text = DOKU.read_text(encoding="utf-8")
    assert "Keine Rechtsberatung" in text
    assert "Stand der Recherche: 30.09.2026" in text
    for row in mfs.profile_rows():
        assert f"### {row['name']}" in text, row["name"]
        for source in row["sources"]:
            assert source["url"] in text, (row["code"], source["url"])


@pytest.mark.django_db
def test_sync_ist_idempotent_und_check_meldet_abweichungen() -> None:
    mfs.sync_profiles()
    created, updated = mfs.sync_profiles()
    assert (created, updated) == (0, 16)
    assert SessionStateProfile.objects.count() == 16
    assert mfs.profile_differences() == []

    SessionStateProfile.objects.filter(code="NW").update(hybrid_committees="none")
    assert mfs.profile_differences() == ["NW: abweichend (hybrid_committees)"]
    with pytest.raises(CommandError):
        call_command("session_state_profiles", "--check", stdout=StringIO())
    ausgabe = StringIO()
    call_command("session_state_profiles", "--sync", stdout=ausgabe)
    assert "0 neu, 16 aktualisiert" in ausgabe.getvalue()
    assert SessionStateProfile.objects.get(code="NW").hybrid_committees == "regular"
    call_command("session_state_profiles", "--check", stdout=StringIO())


# =============================================================================
# Prüfregeln
# =============================================================================


def test_praesenz_ist_immer_zulaessig_auch_ohne_landesprofil(welt: Welt) -> None:
    welt.tenant.state_profile = None
    assert mfs.check(welt.tenant, [welt.rat], "presence").ok


def test_ohne_landesprofil_keine_hybride_sitzung(welt: Welt) -> None:
    welt.tenant.state_profile = None
    result = mfs.check(welt.tenant, [welt.bau], "hybrid")
    assert not result.ok
    assert "Landesprofil" in result.errors[0]


def test_nrw_hauptausschuss_hybrid_wird_mit_begruendung_verhindert(welt: Welt) -> None:
    """Akzeptanzkriterium: ausgeschlossener Gremientyp, Hinweis auf § 58a / § 47a GO NRW."""
    _nachweis(welt.tenant)
    result = mfs.check(welt.tenant, [welt.haupt], "hybrid")
    assert not result.ok
    assert len(result.errors) == 1
    assert "Hauptausschuss" in result.errors[0]
    assert "§ 58a GO NRW" in result.errors[0]
    assert "§ 47a GO NRW" in result.errors[0]
    # In einer begründeten Notlage nach § 47a GO NRW zulässig
    notlage = mfs.check(welt.tenant, [welt.haupt], "hybrid", "Hochwasser; Ratsbeschluss vom 01.10.2026 (2/3)")
    assert notlage.ok
    assert mfs.legal_basis_text(welt.tenant, notlage.rules) == "§ 47a GO NRW (Notlage)"


def test_nrw_fachausschuss_hybrid_braucht_hauptsatzungsnachweis(welt: Welt) -> None:
    ohne = mfs.check(welt.tenant, [welt.bau], "hybrid")
    assert not ohne.ok
    assert "Hauptsatzung" in ohne.errors[0]
    _nachweis(welt.tenant)
    mit = mfs.check(welt.tenant, [welt.bau], "hybrid")
    assert mit.ok
    assert not mit.needs_reason
    assert mfs.legal_basis_text(welt.tenant, mit.rules) == (
        "§ 58a GO NRW i. V. m. Hauptsatzung vom 12.03.2024, § 7 Hauptsatzung, Amtsblatt 2024 Nr. 5"
    )


def test_nrw_rat_hybrid_nur_in_notlage_und_digital_immer_mit_begruendung(welt: Welt) -> None:
    _nachweis(welt.tenant)
    rat = mfs.check(welt.tenant, [welt.rat], "hybrid")
    assert not rat.ok
    assert "begründen" in rat.errors[0]
    assert "Zweidrittelmehrheit" in rat.errors[0]
    assert mfs.check(welt.tenant, [welt.rat], "hybrid", "Epidemische Lage").ok
    digital = mfs.check(welt.tenant, [welt.bau], "digital")
    assert not digital.ok and digital.needs_reason
    assert mfs.check(welt.tenant, [welt.bau], "digital", "Unwetterwarnung, Ratsbeschluss").ok


def test_gemeinsame_sitzung_strengste_regel_gilt(welt: Welt) -> None:
    _nachweis(welt.tenant)
    assert mfs.check(welt.tenant, [welt.bau], "hybrid").ok
    gemeinsam = mfs.check(welt.tenant, [welt.bau, welt.haupt], "hybrid")
    assert not gemeinsam.ok
    strengste = mfs.strictest_rule(gemeinsam.rules)
    assert strengste is not None and strengste.organization == welt.haupt


def test_nicht_vorgesehen_verhindert_mit_norm(welt: Welt) -> None:
    welt.tenant.state_profile = _profile("BY")
    _nachweis(welt.tenant)
    result = mfs.check(welt.tenant, [welt.rat], "digital", "Notlage")
    assert not result.ok
    assert "nicht vorgesehen" in result.errors[0]
    assert "Gemeindeordnung für den Freistaat Bayern" in result.errors[0]
    assert mfs.check(welt.tenant, [welt.rat], "hybrid").ok


def test_ungeklaerte_rechtslage_nur_mit_oertlichem_nachweis(welt: Welt) -> None:
    welt.tenant.state_profile = _profile("BY")
    result = mfs.check(welt.tenant, [welt.bau], "hybrid")
    assert not result.ok
    assert "ungeklärt" in result.errors[0]
    _nachweis(welt.tenant)
    assert mfs.check(welt.tenant, [welt.bau], "hybrid").ok


def test_fraktionen_unterliegen_nicht_den_sitzungsregeln(welt: Welt) -> None:
    result = mfs.check(welt.tenant, [welt.fraktion], "digital")
    assert result.ok
    assert mfs.legal_basis_text(welt.tenant, result.rules) == ""


def test_beschluss_als_voraussetzung_verlangt_begruendung(welt: Welt) -> None:
    welt.tenant.state_profile = _profile("HH")
    assert not mfs.check(welt.tenant, [welt.bau], "hybrid").ok
    assert mfs.check(welt.tenant, [welt.bau], "hybrid", "Beschluss des Ausschusses vom 02.09.2026").ok


# =============================================================================
# Sitzungsformular, Gremium und Einstellungen
# =============================================================================


def _formular(welt: Welt, **extra: Any) -> dict[str, Any]:
    return {
        "name": "Sitzung",
        "organization": str(welt.haupt.pk),
        "start": "2031-03-01T17:00",
        "is_public": "on",
        **extra,
    }


def test_formular_verhindert_hybriden_hauptausschuss_und_speichert_begruendete_notlage(welt: Welt) -> None:
    _nachweis(welt.tenant)
    antwort = welt.client.post("/session/muster/meetings/create/", _formular(welt, format="hybrid"))
    assert antwort.status_code == 200
    assert "§ 58a GO NRW" in antwort.content.decode()
    assert not SessionMeeting.objects.filter(name="Sitzung").exists()

    antwort = welt.client.post(
        "/session/muster/meetings/create/",
        _formular(
            welt,
            format="hybrid",
            format_reason="Hochwasser; Ratsbeschluss vom 01.10.2026",
            remote_access="Konferenzraum 4711, PIN 2468",
            public_access_url="https://stream.example.org/rat",
        ),
    )
    assert antwort.status_code == 302
    sitzung = SessionMeeting.objects.get(name="Sitzung")
    assert sitzung.format == "hybrid"
    assert sitzung.public_access_url == "https://stream.example.org/rat"
    # Zugangsweg nur verschlüsselt gespeichert
    assert sitzung.remote_access_encrypted
    assert b"4711" not in bytes(sitzung.remote_access_encrypted)
    assert cast(Any, sitzung).get_remote_access_decrypted() == "Konferenzraum 4711, PIN 2468"


def test_formular_ohne_format_bleibt_praesenz_und_verwirft_zugangsweg(welt: Welt) -> None:
    antwort = welt.client.post(
        "/session/muster/meetings/create/", _formular(welt, organization=str(welt.rat.pk), remote_access="geheim")
    )
    assert antwort.status_code == 302
    sitzung = SessionMeeting.objects.get(name="Sitzung")
    assert sitzung.format == "presence"
    assert not sitzung.remote_access_encrypted


def test_bearbeiten_prueft_weitere_gremien_der_gemeinsamen_sitzung(welt: Welt) -> None:
    _nachweis(welt.tenant)
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, organization=welt.bau, name="Bau", start="2031-03-01T17:00:00Z", format="hybrid"
    )
    daten = _formular(welt, organization=str(welt.bau.pk), format="hybrid", meeting_state="scheduled")
    antwort = welt.client.post(
        f"/session/muster/meetings/{sitzung.pk}/edit/", {**daten, "joint_organizations": [str(welt.haupt.pk)]}
    )
    assert antwort.status_code == 200
    assert "Hauptausschuss" in antwort.content.decode()
    assert not SessionMeeting.objects.get(pk=sitzung.pk).joint_organizations.exists()


def test_formular_zeigt_landesprofil_und_zugangsweg_vorbelegt(welt: Welt) -> None:
    _nachweis(welt.tenant)
    sitzung = SessionMeeting.objects.create(
        tenant=welt.tenant, organization=welt.bau, name="Bau", start="2031-03-01T17:00:00Z", format="hybrid"
    )
    cast(Any, sitzung).set_remote_access_encrypted("Raum B, Einwahl folgt")
    sitzung.save()
    inhalt = welt.client.get(f"/session/muster/meetings/{sitzung.pk}/edit/").content.decode()
    assert "Landesprofil Nordrhein-Westfalen" in inhalt
    assert "Raum B, Einwahl folgt" in inhalt


def test_gremienformular_setzt_gesetzliche_ausschussart(welt: Welt) -> None:
    antwort = welt.client.post(
        f"/session/muster/organizations/{welt.bau.pk}/edit/",
        {
            "name": "Finanzausschuss",
            "organization_type": "committee",
            "committee_kind": "finance",
            "invitation_period_days": 7,
            "allowance_amount": "0.00",
            "is_active": "on",
        },
    )
    assert antwort.status_code == 302
    welt.bau.refresh_from_db()
    assert welt.bau.committee_kind == "finance"


def test_einstellungen_speichern_landesprofil_und_nachweis_mit_audit(welt: Welt) -> None:
    seite = welt.client.get("/session/muster/settings/meeting-formats/")
    assert seite.status_code == 200
    inhalt = seite.content.decode()
    assert "Keine Rechtsberatung" in inhalt
    assert "https://recht.nrw.de/gvnrw/2022-s490/" in inhalt

    unvollstaendig = welt.client.post(
        "/session/muster/settings/meeting-formats/", {"state_profile": "NI", "hybrid_basis_kind": "hauptsatzung"}
    )
    assert unvollstaendig.status_code == 200
    assert "Art, Datum und Fundstelle" in unvollstaendig.content.decode()

    antwort = welt.client.post(
        "/session/muster/settings/meeting-formats/",
        {
            "state_profile": "NI",
            "hybrid_basis_kind": "hauptsatzung",
            "hybrid_basis_date": "2024-03-12",
            "hybrid_basis_reference": "§ 7 Hauptsatzung",
            "digital_public_registration_days": "2",
        },
    )
    assert antwort.status_code == 302
    welt.tenant.refresh_from_db()
    assert welt.tenant.state_profile_id == "NI"
    assert welt.tenant.hybrid_basis_documented
    assert welt.tenant.digital_public_registration_days == 2
    eintraege = [
        e.changes["sitzungsformate"]
        for e in SessionAuditLog.objects.filter(tenant=welt.tenant, action="update")
        if isinstance(e.changes, dict) and "sitzungsformate" in e.changes
    ]
    assert len(eintraege) == 1
    assert eintraege[0]["state_profile"] == {"alt": "Nordrhein-Westfalen", "neu": "Niedersachsen"}
    assert eintraege[0]["hybrid_basis_reference"] == {"alt": "", "neu": "§ 7 Hauptsatzung"}


def test_einstellungen_nur_mit_recht(welt: Welt) -> None:
    SessionRole.objects.filter(tenant=welt.tenant).update(can_manage_settings=False)
    antwort = welt.client.post("/session/muster/settings/meeting-formats/", {"state_profile": ""})
    assert antwort.status_code in (302, 403)
    welt.tenant.refresh_from_db()
    assert welt.tenant.state_profile_id == "NW"


def test_datenschutzlauf_loescht_zugangsweg_nach_frist(welt: Welt) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.session.services import privacy_service

    alt = SessionMeeting.objects.create(
        tenant=welt.tenant,
        organization=welt.bau,
        name="Alt",
        start=timezone.now() - timedelta(days=800),
        format="hybrid",
    )
    cast(Any, alt).set_remote_access_encrypted("Raum 1, PIN 1234")
    alt.save()
    welt.tenant.settings = {"privacy": {"np_content_years": 1}}
    welt.tenant.save()
    stats = privacy_service.run_privacy_purge(welt.tenant)
    assert stats["np_meetings_cleared"] == 1
    assert not SessionMeeting.objects.get(pk=alt.pk).remote_access_encrypted


# =============================================================================
# Datenmigration und Rückfall per Image
# =============================================================================

VORHER = ("session", "0043_antrag_anhaenge")
NACHHER = ("session", "0045_landesprofile_laden")


@pytest.mark.django_db(transaction=True)
def test_migration_laedt_profile_und_altes_image_legt_weiter_sitzungen_an() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Tenant = alt.get_model("session", "SessionTenant")
        Organization = alt.get_model("session", "SessionOrganization")
        Meeting = alt.get_model("session", "SessionMeeting")
        tenant = Tenant.objects.create(name="Stadt Alt", slug="alt")
        rat = Organization.objects.create(tenant=tenant, name="Rat")
        bestand = Meeting.objects.create(tenant=tenant, organization=rat, name="Bestand", start="2030-01-01T17:00:00Z")

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        assert neu.get_model("session", "SessionStateProfile").objects.count() == 16
        assert neu.get_model("session", "SessionMeeting").objects.get(pk=bestand.pk).format == "presence"

        # Rückfall per Image: Das alte Modell kennt die neuen Spalten nicht und legt trotzdem an
        spaeter = Meeting.objects.create(tenant=tenant, organization=rat, name="Später", start="2030-02-01T17:00:00Z")
        Organization.objects.create(tenant=tenant, name="Neuer Ausschuss")
        Tenant.objects.create(name="Stadt Neu", slug="neu")
        zeile = neu.get_model("session", "SessionMeeting").objects.get(pk=spaeter.pk)
        assert (zeile.format, zeile.format_reason, zeile.public_access_url) == ("presence", "", "")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
