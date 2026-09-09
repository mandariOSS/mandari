# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unit-Tests für Selectors und Services des Aufgaben-Moduls (Issue #160, Service-Layer).

Prüft Sichtbarkeitsregeln, Board-Operationen (Anlegen, Verschieben, Erledigen),
Panel-Änderungen mit Aktivitätsprotokoll, Sichtbarkeit/Freigaben sowie Export
und Datei-Import inklusive Duplikat-Regel.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.work.tasks import export_service, import_service, selectors, services
from apps.work.tasks.models import Task, TaskChecklistItem, TaskLabel, TaskShare


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create"], email="mitglied@example.org")


@pytest.fixture
def other(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view"], email="andere@example.org")


@pytest.fixture
def manager(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.manage"], email="leitung@example.org")


def make_entry(meeting: Any, content: str, **kwargs: Any) -> Any:
    """Protokolleintrag mit verschlüsseltem Inhalt (EncryptionMixin ist untypisiert)."""
    from apps.work.faction.models import FactionProtocolEntry

    entry = cast(Any, FactionProtocolEntry)(meeting=meeting, **kwargs)
    entry.set_content_encrypted(content)
    entry.save()
    return entry


def make_task(org: Any, creator: Any, title: str = "Aufgabe", **kwargs: Any) -> Task:
    kwargs.setdefault("assigned_to", creator)
    return Task.objects.create(organization=org, title=title, created_by=creator, **kwargs)


# ---------------------------------------------------------------------------
# Selectors: Sichtbarkeit und Board
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_visible_tasks_respects_visibility_rules(org: Any, member: Any, other: Any) -> None:
    make_task(org, other, "Privat fremd", visibility="private")
    shared = make_task(org, other, "Geteilt", visibility="shared")
    TaskShare.objects.create(task=shared, membership=member, shared_by=other)
    make_task(org, other, "Orgweit", visibility="organization")
    make_task(org, member, "Eigene", visibility="private")

    titles = set(selectors.visible_tasks(org, member).values_list("title", flat=True))

    assert titles == {"Geteilt", "Orgweit", "Eigene"}


@pytest.mark.django_db
def test_visible_tasks_is_bound_to_organization(org: Any, member: Any, make_member: Any) -> None:
    from apps.common.tests.factories import OrganizationFactory

    foreign_org = OrganizationFactory(name="Fremde Fraktion", slug="fremd")  # type: ignore[no-untyped-call]
    foreign_member = make_member(foreign_org, ["tasks.view"], email="fremd@example.org")
    make_task(foreign_org, foreign_member, "Fremd orgweit", visibility="organization")
    make_task(org, member, "Eigene")

    assert list(selectors.visible_tasks(org, member).values_list("title", flat=True)) == ["Eigene"]
    assert selectors.find_task(org, Task.objects.get(title="Fremd orgweit").id) is None


@pytest.mark.django_db
def test_own_tasks_and_board_filters(org: Any, member: Any, other: Any) -> None:
    make_task(org, member, "Dringend", priority="urgent")
    overdue = make_task(org, member, "Verspaetet", due_date=timezone.now().date() - timedelta(days=1))
    make_task(org, other, "Orgweit fremd", visibility="organization")

    base = selectors.card_queryset(org)
    own = selectors.own_tasks(member, base=base)
    assert set(own.values_list("title", flat=True)) == {"Dringend", "Verspaetet"}

    urgent = selectors.apply_board_filters(own, priority="urgent")
    assert list(urgent.values_list("title", flat=True)) == ["Dringend"]

    late = selectors.apply_board_filters(own, overdue_only=True)
    assert list(late) == [overdue]

    found = selectors.apply_board_filters(own, search="spaet")
    assert list(found) == [overdue]


@pytest.mark.django_db
def test_board_stats_and_column_counts(org: Any, member: Any) -> None:
    make_task(org, member, "A", status="todo")
    make_task(org, member, "B", status="in_progress")
    make_task(org, member, "C", status="done", is_completed=True)

    stats = selectors.board_stats(selectors.visible_tasks(org, member))
    assert stats == {"total": 3, "todo": 1, "in_progress": 1, "done": 1, "overdue": 0}
    assert selectors.column_counts(org) == {"todo_count": 1, "in_progress_count": 1, "done_count": 1}
    assert selectors.next_position(org, "todo") == 1


# ---------------------------------------------------------------------------
# Services: Anlegen, Verschieben, Erledigen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_can_edit_task(org: Any, member: Any, other: Any, manager: Any) -> None:
    task = make_task(org, member)
    assert services.can_edit_task(task, member)
    assert not services.can_edit_task(task, other)
    assert services.can_edit_task(task, manager)


@pytest.mark.django_db
def test_create_task_sets_defaults_and_logs_activity(org: Any, member: Any) -> None:
    make_task(org, member, "Bestehend")
    task = services.create_task(Task(title="Neu"), org, member)

    assert task.organization == org
    assert task.created_by == member
    assert task.assigned_to == member
    assert task.position == 1
    assert list(task.activities.values_list("activity_type", flat=True)) == ["created"]


@pytest.mark.django_db
def test_create_task_notifies_foreign_assignee(org: Any, member: Any, other: Any) -> None:
    from apps.work.notifications.models import Notification

    services.create_task(Task(title="Fuer andere", assigned_to=other), org, member)

    assert Notification.objects.filter(recipient=other, notification_type="task_assigned").exists()


@pytest.mark.django_db
def test_quick_add_appends_to_column(org: Any, member: Any) -> None:
    services.quick_add_task(org, member, title="Erste", status="todo", priority="medium")
    second = services.quick_add_task(org, member, title="Zweite", status="todo", priority="high")

    assert second.position == 1
    assert second.assigned_to == member
    assert second.activities.filter(activity_type="created").exists()


@pytest.mark.django_db
def test_move_task_reorders_both_columns_and_completes(org: Any, member: Any) -> None:
    a = make_task(org, member, "A", status="todo", position=0)
    b = make_task(org, member, "B", status="todo", position=1)
    c = make_task(org, member, "C", status="done", position=0, is_completed=True)

    services.move_task(a, member, new_status="done", new_position=0)

    a.refresh_from_db()
    b.refresh_from_db()
    c.refresh_from_db()
    assert a.status == "done" and a.is_completed and a.completed_at is not None
    assert a.position == 0 and c.position == 1  # Zielspalte nachsortiert
    assert b.position == 0  # Quellspalte lückenlos
    assert a.activities.filter(activity_type="completed").exists()


@pytest.mark.django_db
def test_move_task_logs_status_change_and_rejects_invalid_status(org: Any, member: Any) -> None:
    task = make_task(org, member, status="todo")

    services.move_task(task, member, new_status="in_progress", new_position=0)
    activity = task.activities.get(activity_type="status_changed")
    assert activity.details == {"old": "Zu erledigen", "new": "In Bearbeitung", "field": "status"}

    with pytest.raises(services.InvalidTaskStatusError):
        services.move_task(task, member, new_status="archiv", new_position=0)


@pytest.mark.django_db
def test_toggle_completion_board_variant_is_silent(org: Any, member: Any) -> None:
    task = make_task(org, member, status="in_progress")

    services.toggle_completion(task, member, reopen_status="todo", record_activity=False)
    assert task.is_completed and task.status == "done"
    services.toggle_completion(task, member, reopen_status="todo", record_activity=False)
    assert not task.is_completed and task.status == "todo" and task.completed_at is None
    assert not task.activities.exists()


@pytest.mark.django_db
def test_toggle_completion_panel_variant_logs(org: Any, member: Any) -> None:
    task = make_task(org, member, status="todo")

    services.toggle_completion(task, member)
    services.toggle_completion(task, member)

    assert task.status == "in_progress"
    assert list(task.activities.values_list("activity_type", flat=True)) == ["completed", "reopened"]


@pytest.mark.django_db
def test_apply_panel_update_logs_field_changes(org: Any, member: Any, other: Any) -> None:
    task = make_task(org, member, priority="medium", status="todo")
    old_values = services.capture_old_values(task)
    task.priority = "high"
    task.status = "done"
    task.assigned_to = other

    services.apply_panel_update(task, member, old_values)

    task.refresh_from_db()
    assert task.is_completed and task.completed_at is not None
    types = set(task.activities.values_list("activity_type", flat=True))
    assert types == {"completed", "priority_changed", "assigned"}


# ---------------------------------------------------------------------------
# Services: Sichtbarkeit, Checkliste, Labels, Kommentare
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_visibility_syncs_shares(org: Any, member: Any, other: Any, manager: Any) -> None:
    task = make_task(org, member, visibility="private")

    services.update_visibility(task, member, "shared", [str(other.id), str(manager.id)])
    assert task.visibility == "shared"
    assert set(selectors.task_shares(task).values_list("membership_id", flat=True)) == {other.id, manager.id}

    services.update_visibility(task, member, "shared", [str(other.id)])
    assert list(selectors.task_shares(task).values_list("membership_id", flat=True)) == [other.id]

    services.update_visibility(task, member, "organization", [])
    assert not selectors.task_shares(task).exists()
    assert task.can_access(manager)


@pytest.mark.django_db
def test_checklist_lifecycle(org: Any, member: Any) -> None:
    task = make_task(org, member)
    first = services.add_checklist_item(task, member, TaskChecklistItem(title="Eins"))
    second = services.add_checklist_item(task, member, TaskChecklistItem(title="Zwei"))
    assert (first.position, second.position) == (0, 1)

    services.toggle_checklist_item(task, member, first)
    assert first.is_completed
    assert task.activities.filter(activity_type="checklist_item_completed").exists()

    services.reorder_checklist(task, [str(second.id), str(first.id)])
    assert list(task.checklist_items.order_by("position").values_list("title", flat=True)) == ["Zwei", "Eins"]

    services.delete_checklist_item(second)
    assert task.checklist_items.count() == 1


@pytest.mark.django_db
def test_toggle_label_and_comment(org: Any, member: Any, other: Any) -> None:
    task = make_task(org, member, assigned_to=other)
    label = services.save_label(org, TaskLabel(name="Haushalt", color="green"))

    assert services.toggle_label(task, member, label) is True
    assert selectors.task_label_ids(task) == [label.id]
    assert services.toggle_label(task, member, label) is False
    assert selectors.task_label_ids(task) == []

    activity = services.add_comment(task, member, "Bitte prüfen")
    assert activity.activity_type == "comment"
    from apps.work.notifications.models import Notification

    assert Notification.objects.filter(recipient=other, notification_type="task_comment").exists()

    services.delete_label(label)
    assert selectors.label_rows(org) == []


@pytest.mark.django_db
def test_import_protocol_entries_creates_tasks(org: Any, member: Any, other: Any) -> None:
    from apps.work.faction.models import FactionMeeting

    meeting = FactionMeeting.objects.create(organization=org, title="Fraktionssitzung", start=timezone.now())
    entry = make_entry(meeting, "Antrag vorbereiten", entry_type="action", action_assignee=other, created_by=member)
    note = make_entry(meeting, "Notiz", entry_type="note", created_by=member)

    assert list(selectors.open_protocol_action_items(org)) == [entry]
    created = services.import_protocol_entries(org, member, [str(entry.id), str(note.id), "keine-uuid"])

    assert created == 1
    task = Task.objects.get(related_faction_meeting=meeting)
    assert task.assigned_to == other and task.title == "Antrag vorbereiten"


# ---------------------------------------------------------------------------
# Export und Datei-Import
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_export_formats_contain_visible_tasks_only(org: Any, member: Any, other: Any) -> None:
    make_task(org, member, "Sichtbar", visibility="private")
    make_task(org, other, "Unsichtbar", visibility="private")

    csv_content, csv_type, filename = export_service.export_tasks(org, member, "csv")
    assert csv_type.startswith("text/csv") and filename.endswith(".csv")
    assert isinstance(csv_content, str) and csv_content.startswith(export_service.BOM)
    assert "Sichtbar" in csv_content and "Unsichtbar" not in csv_content

    json_content, _, _ = export_service.export_tasks(org, member, "json")
    payload = json.loads(json_content)
    assert payload["format"] == "mandari-tasks" and [t["title"] for t in payload["tasks"]] == ["Sichtbar"]

    xml_content, xml_type, _ = export_service.export_tasks(org, member, "xml")
    assert xml_type.startswith("application/xml") and b"<title>Sichtbar</title>" in xml_content

    with pytest.raises(export_service.UnknownExportFormatError):
        export_service.export_tasks(org, member, "pdf")


def test_parse_upload_detects_format_and_reads_csv() -> None:
    raw = b"Titel;Status;Prioritaet;Labels\nHaushalt;offen;hoch;Finanzen, Kultur\n"
    file_format, rows = import_service.parse_upload("aufgaben.csv", raw)

    assert file_format == "csv"
    assert rows == [
        {"_line": 2, "title": "Haushalt", "status": "offen", "priority": "hoch", "labels": "Finanzen, Kultur"}
    ]

    with pytest.raises(import_service.TaskImportError):
        import_service.parse_upload("leer.csv", b"")
    with pytest.raises(import_service.TaskImportError):
        import_service.parse_upload("kaputt.json", b"{nicht json")


@pytest.mark.django_db
def test_classify_and_apply_import_is_idempotent(org: Any, member: Any) -> None:
    raw = (
        b"Titel;Status;Prioritaet;Labels;Zugewiesen an\n"
        b"Haushalt;erledigt;hoch;Finanzen;mitglied@example.org\n"
        b"Haushalt;offen;;\n"
        b"Kultur;quatsch;;\n"
    )
    _, rows = import_service.parse_upload("aufgaben.csv", raw)

    report = import_service.classify_rows(org, member, rows)
    assert len(report.create) == 1 and len(report.skip) == 1 and len(report.errors) == 1
    assert report.summary()["preview"] == ["Haushalt"]

    import_service.apply_import(org, member, report)
    task = Task.objects.get(organization=org, title="Haushalt")
    assert task.status == "done" and task.is_completed and task.priority == "high"
    assert task.assigned_to == member
    assert list(task.labels.values_list("name", flat=True)) == ["Finanzen"]

    # Erneuter Import derselben Datei: Titel-Duplikat wird übersprungen
    again = import_service.classify_rows(org, member, rows)
    assert len(again.create) == 0 and len(again.skip) == 2
    assert Task.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_import_updates_by_id_only_with_edit_permission(org: Any, member: Any, other: Any) -> None:
    foreign = make_task(org, other, "Fremde Aufgabe", visibility="organization")
    payload: dict[str, Any] = {"tasks": [{"id": str(foreign.id), "title": "Umbenannt", "status": "in_progress"}]}
    _, rows = import_service.parse_upload("import.json", json.dumps(payload).encode())

    report = import_service.classify_rows(org, member, rows)
    assert report.update == [] and report.skip[0]["reason"] == "keine Berechtigung"

    own = make_task(org, member, "Eigene")
    payload = {"tasks": [{"id": str(own.id), "title": "Eigene neu", "status": "done", "checklist": []}]}
    _, rows = import_service.parse_upload("import.json", json.dumps(payload).encode())
    report = import_service.classify_rows(org, member, rows)
    import_service.apply_import(org, member, report)

    own.refresh_from_db()
    assert own.title == "Eigene neu" and own.is_completed
    assert own.activities.filter(activity_type="completed").exists()
