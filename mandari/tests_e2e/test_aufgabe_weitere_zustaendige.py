# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben mit mehreren Zuständigen im Browser (Issue #1462).

Die Regeln prüft ``apps/work/tasks/tests/test_weitere_zustaendige.py``. Hier geht es um das, was nur der Browser zeigt:

- Im Panel der Aufgabe speichert ein Haken bei „Weitere Zuständige“ sofort (wie Auswahlfelder), die Karte auf dem
  Board nennt danach die Zahl der weiteren Zuständigen; der Haken weg nimmt die Person wieder heraus.
- Beim Anlegen kommt die Auswahl der Kästchen mit dem Formular an.

Beides im bisherigen und im neuen Erscheinungsbild (``Organization.work_new_design``): Die Vorlagen sind getrennt.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from apps.work.tasks.models import Task
from tests_e2e.conftest import ADMIN_PASSWORD, BrowserProblems, wait_for_component


def _kollegin(admin: Any, make_member: Any) -> Any:
    kollegin = make_member(admin.organization, ["dashboard.view", "tasks.view"], email="kollegin@example.org")
    kollegin.user.first_name, kollegin.user.last_name = "Karla", "Kollegin"
    kollegin.user.save(update_fields=["first_name", "last_name"])
    return kollegin


def _anmelden(login: Any, admin: Any, *, neu: bool) -> str:
    org = admin.organization
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    login(admin.user.email, ADMIN_PASSWORD)
    return str(org.slug)


def _weitere(aufgabe: Task) -> set[Any]:
    return set(aufgabe.weitere_zustaendige.values_list("pk", flat=True))


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_panel_speichert_weitere_zustaendige_sofort(
    page: Any, goto: Any, login: Any, admin: Any, make_member: Any, problems: BrowserProblems, neu: bool
) -> None:
    kollegin = _kollegin(admin, make_member)
    aufgabe = Task.objects.create(
        organization=admin.organization,
        created_by=admin,
        assigned_to=admin,
        title="Haushaltsrede vorbereiten",
        visibility="organization",
        status="todo",
    )
    slug = _anmelden(login, admin, neu=neu)
    goto(f"/work/{slug}/tasks/")
    wait_for_component(page, "kanbanBoard")
    # Über Klasse und data-id: Das bisherige Board umhüllt die Karte beim Austausch mit einem Element derselben Kennung
    karte = page.locator(f'.task-card[data-id="{aufgabe.pk}"]')

    karte.click()
    zeile = page.locator('#task-panel-container [data-testid="aufgabe-weitere-zustaendige"]')
    haken = zeile.locator(f'input[name="weitere_zustaendige"][value="{kollegin.pk}"]')
    expect(zeile).to_contain_text("Karla Kollegin")
    expect(haken).not_to_be_checked()

    with page.expect_response(lambda r: "/panel/action/" in r.url and r.request.method == "POST") as antwort:
        haken.check()
    assert antwort.value.status == 200
    assert _weitere(aufgabe) == {kollegin.pk}
    # Die Antwort tauscht die Karte aus: Sie nennt die zugewiesene Person und die Zahl der weiteren
    expect(karte).to_contain_text("+1")

    with page.expect_response(lambda r: "/panel/action/" in r.url and r.request.method == "POST") as antwort:
        haken.uncheck()
    assert antwort.value.status == 200
    assert _weitere(aufgabe) == set()
    expect(karte).not_to_contain_text("+1")
    problems.assert_clean("Aufgaben-Panel")


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_neue_aufgabe_mit_weiteren_zustaendigen(
    page: Any, goto: Any, login: Any, admin: Any, make_member: Any, problems: BrowserProblems, neu: bool
) -> None:
    kollegin = _kollegin(admin, make_member)
    slug = _anmelden(login, admin, neu=neu)
    goto(f"/work/{slug}/tasks/create/")
    wait_for_component(page, "assignmentHandler")

    page.locator('input[name="title"]').fill("Pressemitteilung abstimmen")
    page.locator(f'input[name="weitere_zustaendige"][value="{kollegin.pk}"]').check()
    page.get_by_role("button", name="Aufgabe erstellen").click()
    page.wait_for_url(lambda url: "/tasks/create" not in url)

    aufgabe = Task.objects.get(title="Pressemitteilung abstimmen")
    assert _weitere(aufgabe) == {kollegin.pk}
    problems.assert_clean("Neue Aufgabe")
