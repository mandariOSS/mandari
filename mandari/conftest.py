# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame pytest-Fixtures für das Django-Projekt.

Settings: mandari.settings_test (siehe pyproject.toml). Fabriken: apps.common.tests.factories.
"""

import pytest
from django.test import Client

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, RoleFactory, UserFactory


@pytest.fixture
def org(db):
    """Eine Organisation mit angelegtem Mandantenschlüssel."""
    return OrganizationFactory(name="Fraktion Test", slug="fraktion-test")


@pytest.fixture
def make_member(db):
    """Fabrik-Fixture: make_member(org, permissions=[...], email=...) → Membership mit Rolle."""

    def _make(organization, permissions=(), email=None, is_admin=False, **user_kwargs):
        user = UserFactory(email=email) if email else UserFactory(**user_kwargs)
        role = RoleFactory(organization=organization, permissions=list(permissions), is_admin=is_admin)
        return MembershipFactory(user=user, organization=organization, roles=[role])

    return _make


@pytest.fixture
def client_for(db):
    """Fabrik-Fixture: client_for(user) → eingeloggter Test-Client."""

    def _client(user):
        client = Client()
        client.force_login(user)
        return client

    return _client


@pytest.fixture(autouse=True)
def _robots_ohne_netz(request, monkeypatch):
    """Abrufe aus Django fragen vorher die robots.txt des Hosts (insight_core.services.robots).

    Ohne Fixture ``echte_robots`` antwortet jeder Host „keine robots.txt“ (HTTP 404, alles erlaubt), damit
    Tests nie ins Netz gehen und Abruftests keine robots.txt nachbilden müssen.
    """
    if "echte_robots" in request.fixturenames:
        return
    from insight_core.services import robots

    monkeypatch.setattr(robots, "_fetch", lambda url, agent=None: (404, b""))


@pytest.fixture
def echte_robots():
    """Echte robots.txt-Prüfung im Test; den Abruf (``robots._fetch``) bildet der Test selbst nach."""
