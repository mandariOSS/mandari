# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldungen erscheinen im Work-Portal genau einmal – als Toast.

``work/base_work.html`` gibt alle Meldungen an die Toast-Anzeige weiter; Seiten zeigen sie
nicht zusätzlich im Inhalt an. Eine Seite mit Meldungen trägt eine Kennung ihres Stands (``data-marke``): Holt der
Browser sie aus dem Verlauf zurück, zeigt die Anzeige dieselben Meldungen nicht noch einmal
(``frontend/js/alpine/toast.ts``).
"""

from __future__ import annotations

import re
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


def _seite(org: Any, client: Any, *, meldung: bool) -> str:
    if meldung:
        speicher: Any = CookieStorage(HttpRequest())
        client.cookies[speicher.cookie_name] = speicher._encode([Message(constants.SUCCESS, MELDUNG)])
    response = client.get(reverse("work:profile", kwargs={"org_slug": org.slug}))
    assert response.status_code == 200
    return str(response.content.decode())


@pytest.mark.django_db
def test_seite_mit_meldung_traegt_kennung_ihres_stands(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["dashboard.view", "organization.edit"], email="mitglied@example.org")
    client = client_for(mitglied.user)
    muster = re.compile(r'x-data="toastManager" data-marke="(\d+\.\d{6})"')

    erste = muster.search(_seite(org, client, meldung=True))
    zweite = muster.search(_seite(org, client, meldung=True))

    assert erste and zweite
    # Jede Antwort des Servers ist ein eigener Stand; nur dieselbe Antwort aus dem Verlauf gleicht sich
    assert erste.group(1) != zweite.group(1)


@pytest.mark.django_db
def test_seite_ohne_meldung_bleibt_ohne_kennung(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["dashboard.view", "organization.edit"], email="mitglied@example.org")

    html = _seite(org, client_for(mitglied.user), meldung=False)

    assert 'x-data="toastManager" @show-toast.window' in html
    assert "data-marke" not in html
