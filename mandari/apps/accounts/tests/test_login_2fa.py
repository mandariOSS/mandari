# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anmeldung mit zweitem Faktor, Admin-Anmeldung und Selbstregistrierung.

Sicherheitsrelevant: Mit aktivierter 2FA darf das Passwort allein keine
Sitzung erzeugen; bestehende Konten dürfen nicht über die
Selbstregistrierung übernommen werden.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.models import LoginAttempt, TwoFactorDevice, User
from apps.accounts.services import TwoFactorService
from apps.accounts.views import MAX_2FA_FAILURES, PENDING_2FA_SESSION_KEY
from apps.common.tests.factories import OrganizationFactory
from apps.tenants.models import Membership, Organization

pytestmark = pytest.mark.django_db

PASSWORD = "Sicheres-Passwort-2026!"
BACKUP_CODES = ["abcd-1234", "ef56-7890"]


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def make_user(email: str = "rat@example.org") -> User:
    return cast(User, User.objects.create_user(email=email, password=PASSWORD))  # type: ignore[no-untyped-call]


def enable_2fa(user: User) -> str:
    service = TwoFactorService()
    secret = service.generate_secret()
    TwoFactorDevice.objects.create(
        user=user,
        secret_encrypted=service._encrypt(secret),
        backup_codes_encrypted=service._encrypt(json.dumps(BACKUP_CODES)),
        is_confirmed=True,
        is_active=True,
    )
    return secret


def current_code(secret: str) -> str:
    service = TwoFactorService()
    return service._get_totp_code(secret, int(time.time() // service.TOTP_INTERVAL))


def is_logged_in(client: Client) -> bool:
    return "_auth_user_id" in client.session


def password_step(client: Client, user: User, next_url: str = "") -> Any:
    return client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORD, "next": next_url})


def code_step(client: Client, code: str) -> Any:
    return client.post(reverse("accounts:login_2fa"), {"code": code})


class TestLoginOhne2FA:
    def test_passwort_meldet_direkt_an(self, client: Client) -> None:
        user = make_user()
        response = password_step(client, user)
        assert response.status_code == 302
        assert is_logged_in(client)


class TestLoginMit2FA:
    def test_passwort_allein_erzeugt_keine_sitzung(self, client: Client) -> None:
        user = make_user()
        enable_2fa(user)
        response = password_step(client, user)
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login_2fa")
        assert not is_logged_in(client)

    def test_gueltiger_code_meldet_an(self, client: Client) -> None:
        user = make_user()
        secret = enable_2fa(user)
        password_step(client, user)
        response = code_step(client, current_code(secret))
        assert response.status_code == 302
        assert is_logged_in(client)
        assert PENDING_2FA_SESSION_KEY not in client.session
        assert LoginAttempt.objects.filter(email=user.email, was_successful=True).exists()

    def test_falscher_code_wird_abgelehnt_und_protokolliert(self, client: Client) -> None:
        user = make_user()
        enable_2fa(user)
        password_step(client, user)
        response = code_step(client, "000000")
        assert response.status_code == 200
        assert not is_logged_in(client)
        assert LoginAttempt.objects.filter(email=user.email, failure_reason="invalid_2fa").count() == 1

    def test_code_ist_nicht_wiederverwendbar(self, client: Client) -> None:
        user = make_user()
        secret = enable_2fa(user)
        code = current_code(secret)
        password_step(client, user)
        code_step(client, code)
        assert is_logged_in(client)

        client.logout()
        password_step(client, user)
        response = code_step(client, code)
        assert response.status_code == 200
        assert not is_logged_in(client)

    def test_backup_code_mit_bindestrich_ist_einmalig(self, client: Client) -> None:
        user = make_user()
        enable_2fa(user)
        password_step(client, user)
        code_step(client, "ABCD-1234")
        assert is_logged_in(client)

        client.logout()
        password_step(client, user)
        response = code_step(client, "abcd1234")
        assert response.status_code == 200
        assert not is_logged_in(client)

    def test_sperre_nach_fehlversuchen(self, client: Client) -> None:
        user = make_user()
        secret = enable_2fa(user)
        password_step(client, user)
        for _ in range(MAX_2FA_FAILURES):
            code_step(client, "000000")
        response = code_step(client, current_code(secret))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")
        assert not is_logged_in(client)

    def test_abgelaufener_zwischenschritt(self, client: Client) -> None:
        user = make_user()
        secret = enable_2fa(user)
        password_step(client, user)
        session = client.session
        pending = session[PENDING_2FA_SESSION_KEY]
        pending["started"] -= 3600
        session[PENDING_2FA_SESSION_KEY] = pending
        session.save()

        response = code_step(client, current_code(secret))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")
        assert not is_logged_in(client)

    def test_codeseite_ohne_passwortschritt(self, client: Client) -> None:
        response = client.get(reverse("accounts:login_2fa"))
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")

    def test_codeseite_rendert(self, client: Client) -> None:
        user = make_user()
        enable_2fa(user)
        password_step(client, user)
        response = client.get(reverse("accounts:login_2fa"))
        assert response.status_code == 200
        assert 'autocomplete="one-time-code"' in response.content.decode()


class TestAdminLogin:
    def test_admin_login_leitet_auf_eigene_anmeldung(self, client: Client) -> None:
        response = client.get("/admin/login/?next=/admin/insight_core/")
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))
        assert "next=%2Fadmin%2Finsight_core%2F" in response["Location"]

    def test_admin_login_post_meldet_nicht_an(self, client: Client) -> None:
        user = cast(User, User.objects.create_superuser(email="admin@example.org", password=PASSWORD))  # type: ignore[no-untyped-call]
        enable_2fa(user)
        response = client.post("/admin/login/", {"username": user.email, "password": PASSWORD})
        assert response.status_code == 302
        assert not is_logged_in(client)


class TestTwoFactorSecrets:
    def test_secret_wird_verschluesselt_gespeichert(self) -> None:
        user = make_user()
        service = TwoFactorService()
        secret = service.setup_2fa(user)["secret"]
        device = TwoFactorDevice.objects.get(user=user)
        assert secret.encode() not in bytes(device.secret_encrypted)
        assert service._decrypt(device.secret_encrypted) == secret

    def test_klartext_altbestand_bleibt_lesbar_und_wird_verschluesselt(self) -> None:
        user = make_user()
        service = TwoFactorService()
        secret = service.generate_secret()
        TwoFactorDevice.objects.create(
            user=user,
            secret_encrypted=secret.encode(),
            backup_codes_encrypted=json.dumps(BACKUP_CODES).encode(),
            is_confirmed=True,
            is_active=True,
        )
        assert service.verify_2fa(user, current_code(secret))
        device = TwoFactorDevice.objects.get(user=user)
        assert bytes(device.secret_encrypted) != secret.encode()
        assert service._decrypt(device.secret_encrypted) == secret
        assert json.loads(service._decrypt(device.backup_codes_encrypted)) == BACKUP_CODES

    @override_settings(ENCRYPTION_MASTER_KEY="", ENCRYPTION_KEY="", DEBUG=False)
    def test_ohne_schluessel_kein_klartext_im_betrieb(self) -> None:
        with pytest.raises(ImproperlyConfigured):
            TwoFactorService()._encrypt("GEHEIM")


class TestSelbstregistrierung:
    def _org(self) -> Organization:
        return cast(Organization, OrganizationFactory(registration_enabled=True, registration_auto_approve=True))  # type: ignore[no-untyped-call]

    def _data(self, email: str) -> dict[str, str]:
        return {
            "email": email,
            "first_name": "Eva",
            "last_name": "Muster",
            "password1": "Angreifer-Passwort-1",
            "password2": "Angreifer-Passwort-1",
        }

    def test_bestehendes_konto_wird_nicht_uebernommen(self, client: Client) -> None:
        org = self._org()
        victim = make_user("opfer@example.org")
        response = client.post(
            reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
            self._data("Opfer@Example.org"),
        )
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))
        assert not is_logged_in(client)
        assert not Membership.objects.filter(user=victim, organization=org).exists()
        victim.refresh_from_db()
        assert victim.check_password(PASSWORD)

    def test_neues_konto_wird_angelegt_und_angemeldet(self, client: Client) -> None:
        org = self._org()
        client.post(
            reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
            self._data("neu@example.org"),
        )
        user = User.objects.get(email="neu@example.org")
        assert is_logged_in(client)
        assert Membership.objects.filter(user=user, organization=org).exists()
