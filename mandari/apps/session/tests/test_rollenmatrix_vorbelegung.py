# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechte-Matrix einer bearbeiteten Rolle ist serverseitig angekreuzt (#172).

Vorher kreuzte ein Inline-Skript die Rechte aus einer JSON-Liste an. Unter einer
Content-Security-Policy ohne Inline-Skripte (und ohne JavaScript) sah die Matrix dann leer
aus – wer speicherte, hätte der Rolle alle Rechte entzogen.
"""

from __future__ import annotations

import re
from typing import Any, cast

import pytest
from django.test import Client

from apps.common.tests.factories import UserFactory
from apps.session.models import SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db

ALLE_RECHTE = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]


def _rolle(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionRole:
    flags = {feld: feld in rechte for feld in ALLE_RECHTE}
    return SessionRole.objects.create(tenant=tenant, name=name, is_admin=admin, **flags)


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Matrix", slug="matrix")


@pytest.fixture
def admin_client(tenant: SessionTenant) -> Client:
    user = cast(Any, UserFactory)(email="admin-matrix@example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(_rolle(tenant, "Administration", admin=True))
    client = Client()
    client.force_login(user)
    return client


def _angekreuzt(html: str) -> set[str]:
    return set(re.findall(r'data-perm="(can_\w+)" checked', html))


def test_bearbeitete_rolle_zeigt_ihre_rechte(tenant: SessionTenant, admin_client: Client) -> None:
    rolle = _rolle(tenant, "Sachbearbeitung", "can_view_papers", "can_edit_papers")
    html = admin_client.get(f"/session/{tenant.slug}/settings/roles/?edit={rolle.pk}").content.decode()

    assert _angekreuzt(html) == {"can_view_papers", "can_edit_papers"}
    assert "granted.forEach" not in html


def test_neue_rolle_ohne_vorbelegung(tenant: SessionTenant, admin_client: Client) -> None:
    html = admin_client.get(f"/session/{tenant.slug}/settings/roles/").content.decode()
    assert 'data-perm="can_view_papers"' in html
    assert _angekreuzt(html) == set()
