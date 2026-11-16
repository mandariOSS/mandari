# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aktionen an einer Aufgabe prüfen und schreiben am aktuellen Stand der Datenbank (Teil von #1172).

Board und Panel lesen die Aufgabe zu Beginn der Anfrage. Stellt die erstellende Person sie bis zum Schreiben privat
oder weist sie neu zu, führt die laufende Aktion nichts mehr aus (403), und was sie schreibt, sind nur ihre eigenen
Spalten: Sichtbarkeit und Zuweisung bleiben, wie sie in der Datenbank stehen. SQLite kennt keine Zeilensperre; die
Tests belegen deshalb das frische Lesen: Sie laden die Aufgabe, ändern die Zeile an ihr vorbei und rufen danach den
Dienst mit dem alten Objekt auf.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.work.tasks import import_service, services
from apps.work.tasks.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture
def erstellerin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create"], email="erstellerin@example.org")


@pytest.fixture
def zugewiesen(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view"], email="zugewiesen@example.org")


@pytest.fixture
def leitung(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.manage"], email="leitung@example.org")


def _aufgabe(org: Any, erstellerin: Any, zugewiesen: Any) -> Task:
    """Organisationsweite Aufgabe, wie sie eine Anfrage zu Beginn liest."""
    aufgabe = Task.objects.create(
        organization=org,
        title="Haushalt",
        created_by=erstellerin,
        assigned_to=zugewiesen,
        visibility="organization",
        status="todo",
    )
    return Task.objects.get(pk=aufgabe.pk)


def _entziehen(aufgabe: Task, erstellerin: Any) -> None:
    """Die erstellende Person stellt die Aufgabe privat und weist sie sich selbst zu."""
    Task.objects.filter(pk=aufgabe.pk).update(visibility="private", assigned_to=erstellerin)


def _stand(aufgabe: Task) -> tuple[str, Any, str, bool]:
    aktuell = Task.objects.get(pk=aufgabe.pk)
    return aktuell.visibility, aktuell.assigned_to_id, aktuell.status, aktuell.is_completed


@pytest.mark.parametrize("aktion", ["verschieben", "status", "erledigen", "panel", "sichtbarkeit"])
@pytest.mark.parametrize("wer", ["zugewiesen", "leitung"])
def test_aktion_nach_entzug_wird_abgewiesen_und_schreibt_nichts(
    org: Any, erstellerin: Any, zugewiesen: Any, leitung: Any, aktion: str, wer: str
) -> None:
    handelnd = zugewiesen if wer == "zugewiesen" else leitung
    aufgabe = _aufgabe(org, erstellerin, zugewiesen)
    assert services.can_edit_task(aufgabe, handelnd)  # am gelesenen Stand noch erlaubt
    alte_werte = services.capture_old_values(aufgabe)
    _entziehen(aufgabe, erstellerin)

    with pytest.raises(PermissionDenied):
        if aktion == "verschieben":
            services.move_task(aufgabe, handelnd, new_status="done", new_position=0)
        elif aktion == "status":
            services.set_status(aufgabe, handelnd, "done")
        elif aktion == "erledigen":
            services.toggle_completion(aufgabe, handelnd)
        elif aktion == "panel":
            aufgabe.title = "Umbenannt"
            services.apply_panel_update(aufgabe, handelnd, alte_werte)
        else:
            services.update_visibility(aufgabe, handelnd, "organization", [])

    assert _stand(aufgabe) == ("private", erstellerin.pk, "todo", False)
    aktuell = Task.objects.get(pk=aufgabe.pk)
    assert aktuell.title == "Haushalt" and not aktuell.activities.exists()


@pytest.mark.parametrize("aktion", ["verschieben", "status", "erledigen"])
def test_statusaktion_schreibt_sichtbarkeit_und_zuweisung_nicht_zurueck(
    org: Any, erstellerin: Any, zugewiesen: Any, make_member: Any, aktion: str
) -> None:
    """Das Bearbeitungsrecht bleibt (weiter beteiligt), aber Sichtbarkeit und Zuweisung haben sich geändert."""
    dritte = make_member(org, ["tasks.view"], email="dritte@example.org")
    aufgabe = _aufgabe(org, erstellerin, zugewiesen)
    Task.objects.filter(pk=aufgabe.pk).update(visibility="private", assigned_to=dritte)

    if aktion == "verschieben":
        services.move_task(aufgabe, erstellerin, new_status="done", new_position=0)
    elif aktion == "status":
        services.set_status(aufgabe, erstellerin, "done")
    else:
        services.toggle_completion(aufgabe, erstellerin)

    assert _stand(aufgabe) == ("private", dritte.pk, "done", True)
    # Der Aufrufer sieht das Ergebnis am übergebenen Objekt
    assert (aufgabe.visibility, aufgabe.assigned_to_id, aufgabe.status) == ("private", dritte.pk, "done")


def test_panel_schreibt_nur_die_felder_des_formulars(org: Any, erstellerin: Any, zugewiesen: Any) -> None:
    aufgabe = _aufgabe(org, erstellerin, zugewiesen)
    alte_werte = services.capture_old_values(aufgabe)
    # Inzwischen hat jemand die Karte auf dem Board verschoben
    Task.objects.filter(pk=aufgabe.pk).update(position=7)

    aufgabe.title = "Haushalt 2027"
    aufgabe.status = "in_progress"
    services.apply_panel_update(aufgabe, zugewiesen, alte_werte)

    aktuell = Task.objects.get(pk=aufgabe.pk)
    assert (aktuell.title, aktuell.status, aktuell.position) == ("Haushalt 2027", "in_progress", 7)
    assert aktuell.activities.filter(activity_type="status_changed").exists()


def test_board_aktionen_arbeiten_weiter(org: Any, erstellerin: Any, zugewiesen: Any, client_for: Any) -> None:
    """Die Board-Schnittstelle reicht das handelnde Mitglied an alle drei Statusaktionen durch."""
    aufgabe = _aufgabe(org, erstellerin, zugewiesen)
    client = client_for(zugewiesen.user)
    url = reverse("work:tasks_api", kwargs={"org_slug": org.slug})

    antwort = client.post(url, {"action": "update_status", "task_id": str(aufgabe.pk), "status": "in_progress"})
    assert antwort.status_code == 200 and _stand(aufgabe)[2] == "in_progress"

    antwort = client.post(url, {"action": "toggle_complete", "task_id": str(aufgabe.pk)})
    assert antwort.status_code == 200 and antwort.json() == {"success": True, "is_completed": True, "status": "done"}

    antwort = client.post(
        url,
        json.dumps({"action": "move", "task_id": str(aufgabe.pk), "status": "todo", "position": 0}),
        content_type="application/json",
    )
    assert antwort.status_code == 200 and antwort.json()["status"] == "todo"
    assert _stand(aufgabe) == ("organization", zugewiesen.pk, "todo", False)


def test_import_vermerkt_den_wechsel_der_sichtbarkeit_im_verlauf(org: Any, erstellerin: Any) -> None:
    eigene = Task.objects.create(
        organization=org, title="Eigene", created_by=erstellerin, assigned_to=erstellerin, visibility="private"
    )

    def importieren(zeile: dict[str, Any]) -> None:
        daten = {"tasks": [{"id": str(eigene.pk), "title": "Eigene", **zeile}]}
        _, zeilen = import_service.parse_upload("import.json", json.dumps(daten).encode())
        import_service.apply_import(org, erstellerin, import_service.classify_rows(org, erstellerin, zeilen))

    # Ohne Angabe bleibt die Sichtbarkeit, und es entsteht kein Eintrag
    importieren({"status": "todo"})
    eigene.refresh_from_db()
    assert eigene.visibility == "private" and not eigene.activities.filter(activity_type="visibility_changed").exists()

    importieren({"status": "todo", "visibility": "organization"})
    eigene.refresh_from_db()
    eintrag = eigene.activities.get(activity_type="visibility_changed")
    assert eigene.visibility == "organization" and eintrag.actor == erstellerin
    assert eintrag.details == {"old": "Privat", "new": "Organisation", "field": "visibility"}
