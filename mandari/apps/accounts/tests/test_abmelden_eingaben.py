# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abmelden löscht die im Browser gesicherten, ungespeicherten Eingaben (#854).

Die Seite „Abgemeldet“ trägt die Markierung ``data-eingaben-loeschen``; frontend/js/main.ts löscht daraufhin alle
Sicherungen (frontend/js/eingaben-sicherung.ts). Wer noch angemeldet ist und die Seite direkt aufruft (etwa über
einen fremden Link), verliert nichts.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.common.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

MARKIERUNG = "data-eingaben-loeschen"


def test_nach_dem_abmelden_wird_die_sicherung_geloescht(client: Client) -> None:
    client.force_login(UserFactory())
    antwort = client.post(reverse("accounts:logout"), follow=True)
    assert antwort.redirect_chain[-1][0] == reverse("accounts:logged_out")
    assert MARKIERUNG in antwort.content.decode()


def test_angemeldet_loescht_die_seite_nichts(client: Client) -> None:
    client.force_login(UserFactory())
    antwort = client.get(reverse("accounts:logged_out"))
    assert antwort.status_code == 200
    assert MARKIERUNG not in antwort.content.decode()
