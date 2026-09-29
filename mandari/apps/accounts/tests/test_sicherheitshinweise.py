# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitshinweise (SecurityNotification): angelegt, auf der Sicherheitsseite angezeigt und bei
wichtigen Ereignissen per E-Mail gemeldet.

Vorher wurden die Hinweise nur geschrieben – keine Oberfläche las sie, keine Mail ging hinaus.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from io import StringIO
from typing import Any, cast

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import security_notifications
from apps.accounts.models import SecurityNotification, User, UserSession, WebAuthnCredential
from apps.accounts.services import PasswordService, SessionService, TwoFactorService

pytestmark = pytest.mark.django_db

PASSWORD = "Sicheres-Passwort-2026!"
NEW_PASSWORD = "Noch-sichereres-Passwort-2026!"

CaptureCallbacks = Callable[..., Any]


def make_user(email: str = "person@example.org") -> User:
    return cast(User, User.objects.create_user(email=email, password=PASSWORD, first_name="Pat"))  # type: ignore[no-untyped-call]


def enable_totp(user: User) -> None:
    service = TwoFactorService()
    secret = service.setup_2fa(user)["secret"]
    code = service._get_totp_code(secret, int(time.time() // service.TOTP_INTERVAL))
    assert service.confirm_2fa(user, code)


def hinweise(user: User) -> list[SecurityNotification]:
    return list(SecurityNotification.objects.filter(user=user).order_by("created_at"))


class TestMail:
    def test_passwortwechsel_meldet_per_mail(self, django_capture_on_commit_callbacks: CaptureCallbacks) -> None:
        user = make_user()
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            assert PasswordService.change_password(user, PASSWORD, NEW_PASSWORD)[0]

        [hinweis] = hinweise(user)
        assert hinweis.notification_type == "password_changed"
        assert hinweis.email_sent is True
        [nachricht] = mail.outbox
        assert nachricht.to == ["person@example.org"]
        assert "Passwort geändert" in nachricht.subject
        assert reverse("accounts:password_reset") in nachricht.body
        assert NEW_PASSWORD not in nachricht.body

    def test_zweiter_faktor_ein_und_aus(self, django_capture_on_commit_callbacks: CaptureCallbacks) -> None:
        user = make_user()
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            enable_totp(user)
            assert TwoFactorService().disable_2fa(user)

        assert [h.notification_type for h in hinweise(user)] == ["2fa_enabled", "2fa_disabled"]
        assert len(mail.outbox) == 2

    def test_passwort_link_meldet_zuruecksetzen(self, django_capture_on_commit_callbacks: CaptureCallbacks) -> None:
        user = make_user()
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        client = Client()
        antwort = client.get(reverse("accounts:password_reset_confirm", kwargs={"uidb64": uid, "token": token}))
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            client.post(
                antwort["Location"],
                {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
                REMOTE_ADDR="198.51.100.7",
                HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0)",
            )

        [hinweis] = hinweise(user)
        assert hinweis.title == "Passwort zurückgesetzt"
        assert hinweis.ip_address == "198.51.100.7"
        assert hinweis.device_info == "Windows PC"
        [nachricht] = mail.outbox
        assert "198.51.100.7" in nachricht.body

    def test_sicherheitsschluessel_entfernt(
        self, client_for: Callable[[User], Client], django_capture_on_commit_callbacks: CaptureCallbacks
    ) -> None:
        user = make_user()
        enable_totp(user)
        schluessel = WebAuthnCredential.objects.create(
            user=user, name="Stick", credential_id="abc", public_key=b"x", sign_count=0
        )
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            client_for(user).post(
                reverse("accounts:security_keys"),
                {"action": "remove", "credential_id": str(schluessel.pk), "password": PASSWORD},
            )

        assert hinweise(user)[-1].notification_type == "device_removed"
        assert len(mail.outbox) == 1

    def test_zuruecksetzen_durch_administration(self, django_capture_on_commit_callbacks: CaptureCallbacks) -> None:
        user = make_user()
        enable_totp(user)
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            call_command("reset_two_factor", user.email, "--reason", "Ticket 1", "--yes", stdout=StringIO())

        hinweis = hinweise(user)[-1]
        assert (hinweis.notification_type, hinweis.title) == ("2fa_disabled", "Zweiter Faktor zurückgesetzt")
        assert len(mail.outbox) == 1

    def test_beendete_sitzung_ohne_mail(self, django_capture_on_commit_callbacks: CaptureCallbacks) -> None:
        user = make_user()
        UserSession.objects.create(user=user, session_key="k1", expires_at="2099-01-01T00:00:00Z")
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            assert SessionService.revoke_session(user, "k1")

        assert [h.notification_type for h in hinweise(user)] == ["session_revoked"]
        assert mail.outbox == []

    def test_versandfehler_verhindert_aenderung_nicht(
        self, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: CaptureCallbacks
    ) -> None:
        def kaputt(*args: Any, **kwargs: Any) -> bool:
            raise ConnectionError("SMTP nicht erreichbar")

        monkeypatch.setattr("apps.common.email.send_email", kaputt)
        user = make_user()
        with django_capture_on_commit_callbacks(execute=True):
            assert PasswordService.change_password(user, PASSWORD, NEW_PASSWORD)[0]

        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD)
        [hinweis] = hinweise(user)
        assert hinweis.email_sent is False


class TestSicherheitsseite:
    def test_zeigt_hinweise_und_markiert_gelesen(
        self, org: Any, make_member: Any, client_for: Callable[[Any], Client]
    ) -> None:
        member = make_member(org, permissions=["dashboard.view"])
        security_notifications.notify(member.user, "password_changed", "Passwort geändert", "Geändert.")
        url = f"/work/{org.slug}/profile/security/"

        erste = client_for(member.user).get(url).content.decode()
        assert "Sicherheitshinweise" in erste
        assert "Passwort geändert" in erste
        assert ">Neu<" in _kompakt(erste)
        assert SecurityNotification.objects.get(user=member.user).is_read

        zweite = client_for(member.user).get(url).content.decode()
        assert "Passwort geändert" in zweite
        assert ">Neu<" not in _kompakt(zweite)


def _kompakt(html: str) -> str:
    return "".join(html.split())
