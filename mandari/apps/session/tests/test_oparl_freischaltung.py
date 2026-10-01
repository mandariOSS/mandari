# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Veröffentlichungsumfang der Session-OParl-Schnittstelle je Mandant (Issue #319).

- Ein neuer Mandant ist bis zur Freischaltung nicht abrufbar (404, auch anonyme Session-API), das
  Bürgerportal registriert erst danach eine Quelle; Zurücknehmen nur ohne Bürgerportal.
- E-Mail-Adressen erscheinen nur bei Personen mit Kennzeichen, Datum und Nachweis der Einwilligung.
- Die Datenmigration lässt den Bestand öffentlich und übernimmt Ratsmitglieder.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from apps.session.models import (
    SessionAPIToken,
    SessionAuditLog,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import (
    file_version_service,
    insight_service,
    oparl_access,
    portal_publication,
    privacy_service,
    tenant_provisioning,
)
from apps.session.tests._niederschrift import client as angemeldet
from apps.session.tests._niederschrift import nutzer
from insight_core.models import OParlSource

pytestmark = pytest.mark.django_db

OPARL = "/session/{slug}/api/oparl/"


def _pfade(tenant: SessionTenant) -> list[str]:
    """Alle Arten von Endpunkten: System, Body, Listen, Objekt, Datei-Abruf."""
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=gremium, start=timezone.now(), is_public=True
    )
    datei = SessionFile(tenant=tenant, meeting=sitzung, name="Einladung", is_public=True)
    upload = SimpleUploadedFile("einladung.pdf", b"%PDF-1.4 Einladung", content_type="application/pdf")
    file_version_service.attach_upload(datei, upload, user=None)
    basis = OPARL.format(slug=tenant.slug)
    return [
        basis,
        f"{basis}body/",
        f"{basis}bodies/",
        f"{basis}meetings/",
        f"{basis}organizations/",
        f"{basis}people/",
        f"{basis}meeting/{sitzung.pk}/",
        f"{basis}file/{datei.pk}/",
        f"{basis}file/{datei.pk}/download/",
    ]


def _neu(slug: str = "neustadt") -> SessionTenant:
    """Mandant auf dem Weg, den Befehl und Admin-Assistent nehmen."""
    katalog = tenant_provisioning.load_presets()
    spec = tenant_provisioning.build_spec(
        name="Stadt Neustadt", slug=slug, admin_email="rat@neustadt.example.org", profile="nrw_stadt", catalog=katalog
    )
    result = tenant_provisioning.provision_tenant(spec, catalog=katalog, actor="test")
    return SessionTenant.objects.get(pk=result.tenant_id)


# =============================================================================
# Freischaltung der Schnittstelle
# =============================================================================


class TestFreischaltung:
    def test_neuer_mandant_bis_zur_freischaltung_nicht_abrufbar(self, client: Client) -> None:
        tenant = _neu()
        assert tenant.oparl_public_since is None
        pfade = _pfade(tenant)

        for pfad in pfade:
            antwort = client.get(pfad)
            assert antwort.status_code == 404, pfad
            assert b"Ratssitzung" not in antwort.content and b"Neustadt" not in antwort.content

        assert oparl_access.release(tenant) is True
        for pfad in pfade:
            assert client.get(pfad).status_code == 200, pfad
        assert client.get(OPARL.format(slug=tenant.slug)).json()["name"] == "Sitzungsdienst Stadt Neustadt"

    def test_404_ohne_auskunft_ueber_den_grund(self, client: Client) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        gesperrt = client.get(OPARL.format(slug=tenant.slug))
        assert gesperrt.status_code == 404
        assert b"frei" not in gesperrt.content.lower() and b"Stadt Test" not in gesperrt.content

    def test_anonyme_session_api_erst_nach_freischaltung(self, client: Client) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        pfade = [
            f"/api/v1/session/{tenant.slug}/",
            f"/api/v1/session/{tenant.slug}/meetings/",
            f"/api/v1/session/{tenant.slug}/papers/",
            f"/session/{tenant.slug}/api/",
            f"/session/{tenant.slug}/api/session/meetings/",
            f"/session/{tenant.slug}/api/session/papers/",
        ]
        for pfad in pfade:
            assert client.get(pfad).status_code == 404, pfad

        # Angemeldete Nutzer mit API-Zugang und Sichtrecht sowie API-Token des Mandanten lesen weiter
        leser = angemeldet(nutzer(tenant, "leser", "view_meetings", "view_papers", "access_api"))
        for pfad in pfade:
            assert leser.get(pfad).status_code == 200, pfad
        _token, roh = SessionAPIToken.create_token(tenant, "Integration")
        antwort = client.get(f"/api/v1/session/{tenant.slug}/meetings/", headers={"Authorization": f"Bearer {roh}"})
        assert antwort.status_code == 200

        oparl_access.release(tenant)
        for pfad in pfade:
            assert client.get(pfad).status_code == 200, pfad

    def test_freischalten_und_zuruecknehmen_mit_audit(self, client: Client) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        admin = angemeldet(nutzer(tenant, "verwaltung", "manage_settings"))

        seite = admin.get(f"/session/{tenant.slug}/settings/")
        assert b'data-testid="oparl-gesperrt"' in seite.content
        assert b'data-testid="buergerportal-voraussetzung"' in seite.content

        antwort = admin.post(f"/session/{tenant.slug}/settings/oparl-schnittstelle/", {"public": "1"})
        assert antwort.status_code == 302
        tenant.refresh_from_db()
        assert tenant.oparl_public_since is not None
        assert client.get(OPARL.format(slug=tenant.slug)).status_code == 200
        seite = admin.get(f"/session/{tenant.slug}/settings/")
        assert b'data-testid="oparl-oeffentlich"' in seite.content
        assert b'data-testid="buergerportal-voraussetzung"' not in seite.content

        admin.post(f"/session/{tenant.slug}/settings/oparl-schnittstelle/", {"public": "0"})
        tenant.refresh_from_db()
        assert tenant.oparl_public_since is None
        assert client.get(OPARL.format(slug=tenant.slug)).status_code == 404

        eintraege = SessionAuditLog.objects.filter(tenant=tenant, object_repr="OParl-Schnittstelle").order_by("seq")
        assert [e.action for e in eintraege] == ["publish", "unpublish"]
        assert eintraege[0].changes["oparl_schnittstelle"]["alt"] == "nicht freigeschaltet"

    def test_nur_mit_einstellungsrecht(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        leser = angemeldet(nutzer(tenant, "leser", "view_meetings"))
        antwort = leser.post(f"/session/{tenant.slug}/settings/oparl-schnittstelle/", {"public": "1"})
        assert antwort.status_code in (302, 403)
        tenant.refresh_from_db()
        assert tenant.oparl_public_since is None


# =============================================================================
# Bürgerportal erst nach der Freischaltung
# =============================================================================


def _quellen(tenant: SessionTenant) -> list[OParlSource]:
    return list(insight_service.session_sources(tenant))


class TestBuergerportal:
    def test_veroeffentlichen_setzt_freischaltung_voraus(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")

        with pytest.raises(oparl_access.OParlAccessError):
            portal_publication.resume_publication(tenant)
        tenant.refresh_from_db()
        assert tenant.insight_publish is False
        assert _quellen(tenant) == []

        oparl_access.release(tenant)
        portal_publication.resume_publication(tenant)
        quellen = _quellen(tenant)
        assert len(quellen) == 1 and quellen[0].is_active

    def test_oberflaeche_meldet_fehlende_freischaltung(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        admin = angemeldet(nutzer(tenant, "verwaltung", "manage_settings"))

        antwort = admin.post(f"/session/{tenant.slug}/settings/insight-publish/", {"publish": "1"}, follow=True)

        assert "Schalten Sie zuerst die OParl" in antwort.content.decode()
        tenant.refresh_from_db()
        assert tenant.insight_publish is False
        assert _quellen(tenant) == []

    def test_kein_weg_registriert_ohne_freischaltung(self) -> None:
        # Direktes Speichern (Admin-Skripte, alte Aufrufer): das Signal registriert keine Quelle
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test", insight_publish=True)
        assert _quellen(tenant) == []
        tenant.save()
        assert _quellen(tenant) == []

        # Die Freischaltung holt die Registrierung nach
        oparl_access.release(tenant)
        quellen = _quellen(tenant)
        assert len(quellen) == 1 and quellen[0].is_active

    def test_zuruecknehmen_nur_ohne_buergerportal(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        oparl_access.release(tenant)
        portal_publication.resume_publication(tenant)

        with pytest.raises(oparl_access.OParlAccessError):
            oparl_access.lock(tenant)
        tenant.refresh_from_db()
        assert tenant.oparl_public

        admin = angemeldet(nutzer(tenant, "verwaltung", "manage_settings"))
        antwort = admin.post(f"/session/{tenant.slug}/settings/oparl-schnittstelle/", {"public": "0"}, follow=True)
        assert "Beenden Sie zuerst die Veröffentlichung im Bürgerportal" in antwort.content.decode()
        tenant.refresh_from_db()
        assert tenant.oparl_public

        portal_publication.end_publication(tenant, SessionTenant.PORTAL_END_PAUSED)
        assert oparl_access.lock(tenant) is True

    def test_modell_prueft_voraussetzung(self) -> None:
        from django.core.exceptions import ValidationError

        tenant = SessionTenant(name="Stadt Test", slug="test", insight_publish=True)
        with pytest.raises(ValidationError):
            tenant.clean()
        tenant.oparl_public_since = timezone.now()
        tenant.clean()

    def test_befehl_registriert_erst_nach_freischaltung(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")

        with pytest.raises(CommandError):
            call_command("session_insight_source", "--tenant", "test")
        assert _quellen(tenant) == []

        call_command("session_insight_source", "--tenant", "test", "--oparl-freischalten")
        tenant.refresh_from_db()
        assert tenant.oparl_public and tenant.insight_publish
        assert len(_quellen(tenant)) == 1

    def test_reaktivieren_registriert_nicht_ohne_freischaltung(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test", insight_publish=True)
        tenant_provisioning.set_tenant_active(tenant, False)
        tenant_provisioning.set_tenant_active(tenant, True)
        assert _quellen(tenant) == []

    def test_reaktivieren_vermerkt_gesperrte_schnittstelle(self) -> None:
        # Bei der Einführung inaktive Mandanten bleiben gesperrt – anders als vor ihrer Deaktivierung
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test")
        tenant_provisioning.set_tenant_active(tenant, False)
        tenant_provisioning.set_tenant_active(tenant, True)

        eintrag = SessionAuditLog.objects.filter(tenant=tenant, action="update", model_name="SessionTenant").latest(
            "seq"
        )
        assert eintrag.changes["is_active"]["neu"] is True
        assert eintrag.changes["oparl_schnittstelle"] == tenant_provisioning.REACTIVATED_LOCKED

        oparl_access.release(tenant)
        tenant_provisioning.set_tenant_active(tenant, False)
        tenant_provisioning.set_tenant_active(tenant, True)
        eintrag = SessionAuditLog.objects.filter(tenant=tenant, action="update", model_name="SessionTenant").latest(
            "seq"
        )
        assert "oparl_schnittstelle" not in eintrag.changes

    def test_admin_aktion_warnt_bei_gesperrter_schnittstelle(self) -> None:
        from typing import cast

        from apps.common.tests.factories import UserFactory

        gesperrt = SessionTenant.objects.create(name="Stadt Gesperrt", slug="gesperrt", is_active=False)
        offen = SessionTenant.objects.create(
            name="Stadt Offen", slug="offen", is_active=False, oparl_public_since=timezone.now()
        )
        betrieb = Client()
        betrieb.force_login(cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True))

        antwort = betrieb.post(
            "/admin/session/sessiontenant/",
            {"action": "activate_tenants", "_selected_action": [str(gesperrt.pk), str(offen.pk)]},
            follow=True,
        )

        text = antwort.content.decode()
        assert "2 Mandant(en) wurden aktiviert." in text
        assert "OParl-Schnittstelle nicht freigeschaltet" in text and "Stadt Gesperrt" in text
        assert "Stadt Offen." not in text

    def test_zuruecknehmen_prueft_den_gespeicherten_stand(self) -> None:
        # Zwei gleichzeitige Anfragen: Die Rücknahme arbeitet mit einem veralteten Stand, während eine
        # andere Anfrage inzwischen im Bürgerportal veröffentlicht hat – sie liest unter Sperre neu
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test", oparl_public_since=timezone.now())
        veraltet = SessionTenant.objects.get(pk=tenant.pk)
        portal_publication.resume_publication(tenant)

        with pytest.raises(oparl_access.OParlAccessError):
            oparl_access.lock(veraltet)
        tenant.refresh_from_db()
        assert tenant.oparl_public and tenant.insight_publish

    def test_veroeffentlichen_prueft_den_gespeicherten_stand(self) -> None:
        # Umgekehrt: Die Freischaltung wurde inzwischen zurückgenommen – veröffentlichen scheitert
        tenant = SessionTenant.objects.create(name="Stadt Test", slug="test", oparl_public_since=timezone.now())
        veraltet = SessionTenant.objects.get(pk=tenant.pk)
        assert oparl_access.lock(tenant) is True

        with pytest.raises(oparl_access.OParlAccessError):
            portal_publication.resume_publication(veraltet)
        tenant.refresh_from_db()
        assert not tenant.oparl_public and not tenant.insight_publish
        assert _quellen(tenant) == []


# =============================================================================
# Kontaktdaten nur mit Einwilligung
# =============================================================================


def _person(tenant: SessionTenant, name: str, **felder: Any) -> SessionPerson:
    return SessionPerson.objects.create(
        tenant=tenant, given_name="P", family_name=name, email=f"{name.lower()}@example.org", **felder
    )


class TestKontaktdaten:
    @pytest.fixture
    def tenant(self) -> SessionTenant:
        return SessionTenant.objects.create(name="Stadt Test", slug="test", oparl_public_since=timezone.now())

    def test_email_nur_mit_einwilligung(self, client: Client, tenant: SessionTenant) -> None:
        ohne = _person(tenant, "Ohne")
        mit = _person(
            tenant,
            "Mit",
            contact_publish=True,
            contact_consent_date=date(2026, 9, 1),
            contact_consent_evidence="Schriftliche Erklärung",
        )
        basis = OPARL.format(slug=tenant.slug)

        assert "email" not in client.get(f"{basis}person/{ohne.pk}/").json()
        assert client.get(f"{basis}person/{mit.pk}/").json()["email"] == ["mit@example.org"]
        liste = {p["familyName"]: p for p in client.get(f"{basis}people/").json()["data"]}
        assert "email" not in liste["Ohne"] and liste["Mit"]["email"] == ["mit@example.org"]
        # Datum und Nachweis bleiben intern
        antwort = client.get(f"{basis}people/").content
        assert b"ohne@example.org" not in antwort and b"Schriftliche" not in antwort and b"2026-09-01" not in antwort

    def test_widerruf_aendert_zeitstempel_fuer_abnehmer(self, client: Client, tenant: SessionTenant) -> None:
        person = _person(
            tenant, "Rat", contact_publish=True, contact_consent_date=date(2026, 9, 1), contact_consent_evidence="X"
        )
        vorher = person.updated_at
        person.contact_publish = False
        person.save()

        seit = (vorher + timedelta(microseconds=1)).isoformat()
        antwort = client.get(f"{OPARL.format(slug=tenant.slug)}people/", {"modified_since": seit})
        daten = antwort.json()["data"]
        assert [p["familyName"] for p in daten] == ["Rat"] and "email" not in daten[0]

    def test_formular_verlangt_datum_und_nachweis(self, tenant: SessionTenant) -> None:
        person = _person(tenant, "Beirat")
        admin = angemeldet(nutzer(tenant, "verwaltung", "manage_organizations"))
        url = f"/session/{tenant.slug}/persons/{person.pk}/edit/"
        daten = {"given_name": "P", "family_name": "Beirat", "email": "beirat@example.org", "is_active": "on"}

        antwort = admin.post(url, {**daten, "contact_publish": "on"})
        assert antwort.status_code == 200
        text = antwort.content.decode()
        assert "Bitte das Datum der Einwilligung angeben." in text
        assert "Bitte angeben, wo die Einwilligung nachgewiesen ist." in text
        person.refresh_from_db()
        assert person.contact_publish is False

        morgen = (timezone.localdate() + timedelta(days=1)).isoformat()
        antwort = admin.post(
            url,
            {**daten, "contact_publish": "on", "contact_consent_date": morgen, "contact_consent_evidence": "Akte"},
        )
        assert "Die Einwilligung kann nicht in der Zukunft liegen." in antwort.content.decode()

        antwort = admin.post(
            url,
            {
                **daten,
                "contact_publish": "on",
                "contact_consent_date": "2026-09-01",
                "contact_consent_evidence": " Schriftliche Erklärung ",
            },
        )
        assert antwort.status_code == 302
        person.refresh_from_db()
        assert person.contact_publish is True
        assert person.contact_consent_date == date(2026, 9, 1)
        assert person.contact_consent_evidence == "Schriftliche Erklärung"
        detail = admin.get(f"/session/{tenant.slug}/persons/{person.pk}/").content.decode()
        assert "Einwilligung vom 01.09.2026" in detail

        # Widerruf: Kennzeichen weg, Datum und Nachweis ebenso
        antwort = admin.post(url, daten)
        assert antwort.status_code == 302
        person.refresh_from_db()
        assert (person.contact_publish, person.contact_consent_date, person.contact_consent_evidence) == (
            False,
            None,
            "",
        )

    def test_auskunft_nennt_einwilligung(self, tenant: SessionTenant) -> None:
        mit = _person(
            tenant,
            "Mit",
            contact_publish=True,
            contact_consent_date=date(2026, 9, 1),
            contact_consent_evidence="Schriftliche Erklärung",
        )
        stamm = privacy_service.subject_access_export(tenant, mit)["stammdaten"]
        assert stamm["kontaktdaten_veroeffentlichen"] is True
        assert stamm["einwilligung_vom"] == "2026-09-01"
        assert stamm["einwilligung_nachweis"] == "Schriftliche Erklärung"

        ohne = privacy_service.subject_access_export(tenant, _person(tenant, "Ohne"))["stammdaten"]
        assert (ohne["kontaktdaten_veroeffentlichen"], ohne["einwilligung_vom"], ohne["einwilligung_nachweis"]) == (
            False,
            None,
            "",
        )

    def test_anonymisierung_leert_einwilligung(self, tenant: SessionTenant) -> None:
        person = _person(
            tenant,
            "Alt",
            is_active=False,
            end_date=date(2020, 1, 31),
            contact_publish=True,
            contact_consent_date=date(2019, 5, 1),
            contact_consent_evidence="Schriftliche Erklärung",
        )
        tenant.settings = {"privacy": {"persons_years": 2}}
        tenant.save()

        assert privacy_service.run_privacy_purge(tenant, dry_run=True)["persons_anonymized"] == 1
        vorher = timezone.now()
        assert privacy_service.run_privacy_purge(tenant)["persons_anonymized"] == 1

        person.refresh_from_db()
        assert (person.email, person.contact_publish, person.contact_consent_date, person.contact_consent_evidence) == (
            "",
            False,
            None,
            "",
        )
        eintraege = list(
            SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionPerson", created_at__gte=vorher)
        )
        (eintrag,) = [e for e in eintraege if "dsgvo_anonymisiert" in e.changes]
        assert eintrag.changes["dsgvo_anonymisiert"] == ["E-Mail", "Einwilligung zur Veröffentlichung"]
        # Die gelöschten Werte selbst landen nie im Audit-Log (auch nicht im automatischen Änderungsdiff)
        for e in eintraege:
            assert "Schriftliche" not in str(e.changes) and "alt@example.org" not in str(e.changes)
        # Abnehmer der Schnittstelle (modified_since) erfahren von der Änderung
        assert person.updated_at > vorher

    def test_ohne_email_nichts_zu_veroeffentlichen(self, tenant: SessionTenant) -> None:
        person = SessionPerson.objects.create(tenant=tenant, given_name="P", family_name="Leer")
        admin = angemeldet(nutzer(tenant, "verwaltung", "manage_organizations"))
        antwort = admin.post(
            f"/session/{tenant.slug}/persons/{person.pk}/edit/",
            {
                "given_name": "P",
                "family_name": "Leer",
                "is_active": "on",
                "contact_publish": "on",
                "contact_consent_date": "2026-09-01",
                "contact_consent_evidence": "Akte",
            },
        )
        assert "Ohne E-Mail-Adresse gibt es nichts zu veröffentlichen." in antwort.content.decode()


# =============================================================================
# Datenmigration: Bestand bleibt öffentlich, Ratsmitglieder übernommen
# =============================================================================

VORHER = ("session", "0045_landesprofile_laden")
NACHHER = ("session", "0047_oparl_bestand_uebernehmen")


@pytest.mark.django_db(transaction=True)
def test_migration_uebernimmt_bestand() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        Tenant = alt.get_model("session", "SessionTenant")
        Organization = alt.get_model("session", "SessionOrganization")
        Person = alt.get_model("session", "SessionPerson")
        Membership = alt.get_model("session", "SessionOrganizationMembership")

        aktiv = Tenant.objects.create(name="Aktiv", slug="aktiv")
        portal = Tenant.objects.create(name="Portal", slug="portal", is_active=False, insight_publish=True)
        ruhend = Tenant.objects.create(name="Ruhend", slug="ruhend", is_active=False)
        rat = Organization.objects.create(tenant=aktiv, name="Rat", organization_type="council")
        ausschuss = Organization.objects.create(tenant=aktiv, name="Bauausschuss", organization_type="committee")
        gestern = timezone.localdate() - timedelta(days=1)

        def person(name: str, email: str = "", **felder: Any) -> Any:
            return Person.objects.create(tenant=aktiv, given_name="P", family_name=name, email=email, **felder)

        ratsmitglied = person("Ratsmitglied", "rm@example.org")
        vorsitz = person("Vorsitz", "vs@example.org")
        sachkundig = person("Sachkundig", "sk@example.org")
        ehemalig = person("Ehemalig", "eh@example.org")
        inaktiv = person("Inaktiv", "in@example.org", is_active=False)
        nur_ausschuss = person("Ausschuss", "au@example.org")
        ohne_email = person("OhneEmail")
        Membership.objects.create(organization=rat, person=ratsmitglied, role="member")
        Membership.objects.create(organization=rat, person=vorsitz, role="chair")
        Membership.objects.create(organization=rat, person=sachkundig, role="expert_citizen")
        Membership.objects.create(organization=rat, person=ehemalig, role="member", end_date=gestern)
        Membership.objects.create(organization=rat, person=inaktiv, role="member")
        Membership.objects.create(organization=ausschuss, person=nur_ausschuss, role="member")
        Membership.objects.create(organization=rat, person=ohne_email, role="member")
        vorher_geaendert = {p.pk: p.updated_at for p in Person.objects.all()}

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        neu = MigrationExecutor(connection).loader.project_state([NACHHER]).apps
        Tenant = neu.get_model("session", "SessionTenant")
        Person = neu.get_model("session", "SessionPerson")

        freigeschaltet = {t.slug: t.oparl_public_since for t in Tenant.objects.all()}
        assert freigeschaltet["aktiv"] == Tenant.objects.get(slug="aktiv").created_at
        assert freigeschaltet["portal"] is not None
        assert freigeschaltet["ruhend"] is None
        assert {aktiv.slug, portal.slug, ruhend.slug} == set(freigeschaltet)

        personen = {p.family_name: p for p in Person.objects.all()}
        assert {name for name, p in personen.items() if p.contact_publish} == {"Ratsmitglied", "Vorsitz"}
        uebernommen = personen["Ratsmitglied"]
        assert uebernommen.contact_consent_date == timezone.localdate()
        assert uebernommen.contact_consent_evidence.startswith("Übernahme aus dem Bestand")
        # Wessen Adresse entfällt, der erscheint in modified_since-Abfragen erneut
        for name in ("Sachkundig", "Ehemalig", "Inaktiv", "Ausschuss"):
            assert personen[name].updated_at > vorher_geaendert[personen[name].pk], name
        assert personen["OhneEmail"].updated_at == vorher_geaendert[personen["OhneEmail"].pk]
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
