# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provisionierung: Die Antwort auf das Anlegen enthält keinen Einladungslink, der geht nur per Mail."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from django.core import mail
from django.test import Client

from apps.common.tests.factories import UserFactory
from apps.tenants.models import Organization

SCHLUESSEL = "test-provisioning-schluessel"


@pytest.mark.django_db
def test_antwort_ohne_einladungslink(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monkeypatch.setenv("PROVISIONING_API_KEY", SCHLUESSEL)
    cast(Any, UserFactory)(email="system@example.org", is_superuser=True, is_staff=True)

    response = Client().post(
        "/api/provisioning/organizations/",
        data=json.dumps({"name": "Fraktion Neu", "slug": "fraktion-neu", "admin_email": "vorsitz@example.org"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {SCHLUESSEL}",
    )

    assert response.status_code == 201
    daten = response.json()
    assert daten["slug"] == "fraktion-neu" and daten["invitation_sent"] is True
    assert "invitation_url" not in daten
    assert "/work/invitation/" not in response.content.decode()
    assert Organization.objects.filter(slug="fraktion-neu").exists()
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["vorsitz@example.org"]
    assert "/work/invitation/" in mail.outbox[0].body, "der Link steht in der Mail"
