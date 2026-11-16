# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wer eine Einladung in den Sitzungsdienst mit einem neuen Konto annimmt, ist danach angemeldet. Diese Sitzung steht
wie jede andere in der Geräteliste des Kontos und lässt sich dort beenden.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User, UserSession
from apps.accounts.services import SessionService
from apps.common.tests.factories import UserFactory
from apps.session.models import SessionInvitation, SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db

PASSWORT = "Sicheres-Passwort-2026!"


@pytest.fixture
def einladung() -> SessionInvitation:
    tenant = SessionTenant.objects.create(name="Musterstadt", slug="musterstadt")
    rolle = SessionRole.objects.create(tenant=tenant, name="Sitzungsdienst", can_manage_users=True)
    einladend = SessionUser.objects.create(user=cast(Any, UserFactory)(email="verwaltung@example.org"), tenant=tenant)
    einladend.roles.add(rolle)
    return cast(
        SessionInvitation,
        SessionInvitation.create_for_tenant(tenant=tenant, email="neu@example.org", invited_by=einladend),
    )


def _annehmen(einladung: SessionInvitation) -> Client:
    client = Client()
    antwort = client.post(
        reverse("session:invitation_accept", kwargs={"token": einladung.plain_token}),
        {"first_name": "Nora", "last_name": "Neu", "password": PASSWORT, "password_confirm": PASSWORT},
    )
    assert antwort.status_code == 302
    assert "_auth_user_id" in client.session, "nach der Annahme angemeldet"
    return client


def test_sitzung_aus_der_einladung_steht_in_der_geraeteliste(einladung: SessionInvitation) -> None:
    client = _annehmen(einladung)

    konto = User.objects.get(email="neu@example.org")
    assert list(UserSession.objects.filter(user=konto).values_list("session_key", flat=True)) == [
        client.session.session_key
    ]


def test_sitzung_aus_der_einladung_laesst_sich_beenden(einladung: SessionInvitation) -> None:
    client = _annehmen(einladung)
    konto = User.objects.get(email="neu@example.org")

    assert SessionService.revoke_session(konto, str(client.session.session_key))

    assert "_auth_user_id" not in client.session
