# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mandant anlegen (Issue #317, Teil A): Befehl ``session_create_tenant`` und Admin-Assistent über
denselben Service. Ein neuer Mandant ist mit einem Aufruf arbeitsfähig – Rollen (auch Revision und
Datenschutz), Nummernkreis, Wahlperiode, Gremien, erster Administrator, Mandantenschlüssel –, der
Lauf ist idempotent, der Prüflauf speichert nichts, und keine Ausgabe enthält Passwort oder Token.
"""

from __future__ import annotations

import json
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse

from apps.common.tests.factories import UserFactory
from apps.session.middleware import RESERVED_SLUGS
from apps.session.models import (
    SessionAuditLog,
    SessionInvitation,
    SessionLegislativeTerm,
    SessionOrganization,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import numbering_service, tenant_provisioning

pytestmark = pytest.mark.django_db

STANDARDROLLEN = {"Administrator", "Sachbearbeiter", "Protokollant", "Lesezugriff", "Revision", "Datenschutz"}


def _anlegen(*args: str) -> str:
    out = StringIO()
    call_command("session_create_tenant", *args, stdout=out)
    return out.getvalue()


def _hamburg(*extra: str) -> str:
    return _anlegen(
        "--profile",
        "hamburg_bezirk",
        "--name",
        "Bezirksversammlung Musterbezirk",
        "--slug",
        "musterbezirk",
        "--admin-email",
        "Sitzungsdienst@Example.org",
        *extra,
    )


class TestBefehl:
    def test_neuer_mandant_ist_arbeitsfaehig(self) -> None:
        ausgabe = _hamburg("--ags", "02000000")

        tenant = SessionTenant.objects.get(slug="musterbezirk")
        assert tenant.name == "Bezirksversammlung Musterbezirk"
        assert tenant.body_type == "bezirk" and tenant.ags == "02000000"
        assert set(tenant.roles.values_list("name", flat=True)) == STANDARDROLLEN
        # Nummernkreis je Wahlperiode, Wahlperiode mit Nummer, Start und Ende
        kreise = list(tenant.number_ranges.filter(is_active=True))
        assert [k.pattern for k in kreise] == ["{wp}-{lfd:4}"]
        assert tenant.reference_label == "Drucksache"
        term = SessionLegislativeTerm.objects.get(tenant=tenant)
        assert (term.name, term.number) == ("22. Wahlperiode", 22)
        assert term.start_date == date(2024, 6, 9) and term.end_date == date(2029, 6, 30)
        assert numbering_service.preview(kreise[0]) == "22-0001"
        assert "nächste Nummer 22-0001" in ausgabe
        # Gremienvorlage und Schlüssel für verschlüsselte Felder
        assert set(SessionOrganization.objects.filter(tenant=tenant).values_list("name", "short_name")) == {
            ("Bezirksversammlung", "BV"),
            ("Hauptausschuss", "HA"),
        }
        assert tenant.encryption_key
        # Erster Administrator ohne Konto: Einladung mit Administrator-Rolle, Mail geht hinaus
        einladung = SessionInvitation.objects.get(tenant=tenant)
        assert einladung.email == "sitzungsdienst@example.org"
        assert list(einladung.roles.values_list("name", flat=True)) == ["Administrator"]
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["sitzungsdienst@example.org"]
        # Nie Token oder Einladungslink in der Ausgabe
        assert einladung.token not in ausgabe
        assert "/session/invite/" not in ausgabe
        # Protokoll des Mandanten über die Hash-Kette
        eintrag = SessionAuditLog.objects.get(tenant=tenant, model_name="SessionTenant", action="create")
        assert eintrag.changes["durch"] == "Befehl session_create_tenant"
        assert eintrag.entry_hash

    def test_vorhandenes_konto_wird_administrator(self) -> None:
        konto = cast(Any, UserFactory)(email="leitung@example.org")

        ausgabe = _anlegen(
            "--profile",
            "nrw_stadt",
            "--name",
            "Stadt Musterstadt",
            "--slug",
            "musterstadt",
            "--admin-email",
            "LEITUNG@example.org",
        )

        tenant = SessionTenant.objects.get(slug="musterstadt")
        zugang = SessionUser.objects.get(tenant=tenant, user=konto)
        assert zugang.is_admin()
        assert not SessionInvitation.objects.filter(tenant=tenant).exists()
        assert mail.outbox == []
        assert "als Administrator aufgenommen" in ausgabe
        assert SessionAuditLog.objects.filter(tenant=tenant, action="roles_changed").exists()
        # NRW-Profil: Verwaltung und Politik getrennt, Rat/HA/FA
        assert sorted(tenant.number_ranges.filter(is_active=True).values_list("pattern", flat=True)) == [
            "{lfd:4}/{jahr}",
            "{prefix}/{lfd:4}/{jahr}",
        ]
        assert set(tenant.organizations.values_list("name", flat=True)) == {"Rat", "Hauptausschuss", "Finanzausschuss"}
        assert tenant.body_type == "stadt"

    def test_zweiter_lauf_ergaenzt_nur(self) -> None:
        _hamburg()
        tenant = SessionTenant.objects.get(slug="musterbezirk")
        tenant.roles.filter(name="Datenschutz").delete()

        ausgabe = _hamburg("--ags", "02000000")

        assert set(tenant.roles.values_list("name", flat=True)) == STANDARDROLLEN
        assert tenant.roles.count() == len(STANDARDROLLEN)
        assert SessionOrganization.objects.filter(tenant=tenant).count() == 2
        assert SessionLegislativeTerm.objects.filter(tenant=tenant).count() == 1
        assert SessionInvitation.objects.filter(tenant=tenant).count() == 1
        assert len(mail.outbox) == 1, "keine zweite Einladung"
        assert "bestand bereits" in ausgabe and "offene Einladung besteht bereits" in ausgabe
        tenant.refresh_from_db()
        assert tenant.ags == "02000000", "leere Angaben werden ergänzt"

    def test_pruefauf_speichert_nichts(self) -> None:
        ausgabe = _hamburg("--dry-run")

        assert "Prüflauf" in ausgabe and "angelegt" in ausgabe
        assert not SessionTenant.objects.filter(slug="musterbezirk").exists()
        assert not SessionInvitation.objects.exists()
        assert mail.outbox == []

    @pytest.mark.parametrize(
        ("argumente", "meldung"),
        [
            (("--slug", "Muster Bezirk"), "Kleinbuchstaben"),
            (("--slug", "api"), "reserviert"),
            (("--ags", "12345678X"), "Gemeindeschlüssel"),
            (("--admin-email", "keine-adresse"), "E-Mail-Adresse"),
            (("--term-start", "09.06.2024"), "JJJJ-MM-TT"),
            (("--committees", "gibt-es-nicht"), "Gremienvorlage"),
        ],
    )
    def test_ungueltige_angaben(self, argumente: tuple[str, ...], meldung: str) -> None:
        basis = {
            "--profile": "hamburg_bezirk",
            "--name": "Bezirk",
            "--slug": "bezirk",
            "--admin-email": "a@example.org",
        }
        werte = dict(basis)
        eigene = list(argumente)
        for index in range(0, len(eigene), 2):
            werte[eigene[index]] = eigene[index + 1]
        flach = [teil for paar in werte.items() for teil in paar]

        with pytest.raises(CommandError) as fehler:
            _anlegen(*flach)

        assert meldung in str(fehler.value)
        assert not SessionTenant.objects.exists()

    @pytest.mark.parametrize("slug", sorted(RESERVED_SLUGS))
    def test_reservierte_slugs_der_middleware_werden_abgelehnt(self, slug: str) -> None:
        """Dieselbe Liste wie SessionTenantMiddleware – auch „leitstelle“ aus Teil B (#317)."""
        with pytest.raises(CommandError, match="reserviert"):
            _hamburg("--slug", slug)

        assert "leitstelle" in RESERVED_SLUGS
        assert not SessionTenant.objects.filter(slug=slug).exists()

    def test_hamburg_ohne_nummer_der_wahlperiode_wird_abgelehnt(self) -> None:
        with pytest.raises(CommandError, match="Nummer der Wahlperiode"):
            _hamburg("--term-name", "Eigene Periode", "--term-start", "2024-06-09", "--term-end", "2029-06-30")

    def test_eigene_wahlperiode_ohne_gremien(self) -> None:
        _anlegen(
            "--profile",
            "nrw_stadt",
            "--name",
            "Gemeinde Beispiel",
            "--slug",
            "beispiel",
            "--kind",
            "gemeinde",
            "--admin-email",
            "rat@example.org",
            "--term-name",
            "Wahlperiode 2025–2030",
            "--term-start",
            "2025-11-01",
            "--term-end",
            "2030-10-31",
            "--no-committees",
        )

        tenant = SessionTenant.objects.get(slug="beispiel")
        assert tenant.body_type == "gemeinde"
        assert not tenant.organizations.exists()
        assert SessionLegislativeTerm.objects.get(tenant=tenant).number is None

    def test_presets_anzeigen(self) -> None:
        ausgabe = _anlegen("--list-presets")

        assert "hamburg_bezirk" in ausgabe and "nrw_stadt" in ausgabe
        assert "Bezirksversammlung, Hauptausschuss" in ausgabe
        assert "nrw_verwaltung_politik" in ausgabe and "Kreisfreie Stadt" in ausgabe

    def test_eigene_preset_datei(self, tmp_path: Path) -> None:
        datei = tmp_path / "presets.json"
        datei.write_text(
            json.dumps(
                {
                    "profile": {
                        "gemeinde_klein": {
                            "bezeichnung": "Kleine Gemeinde",
                            "koerperschaftstyp": "gemeinde",
                            "nummernkreis": "gemeinde",
                            "wahlperiode": {"name": "WP 2025", "beginn": "2025-11-01", "ende": "2030-10-31"},
                            "gremienvorlage": "rat",
                        }
                    },
                    "gremienvorlagen": {"rat": {"gremien": [{"name": "Gemeinderat", "art": "council"}]}},
                }
            ),
            encoding="utf-8",
        )

        _anlegen(
            "--preset-file",
            str(datei),
            "--profile",
            "gemeinde_klein",
            "--name",
            "Gemeinde Klein",
            "--slug",
            "klein",
            "--admin-email",
            "gemeinde@example.org",
        )

        tenant = SessionTenant.objects.get(slug="klein")
        assert list(tenant.organizations.values_list("name", flat=True)) == ["Gemeinderat"]
        assert tenant.number_ranges.get(is_active=True).pattern == "{lfd}/{jahr}"

    def test_ungueltige_preset_datei(self, tmp_path: Path) -> None:
        datei = tmp_path / "kaputt.json"
        datei.write_text("{kein json", encoding="utf-8")

        with pytest.raises(CommandError, match="kein gültiges JSON"):
            _anlegen("--preset-file", str(datei), "--list-presets")


class TestService:
    def test_bestandsmandant_bekommt_fehlende_kontrollrollen(self) -> None:
        tenant = SessionTenant.objects.create(name="Alt", slug="alt")
        for key in ("admin", "clerk", "recorder", "viewer"):
            SessionRole.objects.create(tenant=tenant, is_system_role=True, **SessionRole.BASE_ROLES[key])

        neu = cast(Any, SessionRole).ensure_default_roles(tenant)

        assert {rolle.name for rolle in neu.values()} == {"Revision", "Datenschutz"}
        assert set(tenant.roles.values_list("name", flat=True)) == STANDARDROLLEN

    def test_oparl_body_nennt_koerperschaftstyp_und_ags(self) -> None:
        tenant = SessionTenant.objects.create(name="Bezirk Ost", slug="ost", body_type="bezirk", ags="02000000")
        ohne = SessionTenant.objects.create(name="Alt", slug="alt-ohne")

        body = Client().get(f"/session/{tenant.slug}/api/oparl/body/").json()
        alt = Client().get(f"/session/{ohne.slug}/api/oparl/body/").json()

        assert body["classification"] == "Bezirk" and body["ags"] == "02000000"
        assert alt["classification"] == "Kommune" and "ags" not in alt

    def test_build_spec_ausdrueckliche_werte_vor_profil(self) -> None:
        katalog = tenant_provisioning.load_presets()

        spec = tenant_provisioning.build_spec(
            name=" Bezirk ",
            slug="bezirk",
            admin_email="A@Example.org",
            catalog=katalog,
            profile="hamburg_bezirk",
            term_end=date(2029, 5, 31),
            committees="",
        )

        assert spec.name == "Bezirk" and spec.admin_email == "a@example.org"
        assert spec.term_name == "22. Wahlperiode" and spec.term_number == 22
        assert spec.term_end == date(2029, 5, 31)
        assert spec.committees == ""
        assert tenant_provisioning.validate_spec(spec, katalog) == []


class TestAdminAssistent:
    URL = "/admin/session/sessiontenant/anlegen/"

    @pytest.fixture
    def superuser(self) -> Client:
        user = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(user)
        return client

    def test_nur_mit_recht_zum_anlegen(self) -> None:
        staff = cast(Any, UserFactory)(email="staff@example.org", is_staff=True)
        client = Client()
        client.force_login(staff)
        assert client.get(self.URL).status_code == 403

        staff.user_permissions.add(Permission.objects.get(codename="add_sessiontenant"))
        assert client.get(self.URL).status_code == 200

        client.force_login(cast(Any, UserFactory)(email="buerger@example.org"))
        assert client.get(self.URL).status_code == 302, "Nicht-Staff landet bei der Anmeldung"

    def test_hinzufuegen_fuehrt_zum_assistenten(self, superuser: Client) -> None:
        antwort = superuser.get(reverse("admin:session_sessiontenant_add"))

        assert antwort.status_code == 302 and antwort["Location"] == self.URL

    def test_profil_belegt_vor(self, superuser: Client) -> None:
        antwort = superuser.get(self.URL, {"profil": "hamburg_bezirk"})

        form = antwort.context["form"]
        assert form.initial["numbering"] == "hamburg_bezirk"
        assert form.initial["term_number"] == 22
        assert form.initial["committees"] == "hamburg_bezirk"
        assert "22. Wahlperiode" in antwort.content.decode()

    def test_legt_mandanten_an(self, superuser: Client) -> None:
        antwort = superuser.post(
            self.URL,
            {
                "name": "Bezirksversammlung Nord",
                "slug": "nord",
                "body_type": "bezirk",
                "numbering": "hamburg_bezirk",
                "term_name": "22. Wahlperiode",
                "term_number": "22",
                "term_start": "2024-06-09",
                "term_end": "2029-06-30",
                "committees": "hamburg_bezirk",
                "admin_email": "nord@example.org",
            },
        )

        assert antwort.status_code == 200
        tenant = SessionTenant.objects.get(slug="nord")
        assert set(tenant.roles.values_list("name", flat=True)) == STANDARDROLLEN
        seite = antwort.content.decode()
        assert "Mandant angelegt" in seite and "Einladung angelegt" in seite
        einladung = SessionInvitation.objects.get(tenant=tenant)
        assert einladung.token not in seite
        eintrag = SessionAuditLog.objects.get(tenant=tenant, action="create", model_name="SessionTenant")
        assert eintrag.changes["durch"] == "Django-Admin (betrieb@example.org)"

    def test_pruefauf_und_fehler_aus_bekannten_meldungen(self, superuser: Client) -> None:
        daten = {
            "name": "Bezirk",
            "slug": "bezirk",
            "numbering": "hamburg_bezirk",
            "term_name": "Eigene",
            "term_start": "2024-06-09",
            "term_end": "2029-06-30",
            "admin_email": "b@example.org",
            "dry_run": "on",
        }

        fehlerseite = superuser.post(self.URL, daten)
        assert "Nummer der Wahlperiode" in fehlerseite.content.decode()

        pruefung = superuser.post(self.URL, {**daten, "term_number": "22"})
        assert "Prüflauf" in pruefung.content.decode()
        assert not SessionTenant.objects.filter(slug="bezirk").exists()
        assert mail.outbox == []
