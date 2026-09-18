# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mehrere Mandanten je Nutzer (z. B. Leitstelle für mehrere Bezirke): Wechsel in der Seitenleiste,
und Verwaltungsnutzer ohne Fraktion landen nach dem Login in ihrem Mandanten.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client

from apps.accounts.views import LoginView
from apps.common.tests.factories import UserFactory
from apps.session.models import SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db


def _mitglied(user: Any, tenant: SessionTenant) -> None:
    rolle, _ = SessionRole.objects.get_or_create(tenant=tenant, name="Admin", defaults={"is_admin": True})
    SessionUser.objects.create(user=user, tenant=tenant).roles.add(rolle)


def test_wechsel_nur_bei_mehreren_mandanten() -> None:
    nord = SessionTenant.objects.create(name="Bezirk Nord", slug="nord")
    sued = SessionTenant.objects.create(name="Bezirk Süd", slug="sued")
    leitstelle, einzeln = cast(Any, UserFactory)(), cast(Any, UserFactory)()
    _mitglied(leitstelle, nord)
    _mitglied(leitstelle, sued)
    _mitglied(einzeln, nord)

    client = Client()
    client.force_login(leitstelle)
    seite = client.get("/session/nord/").content.decode()
    assert "Mandant wechseln" in seite and "/session/sued/" in seite

    client = Client()
    client.force_login(einzeln)
    assert "Mandant wechseln" not in client.get("/session/nord/").content.decode()


def test_verwaltungsnutzer_landet_im_mandanten() -> None:
    nord = SessionTenant.objects.create(name="Bezirk Nord", slug="nord")
    nutzer = cast(Any, UserFactory)()
    _mitglied(nutzer, nord)
    request = type("R", (), {"user": nutzer, "get_host": lambda self: "testserver", "is_secure": lambda self: False})()
    assert cast(Any, LoginView()).get_success_url(request) == "/session/nord/"
