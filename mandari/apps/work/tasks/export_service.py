# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben-Export als CSV, JSON oder XML (Issue #7, Service-Layer Issue #160).

Exportiert werden alle für das Mitglied sichtbaren Aufgaben der Organisation:
CSV als flache, Excel-taugliche Tabelle, JSON und XML vollständig verschachtelt
inkl. Labels und Checklisten.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any

from django.utils import timezone

from apps.tenants.models import Membership, Organization

from . import selectors
from .models import Task

EXPORT_FORMATS = ("csv", "json", "xml")
BOM = "\ufeff"

CSV_COLUMNS = (
    "ID",
    "Titel",
    "Beschreibung",
    "Status",
    "Prioritaet",
    "Sichtbarkeit",
    "Faellig am",
    "Erledigt",
    "Erledigt am",
    "Zugewiesen an",
    "Erstellt von",
    "Labels",
    "Tags",
    "Erstellt am",
)

XML_SCALAR_FIELDS = (
    "id",
    "title",
    "description",
    "status",
    "priority",
    "visibility",
    "due_date",
    "is_completed",
    "completed_at",
    "assigned_to",
    "created_by",
    "created_at",
    "updated_at",
)


class UnknownExportFormatError(ValueError):
    """Angefordertes Exportformat wird nicht unterstützt."""


def serialize_task(task: Task) -> dict[str, Any]:
    """Serialisiert eine Aufgabe als verschachteltes Dict (JSON/XML-Export)."""
    return {
        "id": str(task.id),
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "visibility": task.visibility,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "is_completed": task.is_completed,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "assigned_to": task.assigned_to.user.email if task.assigned_to else None,
        "created_by": task.created_by.user.email if task.created_by else None,
        "labels": [{"name": label.name, "color": label.color} for label in task.labels.all()],
        "checklist": [
            {"title": item.title, "is_completed": item.is_completed, "position": item.position}
            for item in task.checklist_items.all()
        ],
        "tags": list(task.tags) if isinstance(task.tags, list) else [],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def render_csv(tasks: Iterable[Task]) -> tuple[str, str]:
    """CSV mit Semikolon, CRLF und BOM (Excel erkennt so UTF-8)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for task in tasks:
        writer.writerow(
            [
                str(task.id),
                task.title,
                task.description,
                task.status,
                task.priority,
                task.visibility,
                task.due_date.isoformat() if task.due_date else "",
                "ja" if task.is_completed else "nein",
                task.completed_at.isoformat() if task.completed_at else "",
                task.assigned_to.user.email if task.assigned_to else "",
                task.created_by.user.email if task.created_by else "",
                ", ".join(label.name for label in task.labels.all()),
                ", ".join(str(t) for t in task.tags) if isinstance(task.tags, list) else "",
                task.created_at.isoformat() if task.created_at else "",
            ]
        )
    return BOM + buffer.getvalue(), "text/csv; charset=utf-8"


def render_json(tasks: Iterable[Task], organization: Organization) -> tuple[str, str]:
    """JSON im Format ``mandari-tasks`` Version 1."""
    payload = {
        "format": "mandari-tasks",
        "version": 1,
        "organization": organization.slug,
        "exported_at": timezone.now().isoformat(),
        "tasks": [serialize_task(task) for task in tasks],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8"


def render_xml(tasks: Iterable[Task], organization: Organization) -> tuple[bytes, str]:
    """XML mit ``<tasks>``-Wurzel; Labels, Checkliste und Tags als Unterelemente."""
    root = ET.Element(
        "tasks",
        {
            "format": "mandari-tasks",
            "version": "1",
            "organization": organization.slug,
            "exported_at": timezone.now().isoformat(),
        },
    )
    for task in tasks:
        data = serialize_task(task)
        task_el = ET.SubElement(root, "task")
        for field in XML_SCALAR_FIELDS:
            el = ET.SubElement(task_el, field)
            value = data[field]
            if isinstance(value, bool):
                el.text = "true" if value else "false"
            elif value is not None:
                el.text = str(value)
        labels_el = ET.SubElement(task_el, "labels")
        for label in data["labels"]:
            label_el = ET.SubElement(labels_el, "label", {"color": label["color"]})
            label_el.text = label["name"]
        checklist_el = ET.SubElement(task_el, "checklist")
        for item in data["checklist"]:
            item_el = ET.SubElement(
                checklist_el,
                "item",
                {"completed": "true" if item["is_completed"] else "false", "position": str(item["position"])},
            )
            item_el.text = item["title"]
        tags_el = ET.SubElement(task_el, "tags")
        for tag in data["tags"]:
            tag_el = ET.SubElement(tags_el, "tag")
            tag_el.text = str(tag)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), "application/xml; charset=utf-8"


def export_tasks(
    organization: Organization, membership: Membership, export_format: str
) -> tuple[str | bytes, str, str]:
    """
    Sichtbare Aufgaben im gewünschten Format rendern.

    Returns:
        (Inhalt, Content-Type, Dateiname)

    Raises:
        UnknownExportFormatError: bei einem Format außerhalb von ``csv``, ``json``, ``xml``.
    """
    if export_format not in EXPORT_FORMATS:
        raise UnknownExportFormatError(export_format)
    tasks = selectors.export_queryset(organization, membership)
    filename = f"aufgaben-{organization.slug}-{timezone.now().strftime('%Y%m%d')}.{export_format}"
    content: str | bytes
    if export_format == "csv":
        content, content_type = render_csv(tasks)
    elif export_format == "json":
        content, content_type = render_json(tasks, organization)
    else:
        content, content_type = render_xml(tasks, organization)
    return content, content_type, filename
