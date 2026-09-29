# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einrichtung des zweiten Faktors: Konto-Weg (/accounts/zwei-faktor/einrichten/) und Work-Profil
nutzen dieselbe Bestätigung (apps/accounts/second_factor.py) – gleiche Zählung, gleiches Protokoll.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts import second_factor
from apps.accounts.models import LoginAttempt, SecurityAuditLog
from apps.accounts.services import TwoFactorService

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cache_leeren() -> Any:
    cache.clear()
    yield
    cache.clear()


def _codes(secret: str) -> set[str]:
    service = TwoFactorService()
    jetzt = int(time.time() // service.TOTP_INTERVAL)
    return {service._get_totp_code(secret, jetzt + versatz) for versatz in (-1, 0, 1)}


def _falscher_code(secret: str) -> str:
    gueltig = _codes(secret)
    return next(f"{n:06d}" for n in range(1_000_000) if f"{n:06d}" not in gueltig)


def _richtiger_code(secret: str) -> str:
    service = TwoFactorService()
    return service._get_totp_code(secret, int(time.time() // service.TOTP_INTERVAL))


def _profil_einrichten(client: Client, url: str) -> str:
    antwort = client.post(url, {"action": "setup_2fa"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    return str(antwort.json()["secret"])


class TestProfil:
    def test_fehlversuche_werden_gezaehlt_protokolliert_und_begrenzt(
        self, org: Any, make_member: Any, client_for: Callable[[Any], Client]
    ) -> None:
        user = make_member(org, permissions=["dashboard.view"]).user
        client = client_for(user)
        url = f"/work/{org.slug}/profile/security/"
        secret = _profil_einrichten(client, url)

        for _ in range(second_factor.MAX_FAILURES):
            client.post(url, {"action": "confirm_2fa", "code": _falscher_code(secret)})

        assert LoginAttempt.objects.filter(email=user.email, failure_reason="invalid_2fa").count() == (
            second_factor.MAX_FAILURES
        )
        assert (
            SecurityAuditLog.objects.filter(user_ref=user.pk, event="login_failed").count()
            == second_factor.MAX_FAILURES
        )

        # Danach wird auch ein richtiger Code bis zum Ablauf des Zeitfensters abgewiesen
        client.post(url, {"action": "confirm_2fa", "code": _richtiger_code(secret)})
        assert not TwoFactorService().is_2fa_enabled(user)

    def test_richtiger_code_bestaetigt_ohne_einrichtungsdaten_in_der_sitzung(
        self, org: Any, make_member: Any, client_for: Callable[[Any], Client]
    ) -> None:
        user = make_member(org, permissions=["dashboard.view"]).user
        client = client_for(user)
        url = f"/work/{org.slug}/profile/security/"
        secret = _profil_einrichten(client, url)
        assert "2fa_setup" not in client.session

        client.post(url, {"action": "confirm_2fa", "code": _richtiger_code(secret)})

        assert TwoFactorService().is_2fa_enabled(user)
        assert not LoginAttempt.objects.filter(email=user.email, was_successful=False).exists()


def test_zaehlung_gilt_ueber_beide_wege(org: Any, make_member: Any, client_for: Callable[[Any], Client]) -> None:
    """Fehlversuche im Konto-Weg sperren auch die Bestätigung im Profil."""
    user = make_member(org, permissions=["dashboard.view"]).user
    client = client_for(user)
    konto_url = reverse("accounts:two_factor_enroll")
    client.get(konto_url)
    secret = client.session["auth_2fa_enroll"]["secret"]
    for _ in range(second_factor.MAX_FAILURES):
        client.post(konto_url, {"code": _falscher_code(secret)})

    profil_url = f"/work/{org.slug}/profile/security/"
    neues_secret = _profil_einrichten(client, profil_url)
    client.post(profil_url, {"action": "confirm_2fa", "code": _richtiger_code(neues_secret)})

    assert not TwoFactorService().is_2fa_enabled(user)
