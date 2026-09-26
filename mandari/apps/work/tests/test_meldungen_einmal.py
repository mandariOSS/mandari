# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldungen erscheinen im Work-Portal genau einmal – als Toast.

``work/base_work.html`` gibt alle Meldungen an die Toast-Anzeige weiter; Seiten zeigen sie
nicht zusätzlich im Inhalt an.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.contrib.messages.storage.cookie import CookieStorage
from django.http import HttpRequest
from django.urls import reverse

# Ohne Zeichen, die escapejs für die Toast-Daten umschreibt
MELDUNG = "Einstellung gespeichert Siebzehn"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    [
        "profile",
        "security",
        "profile_absence",
        "profile_requests",
        "profile_visibility",
        "profile_committees",
        "document_topic_list",
    ],
)
def test_meldung_erscheint_nur_einmal(org: Any, make_member: Any, client_for: Any, url_name: str) -> None:
    mitglied = make_member(org, ["dashboard.view", "organization.edit"], email="mitglied@example.org")
    client = client_for(mitglied.user)
    speicher: Any = CookieStorage(HttpRequest())
    client.cookies[speicher.cookie_name] = speicher._encode([Message(constants.SUCCESS, MELDUNG)])

    response = client.get(reverse(f"work:{url_name}", kwargs={"org_slug": org.slug}))

    assert response.status_code == 200
    assert response.content.decode().count(MELDUNG) == 1
