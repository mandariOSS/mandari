# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatisches Speichern im Aufgaben-Panel (#854).

Lehnt der Server das automatische Speichern ab (z. B. Titel leer), zeichnet er das Panel mit markierten Feldern neu.
Die Antwort trägt Status 422, damit die Anzeige „Nicht gespeichert“ zeigt statt eines Hakens
(frontend/js/htmx-setup.ts zeichnet 422 automatisch speichernder Formulare trotzdem ein); das neu gezeichnete Panel
trägt die Ablehnung selbst (`data-autosave-abgelehnt`), ein zusätzlicher Toast entfällt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.urls import reverse

from apps.work.tasks.models import Task

MARKIERUNG = 'data-autosave-abgelehnt="task-update-form"'


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create"], email="aufgaben@example.org")


@pytest.fixture
def task(org: Any, member: Any) -> Task:
    return Task.objects.create(organization=org, title="Protokoll schreiben", created_by=member, assigned_to=member)


def _speichern(client: Any, org: Any, task: Task, **felder: str) -> Any:
    daten = {
        "action": "update",
        "title": task.title,
        "description": "",
        "status": task.status,
        "priority": task.priority,
        "visibility": task.visibility,
        "assigned_to": str(task.assigned_to_id or ""),
        **felder,
    }
    url = reverse("work:task_panel_action", kwargs={"org_slug": org.slug, "task_id": task.id})
    return client.post(url, daten)


@pytest.mark.django_db
def test_abgelehntes_automatisches_speichern_gilt_als_gescheitert(
    org: Any, member: Any, task: Task, client_for: Any
) -> None:
    antwort = _speichern(client_for(member.user), org, task, title="")

    assert antwort.status_code == 422
    assert antwort["HX-Retarget"] == "#task-panel-container"
    assert antwort["HX-Reswap"] == "innerHTML"
    assert "show-toast" not in json.loads(antwort.get("HX-Trigger") or "{}")
    assert MARKIERUNG in antwort.content.decode()
    task.refresh_from_db()
    assert task.title == "Protokoll schreiben"


@pytest.mark.django_db
def test_gelungenes_automatisches_speichern_ohne_markierung(org: Any, member: Any, task: Task, client_for: Any) -> None:
    antwort = _speichern(client_for(member.user), org, task, title="Protokoll schreiben und versenden")

    assert antwort.status_code == 200
    assert MARKIERUNG not in antwort.content.decode()
    task.refresh_from_db()
    assert task.title == "Protokoll schreiben und versenden"
