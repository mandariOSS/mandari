# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einladungen in den Sitzungsdienst (Issue #239): Versand als HTML mit Textfassung im
gemeinsamen Layout, Absendername nennt den Mandanten, Freitexte werden escaped, und
eine offene Einladung lässt sich erneut senden (verlängert die Gültigkeit).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.common.tokens import hash_token
from apps.session.models import SessionInvitation, SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt <Musterstadt> & Co.", slug="musterstadt")


@pytest.fixture
def verwaltung(tenant: SessionTenant) -> tuple[Client, SessionUser]:
    """Angemeldete Person mit dem Recht, Benutzer zu verwalten."""
    role = SessionRole.objects.create(tenant=tenant, name="Sitzungsdienst", can_manage_users=True)
    user = cast(Any, UserFactory)(email="verwaltung@example.org", first_name="Erika", last_name="Muster")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client, session_user


def _einladen(client: Client, tenant: SessionTenant, email: str) -> Any:
    return client.post(
        reverse("session:user_invite", kwargs={"tenant_slug": tenant.slug}), {"email": email}, follow=True
    )


def test_einladung_kommt_als_html_mit_text_im_layout(
    tenant: SessionTenant, verwaltung: tuple[Client, SessionUser]
) -> None:
    client, _ = verwaltung

    _einladen(client, tenant, "neu@example.org")

    assert len(mail.outbox) == 1
    nachricht = cast(Any, mail.outbox[0])
    einladung = SessionInvitation.objects.get(email="neu@example.org")
    assert nachricht.to == ["neu@example.org"]
    assert "Musterstadt" in nachricht.subject
    assert nachricht.alternatives, "HTML-Fassung fehlt"
    html = str(nachricht.alternatives[0][0])
    treffer = re.search(r"/session/invite/([^/\s\"]+)/", nachricht.body)
    assert treffer is not None, "Annahme-Link fehlt"
    token = treffer.group(1)
    assert token in html
    # In der Datenbank steht nur der Hash des Tokens aus der Mail
    assert einladung.token == hash_token(token) != token
    assert "Einladung annehmen" in html
    assert 'style="' in html, "Basis-Layout mit Inliner fehlt"
    # Freitexte werden escaped, der Mandant steht im Absendernamen
    assert "&lt;Musterstadt&gt; &amp; Co." in html and "<Musterstadt>" not in html
    assert "Erika Muster" in html
    assert nachricht.from_email.startswith('"Stadt <Musterstadt> & Co. über mandari"')


def test_offene_einladung_erneut_senden_verlaengert_gueltigkeit(
    tenant: SessionTenant, verwaltung: tuple[Client, SessionUser]
) -> None:
    client, session_user = verwaltung
    einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="wieder@example.org", invited_by=session_user)
    einladung.expires_at = timezone.now() + timedelta(hours=2)
    einladung.save()
    erster_link = reverse("session:invitation_accept", kwargs={"token": einladung.plain_token})

    response = client.post(
        reverse("session:invitation_resend", kwargs={"tenant_slug": tenant.slug, "invitation_id": einladung.id}),
        follow=True,
    )

    assert response.status_code == 200
    einladung.refresh_from_db()
    assert einladung.expires_at > timezone.now() + timedelta(days=6)
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["wieder@example.org"]
    assert "erneut versendet" in response.content.decode()
    # Gespeichert ist nur der Hash: Die neue Mail bringt einen neuen Link, der alte gilt nicht mehr
    treffer = re.search(r"/session/invite/([^/\s\"]+)/", str(mail.outbox[0].body))
    assert treffer is not None
    assert einladung.token == hash_token(treffer.group(1))
    assert Client().get(erster_link).status_code == 404
    assert Client().get(reverse("session:invitation_accept", kwargs={"token": treffer.group(1)})).status_code == 200


def test_angenommene_einladung_laesst_sich_nicht_erneut_senden(
    tenant: SessionTenant, verwaltung: tuple[Client, SessionUser]
) -> None:
    client, session_user = verwaltung
    einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="fertig@example.org", invited_by=session_user)
    einladung.accepted_at = timezone.now()
    einladung.save()

    response = client.post(
        reverse("session:invitation_resend", kwargs={"tenant_slug": tenant.slug, "invitation_id": einladung.id})
    )

    assert response.status_code == 404
    assert mail.outbox == []


def test_erneut_senden_braucht_benutzerverwaltung(tenant: SessionTenant) -> None:
    role = SessionRole.objects.create(tenant=tenant, name="Lesen", can_manage_users=False)
    user = cast(Any, UserFactory)(email="lesend@example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    einladung = SessionInvitation.create_for_tenant(tenant=tenant, email="x@example.org", invited_by=session_user)
    client = Client()
    client.force_login(user)

    response = client.post(
        reverse("session:invitation_resend", kwargs={"tenant_slug": tenant.slug, "invitation_id": einladung.id})
    )

    assert response.status_code in (302, 403)
    assert mail.outbox == []
