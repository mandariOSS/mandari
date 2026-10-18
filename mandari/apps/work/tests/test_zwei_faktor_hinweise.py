# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zwei-Faktor in Work: Empfehlung im Hinweisband auf Start (mit „Später“ für 30 Tage) und die Angabe
„Zweiter Faktor“ in der Mitgliederliste, die nur die Administration und ``organization.edit`` sehen.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import TwoFactorDevice, User
from apps.common.permissions import DEFAULT_ROLES
from apps.tenants.models import Membership, Organization
from apps.work.dashboard import hinweise
from apps.work.notifications import ankuendigung

pytestmark = pytest.mark.django_db

BAND = 'data-testid="hinweisband"'
EMPFEHLUNG = "Schützen Sie Ihr Konto mit einem zweiten Faktor"
SPALTE = 'data-testid="zweiter-faktor"'


def zweiter_faktor(user: User) -> None:
    """Bestätigter, aktiver zweiter Faktor (mehr prüft ``TwoFactorService.is_2fa_enabled`` nicht)."""
    TwoFactorDevice.objects.create(user=user, secret_encrypted=b"-", is_confirmed=True, is_active=True)


def start(client: Client, organization: Organization) -> str:
    antwort = client.get(reverse("work:dashboard", kwargs={"org_slug": organization.slug}))
    assert antwort.status_code == 200
    return str(antwort.content.decode())


# ---- Empfehlung auf Start ------------------------------------------------------------------------------------


@pytest.fixture
def durchsetzung(settings: Any) -> None:
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = ["demo.mandari.de"]


@pytest.fixture
def mitglied(org: Organization, make_member: Any) -> Membership:
    """Fraktionsmitglied ohne Verwaltungsrechte (keine Pflicht, nur Empfehlung)."""
    return cast(
        Membership, make_member(org, DEFAULT_ROLES["faction_member"]["permissions"], email="mitglied@example.org")
    )


@pytest.mark.usefixtures("durchsetzung")
class TestEmpfehlungAufStart:
    @pytest.mark.parametrize("neues_design", [False, True])
    def test_ohne_zweiten_faktor_erscheint_die_empfehlung(
        self, mitglied: Membership, client_for: Any, neues_design: bool
    ) -> None:
        mitglied.organization.work_new_design = neues_design
        mitglied.organization.save(update_fields=["work_new_design"])
        html = start(client_for(mitglied.user), mitglied.organization)
        assert html.count(BAND) == 1
        assert EMPFEHLUNG in html
        assert reverse("work:security", kwargs={"org_slug": mitglied.organization.slug}) in html
        assert reverse("work:two_factor_hint_later", kwargs={"org_slug": mitglied.organization.slug}) in html
        assert ">Später</button>" in html

    def test_mit_zweitem_faktor_kein_hinweis(self, mitglied: Membership, client_for: Any) -> None:
        zweiter_faktor(mitglied.user)
        assert BAND not in start(client_for(mitglied.user), mitglied.organization)

    def test_demo_zugang_ohne_hinweis(self, org: Organization, make_member: Any, client_for: Any) -> None:
        demo = make_member(org, ["dashboard.view"], email="demo-mitglied@demo.mandari.de")
        assert BAND not in start(client_for(demo.user), org)

    def test_ohne_durchsetzung_kein_hinweis(self, mitglied: Membership, client_for: Any, settings: Any) -> None:
        settings.TWO_FACTOR_ENFORCEMENT = False
        assert BAND not in start(client_for(mitglied.user), mitglied.organization)

    def test_spaeter_blendet_den_hinweis_aus(self, mitglied: Membership, client_for: Any) -> None:
        client = client_for(mitglied.user)
        antwort = client.post(
            reverse("work:two_factor_hint_later", kwargs={"org_slug": mitglied.organization.slug}),
            HTTP_HX_REQUEST="true",
        )
        assert antwort.status_code == 200
        assert antwort.content == b""
        mitglied.user.refresh_from_db()
        bis = mitglied.user.settings[hinweise.ZWEI_FAKTOR_SPAETER_SCHLUESSEL]
        assert bis > (timezone.now() + timedelta(days=29)).isoformat()
        assert BAND not in start(client, mitglied.organization)

    def test_spaeter_ohne_htmx_fuehrt_zurueck_auf_start(self, mitglied: Membership, client_for: Any) -> None:
        antwort = client_for(mitglied.user).post(
            reverse("work:two_factor_hint_later", kwargs={"org_slug": mitglied.organization.slug})
        )
        assert antwort.status_code == 302
        assert antwort["Location"] == reverse("work:dashboard", kwargs={"org_slug": mitglied.organization.slug})

    def test_spaeter_behaelt_andere_einstellungen(self, mitglied: Membership, client_for: Any) -> None:
        mitglied.user.settings = {"profile": {"bio": "Ratsfrau"}}
        mitglied.user.save(update_fields=["settings"])
        client_for(mitglied.user).post(
            reverse("work:two_factor_hint_later", kwargs={"org_slug": mitglied.organization.slug})
        )
        mitglied.user.refresh_from_db()
        assert mitglied.user.settings["profile"] == {"bio": "Ratsfrau"}

    def test_spaeter_wirkt_dreissig_tage(self, mitglied: Membership) -> None:
        hinweise.zwei_faktor_spaeter(mitglied.user)
        jetzt = timezone.now()
        assert hinweise.zwei_faktor_zurueckgestellt(mitglied.user, jetzt=jetzt + timedelta(days=29, hours=23))
        assert not hinweise.zwei_faktor_zurueckgestellt(mitglied.user, jetzt=jetzt + timedelta(days=30, minutes=1))

    def test_nach_ablauf_erscheint_der_hinweis_wieder(self, mitglied: Membership, client_for: Any) -> None:
        mitglied.user.settings = {
            hinweise.ZWEI_FAKTOR_SPAETER_SCHLUESSEL: (timezone.now() - timedelta(minutes=1)).isoformat()
        }
        mitglied.user.save(update_fields=["settings"])
        assert EMPFEHLUNG in start(client_for(mitglied.user), mitglied.organization)

    @pytest.mark.parametrize("wert", ["kein Datum", 17, None])
    def test_unbrauchbarer_wert_stellt_nichts_zurueck(self, mitglied: Membership, wert: Any) -> None:
        mitglied.user.settings = {hinweise.ZWEI_FAKTOR_SPAETER_SCHLUESSEL: wert}
        assert not hinweise.zwei_faktor_zurueckgestellt(mitglied.user)

    def test_ankuendigung_hat_vorrang(self, mitglied: Membership, client_for: Any) -> None:
        ankuendigung.ankuendigen(
            ankuendigung.Ankuendigung(schluessel="test-vorrang", titel="Neu in Work", text="Kurz erklärt.")
        )
        html = start(client_for(mitglied.user), mitglied.organization)
        assert html.count(BAND) == 1
        assert "Neu in Work" in html
        assert EMPFEHLUNG not in html

    def test_spaeter_nur_fuer_mitglieder_der_organisation(
        self, mitglied: Membership, make_member: Any, client_for: Any
    ) -> None:
        from apps.common.tests.factories import OrganizationFactory

        fremde = cast(Organization, OrganizationFactory(name="Fraktion Fremd", slug="fraktion-fremd"))  # type: ignore[no-untyped-call]
        antwort = client_for(mitglied.user).post(
            reverse("work:two_factor_hint_later", kwargs={"org_slug": fremde.slug})
        )
        assert antwort.status_code in (403, 404)
        mitglied.user.refresh_from_db()
        assert hinweise.ZWEI_FAKTOR_SPAETER_SCHLUESSEL not in (mitglied.user.settings or {})


# ---- Mitgliederliste ------------------------------------------------------------------------------------------


def mitgliederliste(client: Client, organization: Organization) -> Any:
    return client.get(reverse("work:members", kwargs={"org_slug": organization.slug}))


class TestSpalteZweiterFaktor:
    @pytest.mark.parametrize(
        ("rolle", "sichtbar"),
        [
            ("admin", True),
            ("faction_chair", True),
            ("managing_director", True),
            ("faction_vice_chair", False),
            ("faction_member", False),
            ("faction_staff", False),
        ],
    )
    def test_sichtbar_je_rolle(
        self, org: Organization, make_member: Any, client_for: Any, rolle: str, sichtbar: bool
    ) -> None:
        definition = DEFAULT_ROLES[rolle]
        betrachter = make_member(
            org, definition["permissions"], email=f"{rolle}@example.org", is_admin=bool(definition.get("is_admin"))
        )
        antwort = mitgliederliste(client_for(betrachter.user), org)
        assert antwort.status_code == 200
        assert (SPALTE in antwort.content.decode()) is sichtbar

    def test_einzelrecht_organisationseinstellungen_genuegt(
        self, org: Organization, make_member: Any, client_for: Any
    ) -> None:
        from apps.common.tests.factories import PermissionFactory

        betrachter = make_member(org, ["members.view", "members.view_details"], email="einzel@example.org")
        assert SPALTE not in mitgliederliste(client_for(betrachter.user), org).content.decode()
        betrachter.individual_permissions.add(PermissionFactory(codename="organization.edit"))  # type: ignore[no-untyped-call]
        assert SPALTE in mitgliederliste(client_for(betrachter.user), org).content.decode()

    def test_verweigertes_recht_blendet_aus(self, org: Organization, make_member: Any, client_for: Any) -> None:
        from apps.common.tests.factories import PermissionFactory

        betrachter = make_member(org, DEFAULT_ROLES["faction_chair"]["permissions"], email="vorsitz@example.org")
        betrachter.denied_permissions.add(PermissionFactory(codename="organization.edit"))  # type: ignore[no-untyped-call]
        assert SPALTE not in mitgliederliste(client_for(betrachter.user), org).content.decode()

    def test_ja_und_nein_je_mitglied(self, org: Organization, make_member: Any, client_for: Any) -> None:
        admin = make_member(org, [], email="admin@example.org", is_admin=True)
        geschuetzt = make_member(org, ["dashboard.view"], email="geschuetzt@example.org")
        make_member(org, ["dashboard.view"], email="offen@example.org")
        zweiter_faktor(admin.user)
        zweiter_faktor(geschuetzt.user)
        html = mitgliederliste(client_for(admin.user), org).content.decode()
        assert html.count(SPALTE) == 3
        assert html.count('data-zweiter-faktor="ja"') == 2
        assert html.count('data-zweiter-faktor="nein"') == 1
        offen = html.index("offen@example.org")
        assert html.index('data-zweiter-faktor="nein"', offen) < html.index("Beigetreten", offen)

    def test_unbestaetigter_oder_abgeschalteter_faktor_zaehlt_nicht(
        self, org: Organization, make_member: Any, client_for: Any
    ) -> None:
        admin = make_member(org, [], email="admin@example.org", is_admin=True)
        halb = make_member(org, ["dashboard.view"], email="halb@example.org")
        TwoFactorDevice.objects.create(user=halb.user, secret_encrypted=b"-", is_confirmed=False, is_active=True)
        html = mitgliederliste(client_for(admin.user), org).content.decode()
        assert html.count('data-zweiter-faktor="nein"') == 2
