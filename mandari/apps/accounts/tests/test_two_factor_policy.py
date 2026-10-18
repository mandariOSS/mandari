# SPDX-License-Identifier: AGPL-3.0-or-later
"""
2FA-Pflicht: Richtlinie, Pflicht-Einrichtung beim Login, Middleware für
angemeldete Sitzungen, Netzbeschränkung des Django-Admins und die Schalter
für Organisationen und Session-Mandanten.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import TwoFactorDevice, User
from apps.accounts.services import TwoFactorService
from apps.accounts.two_factor_policy import (
    WORK_VERWALTUNGSRECHTE,
    _parse_networks,
    ip_in_networks,
    two_factor_reasons,
    two_factor_recommended,
    two_factor_required,
)
from apps.accounts.views import ENROLL_SETUP_SESSION_KEY, PENDING_2FA_SESSION_KEY
from apps.common.permissions import DEFAULT_ROLES
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, PermissionFactory, RoleFactory
from apps.session.models import SessionRole, SessionTenant, SessionUser
from apps.tenants.models import Membership, Organization, Permission

pytestmark = pytest.mark.django_db

PASSWORD = "Sicheres-Passwort-2026!"


@pytest.fixture(autouse=True)
def _enforced(settings: Any) -> Iterator[None]:
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = ["demo.mandari.de"]
    settings.ADMIN_ALLOWED_NETWORKS = []
    cache.clear()
    yield
    cache.clear()


def make_user(email: str = "nutzer@example.org", **extra: Any) -> User:
    return cast(User, User.objects.create_user(email=email, password=PASSWORD, **extra))  # type: ignore[no-untyped-call]


def work_member(
    user: User, *, is_admin: bool = False, role_2fa: bool = False, org_2fa: bool = False, active: bool = True
) -> Organization:
    organization = cast(Organization, OrganizationFactory(require_2fa=org_2fa))  # type: ignore[no-untyped-call]
    role = RoleFactory(organization=organization, is_admin=is_admin, require_2fa=role_2fa)  # type: ignore[no-untyped-call]
    membership = cast(
        Membership,
        MembershipFactory(user=user, organization=organization, roles=[role]),  # type: ignore[no-untyped-call]
    )
    if not active:
        membership.is_active = False
        membership.save(update_fields=["is_active"])
    return organization


def _recht(code: str) -> Permission:
    return cast(Permission, PermissionFactory(codename=code))  # type: ignore[no-untyped-call]


def work_member_mit_rechten(
    user: User,
    *,
    rollenrechte: tuple[str, ...] | list[str] = (),
    einzelrechte: tuple[str, ...] = (),
    verweigert: tuple[str, ...] = (),
    is_admin: bool = False,
) -> Membership:
    """Mitgliedschaft mit einer Rolle (``rollenrechte``) sowie Einzel- und verweigerten Rechten der Mitgliedschaft."""
    organization = cast(Organization, OrganizationFactory())  # type: ignore[no-untyped-call]
    role = RoleFactory(organization=organization, permissions=list(rollenrechte), is_admin=is_admin)  # type: ignore[no-untyped-call]
    membership = cast(
        Membership,
        MembershipFactory(user=user, organization=organization, roles=[role]),  # type: ignore[no-untyped-call]
    )
    membership.individual_permissions.add(*[_recht(code) for code in einzelrechte])
    membership.denied_permissions.add(*[_recht(code) for code in verweigert])
    return membership


def session_member(user: User, *, tenant_2fa: bool = False, **role_flags: bool) -> SessionTenant:
    suffix = uuid.uuid4().hex[:8]
    tenant = SessionTenant.objects.create(name=f"Stadt {suffix}", slug=f"stadt-{suffix}", require_2fa=tenant_2fa)
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle {suffix}", **role_flags)
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    session_user.roles.add(role)
    return tenant


def current_code(secret: str) -> str:
    service = TwoFactorService()
    return service._get_totp_code(secret, int(time.time() // service.TOTP_INTERVAL))


def enable_2fa(user: User) -> None:
    service = TwoFactorService()
    secret = service.setup_2fa(user)["secret"]
    assert service.confirm_2fa(user, current_code(secret))


def is_logged_in(client: Client) -> bool:
    return "_auth_user_id" in client.session


def password_step(client: Client, user: User) -> Any:
    return client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORD})


# ---------------------------------------------------------------------------


class TestPolicy:
    def test_normales_mitglied_ist_frei(self) -> None:
        user = make_user()
        work_member(user)
        session_member(user)
        assert not two_factor_required(user)

    def test_superuser_ist_verpflichtet(self) -> None:
        user = make_user(is_superuser=True, is_staff=True)
        assert two_factor_reasons(user) == ["Plattform-Administration"]

    def test_work_admin_ist_verpflichtet(self) -> None:
        user = make_user()
        organization = work_member(user, is_admin=True)
        assert two_factor_reasons(user) == [f"Organisation {organization.name}"]

    def test_rolle_mit_2fa_ist_verpflichtet(self) -> None:
        user = make_user()
        work_member(user, role_2fa=True)
        assert two_factor_required(user)

    def test_organisation_fuer_alle(self) -> None:
        user = make_user()
        work_member(user, org_2fa=True)
        assert two_factor_required(user)

    def test_inaktive_mitgliedschaft_zaehlt_nicht(self) -> None:
        user = make_user()
        work_member(user, is_admin=True, active=False)
        assert not two_factor_required(user)

    @pytest.mark.parametrize("flag", ["is_admin", "can_manage_users", "can_manage_settings"])
    def test_session_verwaltungsrechte_sind_verpflichtet(self, flag: str) -> None:
        user = make_user()
        session_member(user, **{flag: True})
        assert two_factor_required(user)

    def test_session_mandant_fuer_alle(self) -> None:
        user = make_user()
        tenant = session_member(user, tenant_2fa=True)
        assert two_factor_reasons(user) == [f"Verwaltung {tenant.name}"]

    def test_demo_zugaenge_sind_ausgenommen(self) -> None:
        user = make_user("demo-verwaltung@demo.mandari.de")
        work_member(user, is_admin=True)
        session_member(user, is_admin=True)
        assert not two_factor_required(user)

    def test_plattform_administration_ist_nie_ausgenommen(self) -> None:
        user = make_user("demo-verwaltung@demo.mandari.de", is_superuser=True, is_staff=True)
        assert two_factor_required(user)

    def test_ohne_durchsetzung_keine_pflicht(self, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        user = make_user(is_superuser=True, is_staff=True)
        assert not two_factor_required(user)


class TestWorkVerwaltungsrechte:
    """Pflicht nach wirksamen Rechten: Rollen und Einzelrechte, abzüglich verweigerter Rechte."""

    @pytest.mark.parametrize("recht", WORK_VERWALTUNGSRECHTE)
    def test_recht_ueber_die_rolle(self, recht: str) -> None:
        user = make_user()
        membership = work_member_mit_rechten(user, rollenrechte=("dashboard.view", recht))
        assert two_factor_reasons(user) == [f"Organisation {membership.organization.name}"]

    @pytest.mark.parametrize("recht", WORK_VERWALTUNGSRECHTE)
    def test_recht_als_einzelrecht(self, recht: str) -> None:
        user = make_user()
        work_member_mit_rechten(user, rollenrechte=("dashboard.view",), einzelrechte=(recht,))
        assert two_factor_required(user)

    @pytest.mark.parametrize("recht", WORK_VERWALTUNGSRECHTE)
    def test_verweigertes_recht_zaehlt_nicht(self, recht: str) -> None:
        user = make_user()
        work_member_mit_rechten(
            user, rollenrechte=("dashboard.view", recht), einzelrechte=(recht,), verweigert=(recht,)
        )
        assert not two_factor_required(user)

    def test_administration_bleibt_trotz_verweigerter_rechte_verpflichtet(self) -> None:
        user = make_user()
        work_member_mit_rechten(user, is_admin=True, verweigert=WORK_VERWALTUNGSRECHTE)
        assert two_factor_required(user)

    @pytest.mark.parametrize(
        "rechte",
        [
            ("members.view", "members.view_details"),
            ("guests.invite", "guests.manage"),
            ("organization.view", "organization.audit_log"),
            ("faction.manage", "agenda.approve", "motions.approve"),
        ],
    )
    def test_andere_rechte_bleiben_freiwillig(self, rechte: tuple[str, ...]) -> None:
        user = make_user()
        work_member_mit_rechten(user, rollenrechte=rechte, einzelrechte=rechte)
        assert not two_factor_required(user)

    @pytest.mark.parametrize(
        ("rolle", "pflicht"),
        [
            ("faction_chair", True),
            ("faction_vice_chair", True),
            ("managing_director", True),
            ("faction_member", False),
            ("faction_staff", False),
            ("expert_citizen", False),
            ("party_member", False),
            ("workgroup_speaker", False),
        ],
    )
    def test_vorgaberollen(self, rolle: str, pflicht: bool) -> None:
        user = make_user()
        rechte = cast(list[str], DEFAULT_ROLES[rolle]["permissions"])
        work_member_mit_rechten(user, rollenrechte=rechte)
        assert two_factor_required(user) is pflicht

    def test_inaktive_mitgliedschaft_mit_einzelrecht_zaehlt_nicht(self) -> None:
        user = make_user()
        membership = work_member_mit_rechten(user, einzelrechte=("members.manage_roles",))
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        assert not two_factor_required(user)

    def test_nur_die_organisation_mit_verwaltungsrecht_wird_genannt(self) -> None:
        user = make_user()
        verwaltet = work_member_mit_rechten(user, einzelrechte=("organization.api_tokens",))
        work_member_mit_rechten(user, rollenrechte=("dashboard.view", "members.view"))
        assert two_factor_reasons(user) == [f"Organisation {verwaltet.organization.name}"]

    def test_demo_zugang_mit_einzelrecht_bleibt_ausgenommen(self) -> None:
        user = make_user("demo-vorsitz@demo.mandari.de")
        work_member_mit_rechten(user, einzelrechte=("members.manage_roles",))
        assert not two_factor_required(user)


class TestEmpfehlung:
    def test_ohne_zweiten_faktor_empfohlen(self) -> None:
        user = make_user()
        work_member(user)
        assert two_factor_recommended(user)

    def test_mit_zweitem_faktor_nicht(self) -> None:
        user = make_user()
        enable_2fa(user)
        assert not two_factor_recommended(user)

    def test_demo_zugaenge_nicht(self) -> None:
        assert not two_factor_recommended(make_user("demo-mitglied@demo.mandari.de"))

    def test_ohne_durchsetzung_nicht(self, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        assert not two_factor_recommended(make_user())


class TestLoginEnrollment:
    def test_pflichtkonto_wird_erst_nach_einrichtung_angemeldet(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)

        response = password_step(client, user)
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:two_factor_enroll")
        assert not is_logged_in(client)

        response = client.get(reverse("accounts:two_factor_enroll"))
        assert response.status_code == 200
        secret = client.session[ENROLL_SETUP_SESSION_KEY]["secret"]
        assert secret in response.content.decode()

        response = client.post(reverse("accounts:two_factor_enroll"), {"code": current_code(secret)})
        assert response.status_code == 200
        assert "Backup-Codes" in response.content.decode()
        assert not is_logged_in(client)

        response = client.post(reverse("accounts:two_factor_enroll"), {"action": "finish"})
        assert response.status_code == 302
        assert is_logged_in(client)
        assert PENDING_2FA_SESSION_KEY not in client.session
        assert TwoFactorDevice.objects.get(user=user).is_confirmed

    def test_falscher_code_richtet_nichts_ein(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        password_step(client, user)
        client.get(reverse("accounts:two_factor_enroll"))

        response = client.post(reverse("accounts:two_factor_enroll"), {"code": "000000"})
        assert response.status_code == 200
        assert "ungültig" in response.content.decode()
        assert not is_logged_in(client)
        assert not TwoFactorService().is_2fa_enabled(user)

    def test_abschluss_ohne_bestaetigten_code_meldet_nicht_an(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        password_step(client, user)
        client.get(reverse("accounts:two_factor_enroll"))

        response = client.post(reverse("accounts:two_factor_enroll"), {"action": "finish"})
        assert response.status_code == 302
        assert not is_logged_in(client)

    def test_freiwilliges_konto_meldet_direkt_an(self, client: Client) -> None:
        user = make_user()
        work_member(user)
        response = password_step(client, user)
        assert response.status_code == 302
        assert is_logged_in(client)

    def test_einrichtung_ohne_passwortschritt_nicht_erreichbar(self, client: Client) -> None:
        response = client.get(reverse("accounts:two_factor_enroll"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")

    def test_abgelaufene_einrichtung(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        password_step(client, user)
        session = client.session
        pending = session[PENDING_2FA_SESSION_KEY]
        pending["started"] -= 3600
        session[PENDING_2FA_SESSION_KEY] = pending
        session.save()

        response = client.get(reverse("accounts:two_factor_enroll"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")

    def test_codeschritt_akzeptiert_keine_offene_einrichtung(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        password_step(client, user)
        response = client.post(reverse("accounts:login_2fa"), {"code": "000000"})
        assert response.status_code == 302
        assert not is_logged_in(client)


class TestMiddleware:
    def test_angemeldetes_pflichtkonto_wird_umgeleitet(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        client.force_login(user)
        response = client.get("/work/")
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:two_factor_enroll"))
        assert "next=%2Fwork%2F" in response["Location"]

    def test_htmx_anfrage_erhaelt_hx_redirect(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        client.force_login(user)
        response = client.get("/work/", HTTP_HX_REQUEST="true")
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("accounts:two_factor_enroll")

    def test_konto_mit_2fa_wird_nicht_umgeleitet(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        enable_2fa(user)
        client.force_login(user)
        response = client.get("/work/")
        assert reverse("accounts:two_factor_enroll") not in response.get("Location", "")

    def test_freiwilliges_konto_wird_nicht_umgeleitet(self, client: Client) -> None:
        user = make_user()
        work_member(user)
        client.force_login(user)
        response = client.get("/work/")
        assert reverse("accounts:two_factor_enroll") not in response.get("Location", "")

    def test_abmelden_bleibt_erreichbar(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        client.force_login(user)
        assert client.get(reverse("accounts:logout")).status_code == 200

    def test_einrichtung_fuer_angemeldete_konten(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        client.force_login(user)
        client.get("/work/")

        response = client.get(reverse("accounts:two_factor_enroll") + "?next=/work/")
        assert response.status_code == 200
        secret = client.session[ENROLL_SETUP_SESSION_KEY]["secret"]
        client.post(reverse("accounts:two_factor_enroll"), {"code": current_code(secret), "next": "/work/"})
        response = client.post(reverse("accounts:two_factor_enroll"), {"action": "finish", "next": "/work/"})
        assert response.status_code == 302
        assert response["Location"] == "/work/"

        response = client.get("/work/")
        assert reverse("accounts:two_factor_enroll") not in response.get("Location", "")


class TestEinzelrechtFuehrtZurEinrichtung:
    """Konto mit Einzelrecht „Rollen zuweisen“: Einrichtung vor dem Weiterarbeiten, niemand wird ausgesperrt."""

    def test_anmeldung_fuehrt_zur_einrichtung_und_danach_hinein(self, client: Client) -> None:
        user = make_user()
        work_member_mit_rechten(user, rollenrechte=("dashboard.view",), einzelrechte=("members.manage_roles",))

        response = password_step(client, user)
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:two_factor_enroll")
        assert not is_logged_in(client)

        client.get(reverse("accounts:two_factor_enroll"))
        secret = client.session[ENROLL_SETUP_SESSION_KEY]["secret"]
        client.post(reverse("accounts:two_factor_enroll"), {"code": current_code(secret)})
        response = client.post(reverse("accounts:two_factor_enroll"), {"action": "finish"})
        assert response.status_code == 302
        assert is_logged_in(client)

    def test_angemeldete_sitzung_wird_umgeleitet(self, client: Client) -> None:
        user = make_user()
        membership = work_member_mit_rechten(
            user, rollenrechte=("dashboard.view",), einzelrechte=("members.manage_roles",)
        )
        client.force_login(user)
        response = client.get(reverse("work:dashboard", kwargs={"org_slug": membership.organization.slug}))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:two_factor_enroll"))

    def test_speichern_per_fetch_erhaelt_json_403(self, client: Client) -> None:
        """Speichern per fetch (Vorbereitung) bekommt keine Weiterleitung, sondern 403 mit dem Ziel der Einrichtung.

        Darauf hält frontend/js/speichern.ts die Eingabe fest und bietet die Einrichtung an.
        """
        user = make_user()
        membership = work_member_mit_rechten(
            user, rollenrechte=("dashboard.view",), einzelrechte=("members.manage_roles",)
        )
        client.force_login(user)
        response = client.post(
            reverse("work:dashboard", kwargs={"org_slug": membership.organization.slug}),
            data="{}",
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert response.status_code == 403
        assert response.json() == {
            "error": "two_factor_setup_required",
            "redirect": reverse("accounts:two_factor_enroll"),
        }

    def test_mitglied_ohne_verwaltungsrecht_arbeitet_ohne_einrichtung(self, client: Client) -> None:
        user = make_user()
        membership = work_member_mit_rechten(user, rollenrechte=("dashboard.view", "members.view_details"))
        response = password_step(client, user)
        assert is_logged_in(client)
        assert response["Location"] != reverse("accounts:two_factor_enroll")
        start = client.get(reverse("work:dashboard", kwargs={"org_slug": membership.organization.slug}))
        assert start.status_code == 200


class TestAdminNetworks:
    def test_adressen_und_netze(self) -> None:
        networks = _parse_networks(("10.99.0.0/24", "2001:db8::/32"))
        assert ip_in_networks("10.99.0.3", networks)
        assert ip_in_networks("2001:db8::7", networks)
        assert ip_in_networks("::ffff:10.99.0.3", networks)
        assert not ip_in_networks("10.98.0.3", networks)
        assert not ip_in_networks("kein-ip", networks)

    def test_ohne_liste_keine_beschraenkung(self, client: Client) -> None:
        assert client.get("/admin/login/").status_code == 302

    def test_fremdes_netz_wird_abgewiesen(self, client: Client, settings: Any) -> None:
        settings.ADMIN_ALLOWED_NETWORKS = ["10.99.0.0/24"]
        response = client.get("/admin/login/", HTTP_X_FORWARDED_FOR="203.0.113.7")
        assert response.status_code == 403

    def test_freigegebenes_netz_wird_durchgelassen(self, client: Client, settings: Any) -> None:
        settings.ADMIN_ALLOWED_NETWORKS = ["10.99.0.0/24"]
        response = client.get("/admin/login/", HTTP_X_FORWARDED_FOR="10.99.0.3")
        assert response.status_code == 302


class TestSettingsToggles:
    def test_work_admin_setzt_pflicht_fuer_alle(self, client: Client, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        user = make_user()
        organization = work_member(user, is_admin=True)
        client.force_login(user)
        response = client.post(
            reverse("work:organization", kwargs={"org_slug": organization.slug}),
            {"action": "update_security", "require_2fa": "1"},
        )
        assert response.status_code == 302
        organization.refresh_from_db()
        assert organization.require_2fa
        page = client.get(reverse("work:organization", kwargs={"org_slug": organization.slug}))
        assert page.status_code == 200
        assert 'name="require_2fa"' in page.content.decode()

    def test_work_mitglied_ohne_recht_aendert_nichts(self, client: Client, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        user = make_user()
        organization = work_member(user)
        client.force_login(user)
        client.post(
            reverse("work:organization", kwargs={"org_slug": organization.slug}),
            {"action": "update_security", "require_2fa": "1"},
        )
        organization.refresh_from_db()
        assert not organization.require_2fa

    def test_session_verwaltung_setzt_pflicht(self, client: Client, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        user = make_user()
        tenant = session_member(user, can_manage_settings=True)
        client.force_login(user)
        response = client.post(
            reverse("session:settings_two_factor", kwargs={"tenant_slug": tenant.slug}), {"require_2fa": "1"}
        )
        assert response.status_code == 302
        tenant.refresh_from_db()
        assert tenant.require_2fa

    def test_session_ohne_einstellungsrecht_verboten(self, client: Client, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        user = make_user()
        tenant = session_member(user, can_view_meetings=True)
        client.force_login(user)
        client.post(reverse("session:settings_two_factor", kwargs={"tenant_slug": tenant.slug}), {"require_2fa": "1"})
        tenant.refresh_from_db()
        assert not tenant.require_2fa

    def test_pflicht_verhindert_deaktivieren_im_profil(self, client: Client) -> None:
        user = make_user()
        organization = work_member(user, is_admin=True)
        enable_2fa(user)
        client.force_login(user)
        client.post(
            reverse("work:security", kwargs={"org_slug": organization.slug}),
            {"action": "disable_2fa", "password": PASSWORD},
        )
        assert TwoFactorService().is_2fa_enabled(user)


class TestQrCodeDerEinrichtung:
    """Der QR-Code fehlte in Produktion, weil ``import qrcode`` nie gelang.

    Ein Test, der nur den Status 200 prüft, hätte das nicht bemerkt — deshalb
    wird hier das Bild selbst nachgewiesen.
    """

    def test_generate_qr_code_liefert_ein_png(self) -> None:
        import base64

        roh = TwoFactorService().generate_qr_code(
            "otpauth://totp/mandari:a@b.de?secret=JBSWY3DPEHPK3PXP&issuer=mandari"
        )
        assert roh, "Ohne QR-Code müssten alle den Schlüssel abtippen"
        bytes_ = base64.b64decode(roh)
        assert bytes_.startswith(b"\x89PNG\r\n\x1a\n"), "Muss ein PNG sein (Template bettet data:image/png;base64 ein)"
        assert len(bytes_) > 200

    def test_einrichtungsseite_zeigt_den_qr_code(self, client: Client) -> None:
        user = make_user()
        work_member(user, is_admin=True)
        client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORD})
        response = client.get(reverse("accounts:two_factor_enroll"))
        assert response.status_code == 200
        html = response.content.decode()
        assert 'src="data:image/png;base64,' in html, "Die Seite verspricht einen QR-Code — er muss auch da sein"
        # Der Schlüssel selbst bleibt als Rückfall auf der Seite (zum Aufklappen),
        # falls sich der QR-Code nicht scannen lässt.
        geheimnis = client.session[ENROLL_SETUP_SESSION_KEY]["secret"]
        assert geheimnis in html, "Der Schlüssel bleibt als Rückfall nötig"
        assert "manuell eingeben" in html, "Der Schlüssel muss auch auffindbar sein"
