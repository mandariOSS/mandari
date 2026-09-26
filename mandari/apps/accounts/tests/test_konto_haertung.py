# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Härtung von Anmeldung und Konto-Sicherheit.

- Ein aktiver zweiter Faktor wird über die Einrichtungsseite nie ersetzt.
- Die Pflicht zum Sicherheitsschlüssel gilt für jede Schreibweise eines App-Codes.
- Fehlversuche werden auch je Konto und je IPv6-/64-Netz gezählt.
- Passwörter bei Registrierung und Passwortwechsel folgen ``AUTH_PASSWORD_VALIDATORS``.
- „Sitzung beenden“ beendet die Sitzung auch mit einem Cache-gestützten Sitzungsspeicher.
- „Passwort vergessen“ verschickt je Adresse und IP nur begrenzt viele Mails.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from webauthn.helpers import bytes_to_base64url

from apps.accounts.models import TwoFactorDevice, User, WebAuthnCredential
from apps.accounts.services import PasswordService, SessionService, TwoFactorService
from apps.accounts.views import ENROLL_SETUP_SESSION_KEY

pytestmark = pytest.mark.django_db

PASSWORD = "Sicheres-Passwort-2026!"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
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
    return str(data["secret"])


def is_logged_in(client: Client) -> bool:
    return "_auth_user_id" in client.session


def anmelden(client: Client, email: str, passwort: str = PASSWORD, ip: str = "198.51.100.7") -> Any:
    return client.post(reverse("accounts:login"), {"email": email, "password": passwort}, REMOTE_ADDR=ip)


# ---------------------------------------------------------------- Einrichtung des zweiten Faktors


class TestEinrichtungErsetztKeinenAktivenFaktor:
    def test_post_auf_einrichtung_laesst_aktiven_faktor_unveraendert(self, client: Client) -> None:
        user = make_user()
        secret = enable_totp(user)
        vorher = TwoFactorDevice.objects.get(user=user).secret_encrypted
        client.force_login(user)

        antwort = client.post(reverse("accounts:two_factor_enroll"), {"code": "000000"})

        assert antwort.status_code == 302
        geraet = TwoFactorDevice.objects.get(user=user)
        assert geraet.is_confirmed and geraet.is_active
        assert bytes(geraet.secret_encrypted) == bytes(vorher)
        assert TwoFactorService().verify_code(secret, current_code(secret))
        assert ENROLL_SETUP_SESSION_KEY not in client.session

    def test_erstmalige_einrichtung_funktioniert_weiter(self, client: Client) -> None:
        user = make_user()
        client.force_login(user)

        seite = client.get(reverse("accounts:two_factor_enroll"))
        assert seite.status_code == 200
        secret = client.session[ENROLL_SETUP_SESSION_KEY]["secret"]
        antwort = client.post(reverse("accounts:two_factor_enroll"), {"code": current_code(secret)})

        assert antwort.status_code == 200
        assert TwoFactorService().is_2fa_enabled(user)


# ---------------------------------------------------------------- Pflicht zum Sicherheitsschlüssel


class TestSchluesselpflicht:
    @pytest.mark.parametrize("praefix", ["\t", "\n", " ", " \t "])
    def test_leerraum_vor_dem_app_code_umgeht_die_pflicht_nicht(
        self, client: Client, settings: Any, praefix: str
    ) -> None:
        settings.WEBAUTHN_RP_ID = "testserver"
        settings.TWO_FACTOR_ENFORCEMENT = True
        settings.TWO_FACTOR_REQUIRE_SECURITY_KEY_FOR_SUPERUSERS = True
        user = make_user("admin@example.org", is_superuser=True, is_staff=True)
        secret = enable_totp(user)
        WebAuthnCredential.objects.create(
            user=user,
            name="Schlüssel",
            credential_id=bytes_to_base64url(b"schluessel-1"),
            public_key=b"public-key",
            sign_count=1,
            transports=["usb"],
            device_type="single_device",
        )
        anmelden(client, user.email)

        antwort = client.post(reverse("accounts:login_2fa"), {"code": praefix + current_code(secret)})

        assert "Sicherheitsschlüssel vorgeschrieben" in antwort.content.decode()
        assert not is_logged_in(client)


# ---------------------------------------------------------------- Ratenbegrenzung der Anmeldung


class TestRatenbegrenzungJeKonto:
    def test_fehlversuche_aus_vielen_adressen_sperren_das_konto(self, client: Client) -> None:
        user = make_user()
        for i in range(30):
            anmelden(Client(), user.email, "falsches-passwort", ip=f"203.0.113.{i + 1}")

        anmelden(client, user.email, PASSWORD, ip="198.51.100.200")

        assert not is_logged_in(client)

    def test_fehlversuche_aus_einem_ipv6_netz_zaehlen_zusammen(self, client: Client) -> None:
        opfer = make_user("opfer@example.org")
        for i in range(10):
            anmelden(Client(), f"andere-{i}@example.org", "falsch", ip=f"2001:db8:1:2::{i + 1:x}")

        anmelden(client, opfer.email, PASSWORD, ip="2001:db8:1:2::ff")

        assert not is_logged_in(client)

    def test_andere_konten_und_netze_bleiben_unberuehrt(self, client: Client) -> None:
        make_user("gesperrt@example.org")
        frei = make_user("frei@example.org")
        for i in range(30):
            anmelden(Client(), "gesperrt@example.org", "falsch", ip=f"203.0.113.{i + 1}")

        anmelden(client, frei.email, PASSWORD, ip="198.51.100.200")

        assert is_logged_in(client)


# ---------------------------------------------------------------- Passwortrichtlinie


SCHWACH = ["12345678", "passwort", "Passwort1"]


class TestPasswortrichtlinie:
    @pytest.mark.parametrize("passwort", SCHWACH)
    def test_selbstregistrierung_lehnt_schwache_passwoerter_ab(self, org: Any, passwort: str) -> None:
        org.registration_enabled = True
        org.save()
        antwort = Client().post(
            reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
            {
                "email": "neu@example.org",
                "first_name": "N",
                "last_name": "N",
                "password1": passwort,
                "password2": passwort,
            },
        )
        assert antwort.status_code == 200
        assert not User.objects.filter(email="neu@example.org").exists()

    @pytest.mark.parametrize("passwort", SCHWACH)
    def test_einladungsregistrierung_lehnt_schwache_passwoerter_ab(self, passwort: str) -> None:
        from apps.accounts.forms import RegistrationForm

        form = cast(Any, RegistrationForm)(
            {"first_name": "N", "last_name": "N", "password1": passwort, "password2": passwort},
            email="neu@example.org",
        )
        assert not form.is_valid()

    def test_passwortwechsel_folgt_der_richtlinie(self) -> None:
        user = make_user()
        ok, _meldung = PasswordService.change_password(user, PASSWORD, "Kurz-2026")
        assert not ok
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_starkes_passwort_wird_angenommen(self) -> None:
        user = make_user()
        ok, _meldung = PasswordService.change_password(user, PASSWORD, "Neues-Sicheres-Passwort-2027")
        assert ok


# ---------------------------------------------------------------- Sitzung beenden


class TestSitzungBeenden:
    @pytest.fixture(autouse=True)
    def _cached_db(self, settings: Any) -> None:
        settings.SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

    def _zwei_sitzungen(self) -> tuple[User, Client, Client]:
        user = make_user()
        fremd, eigen = Client(), Client()
        anmelden(fremd, user.email, ip="198.51.100.1")
        anmelden(eigen, user.email, ip="198.51.100.2")
        assert is_logged_in(fremd) and is_logged_in(eigen)
        return user, fremd, eigen

    def _angemeldet(self, client: Client) -> bool:
        """Was eine Anfrage (oder ein WebSocket) mit diesem Cookie als Sitzung vorfindet."""
        from importlib import import_module

        from django.conf import settings

        store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=client.session.session_key)
        return "_auth_user_id" in store.load()

    def test_einzelne_sitzung_beenden(self) -> None:
        user, fremd, _eigen = self._zwei_sitzungen()
        assert self._angemeldet(fremd)

        assert SessionService.revoke_session(user, str(fremd.session.session_key))

        assert not self._angemeldet(fremd)

    def test_alle_anderen_sitzungen_beenden(self) -> None:
        user, fremd, eigen = self._zwei_sitzungen()

        SessionService.revoke_all_sessions(user, except_current=str(eigen.session.session_key))

        assert not self._angemeldet(fremd)
        assert self._angemeldet(eigen)


# ---------------------------------------------------------------- Passwort vergessen


class TestPasswortVergessenGedrosselt:
    def test_mails_je_adresse_begrenzt(self) -> None:
        make_user("ziel@example.org")
        for i in range(25):
            Client().post(
                reverse("accounts:password_reset"), {"email": "ziel@example.org"}, REMOTE_ADDR=f"203.0.113.{i}"
            )
        assert 1 <= len(mail.outbox) <= 3

    def test_anfragen_je_ip_begrenzt(self) -> None:
        for i in range(25):
            make_user(f"konto-{i}@example.org")
        for i in range(25):
            Client().post(
                reverse("accounts:password_reset"), {"email": f"konto-{i}@example.org"}, REMOTE_ADDR="203.0.113.9"
            )
        assert 1 <= len(mail.outbox) <= 10

    def test_antwort_bleibt_neutral(self) -> None:
        make_user("ziel@example.org")
        antworten = [
            Client().post(reverse("accounts:password_reset"), {"email": "ziel@example.org"}, REMOTE_ADDR="203.0.113.1")
            for _ in range(5)
        ]
        assert {a.status_code for a in antworten} == {302}
        assert {a["Location"] for a in antworten} == {reverse("accounts:password_reset_done")}
