# SPDX-License-Identifier: AGPL-3.0-or-later
"""Die Benachrichtigungsliste verträgt ungültige Seitenangaben (keine Serverfehler)."""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse


@pytest.mark.django_db
@pytest.mark.parametrize("seite", ["x", "-3", "0", "1.5", ""])
def test_ungueltige_seite_ergibt_erste_seite(org: Any, make_member: Any, client_for: Any, seite: str) -> None:
    mitglied = make_member(org, ["dashboard.view"], email="mitglied@example.org")

    response = client_for(mitglied.user).get(
        reverse("work:notifications", kwargs={"org_slug": org.slug}), {"page": seite}
    )

    assert response.status_code == 200
    assert response.context["page"] == 1
