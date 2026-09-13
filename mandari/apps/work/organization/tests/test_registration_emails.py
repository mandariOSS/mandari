# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Registrierung und Zugangs-Mails.

Geprüft werden die Selbstregistrierung mit E-Mail-Bestätigung, Freischalten und Ablehnen samt
Benachrichtigung, die Trennung offener Anfragen von deaktivierten Mitgliedern sowie der Versand
über das SMTP der Organisation (Passwort-Links ausgenommen).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import views as account_views
from apps.accounts.models import User
from apps.tenants.models import Membership
from apps.work.notifications.models import Notification, NotificationType
from apps.work.organization import selectors, services

pytestmark = pytest.mark.django_db

PASSWORD = "Registrierung-Test-2026!"
CONFIRM_PATH_RE = re.compile(r"/accounts/register/[\w-]+/bestaetigen/[\w-]+/")


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def reviewer(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.invite"], email="freigabe@example.org")


@pytest.fixture
def plain_member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="mitglied@example.org")


def open_registration(org: Any, *, auto_approve: bool = False, domains: list[str] | None = None) -> None:
    org.registration_enabled = True
    org.registration_auto_approve = auto_approve
    org.registration_email_domains = domains or []
    org.save()


def register(client: Client, org: Any, email: str = "neu@example.org") -> Any:
    return client.post(
        reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
        {"email": email, "first_name": "Eva", "last_name": "Muster", "password1": PASSWORD, "password2": PASSWORD},
    )


def mails_to(address: str) -> list[Any]:
    return [message for message in mail.outbox if address in message.to]


def subjects_to(address: str) -> list[str]:
    return [message.subject for message in mails_to(address)]


def confirm_path(message: Any) -> str:
    match = CONFIRM_PATH_RE.search(message.body)
    assert match, message.body
    return match.group(0)


def pending_membership(org: Any, email: str) -> Membership:
    user = User.objects.create_user(email=email, password=PASSWORD, first_name="Paul")  # type: ignore[no-untyped-call]
    return Membership.objects.create(
        user=user, organization=org, is_active=False, registration_requested_at=timezone.now()
    )


# ---------------------------------------------------------------------------
# Selbstregistrierung
# ---------------------------------------------------------------------------


def test_registrierung_verlangt_bestaetigung_und_informiert_freigebende(
    client: Client, org: Any, reviewer: Any, plain_member: Any
) -> None:
    open_registration(org)

    response = register(client, org)
    assert response.status_code == 200
    user = User.objects.get(email="neu@example.org")
    assert not Membership.objects.filter(user=user, organization=org).exists()
    assert "_auth_user_id" not in client.session
    assert subjects_to("neu@example.org") == [f"Bitte bestätige deine Registrierung bei {org.name}"]
    assert not mails_to("freigabe@example.org")

    path = confirm_path(mails_to("neu@example.org")[0])
    mail.outbox.clear()

    # Link-Scanner rufen per GET ab – das darf nichts auslösen
    assert client.get(path).status_code == 200
    assert not Membership.objects.filter(user=user, organization=org).exists()

    response = client.post(path)
    assert response.status_code == 200
    membership = Membership.objects.get(user=user, organization=org)
    assert membership.is_active is False
    assert membership.registration_requested_at is not None
    assert list(selectors.pending_registrations(org)) == [membership]
    user.refresh_from_db()
    assert user.email_verified is True

    assert subjects_to("neu@example.org") == [f"Deine Registrierung bei {org.name} ist eingegangen"]
    request_mails = mails_to("freigabe@example.org")
    assert [message.subject for message in request_mails] == [f"Neue Registrierungsanfrage für {org.name}"]
    assert request_mails[0].reply_to == ["neu@example.org"]
    assert not mails_to("mitglied@example.org")
    assert Notification.objects.filter(notification_type=NotificationType.REGISTRATION_REQUEST).count() == 1

    # Der Link ist nur einmal gültig
    assert client.post(path).status_code == 404


def test_automatische_freischaltung_nach_bestaetigung(client: Client, org: Any, reviewer: Any) -> None:
    open_registration(org, auto_approve=True)
    register(client, org)
    path = confirm_path(mails_to("neu@example.org")[0])
    mail.outbox.clear()

    response = client.post(path)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("accounts:login"))
    membership = Membership.objects.get(user__email="neu@example.org", organization=org)
    assert membership.is_active is True
    assert membership.registration_requested_at is None
    assert subjects_to("neu@example.org") == [f"Willkommen bei {org.name}"]
    assert f"/work/{org.slug}/" in mails_to("neu@example.org")[0].body
    assert not any(subject.startswith("Neue Registrierungsanfrage") for subject in subjects_to("freigabe@example.org"))


def test_fremde_domain_erzeugt_weder_konto_noch_mail(client: Client, org: Any) -> None:
    open_registration(org, domains=["volt.team"])
    response = register(client, org, email="neu@example.org")
    assert response.status_code == 200
    assert not User.objects.filter(email="neu@example.org").exists()
    assert not mail.outbox


def test_bestaetigungsmails_werden_gedrosselt(client: Client, org: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    open_registration(org)
    monkeypatch.setattr(account_views, "SELF_REGISTER_MAILS_PER_IP_PER_HOUR", 1)
    register(client, org, email="erste@example.org")
    response = register(client, org, email="zweite@example.org")
    assert response.status_code == 200
    assert not User.objects.filter(email="zweite@example.org").exists()
    assert subjects_to("erste@example.org")
    assert not mails_to("zweite@example.org")


# ---------------------------------------------------------------------------
# Freischalten, Ablehnen, Reaktivieren
# ---------------------------------------------------------------------------


def test_freischalten_informiert_die_person(client_for: Any, org: Any, reviewer: Any) -> None:
    pending = pending_membership(org, "anfrage@example.org")
    response = client_for(reviewer.user).post(
        reverse("work:member_approve", kwargs={"org_slug": org.slug, "membership_id": pending.id})
    )
    assert response.status_code == 302
    pending.refresh_from_db()
    assert pending.is_active is True
    assert pending.registration_requested_at is None
    assert subjects_to("anfrage@example.org") == [f"Dein Zugang zu {org.name} ist freigeschaltet"]


def test_ablehnen_schickt_begruendung_escaped(client_for: Any, org: Any, reviewer: Any) -> None:
    pending = pending_membership(org, "anfrage@example.org")
    response = client_for(reviewer.user).post(
        reverse("work:member_reject", kwargs={"org_slug": org.slug, "membership_id": pending.id}),
        {"reason": "<b>Nur für Mitglieder</b>"},
    )
    assert response.status_code == 302
    assert not Membership.objects.filter(id=pending.id).exists()
    message = mails_to("anfrage@example.org")[0]
    assert message.subject == f"Deine Registrierungsanfrage bei {org.name}"
    assert "Nur für Mitglieder" in message.body
    html = message.alternatives[0][0]
    assert "&lt;b&gt;Nur für Mitglieder&lt;/b&gt;" in html
    assert "<b>Nur" not in html


def test_deaktivierte_mitglieder_sind_keine_anfragen(
    client_for: Any, org: Any, reviewer: Any, plain_member: Any
) -> None:
    services.deactivate_member(org, plain_member, reviewer.user)
    assert list(selectors.pending_registrations(org)) == []
    assert list(selectors.inactive_members(org)) == [plain_member]

    client = client_for(reviewer.user)
    for name in ("work:member_approve", "work:member_reject"):
        response = client.post(reverse(name, kwargs={"org_slug": org.slug, "membership_id": plain_member.id}))
        assert response.status_code == 404
    assert Membership.objects.filter(id=plain_member.id, is_active=False).exists()


def test_reaktivierung_informiert_per_mail(org: Any, reviewer: Any, plain_member: Any) -> None:
    services.deactivate_member(org, plain_member, reviewer.user)
    assert services.reactivate_member(org, plain_member) is True
    assert subjects_to("mitglied@example.org") == [f"Dein Zugang zu {org.name} ist wieder aktiv"]


# ---------------------------------------------------------------------------
# Versandweg der Organisation
# ---------------------------------------------------------------------------


def use_own_smtp(org: Any) -> None:
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.example.org"
    org.smtp_from_email = "fraktion@example.org"
    org.smtp_from_name = "Fraktion Test"
    org.save()


def test_einladung_laeuft_ueber_smtp_der_organisation(org: Any, reviewer: Any) -> None:
    use_own_smtp(org)
    with mock.patch(
        "apps.common.org_email.get_organization_connection",
        return_value=get_connection("django.core.mail.backends.locmem.EmailBackend"),
    ) as connection:
        services.invite_member(org, reviewer.user, "eingeladen@example.org", [], "Hallo <script>")
    connection.assert_called_once()
    message = mails_to("eingeladen@example.org")[0]
    assert message.from_email == "Fraktion Test <fraktion@example.org>"
    html = message.alternatives[0][0]
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


class FailingBackend(BaseEmailBackend):
    """SMTP der Organisation nicht erreichbar."""

    def send_messages(self, email_messages: Any) -> int:
        raise OSError("SMTP nicht erreichbar")


def test_smtp_fehler_faellt_auf_mandari_standardversand_zurueck(org: Any, reviewer: Any) -> None:
    use_own_smtp(org)
    assert org.smtp_fallback_to_mandari is True
    pending = pending_membership(org, "anfrage@example.org")
    with mock.patch("apps.common.org_email.get_organization_connection", return_value=FailingBackend()):
        assert services.approve_registration(pending, actor=reviewer) is True
    message = mails_to("anfrage@example.org")[0]
    assert "fraktion@example.org" not in message.from_email


def test_ohne_rueckfall_meldet_work_den_versandfehler(client_for: Any, org: Any, reviewer: Any) -> None:
    use_own_smtp(org)
    org.smtp_fallback_to_mandari = False
    org.save()
    pending = pending_membership(org, "anfrage@example.org")
    with mock.patch("apps.common.org_email.get_organization_connection", return_value=FailingBackend()):
        response = client_for(reviewer.user).post(
            reverse("work:member_approve", kwargs={"org_slug": org.slug, "membership_id": pending.id})
        )
    pending.refresh_from_db()
    assert pending.is_active is True
    assert not mails_to("anfrage@example.org")
    assert any("nicht versendet" in str(message) for message in get_messages(response.wsgi_request))


def test_passwort_links_fuer_gaeste_laufen_nie_ueber_fremdes_smtp(org: Any, make_member: Any) -> None:
    inviter = make_member(org, ["guests.invite"], email="einladend@example.org", is_admin=True)
    use_own_smtp(org)
    with mock.patch("apps.common.org_email.get_organization_connection") as connection:
        services.invite_guest(
            org, inviter, email="gast@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
        )
    connection.assert_not_called()
    message = mails_to("gast@example.org")[0]
    assert "fraktion@example.org" not in message.from_email
    assert "/accounts/password-reset/" in message.body
