# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitsschlüssel und Passkeys: Registrierung, Anmeldung im zweiten Schritt,
Pflicht für die Plattform-Administration, Zurücksetzen per Management-Befehl.

Die kryptografische Prüfung selbst (py_webauthn) wird ersetzt; getestet wird
der Ablauf drumherum: Challenges, Zuordnung zum Konto, Sperren, Protokollierung.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory
from django.urls import reverse
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse

from apps.accounts import webauthn_service
from apps.accounts.models import LoginAttempt, TwoFactorDevice, User, WebAuthnCredential
from apps.accounts.services import TwoFactorService
from apps.accounts.webauthn_service import WebAuthnError

pytestmark = pytest.mark.django_db

PASSWORD = "Sicheres-Passwort-2026!"
BACKUP_CODE = "abcd-1234"


@pytest.fixture(autouse=True)
def _settings(settings: Any) -> Iterator[None]:
    settings.WEBAUTHN_RP_ID = "testserver"
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS = False
    cache.clear()
    yield
    cache.clear()


def make_user(email: str = "person@example.org", **extra: Any) -> User:
    return cast(User, User.objects.create_user(email=email, password=PASSWORD, **extra))  # type: ignore[no-untyped-call]


def current_code(secret: str) -> str:
    service = TwoFactorService()
    return service._get_totp_code(secret, int(time.time() // service.TOTP_INTERVAL))


def enable_totp(user: User) -> str:
    service = TwoFactorService()
    data = service.setup_2fa(user)
    assert service.confirm_2fa(user, current_code(data["secret"]))
    device = TwoFactorDevice.objects.get(user=user)
    device.backup_codes_encrypted = service._encrypt(f'["{BACKUP_CODE}"]')
    device.save(update_fields=["backup_codes_encrypted"])
    return str(data["secret"])


def add_key(user: User, raw_id: bytes = b"schluessel-1", sign_count: int = 1) -> WebAuthnCredential:
    return WebAuthnCredential.objects.create(
        user=user,
        name="YubiKey",
        credential_id=bytes_to_base64url(raw_id),
        public_key=b"public-key",
        sign_count=sign_count,
        transports=["usb"],
        device_type="single_device",
    )


def is_logged_in(client: Client) -> bool:
    return "_auth_user_id" in client.session


def password_step(client: Client, user: User) -> Any:
    return client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORD})


def post_json(client: Client, name: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(reverse(name), data=body or {}, content_type="application/json")


# ---------------------------------------------------------------------------


class TestOrigin:
    def test_hauptdomain_und_subdomains_erlaubt(self, settings: Any) -> None:
        settings.ALLOWED_HOSTS = ["*"]
        factory = RequestFactory()
        assert webauthn_service.expected_origin(factory.get("/", HTTP_HOST="testserver")) == "http://testserver"
        assert (
            webauthn_service.expected_origin(factory.get("/", HTTP_HOST="volt.testserver")) == "http://volt.testserver"
        )

    def test_fremde_domain_abgelehnt(self, settings: Any) -> None:
        settings.ALLOWED_HOSTS = ["*"]
        with pytest.raises(WebAuthnError):
            webauthn_service.expected_origin(RequestFactory().get("/", HTTP_HOST="mandari.example"))


class TestRegistration:
    def test_ohne_authenticator_app_keine_registrierung(self, client: Client) -> None:
        user = make_user()
        client.force_login(user)
        response = post_json(client, "accounts:webauthn_register_options")
        assert response.status_code == 409

    def test_registrierung_speichert_schluessel(self, client: Client, monkeypatch: Any) -> None:
        user = make_user()
        enable_totp(user)
        client.force_login(user)

        options = post_json(client, "accounts:webauthn_register_options")
        assert options.status_code == 200
        assert options.json()["rp"]["id"] == "testserver"
        assert "challenge" in options.json()

        captured: dict[str, Any] = {}

        def fake_verify(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                credential_id=b"neuer-schluessel",
                credential_public_key=b"public-key",
                sign_count=0,
                aaguid="00000000-0000-0000-0000-000000000000",
                credential_device_type=SimpleNamespace(value="single_device"),
                credential_backed_up=False,
            )

        monkeypatch.setattr(webauthn_service, "verify_registration_response", fake_verify)
        response = post_json(
            client,
            "accounts:webauthn_register",
            {"credential": {"id": "x", "response": {"transports": ["usb", "unbekannt"]}}, "name": "YubiKey 5"},
        )
        assert response.status_code == 200, response.content
        credential = WebAuthnCredential.objects.get(user=user)
        assert credential.name == "YubiKey 5"
        assert credential.credential_id == bytes_to_base64url(b"neuer-schluessel")
        assert credential.transports == ["usb"]
        assert credential.is_hardware_key
        assert captured["expected_rp_id"] == "testserver"
        assert captured["expected_origin"] == "http://testserver"

    def test_ohne_challenge_abgelehnt(self, client: Client) -> None:
        user = make_user()
        enable_totp(user)
        client.force_login(user)
        response = post_json(client, "accounts:webauthn_register", {"credential": {"id": "x"}, "name": "Test"})
        assert response.status_code == 400
        assert not WebAuthnCredential.objects.filter(user=user).exists()

    def test_entfernen_braucht_passwort(self, client: Client) -> None:
        user = make_user()
        enable_totp(user)
        credential = add_key(user)
        client.force_login(user)

        client.post(
            reverse("accounts:security_keys"),
            {"action": "remove", "credential_id": str(credential.pk), "password": "falsch"},
        )
        assert WebAuthnCredential.objects.filter(pk=credential.pk).exists()

        client.post(
            reverse("accounts:security_keys"),
            {"action": "remove", "credential_id": str(credential.pk), "password": PASSWORD},
        )
        assert not WebAuthnCredential.objects.filter(pk=credential.pk).exists()

    def test_verwaltungsseite_listet_schluessel(self, client: Client) -> None:
        user = make_user()
        enable_totp(user)
        add_key(user)
        client.force_login(user)
        response = client.get(reverse("accounts:security_keys") + "?next=https://evil.example/")
        content = response.content.decode()
        assert response.status_code == 200
        assert "YubiKey" in content
        assert "Hardware-Schlüssel" in content
        assert "evil.example" not in content

    def test_deaktivieren_der_app_entfernt_schluessel(self) -> None:
        user = make_user()
        enable_totp(user)
        add_key(user)
        assert TwoFactorService().disable_2fa(user)
        assert not WebAuthnCredential.objects.filter(user=user).exists()


class TestLoginWithSecurityKey:
    def test_codeschritt_bietet_schluessel_an(self, client: Client) -> None:
        user = make_user()
        enable_totp(user)
        add_key(user)
        password_step(client, user)
        response = client.get(reverse("accounts:login_2fa"))
        assert "Mit Sicherheitsschlüssel anmelden" in response.content.decode()

    def test_optionen_nur_im_zweiten_schritt(self, client: Client) -> None:
        response = post_json(client, "accounts:webauthn_login_options")
        assert response.status_code == 401

    def test_anmeldung_mit_schluessel(self, client: Client, monkeypatch: Any) -> None:
        user = make_user()
        enable_totp(user)
        credential = add_key(user, sign_count=4)
        password_step(client, user)

        options = post_json(client, "accounts:webauthn_login_options")
        assert options.status_code == 200
        assert options.json()["allowCredentials"][0]["id"] == credential.credential_id

        monkeypatch.setattr(
            webauthn_service,
            "verify_authentication_response",
            lambda **kwargs: SimpleNamespace(new_sign_count=kwargs["credential_current_sign_count"] + 1),
        )
        response = post_json(client, "accounts:webauthn_login", {"credential": {"id": credential.credential_id}})
        assert response.status_code == 200, response.content
        assert "redirect" in response.json()
        assert is_logged_in(client)
        credential.refresh_from_db()
        assert credential.sign_count == 5
        assert credential.last_used_at is not None
        assert LoginAttempt.objects.filter(email=user.email, was_successful=True).exists()

    def test_fremder_schluessel_abgelehnt(self, client: Client) -> None:
        user = make_user()
        enable_totp(user)
        add_key(user)
        password_step(client, user)
        post_json(client, "accounts:webauthn_login_options")

        response = post_json(client, "accounts:webauthn_login", {"credential": {"id": "unbekannt"}})
        assert response.status_code == 400
        assert not is_logged_in(client)
        assert LoginAttempt.objects.filter(email=user.email, failure_reason="invalid_2fa").exists()

    def test_ungueltige_signatur_abgelehnt(self, client: Client, monkeypatch: Any) -> None:
        user = make_user()
        enable_totp(user)
        credential = add_key(user)
        password_step(client, user)
        post_json(client, "accounts:webauthn_login_options")

        def fail(**kwargs: Any) -> Any:
            raise InvalidAuthenticationResponse("Signatur ungültig")

        monkeypatch.setattr(webauthn_service, "verify_authentication_response", fail)
        response = post_json(client, "accounts:webauthn_login", {"credential": {"id": credential.credential_id}})
        assert response.status_code == 400
        assert not is_logged_in(client)

    def test_challenge_ist_einmalig(self, client: Client, monkeypatch: Any) -> None:
        user = make_user()
        enable_totp(user)
        credential = add_key(user)
        password_step(client, user)
        post_json(client, "accounts:webauthn_login_options")
        monkeypatch.setattr(
            webauthn_service, "verify_authentication_response", lambda **kwargs: SimpleNamespace(new_sign_count=9)
        )
        session = client.session
        session["auth_webauthn_authentication"]["at"] -= 3600
        session.save()
        response = post_json(client, "accounts:webauthn_login", {"credential": {"id": credential.credential_id}})
        assert response.status_code == 400
        assert not is_logged_in(client)


class TestSecurityKeyRequirement:
    def test_superuser_mit_schluessel_kann_keinen_app_code_nutzen(self, client: Client, settings: Any) -> None:
        settings.TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS = True
        user = make_user("admin@example.org", is_superuser=True, is_staff=True)
        secret = enable_totp(user)
        add_key(user)

        password_step(client, user)
        response = client.post(reverse("accounts:login_2fa"), {"code": current_code(secret)})
        assert response.status_code == 200
        assert "Sicherheitsschlüssel vorgeschrieben" in response.content.decode()
        assert not is_logged_in(client)

        response = client.post(reverse("accounts:login_2fa"), {"code": BACKUP_CODE})
        assert response.status_code == 302
        assert is_logged_in(client)

    def test_superuser_ohne_schluessel_wird_zur_registrierung_geleitet(self, client: Client, settings: Any) -> None:
        settings.TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS = True
        user = make_user("admin@example.org", is_superuser=True, is_staff=True)
        enable_totp(user)
        client.force_login(user)
        response = client.get("/work/")
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:security_keys"))

    def test_ohne_schalter_keine_schluesselpflicht(self, client: Client) -> None:
        user = make_user("admin@example.org", is_superuser=True, is_staff=True)
        enable_totp(user)
        client.force_login(user)
        response = client.get("/work/")
        assert reverse("accounts:security_keys") not in response.get("Location", "")


class TestResetCommand:
    def test_zuruecksetzen_entfernt_alle_faktoren(self) -> None:
        user = make_user()
        enable_totp(user)
        add_key(user)
        call_command("reset_two_factor", user.email, reason="Test: Identität per Rückruf geprüft", yes=True)
        assert not TwoFactorDevice.objects.filter(user=user).exists()
        assert not WebAuthnCredential.objects.filter(user=user).exists()

    def test_unbekanntes_konto(self) -> None:
        with pytest.raises(CommandError):
            call_command("reset_two_factor", "niemand@example.org", reason="Test", yes=True)
