# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abmelden löscht die im Browser gesicherten, ungespeicherten Eingaben (#854).

Die Seite „Abgemeldet“ trägt die Markierung ``data-eingaben-loeschen``; frontend/js/main.ts löscht daraufhin alle
Sicherungen (frontend/js/eingaben-sicherung.ts). Wer noch angemeldet ist und die Seite direkt aufruft (etwa über
einen fremden Link), verliert nichts.

Endet eine Anmeldung ohne diese Seite (Sitzung anderswo beendet, Passwort geändert) und meldet sich danach ein
anderes Konto im selben Browser an, tragen die Seiten von Work die Kennung des angemeldeten Kontos
(``data-konto`` am body); main.ts löscht dann die Sicherungen aller anderen Konten.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db

MARKIERUNG = "data-eingaben-loeschen"


def _konto() -> User:
    return cast(User, User.objects.create_user(email="abmelden@example.org", password="Sicheres-Passwort-2026!"))  # type: ignore[no-untyped-call]


def test_nach_dem_abmelden_wird_die_sicherung_geloescht(client: Client) -> None:
    client.force_login(_konto())
    antwort = client.post(reverse("accounts:logout"), follow=True)
    assert antwort.redirect_chain[-1][0] == reverse("accounts:logged_out")
    assert MARKIERUNG in antwort.content.decode()


def test_angemeldet_loescht_die_seite_nichts(client: Client) -> None:
    client.force_login(_konto())
    antwort = client.get(reverse("accounts:logged_out"))
    assert antwort.status_code == 200
    assert MARKIERUNG not in antwort.content.decode()


@pytest.mark.parametrize("neues_design", [False, True])
def test_work_traegt_die_kennung_des_angemeldeten_kontos(
    org: Any, make_member: Any, client_for: Any, neues_design: bool
) -> None:
    org.work_new_design = neues_design
    org.save(update_fields=["work_new_design"])
    mitglied = make_member(org, ["tasks.view"], email="konto@example.org")
    antwort = client_for(mitglied.user).get(reverse("work:tasks", kwargs={"org_slug": org.slug}))
    assert antwort.status_code == 200
    assert f'data-konto="{mitglied.user.pk}"' in antwort.content.decode()
