# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweis auf neue Benachrichtigungen: jede einmal, nicht bei jedem Seitenaufruf.

Das Skript der Seite (``static/js/notifications.js``) merkt sich im Browser den Zeitpunkt der neuesten schon
gezeigten Benachrichtigung und fragt mit ``?since=`` nur nach neueren. Darauf verlässt es sich: Genau dieser
Zeitpunkt kommt nicht noch einmal, Neueres schon, die neueste steht vorn. Bisher blendete jede Seite die neueste
ungelesene erneut ein.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.work.notifications.models import Notification, NotificationType


def _benachrichtigung(mitglied: Any, titel: str) -> Notification:
    # Direkt angelegt, am Hub vorbei: die gemerkte Zahl der ungelesenen gilt dann nicht mehr
    cache.clear()
    return Notification.objects.create(
        recipient=mitglied, notification_type=NotificationType.TASK_ASSIGNED, title=titel, message="Bitte prüfen"
    )


@pytest.mark.django_db
def test_since_liefert_nur_neuere_als_den_gemerkten_stand(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["dashboard.view"], email="mitglied@example.org")
    client = client_for(mitglied.user)
    url = reverse("work:notification_latest", kwargs={"org_slug": org.slug})
    _benachrichtigung(mitglied, "Erste")

    erste = client.get(url).json()
    assert [eintrag["title"] for eintrag in erste["notifications"]] == ["Erste"]
    # Der Browser merkt sich den Zeitpunkt so, wie die Antwort ihn nennt
    stand = erste["notifications"][0]["created_at"]

    # Jeder weitere Seitenaufruf: nichts einzublenden, obwohl die Benachrichtigung ungelesen bleibt
    for _ in range(2):
        wieder = client.get(url, {"since": stand}).json()
        assert wieder["notifications"] == []
        assert wieder["count"] == 1

    _benachrichtigung(mitglied, "Zweite")
    _benachrichtigung(mitglied, "Dritte")
    neu = client.get(url, {"since": stand}).json()
    assert [eintrag["title"] for eintrag in neu["notifications"]] == ["Dritte", "Zweite"]
    assert neu["count"] == 3
    assert client.get(url, {"since": neu["notifications"][0]["created_at"]}).json()["notifications"] == []


@pytest.mark.django_db
def test_unlesbarer_stand_liefert_alle_ungelesenen(org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["dashboard.view"], email="mitglied@example.org")
    client = client_for(mitglied.user)
    url = reverse("work:notification_latest", kwargs={"org_slug": org.slug})
    _benachrichtigung(mitglied, "Erste")

    antwort = client.get(url, {"since": "kein Zeitpunkt"})

    assert antwort.status_code == 200
    assert [eintrag["title"] for eintrag in antwort.json()["notifications"]] == ["Erste"]
