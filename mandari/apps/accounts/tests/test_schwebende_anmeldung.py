# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Zustand zwischen Passwort und zweitem Schritt gilt nur für das geprüfte Passwort und den eigenen Faktor.

- Ändert sich das Passwort des Kontos (Zurücksetzen, Entwerten), ist eine schwebende Anmeldung beendet: weder der
  Code-Schritt noch die Pflicht-Einrichtung lassen sich danach abschließen.
- Die Pflicht-Einrichtung meldet nur an, wenn diese Sitzung den aktiven Faktor selbst bestätigt hat. Hat ihn
  inzwischen jemand anderes eingerichtet, führt der Abschluss in den regulären Code-Schritt.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import TwoFactorDevice, User
from apps.accounts.services import TwoFactorService
from apps.accounts.views import ENROLL_SETUP_SESSION_KEY, PENDING_2FA_SESSION_KEY
from apps.common.tests.factories import MembershipFactory, OrganizationFactory

pytestmark = pytest.mark.django_db

PASSWORD = "Sicheres-Passwort-2026!"
NEUES_PASSWORT = "Ganz-anderes-Passwort-2026!"


@pytest.fixture(autouse=True)
def _pflicht(settings: Any) -> Iterator[None]:
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = []
    cache.clear()
    yield
    cache.clear()


def _konto(email: str = "rat@example.org", *, pflicht: bool = False) -> User:
    user = cast(User, User.objects.create_user(email=email, password=PASSWORD))  # type: ignore[no-untyped-call]
    if pflicht:
        organisation = OrganizationFactory(require_2fa=True)  # type: ignore[no-untyped-call]
        MembershipFactory(user=user, organization=organisation)  # type: ignore[no-untyped-call]
    return user


def _faktor(user: User) -> str:
    service = TwoFactorService()
    secret = service.generate_secret()
    TwoFactorDevice.objects.create(
        user=user,
        secret_encrypted=service._encrypt(secret),
        backup_codes_encrypted=service._encrypt(json.dumps(["abcd-1234"])),
        is_confirmed=True,
        is_active=True,
    )
    return secret


def _code(secret: str) -> str:
    service = TwoFactorService()
    return service._get_totp_code(secret, int(time.time() // service.TOTP_INTERVAL))


def _passwort(client: Client, user: User) -> Any:
    return client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORD})


def _angemeldet(client: Client) -> bool:
    return "_auth_user_id" in client.session


# --- Passwort geändert -------------------------------------------------------------------------------------


def test_code_schritt_gilt_nach_neuem_passwort_nicht_mehr(client: Client) -> None:
    user = _konto()
    secret = _faktor(user)
    assert _passwort(client, user).url == reverse("accounts:login_2fa")

    user.set_password(NEUES_PASSWORT)
    user.save()
    antwort = client.post(reverse("accounts:login_2fa"), {"code": _code(secret)})

    assert antwort.status_code == 302 and antwort["Location"] == reverse("accounts:login")
    assert not _angemeldet(client)
    assert PENDING_2FA_SESSION_KEY not in client.session


def test_code_schritt_gilt_nach_entwertetem_passwort_nicht_mehr(client: Client) -> None:
    """Eine Einladung entwertet das Passwort eines nie bestätigten Kontos; schwebende Anmeldungen enden damit."""
    user = _konto()
    secret = _faktor(user)
    _passwort(client, user)

    user.set_unusable_password()
    user.save()
    client.post(reverse("accounts:login_2fa"), {"code": _code(secret)})

    assert not _angemeldet(client)


def test_code_schritt_ohne_passwortwechsel_meldet_weiter_an(client: Client) -> None:
    user = _konto()
    secret = _faktor(user)
    _passwort(client, user)

    client.post(reverse("accounts:login_2fa"), {"code": _code(secret)})

    assert _angemeldet(client)


def test_einrichtung_gilt_nach_neuem_passwort_nicht_mehr(client: Client) -> None:
    user = _konto(pflicht=True)
    assert _passwort(client, user).url == reverse("accounts:two_factor_enroll")

    user.set_password(NEUES_PASSWORT)
    user.save()
    seite = client.get(reverse("accounts:two_factor_enroll"))
    abschluss = client.post(reverse("accounts:two_factor_enroll"), {"code": "000000"})

    assert seite.status_code == 302 and seite["Location"] == reverse("accounts:login")
    assert abschluss.status_code == 302 and abschluss["Location"] == reverse("accounts:login")
    # Kein Gerät angelegt, kein Zustand übrig
    assert not TwoFactorDevice.objects.filter(user=user).exists()
    assert PENDING_2FA_SESSION_KEY not in client.session and ENROLL_SETUP_SESSION_KEY not in client.session
    assert not _angemeldet(client)


def test_zustand_aus_der_zeit_vor_der_bindung_ist_ungueltig(client: Client) -> None:
    """Ein schwebender Zustand ohne Bezug zum Passwort (vor dem Update begonnen) verlangt die Anmeldung neu."""
    user = _konto()
    secret = _faktor(user)
    _passwort(client, user)
    sitzung = client.session
    zustand = dict(sitzung[PENDING_2FA_SESSION_KEY])
    del zustand["auth"]
    sitzung[PENDING_2FA_SESSION_KEY] = zustand
    sitzung.save()

    client.post(reverse("accounts:login_2fa"), {"code": _code(secret)})

    assert not _angemeldet(client)


# --- Einrichtung nur mit dem eigenen Faktor abschließen ----------------------------------------------------------


def _einrichten(client: Client) -> None:
    """Pflicht-Einrichtung in dieser Sitzung bis zur Anzeige der Backup-Codes."""
    client.get(reverse("accounts:two_factor_enroll"))
    secret = client.session[ENROLL_SETUP_SESSION_KEY]["secret"]
    client.post(reverse("accounts:two_factor_enroll"), {"code": _code(secret)})


def test_fremd_eingerichteter_faktor_schliesst_die_schwebende_anmeldung_nicht_ab() -> None:
    user = _konto(pflicht=True)
    fremd, inhaberin = Client(), Client()
    # Die fremde Anmeldung besteht den Passwortschritt, bevor die Inhaberin ihren Faktor einrichtet
    _passwort(fremd, user)
    fremd.get(reverse("accounts:two_factor_enroll"))
    _passwort(inhaberin, user)
    _einrichten(inhaberin)
    assert TwoFactorService().is_2fa_enabled(user)

    antwort = fremd.post(reverse("accounts:two_factor_enroll"), {"action": "finish"})

    assert antwort.status_code == 302 and antwort["Location"] == reverse("accounts:login_2fa")
    assert not _angemeldet(fremd)
    # Die Anmeldung schwebt weiter, jetzt vor dem Code-Schritt
    assert "enroll" not in fremd.session[PENDING_2FA_SESSION_KEY]
    assert ENROLL_SETUP_SESSION_KEY not in fremd.session


def test_selbst_bestaetigte_einrichtung_meldet_an() -> None:
    user = _konto(pflicht=True)
    client = Client()
    _passwort(client, user)
    _einrichten(client)

    antwort = client.post(reverse("accounts:two_factor_enroll"), {"action": "finish"})

    assert antwort.status_code == 302
    assert _angemeldet(client)


def test_bestaetigte_daten_eines_ersetzten_faktors_zaehlen_nicht() -> None:
    """Die Sitzung hat einen Faktor bestätigt, aktiv ist inzwischen ein anderer: kein Abschluss ohne Code."""
    user = _konto(pflicht=True)
    client = Client()
    _passwort(client, user)
    _einrichten(client)
    service = TwoFactorService()
    TwoFactorDevice.objects.filter(user=user).update(secret_encrypted=service._encrypt(service.generate_secret()))

    antwort = client.post(reverse("accounts:two_factor_enroll"), {"action": "finish"})

    assert antwort["Location"] == reverse("accounts:login_2fa")
    assert not _angemeldet(client)


def test_aktives_secret_wird_erkannt() -> None:
    user = _konto()
    service = TwoFactorService()
    assert not service.is_active_secret(user, "ABC")
    secret = _faktor(user)
    user = User.objects.get(pk=user.pk)

    assert service.is_active_secret(user, secret)
    assert not service.is_active_secret(user, service.generate_secret())
    assert not service.is_active_secret(user, "")
    TwoFactorDevice.objects.filter(user=user).update(is_confirmed=False)
    assert not service.is_active_secret(User.objects.get(pk=user.pk), secret)
