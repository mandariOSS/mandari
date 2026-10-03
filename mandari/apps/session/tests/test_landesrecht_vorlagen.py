# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gremientypen, Funktionen und Vorlagen nach Landesrecht (Issue #757, Teil L2b).

- Vorlagen je Körperschaftstyp (Samtgemeinde, Mitgliedsgemeinde mit/ohne Verwaltungsausschuss und
  Gemeindedirektor, Stadt mit Ortsräten, Landkreis): Landesprofil, Wahlperiode 2026–2031, Gremientypen mit
  gesetzlicher Ausschussart und Ladungsfrist, Geschäftsordnung, Einwohnerfragestunde – für den Mandanten und für
  jede weitere Körperschaft
- Gesetzliche Bezeichnung des Hauptausschusses und der Vertretung je Körperschaftstyp
- Funktionen: Grundmandat und Hinzugewählte ohne Stimmrecht, Ämter erst ab 18, Sperrvermerk nach Abberufung
- Tagesordnungsvorlage „Konstituierende Sitzung (Niedersachsen)“ mit Präsenz als Standard
- Datenmigration und Rückfall per Image
"""

from __future__ import annotations

from datetime import date, datetime, time
from io import StringIO
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionBody,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionStandardAgendaItem,
    SessionTenant,
)
from apps.session.services import (
    agenda_service,
    agenda_template_service,
    attendance_service,
    body_service,
    meeting_format_service,
    membership_service,
    state_law_service,
    tenant_provisioning,
    textblock_service,
)
from apps.session.services.state_law_service import LocalRules
from apps.session.tests._niederschrift import client, nutzer
from hub.ris.mapping.session import ORGANIZATION_TYPES

pytestmark = pytest.mark.django_db

ARTEN = ("ni_samtgemeinde", "ni_mitgliedsgemeinde", "ni_stadt_ortsraete", "ni_landkreis")


def _mandant(profil: str, slug: str | None = None) -> SessionTenant:
    meeting_format_service.sync_profiles()
    katalog = tenant_provisioning.load_presets()
    slug = slug or profil.replace("_", "-")
    spec = tenant_provisioning.build_spec(
        name=f"Test {profil}", slug=slug, admin_email=f"admin@{slug}.example.org", catalog=katalog, profile=profil
    )
    tenant_provisioning.provision_tenant(spec, catalog=katalog, actor="Test")
    return SessionTenant.objects.get(slug=slug)


def _gremien(tenant: SessionTenant) -> dict[str, SessionOrganization]:
    return {org.name: org for org in SessionOrganization.objects.filter(tenant=tenant)}


def _am(tag: date) -> datetime:
    return timezone.make_aware(datetime.combine(tag, time(18, 0)))


# =============================================================================
# Vorlagen je Körperschaftstyp
# =============================================================================


def test_vorlagen_sind_gueltig_und_tragen_landesprofil_und_wahlperiode() -> None:
    katalog = tenant_provisioning.load_presets()
    for key in (*ARTEN, "ni_mitgliedsgemeinde_ohne_va", "ni_mitgliedsgemeinde_gd", "ni_mitgliedsgemeinde_ohne_va_gd"):
        profil = katalog.profiles[key]
        assert profil.state_profile == "NI", key
        assert (profil.term_name, profil.term_start, profil.term_end) == (
            "Wahlperiode 2026–2031",
            date(2026, 11, 1),
            date(2031, 10, 31),
        )
        vorlage = katalog.committee_templates[profil.committees]
        assert vorlage.local_rules["invitation_days"] == 7, key
        assert any(top.kind == "residents_questions" for top in vorlage.standard_items), key
    # Mit Gemeindedirektor unterzeichnet sie bzw. er die Niederschrift mit
    gd = katalog.committee_templates["ni_mitgliedsgemeinde_gd"].local_rules["minutes_signers"]
    assert "Gemeindedirektor" in gd


@pytest.mark.parametrize(
    ("profil", "vertretung", "hauptausschuss", "body_type"),
    [
        ("ni_samtgemeinde", "Samtgemeinderat", "Samtgemeindeausschuss", "samtgemeinde"),
        ("ni_mitgliedsgemeinde", "Rat", "Verwaltungsausschuss", "mitgliedsgemeinde"),
        ("ni_stadt_ortsraete", "Rat", "Verwaltungsausschuss", "stadt"),
        ("ni_landkreis", "Kreistag", "Kreisausschuss", "kreis"),
    ],
)
def test_mandant_aus_der_vorlage_ist_vorbelegt(
    profil: str, vertretung: str, hauptausschuss: str, body_type: str
) -> None:
    tenant = _mandant(profil)
    assert tenant.state_profile_id == "NI" and tenant.body_type == body_type
    gremien = _gremien(tenant)
    rat, ha = gremien[vertretung], gremien[hauptausschuss]
    assert rat.organization_type == "council" and ha.committee_kind == SessionOrganization.COMMITTEE_KIND_MAIN
    # Gesetzliche Bezeichnungen nach Körperschaftstyp
    assert state_law_service.legal_designation(rat) == vertretung
    assert state_law_service.legal_designation(ha) == hauptausschuss
    assert ha.invitation_period_days in (6, 7)
    # Geschäftsordnung und Einwohnerfragestunde
    regeln = LocalRules.of(body_service.default_body(tenant))
    assert (regeln.invitation_days, regeln.question_days, regeln.residents_questions_minutes) == (7, 5, 30)
    assert regeln.minutes_signers
    top = SessionStandardAgendaItem.objects.get(tenant=tenant, organization=rat)
    assert (top.name, top.kind) == ("Einwohnerfragestunde", "residents_questions")
    assert SessionLegislativeTerm.objects.get(tenant=tenant).start_date == date(2026, 11, 1)
    # Prüfprotokoll nennt das Landesprofil
    eintrag = SessionAuditLog.objects.filter(tenant=tenant, changes__anlass="Mandant angelegt").first()
    assert eintrag is not None and eintrag.changes["landesprofil"] == "NI"


def test_stadt_mit_ortsraeten_und_landkreis_haben_ihre_gremientypen() -> None:
    stadt = _gremien(_mandant("ni_stadt_ortsraete"))
    assert stadt["Ortsrat (Ortschaft benennen)"].organization_type == SessionOrganization.TYPE_LOCAL_COUNCIL
    kreis = _gremien(_mandant("ni_landkreis"))
    assert kreis["Jugendhilfeausschuss"].committee_kind == SessionOrganization.COMMITTEE_KIND_SPECIAL


def test_mitgliedsgemeinde_ohne_verwaltungsausschuss_und_erneuter_lauf() -> None:
    tenant = _mandant("ni_mitgliedsgemeinde_ohne_va")
    assert set(_gremien(tenant)) == {"Rat"}
    body = body_service.default_body(tenant)
    body.local_rules = {"invitation_days": 10}
    body.save()
    # Idempotent: vorhandene Gremien und eigenes Ortsrecht bleiben unverändert
    _mandant("ni_mitgliedsgemeinde_ohne_va", slug=tenant.slug)
    assert SessionOrganization.objects.filter(tenant=tenant).count() == 1
    body.refresh_from_db()
    assert body.local_rules == {"invitation_days": 10}


def test_landesprofil_bleibt_wenn_der_mandant_schon_eines_hat() -> None:
    meeting_format_service.sync_profiles()
    tenant = SessionTenant.objects.create(name="Bestand", slug="bestand", state_profile_id="NW")
    katalog = tenant_provisioning.load_presets()
    spec = tenant_provisioning.build_spec(
        name="Bestand", slug="bestand", admin_email="a@bestand.example.org", catalog=katalog, profile="ni_landkreis"
    )
    ergebnis = tenant_provisioning.provision_tenant(spec, catalog=katalog)
    tenant.refresh_from_db()
    assert tenant.state_profile_id == "NW"
    assert any("Landesprofil bleibt NW" in w for w in ergebnis.warnings)
    with pytest.raises(tenant_provisioning.ProvisioningError, match="Unbekanntes Landesprofil"):
        tenant_provisioning.provision_tenant(
            tenant_provisioning.build_spec(
                name="X", slug="x-land", admin_email="a@x.example.org", catalog=katalog, state_profile="XX"
            ),
            catalog=katalog,
        )


def test_befehl_uebernimmt_das_landesprofil() -> None:
    meeting_format_service.sync_profiles()
    out = StringIO()
    call_command("session_create_tenant", "--list-presets", stdout=out)
    assert "ni_landkreis" in out.getvalue() and "Landesprofil NI" in out.getvalue()
    call_command(
        "session_create_tenant",
        "--profile",
        "ni_samtgemeinde",
        "--name",
        "Samtgemeinde Muster",
        "--slug",
        "sg-muster",
        "--admin-email",
        "rat@sg-muster.example.org",
        stdout=StringIO(),
    )
    assert SessionTenant.objects.get(slug="sg-muster").state_profile_id == "NI"


def test_weitere_koerperschaft_aus_der_vorlage() -> None:
    tenant = _mandant("ni_samtgemeinde", slug="sg-nord")
    einstellungen = client(nutzer(tenant, "einstellungen", "view_meetings", "manage_settings"))
    seite = einstellungen.get(f"/session/{tenant.slug}/settings/koerperschaften/neu/").content.decode()
    assert "ni_mitgliedsgemeinde_gd" in seite
    antwort = einstellungen.post(
        f"/session/{tenant.slug}/settings/koerperschaften/neu/",
        {
            "name": "Gemeinde Musterdorf",
            "short_name": "MD",
            "body_type": "mitgliedsgemeinde",
            "parent": str(body_service.default_body(tenant).pk),
            "is_active": "on",
            "template": "ni_mitgliedsgemeinde",
        },
    )
    assert antwort.status_code == 302
    body = SessionBody.objects.get(tenant=tenant, name="Gemeinde Musterdorf")
    gremien = {org.name: org for org in SessionOrganization.objects.filter(body=body)}
    assert set(gremien) == {"Rat", "Verwaltungsausschuss"}
    assert state_law_service.legal_designation(gremien["Verwaltungsausschuss"]) == "Verwaltungsausschuss"
    assert LocalRules.of(body).minutes_signers.startswith("Bürgermeister/in")
    assert SessionStandardAgendaItem.objects.filter(organization=gremien["Rat"], kind="residents_questions").exists()
    meldungen = " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert "Gremien aus der Vorlage" in meldungen
    # Die Samtgemeinde behält ihre Gremien; der Rat der Mitgliedsgemeinde ist ein eigenes Gremium
    assert SessionOrganization.objects.filter(tenant=tenant, name="Rat").count() == 1


def test_gesetzliche_bezeichnung_auf_der_gremienseite() -> None:
    meeting_format_service.sync_profiles()
    tenant = SessionTenant.objects.create(
        name="Landkreis Muster", slug="lk-muster", state_profile_id="NI", body_type="kreis"
    )
    ha = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", committee_kind="main")
    seite = client(nutzer(tenant, "leser", "view_meetings")).get(f"/session/{tenant.slug}/organizations/{ha.pk}/")
    assert "Gesetzliche Bezeichnung: Kreisausschuss" in seite.content.decode()
    # Ohne Landesprofil keine Bezeichnung
    tenant.state_profile = None
    tenant.save()
    ha.refresh_from_db()
    assert state_law_service.legal_designation(ha) == ""


# =============================================================================
# Funktionen
# =============================================================================


def _ni_mandant() -> tuple[SessionTenant, Any]:
    tenant = _mandant("ni_stadt_ortsraete", slug="stadt-ni")
    pflege = client(nutzer(tenant, "stammdaten", "view_meetings", "manage_organizations"))
    return tenant, pflege


def test_grundmandat_und_hinzugewaehlte_ohne_stimmrecht() -> None:
    tenant, pflege = _ni_mandant()
    fa = _gremien(tenant)["Finanzausschuss"]
    person = SessionPerson.objects.create(tenant=tenant, given_name="G", family_name="Grund")
    pflege.post(
        f"/session/{tenant.slug}/organizations/{fa.pk}/memberships/add/",
        {"person": str(person.pk), "role": "basic_mandate", "has_voting_rights": "on", "start_date": "2026-11-10"},
    )
    besetzung = SessionOrganizationMembership.objects.get(organization=fa, person=person)
    assert besetzung.role == "basic_mandate" and not besetzung.has_voting_rights
    assert membership_service.voting_rights("co_opted", True) is False
    assert membership_service.voting_rights("member", True) is True
    # Anwesenheit: beratend, HVB zählt als Mitglied
    assert attendance_service._ROLE_MAP["basic_mandate"] == "expert"
    assert attendance_service._ROLE_MAP["hvb"] == "member"
    assert ORGANIZATION_TYPES["group"] == "Fraktion"


def test_aemter_erst_ab_18_ab_der_fassung_vom_01_11_2026() -> None:
    tenant, pflege = _ni_mandant()
    ortsrat = _gremien(tenant)["Ortsrat (Ortschaft benennen)"]
    jung = SessionPerson.objects.create(tenant=tenant, given_name="J", family_name="Jung", adult_from=date(2027, 3, 1))
    url = f"/session/{tenant.slug}/organizations/{ortsrat.pk}/memberships/add/"

    antwort = pflege.post(url, {"person": str(jung.pk), "role": "chair", "start_date": "2026-11-15"})
    meldungen = " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert "erst ab 18 Jahren" in meldungen and "§ 92 Abs. 1" in meldungen and "01.03.2027" in meldungen
    assert not SessionOrganizationMembership.objects.filter(organization=ortsrat, person=jung).exists()

    # Als Mitglied zulässig; vor dem 01.11.2026 kennt die Fassung keine Altersgrenze für Ämter
    assert membership_service.role_problems(ortsrat, jung, "member", date(2026, 11, 15)) == []
    assert membership_service.role_problems(ortsrat, jung, "chair", date(2026, 10, 15)) == []
    assert membership_service.role_problems(ortsrat, jung, "chair", date(2027, 3, 1)) == []
    rat = _gremien(tenant)["Rat"]
    assert membership_service.role_problems(rat, jung, "local_mayor", date(2026, 12, 1))
    assert membership_service.office_of("chair", rat) == ""


def test_abberufener_ausschussvorsitz_nicht_erneut_benennbar() -> None:
    tenant, pflege = _ni_mandant()
    fa = _gremien(tenant)["Finanzausschuss"]
    vorsitz = SessionPerson.objects.create(tenant=tenant, given_name="V", family_name="Vorsitz")
    andere = SessionPerson.objects.create(tenant=tenant, given_name="A", family_name="Andere")
    url = f"/session/{tenant.slug}/organizations/{fa.pk}/memberships/add/"
    pflege.post(
        url, {"person": str(vorsitz.pk), "role": "chair", "has_voting_rights": "on", "start_date": "2026-11-10"}
    )
    besetzung = SessionOrganizationMembership.objects.get(organization=fa, person=vorsitz)
    pflege.post(
        f"/session/{tenant.slug}/memberships/{besetzung.pk}/end/", {"end_date": "2026-12-31", "end_reason": "recalled"}
    )
    besetzung.refresh_from_db()
    assert besetzung.end_reason == SessionOrganizationMembership.END_RECALLED

    antwort = pflege.post(url, {"person": str(vorsitz.pk), "role": "chair", "start_date": "2027-02-01"})
    meldungen = " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert "nicht erneut benannt werden (§ 71 Abs. 8 NKomVG)" in meldungen
    assert SessionOrganizationMembership.objects.filter(organization=fa, person=vorsitz).count() == 1
    # Als einfaches Mitglied und für andere Personen zulässig
    assert membership_service.role_problems(fa, vorsitz, "member", date(2027, 2, 1)) == []
    assert membership_service.role_problems(fa, andere, "chair", date(2027, 2, 1)) == []


# =============================================================================
# Einwohnerfragestunde und konstituierende Sitzung
# =============================================================================


def _sitzung(tenant: SessionTenant, gremium: SessionOrganization, **felder: Any) -> SessionMeeting:
    return SessionMeeting.objects.create(
        tenant=tenant, organization=gremium, name="Sitzung", start=_am(date(2026, 11, 20)), **felder
    )


def test_einwohnerfragestunde_aus_dem_standard_top_und_nur_oeffentlich() -> None:
    tenant = _mandant("ni_landkreis")
    sitzung = _sitzung(tenant, _gremien(tenant)["Kreistag"])
    textblock_service.apply_standard_items(sitzung)
    top = SessionAgendaItem.objects.get(meeting=sitzung, kind="residents_questions")
    assert top.is_public and agenda_service.visibility_errors(top) == {}
    top.is_public = False
    assert "öffentlichen Teil" in agenda_service.visibility_errors(top)["is_public"]


def test_konstituierende_sitzung_aus_der_vorlage_in_praesenz() -> None:
    tenant = _mandant("ni_samtgemeinde", slug="sg-konst")
    rat = _gremien(tenant)["Samtgemeinderat"]
    sitzung = _sitzung(tenant, rat, format=SessionMeeting.FORMAT_HYBRID, meeting_state="draft")
    bearbeiten = client(nutzer(tenant, "sitzungsdienst", "view_meetings", "edit_meetings", "view_non_public_meetings"))
    seite = bearbeiten.get(f"/session/{tenant.slug}/meetings/{sitzung.pk}/").content.decode()
    assert 'data-testid="tagesordnungsvorlage"' in seite and "konstituierend_ni_mitgliedsgemeinde" in seite

    url = f"/session/{tenant.slug}/meetings/{sitzung.pk}/agenda/vorlage/"
    antwort = bearbeiten.post(url, {"vorlage": "konstituierend_ni"})
    assert antwort.status_code == 302
    tops = list(SessionAgendaItem.objects.filter(meeting=sitzung).order_by("order"))
    assert [t.number for t in tops][:3] == ["1", "2", "3"]
    wahlen = [t.name for t in tops if t.is_election]
    assert "Wahl der bzw. des Vorsitzenden der Vertretung" in wahlen and len(wahlen) == 3
    sitzung.refresh_from_db()
    assert sitzung.format == SessionMeeting.FORMAT_PRESENCE
    meldungen = " ".join(str(m) for m in get_messages(antwort.wsgi_request))
    assert "Präsenzsitzung" in meldungen and "§ 67 Satz 2 NKomVG" in meldungen
    assert SessionAuditLog.objects.filter(tenant=tenant, changes__tagesordnungsvorlage__isnull=False).exists()

    # Unbekannte Vorlage: Meldung statt Fehler
    antwort = bearbeiten.post(url, {"vorlage": "gibt-es-nicht"})
    assert "Bitte eine Tagesordnungsvorlage" in " ".join(str(m) for m in get_messages(antwort.wsgi_request))


def test_nach_der_ladung_bleibt_das_format_mit_hinweis() -> None:
    tenant = _mandant("ni_mitgliedsgemeinde", slug="mg-konst")
    sitzung = _sitzung(
        tenant,
        _gremien(tenant)["Rat"],
        format=SessionMeeting.FORMAT_HYBRID,
        meeting_state="invitation_sent",
        invitation_sent_at=timezone.now(),
    )
    vorlage = agenda_template_service.load()["konstituierend_ni_mitgliedsgemeinde"]
    ergebnis = agenda_template_service.apply(sitzung, vorlage)
    sitzung.refresh_from_db()
    assert sitzung.format == SessionMeeting.FORMAT_HYBRID and not ergebnis.format_changed
    assert "bereits geladen" in ergebnis.warning
    assert all(t.is_supplementary for t in SessionAgendaItem.objects.filter(meeting=sitzung))


def test_vorlagen_nur_fuer_das_eigene_land() -> None:
    meeting_format_service.sync_profiles()
    nrw = SessionTenant.objects.create(name="Stadt NRW", slug="nrw", state_profile_id="NW")
    assert agenda_template_service.available(nrw) == []
    vorlagen = agenda_template_service.load()
    assert set(vorlagen) == {"konstituierend_ni", "konstituierend_ni_mitgliedsgemeinde"}
    assert all(v.presence and v.state_profile == "NI" for v in vorlagen.values())


# =============================================================================
# Datenmigration und Rückfall per Image
# =============================================================================

VORHER = ("session", "0058_landesprofil_sitzungsrecht")
NACHHER = ("session", "0059_gremientypen_funktionen")


@pytest.mark.django_db(transaction=True)
def test_altes_image_legt_nach_der_migration_weiter_an() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        Tenant = alt.get_model("session", "SessionTenant")
        Organization = alt.get_model("session", "SessionOrganization")
        Person = alt.get_model("session", "SessionPerson")
        Membership = alt.get_model("session", "SessionOrganizationMembership")
        Meeting = alt.get_model("session", "SessionMeeting")
        Item = alt.get_model("session", "SessionAgendaItem")
        Standard = alt.get_model("session", "SessionStandardAgendaItem")
        tenant = Tenant.objects.create(name="Alt", slug="alt-b")
        rat = Organization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        person = Person.objects.create(tenant=tenant, given_name="A", family_name="Alt")
        besetzung = Membership.objects.create(organization=rat, person=person)
        sitzung = Meeting.objects.create(tenant=tenant, organization=rat, name="Rat", start="2030-01-01T17:00:00Z")
        top = Item.objects.create(meeting=sitzung, number="1", order=1, name="Bestand")
        standard = Standard.objects.create(tenant=tenant, name="Eröffnung")
        assert neu.get_model("session", "SessionAgendaItem").objects.get(pk=top.pk).kind == ""
        assert neu.get_model("session", "SessionStandardAgendaItem").objects.get(pk=standard.pk).kind == ""
        assert neu.get_model("session", "SessionOrganizationMembership").objects.get(pk=besetzung.pk).end_reason == ""
        assert neu.get_model("session", "SessionPerson").objects.get(pk=person.pk).adult_from is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
