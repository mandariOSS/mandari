# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Körperschaften im Mandanten (Issue #756): Datenmodell-Kern.

- Jeder Mandant hat genau eine Standardkörperschaft – neue ab der Anlage, Bestandsmandanten per Migration.
- Gremien und Vorlagen gehören zu einer Körperschaft; ohne Angabe zur Standardkörperschaft, Vorlagen zur
  Körperschaft ihres federführenden Gremiums. Nachzügler eines älteren Images ordnet jeder migrate-Lauf zu.
- Nummernkreise gelten für alle Körperschaften oder eine; {koerperschaft} zählt je Körperschaft.
- Gemeinsame Sitzungen bleiben innerhalb einer Körperschaft.
- OParl-Ausgabe und Kennungen ändern sich durch die Zuordnung nicht.
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any, cast

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAuditLog,
    SessionBody,
    SessionMeeting,
    SessionNumberRange,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)
from apps.session.services import body_service, numbering_service
from apps.session.services.joint_meeting_service import JointOrganizationError
from apps.session.services.numbering_service import NumberingError

pytestmark = pytest.mark.django_db
JAHR = timezone.localdate().year


def _samtgemeinde(slug: str = "sg-muster") -> tuple[SessionTenant, SessionBody, SessionBody]:
    """Mandant mit Samtgemeinde (Standard) und einer Mitgliedsgemeinde."""
    tenant = SessionTenant.objects.create(
        name="Samtgemeinde Muster", slug=slug, short_name="SG", body_type="samtgemeinde"
    )
    samtgemeinde = body_service.default_body(tenant)
    mitglied = SessionBody.objects.create(
        tenant=tenant, name="Gemeinde Musterdorf", short_name="MD", body_type="mitgliedsgemeinde", parent=samtgemeinde
    )
    return tenant, samtgemeinde, mitglied


# =============================================================================
# Standardkörperschaft
# =============================================================================


class TestStandardkoerperschaft:
    def test_neuer_mandant_hat_genau_eine_aus_seinen_angaben(self) -> None:
        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", short_name="MS", body_type="stadt", ags="05515000"
        )
        bodies = list(SessionBody.objects.filter(tenant=tenant))
        assert len(bodies) == 1
        body = bodies[0]
        assert body.is_default and body.is_active
        assert (body.name, body.short_name, body.slug, body.body_type, body.ags) == (
            "Stadt Musterstadt",
            "MS",
            "musterstadt",
            "stadt",
            "05515000",
        )
        assert body_service.default_body(tenant) == body

    def test_anlage_ohne_eintrag_im_protokoll(self) -> None:
        # Teil der Mandantenanlage: Die Hash-Kette des Mandanten beginnt wie bisher (Archivierung alter Einträge)
        tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
        SessionOrganization.objects.create(tenant=tenant, name="Rat")
        assert not SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionBody").exists()

    def test_fehlt_sie_entsteht_sie_bei_bedarf(self) -> None:
        tenant = SessionTenant.objects.create(name="Alt", slug="alt")
        SessionBody.objects.filter(tenant=tenant).delete()
        gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        assert gremium.body is not None and gremium.body.is_default
        assert SessionBody.objects.filter(tenant=tenant).count() == 1

    def test_hoechstens_eine_je_mandant(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        mitglied.is_default = True
        with pytest.raises(IntegrityError), transaction.atomic():
            mitglied.save()

    def test_kurzkennung_eindeutig_je_mandant(self) -> None:
        tenant, _, _ = _samtgemeinde()
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionBody.objects.create(tenant=tenant, name="Doppelt", slug="md")
        # In einem anderen Mandanten ist dieselbe Kennung frei
        andere = SessionTenant.objects.create(name="Andere", slug="andere")
        SessionBody.objects.create(tenant=andere, name="Gemeinde Musterdorf", slug="md")

    def test_standard_wechseln_mit_protokoll(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        body_service.set_default(mitglied)
        samtgemeinde.refresh_from_db()
        mitglied.refresh_from_db()
        assert mitglied.is_default and not samtgemeinde.is_default
        assert body_service.default_body(tenant) == mitglied
        eintraege = SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionBody", action="update")
        assert eintraege.count() == 2

    def test_geleerte_kurzkennung_bleibt_dieselbe(self) -> None:
        # Bestehende Filteradressen (?koerperschaft=md) bleiben gültig
        tenant, _, mitglied = _samtgemeinde()
        assert mitglied.slug == "md"
        mitglied.slug = ""
        mitglied.save()
        mitglied.refresh_from_db()
        assert mitglied.slug == "md"
        # Eine neue Körperschaft mit demselben Kurznamen weicht weiterhin aus
        assert SessionBody.objects.create(tenant=tenant, name="Gemeinde Musterdorf-Neu", short_name="MD").slug == (
            "md-2"
        )

    def test_anlegen_steht_im_protokoll(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        assert SessionAuditLog.objects.filter(
            tenant=tenant, model_name="SessionBody", object_id=str(mitglied.pk), action="create"
        ).exists()

    def test_standardkoerperschaft_laesst_sich_nicht_deaktivieren(self) -> None:
        from django.core.exceptions import ValidationError

        tenant, samtgemeinde, _ = _samtgemeinde()
        samtgemeinde.is_active = False
        with pytest.raises(ValidationError):
            samtgemeinde.full_clean()

    def test_uebergeordnete_aus_fremdem_mandanten_unzulaessig(self) -> None:
        from django.core.exceptions import ValidationError

        _, samtgemeinde, _ = _samtgemeinde()
        fremd = SessionTenant.objects.create(name="Fremd", slug="fremd")
        body = SessionBody(tenant=fremd, name="Ortsteil", slug="ortsteil", parent=samtgemeinde)
        with pytest.raises(ValidationError, match="selben Mandanten"):
            body.full_clean()

    def test_koerperschaft_mit_gremien_nicht_loeschbar_mandant_schon(self) -> None:
        from django.db.models import RestrictedError

        tenant, _, mitglied = _samtgemeinde()
        SessionOrganization.objects.create(tenant=tenant, name="Rat Musterdorf", body=mitglied)
        with pytest.raises(RestrictedError):
            mitglied.delete()
        tenant.delete()
        assert not SessionBody.objects.filter(pk=mitglied.pk).exists()

    def test_geltungsbereich_koerperschaft_wirkt_bis_759_wie_mandant(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        user = cast(Any, UserFactory)(email="sachbearbeitung@example.org")
        session_user = tenant.users.create(user=user)
        assert set(body_service.bodies_in_scope(session_user)) == {samtgemeinde, mitglied}

    def test_mehrere_nur_mit_zweiter_aktiver(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        assert body_service.has_multiple(tenant)
        mitglied.is_active = False
        mitglied.save()
        assert not body_service.has_multiple(tenant)


# =============================================================================
# Gremien und Vorlagen
# =============================================================================


class TestZuordnung:
    def test_gremium_und_vorlage_ohne_angabe_zur_standardkoerperschaft(self) -> None:
        tenant, samtgemeinde, _ = _samtgemeinde()
        gremium = SessionOrganization.objects.create(tenant=tenant, name="Samtgemeinderat")
        vorlage = SessionPaper.objects.create(tenant=tenant, name="Haushalt")
        assert gremium.body == samtgemeinde
        assert vorlage.body == samtgemeinde

    def test_vorlage_folgt_dem_federfuehrenden_gremium(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat Musterdorf", body=mitglied)
        vorlage = SessionPaper.objects.create(tenant=tenant, name="Spielplatz", main_organization=rat)
        assert vorlage.body == mitglied

    def test_gewaehlte_koerperschaft_bleibt(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        rat = SessionOrganization.objects.create(tenant=tenant, name="Samtgemeinderat")
        vorlage = SessionPaper.objects.create(tenant=tenant, name="Anhörung", main_organization=rat, body=mitglied)
        assert rat.body == samtgemeinde
        assert vorlage.body == mitglied

    def test_speichern_mit_update_fields_ergaenzt_die_koerperschaft(self) -> None:
        tenant, samtgemeinde, _ = _samtgemeinde()
        gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        SessionOrganization.objects.filter(pk=gremium.pk).update(body=None)
        gremium.refresh_from_db()
        gremium.name = "Samtgemeinderat"
        gremium.save(update_fields=["name"])
        gremium.refresh_from_db()
        assert gremium.body == samtgemeinde

    def test_gremium_einer_fremden_koerperschaft_unzulaessig(self) -> None:
        from django.core.exceptions import ValidationError

        _, _, mitglied = _samtgemeinde()
        fremd = SessionTenant.objects.create(name="Fremd", slug="fremd")
        gremium = SessionOrganization(tenant=fremd, name="Rat", body=mitglied)
        with pytest.raises(ValidationError, match="Mandanten des Gremiums"):
            gremium.full_clean()

    def test_filter_zaehlt_leere_zur_standardkoerperschaft(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        sg_rat = SessionOrganization.objects.create(tenant=tenant, name="Samtgemeinderat")
        md_rat = SessionOrganization.objects.create(tenant=tenant, name="Rat Musterdorf", body=mitglied)
        nachzuegler = SessionOrganization.objects.create(tenant=tenant, name="Ausschuss")
        SessionOrganization.objects.filter(pk=nachzuegler.pk).update(body=None)
        gremien = SessionOrganization.objects.filter(tenant=tenant)
        assert set(gremien.filter(body_service.body_q(samtgemeinde))) == {sg_rat, nachzuegler}
        assert set(gremien.filter(body_service.body_q(mitglied))) == {md_rat}
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, name="Sitzung", organization=md_rat, start=timezone.now()
        )
        assert list(SessionMeeting.objects.filter(body_service.body_q(mitglied, "organization__"))) == [sitzung]


class TestNachzuegler:
    def test_migrate_ordnet_zu_ohne_aenderungszeitpunkt(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        md_rat = SessionOrganization.objects.create(tenant=tenant, name="Rat Musterdorf", body=mitglied)
        ausschuss = SessionOrganization.objects.create(tenant=tenant, name="Ausschuss")
        md_vorlage = SessionPaper.objects.create(tenant=tenant, name="Spielplatz", main_organization=md_rat)
        freie_vorlage = SessionPaper.objects.create(tenant=tenant, name="Mitteilung")
        # Wie von einem älteren Image angelegt: ohne Körperschaft
        SessionOrganization.objects.filter(pk=ausschuss.pk).update(body=None)
        SessionPaper.objects.filter(pk__in=[md_vorlage.pk, freie_vorlage.pk]).update(body=None)
        ohne = SessionTenant.objects.create(name="Ohne", slug="ohne")
        SessionBody.objects.filter(tenant=ohne).delete()
        vorher = {p.pk: p.updated_at for p in SessionPaper.objects.all()}

        counts = body_service.assign_missing()

        assert counts == {"bodies": 1, "organizations": 1, "papers": 2}
        assert SessionOrganization.objects.get(pk=ausschuss.pk).body == samtgemeinde
        assert SessionPaper.objects.get(pk=md_vorlage.pk).body == mitglied
        assert SessionPaper.objects.get(pk=freie_vorlage.pk).body == samtgemeinde
        assert SessionBody.objects.get(tenant=ohne).is_default
        assert {p.pk: p.updated_at for p in SessionPaper.objects.all()} == vorher
        # Zweiter Lauf findet nichts mehr
        assert body_service.assign_missing() == {"bodies": 0, "organizations": 0, "papers": 0}

    def test_ohne_modell_im_migrationsstand_geschieht_nichts(self) -> None:
        class OhneKoerperschaft:
            def get_model(self, app_label: str, model_name: str) -> Any:
                raise LookupError(model_name)

        assert body_service.assign_missing(OhneKoerperschaft()) == {}

    def test_post_migrate_ist_verbunden(self) -> None:
        from django.db.models.signals import post_migrate

        assert any("session_bodies_post_migrate" in str(eintrag[0]) for eintrag in post_migrate.receivers)


# =============================================================================
# Nummernkreise je Körperschaft
# =============================================================================


class TestNummernkreise:
    def test_kreis_der_koerperschaft_vor_dem_allgemeinen(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        SessionNumberRange.objects.create(tenant=tenant, body=mitglied, name="Musterdorf", pattern="MD/{jahr}/{lfd:3}")
        md_rat = SessionOrganization.objects.create(tenant=tenant, name="Rat Musterdorf", body=mitglied)
        sg = SessionPaper.objects.create(tenant=tenant, name="Haushalt")
        md = SessionPaper.objects.create(tenant=tenant, name="Spielplatz", main_organization=md_rat)
        assert sg.reference == f"V/{JAHR}/0001"
        assert md.reference == f"MD/{JAHR}/001"

    def test_kreis_fremder_koerperschaft_gilt_nie(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        numbering_service.apply_preset(tenant, "standard")
        SessionNumberRange.objects.filter(tenant=tenant, body__isnull=True).update(is_active=False)
        SessionNumberRange.objects.create(tenant=tenant, body=mitglied, name="Musterdorf", pattern="MD/{jahr}/{lfd}")
        assert numbering_service.range_for(tenant, "proposal", samtgemeinde) is None
        vorlage = SessionPaper.objects.create(tenant=tenant, name="Haushalt", body=samtgemeinde)
        assert vorlage.reference == ""

    def test_vorrang_koerperschaft_vor_vorlagenart(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        antraege = SessionNumberRange.objects.create(
            tenant=tenant, name="Anträge", pattern="AN/{jahr}/{lfd}", paper_types=["motion"]
        )
        md_alle = SessionNumberRange.objects.create(tenant=tenant, body=mitglied, name="MD", pattern="MD/{jahr}/{lfd}")
        md_antraege = SessionNumberRange.objects.create(
            tenant=tenant, body=mitglied, name="MD Anträge", pattern="MD-A/{jahr}/{lfd}", paper_types=["motion"]
        )
        assert numbering_service.range_for(tenant, "motion", samtgemeinde) == antraege
        assert numbering_service.range_for(tenant, "motion", mitglied) == md_antraege
        assert numbering_service.range_for(tenant, "proposal", mitglied) == md_alle

    def test_platzhalter_koerperschaft_zaehlt_je_koerperschaft(self) -> None:
        tenant, samtgemeinde, mitglied = _samtgemeinde()
        SessionNumberRange.objects.filter(tenant=tenant).update(pattern="{koerperschaft}/{jahr}/{lfd:3}")
        refs = [
            SessionPaper.objects.create(tenant=tenant, name=f"V{i}", body=body).reference
            for i, body in enumerate([samtgemeinde, mitglied, samtgemeinde, mitglied, mitglied])
        ]
        assert refs == [
            f"SG/{JAHR}/001",
            f"MD/{JAHR}/001",
            f"SG/{JAHR}/002",
            f"MD/{JAHR}/002",
            f"MD/{JAHR}/003",
        ]
        kreis = SessionNumberRange.objects.get(tenant=tenant)
        assert sorted(kreis.counters.values_list("scope", "value")) == [
            (f"{JAHR}|ks:MD", 3),
            (f"{JAHR}|ks:SG", 2),
        ]

    def test_platzhalter_ohne_kurzname_klarer_fehler_vorschau_mit_platzhalter(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        SessionBody.objects.filter(pk=mitglied.pk).update(short_name="")
        kreis = SessionNumberRange.objects.get(tenant=tenant)
        kreis.pattern = "{koerperschaft}/{jahr}/{lfd}"
        kreis.save()
        mitglied.refresh_from_db()
        with pytest.raises(NumberingError, match="Kurznamen"):
            SessionPaper.objects.create(tenant=tenant, name="Spielplatz", body=mitglied)
        assert numbering_service.preview(kreis) == f"KS/{JAHR}/1"
        assert numbering_service.validate_pattern("{koerperschaft}/{jahr}/{lfd}", "yearly") == []

    def test_startwert_nur_fuer_kreis_einer_koerperschaft(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        allgemein = SessionNumberRange.objects.get(tenant=tenant)
        allgemein.pattern = "{koerperschaft}/{jahr}/{lfd}"
        allgemein.save()
        with pytest.raises(NumberingError, match="alle Körperschaften"):
            numbering_service.set_next_number(allgemein, 50)
        eigener = SessionNumberRange.objects.create(
            tenant=tenant, body=mitglied, name="MD", pattern="{koerperschaft}-{jahr}-{lfd}"
        )
        numbering_service.set_next_number(eigener, 50)
        assert SessionPaper.objects.create(tenant=tenant, name="Spielplatz", body=mitglied).reference == (
            f"MD-{JAHR}-50"
        )

    def test_preset_laesst_kreise_einzelner_koerperschaften_unberuehrt(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        eigener = SessionNumberRange.objects.create(tenant=tenant, body=mitglied, name="MD", pattern="MD/{jahr}/{lfd}")
        numbering_service.apply_preset(tenant, "stadtstaat_bezirk")
        eigener.refresh_from_db()
        assert eigener.is_active
        assert not SessionNumberRange.objects.filter(tenant=tenant, body__isnull=True, pattern="V/{jahr}/{lfd:4}")[
            0
        ].is_active

    def test_unternummer_im_kreis_der_bezugsvorlage(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        SessionNumberRange.objects.create(
            tenant=tenant, body=mitglied, name="MD", pattern="MD/{jahr}/{lfd}", sub_pattern="{parent}-E{sub}"
        )
        bezug = SessionPaper.objects.create(tenant=tenant, name="Spielplatz", body=mitglied)
        ergaenzung = SessionPaper.objects.create(
            tenant=tenant, name="Ergänzung", parent_paper=bezug, relation_type="supplement", body=mitglied
        )
        assert ergaenzung.reference == f"MD/{JAHR}/1-E1"


# =============================================================================
# Gemeinsame Sitzungen
# =============================================================================


class TestGemeinsameSitzung:
    def test_nur_innerhalb_einer_koerperschaft(self) -> None:
        tenant, _, mitglied = _samtgemeinde()
        sg_rat = SessionOrganization.objects.create(tenant=tenant, name="Samtgemeinderat")
        sg_ausschuss = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
        md_rat = SessionOrganization.objects.create(tenant=tenant, name="Rat Musterdorf", body=mitglied)
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, name="Sitzung", organization=sg_rat, start=timezone.now()
        )
        sitzung.joint_organizations.add(sg_ausschuss)
        with pytest.raises(JointOrganizationError, match="derselben Körperschaft"), transaction.atomic():
            sitzung.joint_organizations.add(md_rat)
        with pytest.raises(JointOrganizationError, match="derselben Körperschaft"), transaction.atomic():
            md_rat.joint_meetings.add(sitzung)
        assert list(sitzung.joint_organizations.all()) == [sg_ausschuss]

    def test_nachzuegler_ohne_koerperschaft_zaehlt_zur_standardkoerperschaft(self) -> None:
        tenant, _, _ = _samtgemeinde()
        sg_rat = SessionOrganization.objects.create(tenant=tenant, name="Samtgemeinderat")
        alt = SessionOrganization.objects.create(tenant=tenant, name="Ausschuss")
        SessionOrganization.objects.filter(pk=alt.pk).update(body=None)
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, name="Sitzung", organization=sg_rat, start=timezone.now()
        )
        sitzung.joint_organizations.add(alt)
        assert list(sitzung.joint_organizations.all()) == [alt]


# =============================================================================
# Bereitstellung und Admin
# =============================================================================


class TestBetrieb:
    def test_bereitstellung_ergaenzt_leere_angaben_der_standardkoerperschaft(self) -> None:
        from apps.session.services import tenant_provisioning

        tenant = SessionTenant.objects.create(name="Gemeinde Neu", slug="gemeinde-neu")
        katalog = tenant_provisioning.load_presets()
        spec = tenant_provisioning.build_spec(
            name="Gemeinde Neu",
            slug="gemeinde-neu",
            admin_email="verwaltung@gemeinde-neu.example.org",
            profile="nrw_stadt",
            catalog=katalog,
            ags="05515000",
        )
        tenant_provisioning.provision_tenant(spec, catalog=katalog, actor="test")
        body = body_service.default_body(tenant)
        assert body.ags == "05515000"
        assert SessionBody.objects.filter(tenant=tenant).count() == 1

    def test_admin_legt_weitere_koerperschaft_an(self) -> None:
        tenant = SessionTenant.objects.create(name="Samtgemeinde Muster", slug="sg-muster")
        standard = body_service.default_body(tenant)
        betrieb = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(betrieb)
        url = reverse("admin:session_sessiontenant_change", args=[tenant.pk])
        seite = client.get(url)
        assert seite.status_code == 200
        assert "Körperschaften (die Verwaltung führt den Sitzungsdienst für jede davon)" in seite.content.decode()
        formular = seite.context["adminform"].form
        daten = {
            key: value
            for key, value in formular.initial.items()
            if value is not None and not isinstance(value, list | dict) and key != "logo"
        }
        daten.update(
            {
                "settings": "{}",
                "reminder_settings": "{}",
                "bodies-TOTAL_FORMS": "2",
                "bodies-INITIAL_FORMS": "1",
                "bodies-MIN_NUM_FORMS": "0",
                "bodies-MAX_NUM_FORMS": "1000",
                "bodies-0-id": str(standard.pk),
                "bodies-0-tenant": str(tenant.pk),
                "bodies-0-name": standard.name,
                "bodies-0-slug": standard.slug,
                "bodies-0-is_active": "on",
                "bodies-1-tenant": str(tenant.pk),
                "bodies-1-name": "Gemeinde Musterdorf",
                "bodies-1-short_name": "MD",
                "bodies-1-slug": "musterdorf",
                "bodies-1-body_type": "mitgliedsgemeinde",
                "bodies-1-parent": str(standard.pk),
                "bodies-1-is_active": "on",
            }
        )
        antwort = client.post(url, daten)
        assert antwort.status_code == 302, antwort.content.decode()[:2000]
        neu = SessionBody.objects.get(tenant=tenant, slug="musterdorf")
        assert (neu.parent, neu.is_default) == (standard, False)


# =============================================================================
# OParl unverändert
# =============================================================================


def test_oparl_ausgabe_und_kennungen_unabhaengig_von_der_zuordnung(client: Client) -> None:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt",
        slug="musterstadt",
        body_type="stadt",
        ags="05515000",
        oparl_public_since=timezone.now(),
    )
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    SessionMeeting.objects.create(tenant=tenant, name="Ratssitzung", organization=rat, start=timezone.now())
    SessionPaper.objects.create(tenant=tenant, name="Haushalt", main_organization=rat, is_public=True)
    basis = f"/session/{tenant.slug}/api/oparl/"
    pfade = [basis, f"{basis}body/", f"{basis}organizations/", f"{basis}meetings/", f"{basis}papers/"]

    def ausgabe() -> list[Any]:
        return [client.get(pfad).json() for pfad in pfade]

    zugeordnet = ausgabe()
    # Stand vor der Migration: keine Körperschaft an Gremien und Vorlagen
    SessionOrganization.objects.filter(tenant=tenant).update(body=None)
    SessionPaper.objects.filter(tenant=tenant).update(body=None)
    assert ausgabe() == zugeordnet
    assert zugeordnet[1]["classification"] == "Stadt"


# =============================================================================
# Standard wechseln unter Nebenläufigkeit (nur PostgreSQL, CI)
# =============================================================================

#: Frist für Warten auf Sperren und Threads (Last in der CI)
FRIST = 60.0


def _wartet_jemand_auf_eine_sperre() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )
        zeile = cursor.fetchone()
    return bool(zeile and zeile[0])


@pytest.mark.django_db(transaction=True)
def test_gleichzeitiger_wechsel_der_standardkoerperschaft_ohne_fehler() -> None:
    """
    Zwei Wechsel auf verschiedene Körperschaften zugleich: Der zweite wartet auf den ersten und setzt danach
    dessen neue Standardkörperschaft zurück – statt nach dem Warten keine mehr zu finden und an der
    Datenbankregel „höchstens eine“ zu scheitern (Serverfehler).
    """
    if connection.vendor != "postgresql":
        pytest.skip("Zeilensperren prüft nur PostgreSQL (CI)")
    tenant, samtgemeinde, mitglied = _samtgemeinde()
    dritte = SessionBody.objects.create(
        tenant=tenant, name="Gemeinde Beispielhausen", short_name="BH", body_type="mitgliedsgemeinde"
    )
    erster_gewechselt = threading.Event()
    freigeben = threading.Event()
    fehler: list[BaseException] = []

    def erster() -> None:
        try:
            with transaction.atomic():
                body_service.set_default(SessionBody.objects.get(pk=mitglied.pk))
                # Sperren halten, bis der zweite Wechsel nachweislich darauf wartet
                erster_gewechselt.set()
                freigeben.wait(FRIST)
        except BaseException as exc:  # noqa: BLE001 – im Hauptthread geprüft
            fehler.append(exc)
        finally:
            connection.close()

    def zweiter() -> None:
        try:
            body_service.set_default(SessionBody.objects.get(pk=dritte.pk))
        except BaseException as exc:  # noqa: BLE001 – im Hauptthread geprüft
            fehler.append(exc)
        finally:
            connection.close()

    t1 = threading.Thread(target=erster, name="erster")
    t2 = threading.Thread(target=zweiter, name="zweiter")
    t1.start()
    try:
        assert erster_gewechselt.wait(FRIST)
        t2.start()
        ende = time.monotonic() + FRIST
        while not _wartet_jemand_auf_eine_sperre() and time.monotonic() < ende:
            time.sleep(0.01)
        assert _wartet_jemand_auf_eine_sperre(), "Der zweite Wechsel wartet nicht auf den ersten."
    finally:
        freigeben.set()
        t1.join(FRIST)
        if t2.ident is not None:
            t2.join(FRIST)

    assert fehler == []
    standard = list(SessionBody.objects.filter(tenant=tenant, is_default=True).values_list("pk", flat=True))
    assert standard == [dritte.pk]
    assert body_service.default_body(tenant).pk == dritte.pk
    assert not SessionBody.objects.get(pk=samtgemeinde.pk).is_default


# =============================================================================
# Datenmigration
# =============================================================================


def _migrationen() -> tuple[tuple[str, str], tuple[str, str]]:
    """
    Vorgängerin der Schemamigration und die Datenmigration, aus dem Migrationsgraphen gelesen: Passt der
    Merge die Nummern an die dev-Spitze an, rollt der Test nur bis zur neuen Vorgängerin zurück.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    namen = sorted(name for app, name in loader.disk_migrations if app == "session")
    schema = next(name for name in namen if name.endswith("_koerperschaften"))
    daten = next(name for name in namen if name.endswith("_koerperschaften_zuordnen"))
    vorher = next(dep for dep in loader.disk_migrations[("session", schema)].dependencies if dep[0] == "session")
    return (vorher[0], vorher[1]), ("session", daten)


@pytest.mark.django_db(transaction=True)
def test_migration_ordnet_den_bestand_zu() -> None:
    ausgang, ziel = _migrationen()
    executor = MigrationExecutor(connection)
    executor.migrate([ausgang])
    try:
        alt = executor.loader.project_state([ausgang]).apps
        Tenant = alt.get_model("session", "SessionTenant")
        Organization = alt.get_model("session", "SessionOrganization")
        Paper = alt.get_model("session", "SessionPaper")

        stadt = Tenant.objects.create(
            name="Stadt Alt", slug="stadt-alt", short_name="SA", body_type="stadt", ags="05515000"
        )
        leer = Tenant.objects.create(name="Bezirk Ohne", slug="bezirk-ohne")
        rat = Organization.objects.create(tenant=stadt, name="Rat", organization_type="council")
        amt = Organization.objects.create(tenant=stadt, name="Bauamt", organization_type="department")
        mit_gremium = Paper.objects.create(tenant=stadt, name="Haushalt", main_organization=rat, reference="V/1")
        ohne_gremium = Paper.objects.create(tenant=stadt, name="Mitteilung", reference="V/2")
        vorher = {p.pk: p.updated_at for p in Paper.objects.all()}

        executor = MigrationExecutor(connection)
        executor.migrate([ziel])
        neu = MigrationExecutor(connection).loader.project_state([ziel]).apps
        Body = neu.get_model("session", "SessionBody")
        Organization = neu.get_model("session", "SessionOrganization")
        Paper = neu.get_model("session", "SessionPaper")

        assert Body.objects.count() == 2
        sb = Body.objects.get(tenant_id=stadt.pk)
        assert (sb.is_default, sb.is_active, sb.name, sb.short_name, sb.slug, sb.body_type, sb.ags) == (
            True,
            True,
            "Stadt Alt",
            "SA",
            "stadt-alt",
            "stadt",
            "05515000",
        )
        ob = Body.objects.get(tenant_id=leer.pk)
        assert (ob.is_default, ob.name, ob.short_name, ob.body_type, ob.ags) == (True, "Bezirk Ohne", "", "", "")
        assert {o.pk: o.body_id for o in Organization.objects.all()} == {rat.pk: sb.pk, amt.pk: sb.pk}
        assert {p.pk: p.body_id for p in Paper.objects.all()} == {mit_gremium.pk: sb.pk, ohne_gremium.pk: sb.pk}
        # Kein Änderungszeitpunkt: OParl-Abnehmer sehen keine Änderung
        assert {p.pk: p.updated_at for p in Paper.objects.all()} == vorher

        # Idempotent: ein zweiter Lauf legt nichts an und ändert nichts
        schritt = importlib.import_module(f"apps.session.migrations.{ziel[1]}")
        schritt.zuordnen(neu, None)
        assert Body.objects.count() == 2
        assert {p.pk: p.body_id for p in Paper.objects.all()} == {mit_gremium.pk: sb.pk, ohne_gremium.pk: sb.pk}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
