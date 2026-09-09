# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering des Aufgaben-Boards und des Aufgaben-Panels (Issue #174, Satz A): Die Alpine-Komponenten
`kanbanBoard`, `dropZone`, `labelPicker`, `importManager` und `fileImportManager` leben in
`frontend/alpine/task-board.ts` bzw. `task-import.ts`; das Template liefert die URLs als JSON
(`json_script`), die Kanban-Styles liegen in `static/css/input.css`, und das Panel nutzt keine
`onclick=`-Handler mehr.
"""

import json
import re
from typing import Any

import pytest

from apps.work.tasks.models import Task

CONFIG_RE = re.compile(r'<script[^>]*id="task-board-config"[^>]*>(.*?)</script>', re.S)
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create"], email="aufgaben@example.org")


@pytest.fixture
def task(org: Any, member: Any) -> Task:
    return Task.objects.create(organization=org, title="Protokoll schreiben", created_by=member, assigned_to=member)


def read_config(html: str) -> dict[str, Any]:
    match = CONFIG_RE.search(html)
    assert match, "json_script #task-board-config fehlt"
    config: dict[str, Any] = json.loads(match.group(1))
    return config


@pytest.mark.django_db
def test_board_page_provides_json_config_and_no_inline_script(
    org: Any, member: Any, task: Task, client_for: Any
) -> None:
    response = client_for(member.user).get(f"/work/{org.slug}/tasks/?open={task.id}")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="kanbanBoard"' in html
    assert 'x-data="importManager"' in html
    assert 'x-data="fileImportManager"' in html
    assert "Protokoll schreiben" in html
    assert 'id="column-todo"' in html and 'id="column-in_progress"' in html and 'id="column-done"' in html

    config = read_config(html)
    assert config["autoOpenTaskId"] == str(task.id)
    assert config["urls"]["api"] == f"/work/{org.slug}/tasks/api/"
    assert config["urls"]["panel"] == f"/work/{org.slug}/tasks/{PLACEHOLDER}/panel/"
    assert config["urls"]["labels"] == f"/work/{org.slug}/tasks/labels/"
    assert config["urls"]["importProtocol"] == f"/work/{org.slug}/tasks/import/"
    assert config["urls"]["importFile"] == f"/work/{org.slug}/tasks/import-file/"

    # Kein Inline-JS/-CSS und keine on*-Handler mehr aus dem Template
    for marker in ("function kanbanBoard", "kanbanBoardInstance", "function importManager", ".kanban-board {"):
        assert marker not in html, marker
    assert not re.search(r"\son(change|click|submit)=\"", html)
    assert "Sortable.min.js" in html  # Vendor-Skript bleibt als src-Einbindung

    used_templates = {t.name for t in response.templates if t.name}
    for partial in ("_board_filters", "_kanban_column", "_import_modal", "_file_import_modal", "_panel_shell"):
        assert f"work/tasks/partials/{partial}.html" in used_templates, partial


@pytest.mark.django_db
def test_task_panel_uses_alpine_actions_instead_of_onclick(org: Any, member: Any, task: Task, client_for: Any) -> None:
    response = client_for(member.user).get(f"/work/{org.slug}/tasks/{task.id}/panel/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="task-update-form"' in html  # bearbeitbar (Ersteller)
    assert "copyTaskLink(" in html
    assert "deleteTask(" in html
    assert 'x-data="labelPicker"' in html
    assert 'x-data="dropZone"' in html
    assert "onclick=" not in html
    assert f"?open={task.id}" in html

    used_templates = {t.name for t in response.templates if t.name}
    assert "work/tasks/partials/_panel_edit.html" in used_templates
    assert "work/tasks/partials/_panel_comment_form.html" in used_templates


@pytest.mark.django_db
def test_task_panel_readonly_for_other_member(org: Any, member: Any, make_member: Any, client_for: Any) -> None:
    task = Task.objects.create(organization=org, title="Nur lesen", created_by=member, visibility="organization")
    viewer = make_member(org, ["tasks.view"], email="leser@example.org")

    response = client_for(viewer.user).get(f"/work/{org.slug}/tasks/{task.id}/panel/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="task-update-form"' not in html
    assert "Nur lesen" in html
    assert "copyTaskLink(" in html
    assert "onclick=" not in html
    used_templates = {t.name for t in response.templates if t.name}
    assert "work/tasks/partials/_panel_readonly.html" in used_templates
