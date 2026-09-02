# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben-Export und -Import für das Work-Modul (Issue #7).

Export: Alle für das Mitglied sichtbaren Aufgaben der Organisation als
CSV (flache Tabelle, Excel-tauglich), JSON (vollständig, verschachtelt
inkl. Labels und Checklisten) oder XML.

Import: Datei-Upload (CSV, JSON, XML) mit Validierung, Duplikat-Regel und
Fehlerbericht. JSON versteht zusätzlich Trello-Exporte (cards/lists/labels).

Duplikat-Regel (idempotent):
- Zeilen mit bekannter Aufgaben-ID (gleiche Organisation) aktualisieren die
  bestehende Aufgabe — sofern das Mitglied sie bearbeiten darf.
- Zeilen ohne ID, deren Titel bereits in einer sichtbaren Aufgabe der
  Organisation existiert, werden als Duplikat übersprungen.
- Ein erneuter Import derselben Datei erzeugt daher keine Duplikate.
"""

import csv
import io
import json
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as defused_fromstring
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

from ..activity import log_activity
from ..models import Task, TaskChecklistItem, TaskLabel

logger = logging.getLogger(__name__)

MAX_IMPORT_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_IMPORT_ROWS = 1000

# Spalten-Mapping: akzeptierte Header (kleingeschrieben) -> internes Feld
CSV_HEADER_ALIASES = {
    "id": "id",
    "titel": "title",
    "title": "title",
    "beschreibung": "description",
    "description": "description",
    "status": "status",
    "spalte": "status",
    "liste": "status",
    "prioritaet": "priority",
    "priorität": "priority",
    "priority": "priority",
    "sichtbarkeit": "visibility",
    "visibility": "visibility",
    "faellig am": "due_date",
    "fällig am": "due_date",
    "faellig": "due_date",
    "fällig": "due_date",
    "due date": "due_date",
    "due_date": "due_date",
    "due": "due_date",
    "zugewiesen an": "assigned_to",
    "zugewiesen": "assigned_to",
    "assigned to": "assigned_to",
    "assigned_to": "assigned_to",
    "assignee": "assigned_to",
    "labels": "labels",
    "label": "labels",
    "erledigt": "is_completed",
    "completed": "is_completed",
    "is_completed": "is_completed",
    "erledigt am": "completed_at",
    "completed_at": "completed_at",
    "tags": "tags",
}

STATUS_ALIASES = {
    "todo": "todo",
    "to do": "todo",
    "offen": "todo",
    "open": "todo",
    "zu erledigen": "todo",
    "in_progress": "in_progress",
    "in progress": "in_progress",
    "in bearbeitung": "in_progress",
    "in arbeit": "in_progress",
    "doing": "in_progress",
    "done": "done",
    "erledigt": "done",
    "fertig": "done",
    "abgeschlossen": "done",
}

PRIORITY_ALIASES = {
    "urgent": "urgent",
    "dringend": "urgent",
    "high": "high",
    "hoch": "high",
    "medium": "medium",
    "mittel": "medium",
    "low": "low",
    "niedrig": "low",
}

VISIBILITY_ALIASES = {
    "private": "private",
    "privat": "private",
    "shared": "shared",
    "geteilt": "shared",
    "organization": "organization",
    "organisation": "organization",
    "org": "organization",
}

TRUE_VALUES = {"1", "true", "ja", "yes", "x", "wahr"}
FALSE_VALUES = {"", "0", "false", "nein", "no", "falsch"}


def _visible_tasks(organization, membership):
    """Alle Aufgaben der Organisation, die das Mitglied sehen darf."""
    from django.db.models import Q

    return (
        Task.objects.filter(organization=organization)
        .filter(
            Q(visibility="organization")
            | Q(created_by=membership)
            | Q(assigned_to=membership)
            | Q(shares__membership=membership)
        )
        .distinct()
    )


def _task_to_dict(task) -> dict:
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


class TaskExportView(WorkViewMixin, View):
    """Export der sichtbaren Aufgaben als CSV, JSON oder XML (Download)."""

    permission_required = "tasks.view"

    def get(self, request, *args, **kwargs):
        export_format = (request.GET.get("format") or "csv").lower()
        if export_format not in ("csv", "json", "xml"):
            return JsonResponse({"error": "Unbekanntes Format. Erlaubt: csv, json, xml."}, status=400)

        tasks = (
            _visible_tasks(self.organization, self.membership)
            .select_related("assigned_to__user", "created_by__user")
            .prefetch_related("labels", "checklist_items")
            .order_by("status", "position", "created_at")
        )

        filename = f"aufgaben-{self.organization.slug}-{timezone.now().strftime('%Y%m%d')}.{export_format}"

        if export_format == "csv":
            content, content_type = self._render_csv(tasks)
        elif export_format == "json":
            content, content_type = self._render_json(tasks)
        else:
            content, content_type = self._render_xml(tasks)

        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _render_csv(self, tasks):
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(
            [
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
            ]
        )
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
        # BOM, damit Excel UTF-8 korrekt erkennt
        return "﻿" + buffer.getvalue(), "text/csv; charset=utf-8"

    def _render_json(self, tasks):
        payload = {
            "format": "mandari-tasks",
            "version": 1,
            "organization": self.organization.slug,
            "exported_at": timezone.now().isoformat(),
            "tasks": [_task_to_dict(task) for task in tasks],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8"

    def _render_xml(self, tasks):
        root = ET.Element(
            "tasks",
            {
                "format": "mandari-tasks",
                "version": "1",
                "organization": self.organization.slug,
                "exported_at": timezone.now().isoformat(),
            },
        )
        for task in tasks:
            data = _task_to_dict(task)
            task_el = ET.SubElement(root, "task")
            for field in (
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
            ):
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
        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        return xml_bytes, "application/xml; charset=utf-8"


# =============================================================================
# Import
# =============================================================================


def _parse_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def _parse_date(value) -> tuple[date | None, str | None]:
    """Datum aus ISO (YYYY-MM-DD), ISO-Datetime oder DD.MM.YYYY."""
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    try:
        return date.fromisoformat(text[:10]), None
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d.%m.%Y").date(), None
    except ValueError:
        return None, f"Ungültiges Datum: {text!r} (erwartet YYYY-MM-DD oder TT.MM.JJJJ)"


def _split_labels(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _rows_from_csv(text: str) -> tuple[list[dict], list[str]]:
    """Liest CSV-Zeilen und mappt Spalten auf interne Felder."""
    errors: list[str] = []
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return [], ["Die CSV-Datei ist leer."]

    field_map: dict[int, str] = {}
    for idx, raw_name in enumerate(header):
        field = CSV_HEADER_ALIASES.get(raw_name.strip().lstrip("﻿").lower())
        if field:
            field_map[idx] = field

    if "title" not in field_map.values():
        return [], ['Keine Titel-Spalte gefunden (erwartet z. B. "Titel" oder "title").']

    rows = []
    for line_no, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue
        row: dict = {"_line": line_no}
        for idx, field in field_map.items():
            if idx < len(cells):
                row[field] = cells[idx].strip()
        rows.append(row)
    return rows, errors


def _rows_from_mandari_json(data: dict) -> tuple[list[dict], list[str]]:
    rows = []
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return [], ['JSON-Datei enthält keine "tasks"-Liste.']
    for idx, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            continue
        row = {"_line": idx}
        for field in (
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
            "tags",
        ):
            if item.get(field) is not None:
                row[field] = item[field]
        row["labels"] = [
            label.get("name") if isinstance(label, dict) else str(label) for label in item.get("labels") or []
        ]
        row["checklist"] = [
            {
                "title": str(cl.get("title", "")).strip(),
                "is_completed": bool(cl.get("is_completed")),
                "position": cl.get("position", pos),
            }
            for pos, cl in enumerate(item.get("checklist") or [])
            if isinstance(cl, dict) and str(cl.get("title", "")).strip()
        ]
        rows.append(row)
    return rows, []


def _rows_from_trello_json(data: dict) -> tuple[list[dict], list[str]]:
    """Basis-Mapping für Trello-Board-Exporte (cards/lists/labels)."""
    lists_by_id = {lst.get("id"): lst.get("name", "") for lst in data.get("lists") or [] if isinstance(lst, dict)}
    labels_by_id = {
        lbl.get("id"): lbl.get("name") or lbl.get("color") or ""
        for lbl in data.get("labels") or []
        if isinstance(lbl, dict)
    }

    rows = []
    for idx, card in enumerate(data.get("cards") or [], start=1):
        if not isinstance(card, dict) or card.get("closed"):
            continue  # archivierte Karten überspringen
        list_name = (lists_by_id.get(card.get("idList")) or "").lower()
        status = STATUS_ALIASES.get(list_name, "todo")
        row = {
            "_line": idx,
            "title": card.get("name", ""),
            "description": card.get("desc", ""),
            "status": status,
            "labels": [labels_by_id[lid] for lid in card.get("idLabels") or [] if labels_by_id.get(lid)],
        }
        if card.get("due"):
            row["due_date"] = card["due"]
        if card.get("dueComplete"):
            row["is_completed"] = True
        checklist_items = []
        for checklist in card.get("checklists") or []:
            for pos, check_item in enumerate(checklist.get("checkItems") or []):
                name = str(check_item.get("name", "")).strip()
                if name:
                    checklist_items.append(
                        {
                            "title": name,
                            "is_completed": check_item.get("state") == "complete",
                            "position": pos,
                        }
                    )
        if checklist_items:
            row["checklist"] = checklist_items
        rows.append(row)

    if not rows:
        return [], ["Keine importierbaren Karten im Trello-Export gefunden."]
    return rows, []


def _rows_from_json(text: str) -> tuple[list[dict], list[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"Ungültiges JSON (Zeile {exc.lineno}, Spalte {exc.colno})."]
    if not isinstance(data, dict):
        return [], ["Unbekanntes JSON-Format (Objekt erwartet)."]
    if isinstance(data.get("tasks"), list):
        return _rows_from_mandari_json(data)
    if isinstance(data.get("cards"), list):
        return _rows_from_trello_json(data)
    return [], ['Unbekanntes JSON-Format (weder "tasks"- noch Trello-"cards"-Liste gefunden).']


def _rows_from_xml(text: str) -> tuple[list[dict], list[str]]:
    try:
        root = defused_fromstring(text)
    except ET.ParseError as exc:
        position = getattr(exc, "position", None)
        detail = f" (Zeile {position[0]}, Spalte {position[1]})" if position else ""
        return [], [f"Ungültiges XML{detail}."]
    except DefusedXmlException:
        return [], ["Unzulässiges XML: DTDs, Entitäten und externe Referenzen sind nicht erlaubt."]

    task_elements = root.findall(".//task")
    if not task_elements:
        return [], ["Keine <task>-Elemente im XML gefunden."]

    rows = []
    for idx, task_el in enumerate(task_elements, start=1):
        row: dict = {"_line": idx}
        for field in (
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
        ):
            el = task_el.find(field)
            if el is not None and el.text is not None and el.text.strip():
                row[field] = el.text.strip()
        row["labels"] = [label_el.text.strip() for label_el in task_el.findall("labels/label") if label_el.text]
        row["checklist"] = [
            {
                "title": item_el.text.strip(),
                "is_completed": item_el.get("completed") == "true",
                "position": pos,
            }
            for pos, item_el in enumerate(task_el.findall("checklist/item"))
            if item_el.text and item_el.text.strip()
        ]
        row["tags"] = [tag_el.text.strip() for tag_el in task_el.findall("tags/tag") if tag_el.text]
        rows.append(row)
    return rows, []


class TaskFileImportView(WorkViewMixin, View):
    """
    Datei-Import von Aufgaben (CSV, JSON, XML).

    POST mit multipart "file". Mit dry_run=1 wird nur validiert und ein
    Vorschau-Bericht zurückgegeben, ohne Daten zu schreiben.
    """

    permission_required = "tasks.create"

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get("file")
        if not upload:
            return JsonResponse({"error": "Keine Datei übermittelt."}, status=400)
        if upload.size > MAX_IMPORT_FILE_SIZE:
            return JsonResponse({"error": "Datei zu groß (max. 5 MB)."}, status=400)

        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return JsonResponse({"error": "Datei ist nicht UTF-8-kodiert."}, status=400)

        file_format = self._detect_format(upload.name or "", text)
        if file_format == "csv":
            rows, parse_errors = _rows_from_csv(text)
        elif file_format == "json":
            rows, parse_errors = _rows_from_json(text)
        elif file_format == "xml":
            rows, parse_errors = _rows_from_xml(text)
        else:
            return JsonResponse({"error": "Unbekanntes Dateiformat. Erlaubt: CSV, JSON, XML."}, status=400)

        if parse_errors:
            return JsonResponse({"error": " ".join(parse_errors)}, status=400)
        if not rows:
            return JsonResponse({"error": "Keine importierbaren Zeilen gefunden."}, status=400)
        if len(rows) > MAX_IMPORT_ROWS:
            return JsonResponse({"error": f"Zu viele Zeilen (max. {MAX_IMPORT_ROWS})."}, status=400)

        dry_run = request.POST.get("dry_run") in ("1", "true")
        report = self._classify_rows(rows)

        if not dry_run:
            with transaction.atomic():
                self._apply(report)

        return JsonResponse(
            {
                "success": True,
                "dry_run": dry_run,
                "format": file_format,
                "report": {
                    "total": len(rows),
                    "created": len(report["create"]),
                    "updated": len(report["update"]),
                    "skipped": len(report["skip"]),
                    "failed": len(report["errors"]),
                    "errors": report["errors"][:20],
                    "warnings": report["warnings"][:20],
                    "preview": [row["data"]["title"] for row in report["create"][:10]],
                },
            }
        )

    def _detect_format(self, filename: str, text: str) -> str | None:
        name = filename.lower()
        if name.endswith(".csv"):
            return "csv"
        if name.endswith(".json"):
            return "json"
        if name.endswith(".xml"):
            return "xml"
        stripped = text.lstrip("﻿").lstrip()
        if stripped.startswith("{"):
            return "json"
        if stripped.startswith("<"):
            return "xml"
        if stripped:
            return "csv"
        return None

    def _classify_rows(self, rows: list[dict]) -> dict:
        """Validiert alle Zeilen und teilt sie in create/update/skip/errors ein (nur Lesezugriffe)."""
        report: dict = {"create": [], "update": [], "skip": [], "errors": [], "warnings": []}

        memberships_by_email = {
            ms.user.email.lower(): ms
            for ms in self.organization.memberships.filter(is_active=True).select_related("user")
        }
        existing_titles = {
            title.strip().lower()
            for title in _visible_tasks(self.organization, self.membership).values_list("title", flat=True)
        }
        can_manage = self.membership.has_permission("tasks.manage")

        seen_in_file: set[str] = set()

        for row in rows:
            line = row.get("_line", "?")
            data, row_errors, row_warnings = self._normalize_row(row, memberships_by_email)
            for warning in row_warnings:
                report["warnings"].append(f"Zeile {line}: {warning}")
            if row_errors:
                report["errors"].append(f"Zeile {line}: {'; '.join(row_errors)}")
                continue

            title_key = data["title"].strip().lower()

            # Update-Pfad: bekannte ID in dieser Organisation
            existing = None
            if data.get("id"):
                existing = Task.objects.filter(id=data["id"], organization=self.organization).first()
            if existing is not None:
                if existing.can_edit(self.membership) or can_manage:
                    report["update"].append({"line": line, "data": data, "task": existing})
                else:
                    report["skip"].append({"line": line, "reason": "keine Berechtigung"})
                    report["warnings"].append(
                        f"Zeile {line}: Aufgabe {existing.id} existiert, keine Bearbeitungsberechtigung — übersprungen."
                    )
                continue

            # Duplikat-Regel: gleicher Titel bereits vorhanden (DB oder Datei)
            if title_key in existing_titles or title_key in seen_in_file:
                report["skip"].append({"line": line, "reason": "Duplikat"})
                continue

            seen_in_file.add(title_key)
            report["create"].append({"line": line, "data": data})

        return report

    def _normalize_row(self, row: dict, memberships_by_email: dict) -> tuple[dict, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        data: dict = {}

        title = str(row.get("title") or "").strip()
        if not title:
            errors.append("Titel fehlt.")
        elif len(title) > 200:
            warnings.append("Titel auf 200 Zeichen gekürzt.")
            title = title[:200]
        data["title"] = title

        data["description"] = str(row.get("description") or "").strip()[:2000]

        raw_id = str(row.get("id") or "").strip()
        if raw_id:
            try:
                data["id"] = uuid.UUID(raw_id)
            except ValueError:
                warnings.append(f"Ungültige ID {raw_id!r} — wird als neue Aufgabe behandelt.")

        raw_status = str(row.get("status") or "").strip().lower()
        if raw_status:
            status = STATUS_ALIASES.get(raw_status)
            if status is None:
                errors.append(f"Unbekannter Status: {row.get('status')!r}")
            else:
                data["status"] = status
        else:
            data["status"] = "todo"

        raw_priority = str(row.get("priority") or "").strip().lower()
        if raw_priority:
            priority = PRIORITY_ALIASES.get(raw_priority)
            if priority is None:
                errors.append(f"Unbekannte Priorität: {row.get('priority')!r}")
            else:
                data["priority"] = priority
        else:
            data["priority"] = "medium"

        raw_visibility = str(row.get("visibility") or "").strip().lower()
        if raw_visibility:
            visibility = VISIBILITY_ALIASES.get(raw_visibility)
            if visibility is None:
                errors.append(f"Unbekannte Sichtbarkeit: {row.get('visibility')!r}")
            else:
                data["visibility"] = visibility
        else:
            data["visibility"] = "organization"

        due_date, date_error = _parse_date(row.get("due_date"))
        if date_error:
            errors.append(date_error)
        data["due_date"] = due_date

        is_completed = _parse_bool(row.get("is_completed"))
        if data.get("status") == "done" or is_completed:
            data["status"] = "done"
            data["is_completed"] = True
        else:
            data["is_completed"] = False

        assignee_email = str(row.get("assigned_to") or "").strip().lower()
        data["assigned_to"] = None
        if assignee_email:
            membership = memberships_by_email.get(assignee_email)
            if membership is None:
                warnings.append(f"Kein Mitglied mit E-Mail {assignee_email!r} — Aufgabe bleibt unzugewiesen.")
            else:
                data["assigned_to"] = membership

        data["labels"] = [name[:50] for name in _split_labels(row.get("labels"))]
        data["tags"] = (
            [str(t) for t in row.get("tags") or [] if str(t).strip()]
            if isinstance(row.get("tags"), list)
            else _split_labels(row.get("tags"))
        )
        data["checklist"] = row.get("checklist") or []

        return data, errors, warnings

    def _apply(self, report: dict) -> None:
        """Schreibt die klassifizierten Zeilen in die Datenbank."""
        label_cache: dict[str, TaskLabel] = {
            label.name.lower(): label for label in TaskLabel.objects.filter(organization=self.organization)
        }

        def resolve_labels(names: list[str]) -> list[TaskLabel]:
            resolved = []
            for name in names:
                key = name.lower()
                if key not in label_cache:
                    label_cache[key] = TaskLabel.objects.create(organization=self.organization, name=name, color="blue")
                resolved.append(label_cache[key])
            return resolved

        position_counters = {
            status: Task.objects.filter(organization=self.organization, status=status).count()
            for status in ("todo", "in_progress", "done")
        }

        now = timezone.now()

        for entry in report["create"]:
            data = entry["data"]
            status = data["status"]
            task = Task.objects.create(
                organization=self.organization,
                title=data["title"],
                description=data["description"],
                status=status,
                priority=data["priority"],
                visibility=data["visibility"],
                due_date=data["due_date"],
                is_completed=data["is_completed"],
                completed_at=now if data["is_completed"] else None,
                position=position_counters[status],
                created_by=self.membership,
                assigned_to=data["assigned_to"] or self.membership,
            )
            position_counters[status] += 1
            labels = resolve_labels(data["labels"])
            if labels:
                task.labels.set(labels)
            if data["tags"]:
                task.tags = data["tags"]
                task.save(update_fields=["tags"])
            for pos, item in enumerate(data["checklist"]):
                TaskChecklistItem.objects.create(
                    task=task,
                    title=str(item.get("title", ""))[:300],
                    is_completed=bool(item.get("is_completed")),
                    position=item.get("position", pos),
                )
            log_activity(task, self.membership, "created")

        for entry in report["update"]:
            task = entry["task"]
            data = entry["data"]
            task.title = data["title"]
            task.description = data["description"]
            task.priority = data["priority"]
            task.visibility = data["visibility"]
            task.due_date = data["due_date"]
            old_status = task.status
            task.status = data["status"]
            if data["is_completed"] and not task.is_completed:
                task.is_completed = True
                task.completed_at = now
            elif not data["is_completed"] and task.is_completed:
                task.is_completed = False
                task.completed_at = None
            if data["assigned_to"] is not None:
                task.assigned_to = data["assigned_to"]
            task.save()
            if data["labels"]:
                task.labels.set(resolve_labels(data["labels"]))
            if old_status != task.status:
                if task.status == "done":
                    log_activity(task, self.membership, "completed")
                elif old_status == "done":
                    log_activity(task, self.membership, "reopened")
