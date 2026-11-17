# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben-Panel im Browser: Jedes Feld speichert sofort, und die Karte steht danach genau einmal im Board.

Zwei Fehler, die nur der Browser zeigt:

- Das Panel hing das Sofort-Speichern mit ``from:find <Auswahl>`` an; htmx hört dabei nur auf das erste passende
  Element. Von den Auswahlfeldern speicherte deshalb nur der Status, von der Sichtbarkeit nur die erste Wahl, von den
  weiteren Zuständigen nur das erste Kästchen. Priorität, Zuweisung und die übrigen gingen erst mit der nächsten
  anderen Änderung oder dem Speichern-Knopf mit, die Anzeige „Gespeichert“ blieb aus.
- Im bisherigen Erscheinungsbild trug eine Hülle mit derselben Kennung den Austausch der Karte; htmx 2 setzte sie mit
  ein, die Karte stand danach verschachtelt in einem zweiten Element mit derselben Kennung und war kein direktes Kind
  der Spalte mehr.

Geprüft in beiden Erscheinungsbildern.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from apps.work.tasks.models import Task
from tests_e2e.conftest import ADMIN_PASSWORD, BrowserProblems, component_state, wait_for_component

PANEL = "#task-panel-container #task-update-form"


def _mitglied(admin: Any, make_member: Any, vorname: str) -> Any:
    mitglied = make_member(admin.organization, ["dashboard.view", "tasks.view"], email=f"{vorname.lower()}@example.org")
    mitglied.user.first_name, mitglied.user.last_name = vorname, "Beispiel"
    mitglied.user.save(update_fields=["first_name", "last_name"])
    return mitglied


def _board(page: Any, goto: Any, login: Any, admin: Any, *, neu: bool) -> Task:
    org = admin.organization
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    aufgabe = Task.objects.create(
        organization=org,
        created_by=admin,
        assigned_to=admin,
        title="Haushaltsrede vorbereiten",
        visibility="organization",
        status="todo",
        priority="medium",
    )
    login(admin.user.email, ADMIN_PASSWORD)
    goto(f"/work/{org.slug}/tasks/")
    wait_for_component(page, "kanbanBoard")
    return aufgabe


def _speichert(page: Any, aktion: Any) -> None:
    """Die Aktion löst genau die Anfrage des Panels aus; ohne Anfrage scheitert der Test an der Wartezeit."""
    with page.expect_response(
        lambda r: "/panel/action/" in r.url and r.request.method == "POST", timeout=10000
    ) as antwort:
        aktion()
    assert antwort.value.status == 200


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_jedes_feld_des_panels_speichert_sofort(
    page: Any, goto: Any, login: Any, admin: Any, make_member: Any, problems: BrowserProblems, neu: bool
) -> None:
    anna, ben = _mitglied(admin, make_member, "Anna"), _mitglied(admin, make_member, "Ben")
    aufgabe = _board(page, goto, login, admin, neu=neu)
    page.locator(f'.task-card[data-id="{aufgabe.pk}"]').click()
    form = page.locator(PANEL)
    form.wait_for()

    # Zweites Auswahlfeld
    _speichert(page, lambda: form.locator('select[name="priority"]').select_option("urgent"))
    aufgabe.refresh_from_db()
    assert aufgabe.priority == "urgent"

    # Letzte Wahl der Sichtbarkeit
    _speichert(page, lambda: form.locator('input[name="visibility"][value="private"]').check())
    aufgabe.refresh_from_db()
    assert aufgabe.visibility == "private"

    # Weitere Zuständige: beide Kästchen, nicht nur das erste
    for person in (anna, ben):
        _speichert(
            page,
            lambda person=person: form.locator(f'input[name="weitere_zustaendige"][value="{person.pk}"]').check(),
        )
    assert set(aufgabe.weitere_zustaendige.values_list("pk", flat=True)) == {anna.pk, ben.pk}

    # Erstes Auswahlfeld speichert weiter
    _speichert(page, lambda: form.locator('select[name="status"]').select_option("in_progress"))
    aufgabe.refresh_from_db()
    assert aufgabe.status == "in_progress"
    problems.assert_clean("Aufgaben-Panel")


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_karte_steht_nach_dem_speichern_genau_einmal_im_board(
    page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems, neu: bool
) -> None:
    aufgabe = _board(page, goto, login, admin, neu=neu)
    karte = page.locator(f"#task-card-{aufgabe.pk}")
    kennung_der_spalte = karte.locator("xpath=..").get_attribute("id")

    karte.click()
    page.locator(PANEL).wait_for()
    _speichert(page, lambda: page.locator(f'{PANEL} select[name="priority"]').select_option("urgent"))

    # Genau ein Element mit der Kennung, weiter direkt in seiner Spalte (SortableJS zieht nur direkte Kinder)
    expect(karte).to_have_count(1)
    assert karte.locator("xpath=..").get_attribute("id") == kennung_der_spalte
    assert "task-card" in (karte.get_attribute("class") or "")

    # Die ausgetauschte Karte öffnet das Panel wie zuvor
    page.evaluate("window.Alpine.$data(document.querySelector('[x-data^=\"kanbanBoard\"]')).closePanel()")
    assert component_state(page, "kanbanBoard", "data.panelOpen") is False
    karte.click()
    page.wait_for_function(
        "window.Alpine.$data(document.querySelector('[x-data^=\"kanbanBoard\"]')).panelOpen === true"
    )
    problems.assert_clean("Aufgaben-Board")
