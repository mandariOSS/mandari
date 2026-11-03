# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangslinks laufen nie über das eigene SMTP einer Organisation.

Wer den Einladungs- oder Bestätigungslink einlöst, gilt als Inhaberin des Postfachs
(``email_verified``). Das trägt nur, wenn der Link ausschließlich im Postfach ankommt. Den
SMTP-Server einer Organisation trägt deren Verwaltung selbst ein; über ihn dürfen solche Links
deshalb nicht laufen. Dazu gehört, dass eine Einladung ein vorhandenes Konto auch in anderer
Schreibweise der Adresse findet und nicht neben ihm ein zweites anlegt.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.mail_backends import build_backend
from apps.tenants.models import Membership, UserInvitation
from apps.work.organization import selectors, services

pytestmark = pytest.mark.django_db

PASSWORT = "Zugangslink-Test-2026!"
BESTANDSPASSWORT = "Bestandskonto-Test-2026!"


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def eigenes_smtp(org: Any) -> Iterator[mock.MagicMock]:
    """Organisation mit eigenem SMTP; jede Verbindung dorthin wird erfasst statt aufgebaut."""
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.example.org"
    org.smtp_from_email = "fraktion@example.org"
    org.smtp_from_name = "Fraktion Test"
    org.contact_email = "kontakt@example.org"
    org.save()
    with mock.patch(
        "apps.common.org_email.get_organization_connection",
        return_value=build_backend("django.core.mail.backends.locmem.EmailBackend"),
    ) as verbindung:
        yield verbindung


@pytest.fixture
def einladend(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.invite"], email="einladend@example.org", is_admin=True)


def _an(adresse: str) -> list[Any]:
    return [nachricht for nachricht in mail.outbox if adresse in nachricht.to]


def _registrieren(client: Client, org: Any, email: str) -> Any:
    return client.post(
        reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
        {"email": email, "first_name": "Eva", "last_name": "Muster", "password1": PASSWORT, "password2": PASSWORT},
    )


# ---------------------------------------------------------------------------
# Versandweg der Links
# ---------------------------------------------------------------------------


def test_einladung_erreicht_das_smtp_der_organisation_nicht(
    org: Any, einladend: Any, eigenes_smtp: mock.MagicMock
) -> None:
    services.invite_member(org, einladend.user, "eingeladen@example.org", [], "Hallo")

    eigenes_smtp.assert_not_called()
    [nachricht] = _an("eingeladen@example.org")
    assert "/work/invitation/" in nachricht.body
    assert "fraktion@example.org" not in nachricht.from_email
    # Antworten erreichen weiterhin die Organisation
    assert nachricht.reply_to == ["kontakt@example.org"]


def test_erneute_einladung_erreicht_das_smtp_der_organisation_nicht(
    org: Any, einladend: Any, eigenes_smtp: mock.MagicMock
) -> None:
    invitation = UserInvitation.create_for_organization(
        organization=org, email="eingeladen@example.org", invited_by=einladend.user
    )

    services.resend_invitation(org, invitation)

    eigenes_smtp.assert_not_called()
    assert len(_an("eingeladen@example.org")) == 1


def test_bestaetigungslink_erreicht_das_smtp_der_organisation_nicht(
    client: Client, org: Any, eigenes_smtp: mock.MagicMock
) -> None:
    org.registration_enabled = True
    org.save()

    assert _registrieren(client, org, "neu@example.org").status_code == 200

    eigenes_smtp.assert_not_called()
    [nachricht] = _an("neu@example.org")
    assert f"/accounts/register/{org.slug}/bestaetigen/" in nachricht.body
    assert "fraktion@example.org" not in nachricht.from_email


def test_andere_zugangsmails_nutzen_weiter_das_smtp_der_organisation(
    org: Any, einladend: Any, eigenes_smtp: mock.MagicMock
) -> None:
    """Mails ohne Zugangslink bleiben auf dem gewählten Weg der Organisation."""
    konto = User.objects.create_user(email="anfrage@example.org", password=PASSWORT)  # type: ignore[no-untyped-call]
    anfrage = Membership.objects.create(
        user=konto, organization=org, is_active=False, registration_requested_at=timezone.now()
    )

    assert services.approve_registration(anfrage, actor=einladend) is True

    eigenes_smtp.assert_called_once()
    [nachricht] = _an("anfrage@example.org")
    assert nachricht.from_email == "Fraktion Test <fraktion@example.org>"


# ---------------------------------------------------------------------------
# Vorhandene Konten in anderer Schreibweise
# ---------------------------------------------------------------------------


def test_konto_wird_ohne_beachtung_der_schreibweise_gefunden() -> None:
    konto = User.objects.create_user(email="Otto.Gross@example.org", password=BESTANDSPASSWORT)  # type: ignore[no-untyped-call]

    assert selectors.find_user_by_email("otto.gross@example.org") == konto
    assert selectors.find_user_by_email("OTTO.GROSS@example.org") == konto


def test_einladung_in_kleinschreibung_legt_kein_zweites_konto_an(org: Any, einladend: Any) -> None:
    bestand = User.objects.create_user(email="Otto.Gross@example.org", password=BESTANDSPASSWORT)  # type: ignore[no-untyped-call]
    invitation = UserInvitation.create_for_organization(
        organization=org, email="otto.gross@example.org", invited_by=einladend.user
    )
    client = Client()

    # Der Link führt zur Anmeldung des vorhandenen Kontos, nicht zur Registrierung
    antwort = client.get(reverse("work:accept_invitation", kwargs={"token": invitation.plain_token}))
    assert antwort.status_code == 302
    assert antwort["Location"].startswith(reverse("accounts:login"))

    # Auch direkt lässt sich daneben kein zweites Konto anlegen
    antwort = client.get(reverse("accounts:register"))
    assert antwort.status_code == 302
    assert antwort["Location"].startswith(reverse("accounts:login"))
    antwort = client.post(
        reverse("accounts:register"),
        {"first_name": "Fremd", "last_name": "Person", "password1": PASSWORT, "password2": PASSWORT},
    )
    assert antwort.status_code == 302
    assert antwort["Location"].startswith(reverse("accounts:login"))

    assert list(User.objects.filter(email__iexact="otto.gross@example.org")) == [bestand]
    bestand.refresh_from_db()
    assert bestand.check_password(BESTANDSPASSWORT)
    assert "_auth_user_id" not in client.session


def test_registrierung_per_einladung_weist_konto_gleicher_schreibweise_ab(org: Any, einladend: Any) -> None:
    bestand = User.objects.create_user(email="otto.gross@example.org", password=BESTANDSPASSWORT)  # type: ignore[no-untyped-call]
    invitation = UserInvitation.create_for_organization(
        organization=org, email="otto.gross@example.org", invited_by=einladend.user
    )
    client = Client()
    session = client.session
    session["pending_invitation_token"] = invitation.plain_token
    session.save()

    antwort = client.post(
        reverse("accounts:register"),
        {"first_name": "Fremd", "last_name": "Person", "password1": PASSWORT, "password2": PASSWORT},
    )

    assert antwort.status_code == 302
    assert antwort["Location"].startswith(reverse("accounts:login"))
    bestand.refresh_from_db()
    assert bestand.check_password(BESTANDSPASSWORT)
    assert User.objects.filter(email__iexact="otto.gross@example.org").count() == 1
