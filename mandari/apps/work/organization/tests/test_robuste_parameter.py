# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ungültige IDs aus Formularen führen zu einer Meldung, nicht zu einem Serverfehler."""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_vertrauenswuerdiges_geraet_mit_ungueltiger_id(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["dashboard.view"], email="mitglied@example.org")

    response = client_for(mitglied.user).post(
        reverse("work:security", kwargs={"org_slug": org.slug}),
        {"action": "remove_trusted_device", "device_id": "keine-uuid"},
    )

    assert response.status_code == 302
