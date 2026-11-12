# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben-Import aus CSV, JSON oder XML (Issue #7, Service-Layer Issue #160).

Ablauf: Datei erkennen und parsen (:func:`parse_upload`), Zeilen validieren und
in create/update/skip/errors einteilen (:func:`classify_rows`, nur Lesezugriffe),
anschließend in einer Transaktion schreiben (:func:`apply_import`).
JSON versteht zusätzlich Trello-Exporte (cards/lists/labels).

Duplikat-Regel (idempotent):
- Zeilen mit bekannter Aufgaben-ID (gleiche Organisation) aktualisieren die
  bestehende Aufgabe, sofern das Mitglied sie bearbeiten darf.
- Zeilen ohne ID, deren Titel bereits in einer sichtbaren Aufgabe der
  Organisation existiert, werden als Duplikat übersprungen.
- Ein erneuter Import derselben Datei erzeugt daher keine Duplikate.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as defused_fromstring
from django.db import transaction
from django.utils import timezone

from apps.common import csv_safety
from apps.tenants.models import Membership, Organization

from . import selectors
from .activity import log_activity
from .models import Task, TaskChecklistItem, TaskLabel

MAX_IMPORT_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_IMPORT_ROWS = 1000
BOM = "\ufeff"

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

JSON_ROW_FIELDS = (
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
)
XML_ROW_FIELDS = JSON_ROW_FIELDS[:-1]

Row = dict[str, Any]


class TaskImportError(ValueError):
    """Datei kann nicht importiert werden (Format, Kodierung, Größe, Inhalt).

    Die Meldung wird der hochladenden Person angezeigt und enthält deshalb nur fest
    formulierten Text, höchstens ergänzt um Zeilen-/Spaltenangaben aus der Datei –
    niemals den Text einer Parser-Ausnahme oder einen Stack-Trace.
    """


@dataclass
class ImportReport:
    """Ergebnis der Klassifikation; ``create``/``update`` tragen die normalisierten Daten."""

    total: int = 0
    create: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    skip: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Kompakte Zusammenfassung für die JSON-Antwort."""
        return {
            "total": self.total,
            "created": len(self.create),
            "updated": len(self.update),
            "skipped": len(self.skip),
            "failed": len(self.errors),
            "errors": self.errors[:20],
            "warnings": self.warnings[:20],
            "preview": [row["data"]["title"] for row in self.create[:10]],
        }


# ---------------------------------------------------------------------------
# Parsen
# ---------------------------------------------------------------------------


def parse_bool(value: Any) -> bool | None:
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


def parse_date(value: Any) -> tuple[date | None, str | None]:
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


#: Grenzen für Labels und Schlagworte einer Zeile: Anzahl und Länge je Eintrag. Gekürzt wird beim Sammeln, damit
#: ein langer Name, auf den viele Einträge verweisen, nicht je Verweis in voller Länge im Speicher steht
MAX_LABELS_PER_ROW = 50
MAX_LABEL_LENGTH = 100
#: Länge, mit der der Name eines Labels gespeichert wird (``normalize_row``)
LABEL_NAME_LENGTH = 50


#: Ein Eintrag eines kommagetrennten Texts: beginnt am ersten Zeichen, das weder Komma noch Leerraum ist. Leere
#: Felder überspringt die Suche, ohne sie einzeln anzulegen
_LABEL_IN_TEXT = re.compile(r"[^,\s][^,]*")


def split_labels(value: Any) -> list[str]:
    """
    Labels aus einer Liste oder einem Text mit Kommas: die ersten ``MAX_LABELS_PER_ROW`` nicht leeren, gekürzt.

    Der Text wird schrittweise gelesen und nach dem letzten gebrauchten Eintrag nicht weiter zerlegt; ein Feld aus
    Millionen kurzer Einträge legt so keine Liste aller Teile an.
    """
    if isinstance(value, list):
        parts: Any = (str(v) for v in value)
    elif value:
        parts = (match.group() for match in _LABEL_IN_TEXT.finditer(str(value)))
    else:
        return []
    labels: list[str] = []
    for part in parts:
        label = part.strip()[:MAX_LABEL_LENGTH]
        if label:
            labels.append(label)
            if len(labels) >= MAX_LABELS_PER_ROW:
                break
    return labels


def rows_from_csv(text: str) -> tuple[list[Row], list[str]]:
    """Liest CSV-Zeilen und mappt Spalten auf interne Felder."""
    text = text.lstrip(BOM)
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ";"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return [], ["Die CSV-Datei ist leer."]

    field_map: dict[int, str] = {}
    for idx, raw_name in enumerate(header):
        mapped = CSV_HEADER_ALIASES.get(raw_name.strip().lstrip(BOM).lower())
        if mapped:
            field_map[idx] = mapped

    if "title" not in field_map.values():
        return [], ['Keine Titel-Spalte gefunden (erwartet z. B. "Titel" oder "title").']

    rows: list[Row] = []
    for line_no, cells in enumerate(reader, start=2):
        if not any(cell.strip() for cell in cells):
            continue
        row: Row = {"_line": line_no}
        for idx, mapped in field_map.items():
            if idx < len(cells):
                # Eigene Exporte entschärfen Formel-Anfänge mit einem Hochkomma (apps/common/csv_safety.py)
                row[mapped] = csv_safety.csv_unescape_cell(cells[idx].strip())
        rows.append(row)
    return rows, []


def rows_from_mandari_json(data: dict[str, Any]) -> tuple[list[Row], list[str]]:
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return [], ['JSON-Datei enthält keine "tasks"-Liste.']
    rows: list[Row] = []
    for idx, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            continue
        row: Row = {"_line": idx}
        for name in JSON_ROW_FIELDS:
            if item.get(name) is not None:
                row[name] = item[name]
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


def _dicts(value: Any) -> list[dict[str, Any]]:
    """Die Einträge einer Liste, die Objekte sind; alles andere (auch keine Liste) ergibt nichts."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def rows_from_trello_json(data: dict[str, Any]) -> tuple[list[Row], list[str]]:
    """Basis-Mapping für Trello-Board-Exporte (cards/lists/labels)."""
    # Listen und Labels je einmal auflösen und kürzen: Karten verweisen nur darauf, und ein langer Name würde sonst
    # für jede Karte bzw. jeden Verweis erneut verarbeitet
    status_by_list = {
        lst["id"]: STATUS_ALIASES.get(str(lst.get("name") or "").strip().lower(), "todo")
        for lst in _dicts(data.get("lists"))
        if isinstance(lst.get("id"), str)
    }
    # Auf die Länge gekürzt, mit der ein Label gespeichert wird: So fallen Namen, die erst dort gleich würden, schon
    # beim Entdoppeln zusammen
    labels_by_id = {
        lbl["id"]: str(lbl.get("name") or lbl.get("color") or "").strip()[:LABEL_NAME_LENGTH]
        for lbl in _dicts(data.get("labels"))
        if isinstance(lbl.get("id"), str)
    }
    # Archivierte Karten zählen nicht. Zu viele Karten weist der Import ab, bevor er eine davon umwandelt
    cards = [(idx, card) for idx, card in enumerate(_dicts(data.get("cards")), start=1) if not card.get("closed")]
    if len(cards) > MAX_IMPORT_ROWS:
        raise TaskImportError(f"Zu viele Zeilen (max. {MAX_IMPORT_ROWS}).")

    rows: list[Row] = []
    for idx, card in cards:
        list_id, label_ids = card.get("idList"), card.get("idLabels")
        labels: dict[str, None] = {}
        for lid in label_ids if isinstance(label_ids, list) else []:
            name = labels_by_id.get(lid) if isinstance(lid, str) else None
            if name:
                labels[name] = None
                if len(labels) >= MAX_LABELS_PER_ROW:
                    break
        row: Row = {
            "_line": idx,
            "title": card.get("name", ""),
            "description": card.get("desc", ""),
            "status": status_by_list.get(list_id, "todo") if isinstance(list_id, str) else "todo",
            "labels": list(labels),
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
                        {"title": name, "is_completed": check_item.get("state") == "complete", "position": pos}
                    )
        if checklist_items:
            row["checklist"] = checklist_items
        rows.append(row)

    if not rows:
        return [], ["Keine importierbaren Karten im Trello-Export gefunden."]
    return rows, []


def rows_from_json(text: str) -> tuple[list[Row], list[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"Ungültiges JSON (Zeile {exc.lineno}, Spalte {exc.colno})."]
    if not isinstance(data, dict):
        return [], ["Unbekanntes JSON-Format (Objekt erwartet)."]
    if isinstance(data.get("tasks"), list):
        return rows_from_mandari_json(data)
    if isinstance(data.get("cards"), list):
        return rows_from_trello_json(data)
    return [], ['Unbekanntes JSON-Format (weder "tasks"- noch Trello-"cards"-Liste gefunden).']


def rows_from_xml(text: str) -> tuple[list[Row], list[str]]:
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

    rows: list[Row] = []
    for idx, task_el in enumerate(task_elements, start=1):
        row: Row = {"_line": idx}
        for name in XML_ROW_FIELDS:
            el = task_el.find(name)
            if el is not None and el.text is not None and el.text.strip():
                row[name] = el.text.strip()
        row["labels"] = [label_el.text.strip() for label_el in task_el.findall("labels/label") if label_el.text]
        row["checklist"] = [
            {"title": item_el.text.strip(), "is_completed": item_el.get("completed") == "true", "position": pos}
            for pos, item_el in enumerate(task_el.findall("checklist/item"))
            if item_el.text and item_el.text.strip()
        ]
        row["tags"] = [tag_el.text.strip() for tag_el in task_el.findall("tags/tag") if tag_el.text]
        rows.append(row)
    return rows, []


def detect_format(filename: str, text: str) -> str | None:
    """Format aus Dateiendung, sonst aus dem Inhalt ableiten."""
    name = filename.lower()
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".xml"):
        return "xml"
    stripped = text.lstrip(BOM).lstrip()
    if stripped.startswith("{"):
        return "json"
    if stripped.startswith("<"):
        return "xml"
    if stripped:
        return "csv"
    return None


def parse_upload(filename: str, raw: bytes) -> tuple[str, list[Row]]:
    """
    Upload dekodieren, Format erkennen und Zeilen lesen.

    Returns:
        (Format, Zeilen)

    Raises:
        TaskImportError: mit einer Meldung für die Nutzer:in.
    """
    if len(raw) > MAX_IMPORT_FILE_SIZE:
        raise TaskImportError("Datei zu groß (max. 5 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TaskImportError("Datei ist nicht UTF-8-kodiert.") from exc

    file_format = detect_format(filename, text)
    if file_format == "csv":
        rows, parse_errors = rows_from_csv(text)
    elif file_format == "json":
        rows, parse_errors = rows_from_json(text)
    elif file_format == "xml":
        rows, parse_errors = rows_from_xml(text)
    else:
        raise TaskImportError("Unbekanntes Dateiformat. Erlaubt: CSV, JSON, XML.")

    if parse_errors:
        raise TaskImportError(" ".join(parse_errors))
    if not rows:
        raise TaskImportError("Keine importierbaren Zeilen gefunden.")
    if len(rows) > MAX_IMPORT_ROWS:
        raise TaskImportError(f"Zu viele Zeilen (max. {MAX_IMPORT_ROWS}).")
    return file_format, rows


# ---------------------------------------------------------------------------
# Normalisieren und klassifizieren (nur Lesezugriffe)
# ---------------------------------------------------------------------------


def _normalize_choice(
    row: Row, key: str, aliases: dict[str, str], default: str, label: str, errors: list[str]
) -> str | None:
    raw = str(row.get(key) or "").strip().lower()
    if not raw:
        return default
    value = aliases.get(raw)
    if value is None:
        errors.append(f"{label}: {row.get(key)!r}")
    return value


def normalize_row(row: Row, memberships_by_email: dict[str, Membership]) -> tuple[Row, list[str], list[str]]:
    """Rohzeile in Modellwerte überführen; liefert (Daten, Fehler, Warnungen)."""
    errors: list[str] = []
    warnings: list[str] = []
    data: Row = {}

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

    status = _normalize_choice(row, "status", STATUS_ALIASES, "todo", "Unbekannter Status", errors)
    if status is not None:
        data["status"] = status
    priority = _normalize_choice(row, "priority", PRIORITY_ALIASES, "medium", "Unbekannte Priorität", errors)
    if priority is not None:
        data["priority"] = priority
    visibility = _normalize_choice(
        row, "visibility", VISIBILITY_ALIASES, "organization", "Unbekannte Sichtbarkeit", errors
    )
    if visibility is not None:
        data["visibility"] = visibility

    due_date, date_error = parse_date(row.get("due_date"))
    if date_error:
        errors.append(date_error)
    data["due_date"] = due_date

    is_completed = parse_bool(row.get("is_completed"))
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

    data["labels"] = list(dict.fromkeys(name[:LABEL_NAME_LENGTH] for name in split_labels(row.get("labels"))))
    raw_tags = row.get("tags")
    data["tags"] = (
        [str(t) for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else split_labels(raw_tags)
    )
    data["checklist"] = row.get("checklist") or []
    return data, errors, warnings


def _darf_aendern(task: Task, membership: Membership) -> bool:
    """Bearbeitungsrecht an einer bestehenden Aufgabe, wie ``services.can_edit_task`` (dort importiert dieses Modul nicht)."""
    from . import services

    return services.can_edit_task(task, membership)


def classify_rows(organization: Organization, membership: Membership, rows: list[Row]) -> ImportReport:
    """Validiert alle Zeilen und teilt sie in create/update/skip/errors ein (nur Lesezugriffe)."""
    report = ImportReport(total=len(rows))
    memberships_by_email = selectors.memberships_by_email(organization)
    existing_titles = selectors.visible_task_titles(organization, membership)
    seen_in_file: set[str] = set()

    for row in rows:
        line = row.get("_line", "?")
        data, row_errors, row_warnings = normalize_row(row, memberships_by_email)
        report.warnings.extend(f"Zeile {line}: {warning}" for warning in row_warnings)
        if row_errors:
            report.errors.append(f"Zeile {line}: {'; '.join(row_errors)}")
            continue

        title_key = data["title"].strip().lower()

        # Update-Pfad: bekannte ID in dieser Organisation
        existing = selectors.find_task(organization, data["id"]) if data.get("id") else None
        if existing is not None:
            # Dieselbe Regel wie beim Bearbeiten in der Oberfläche: ``tasks.manage`` gilt nur für Aufgaben,
            # die die Person sehen darf – sonst ließen sich private Aufgaben anderer per Kennung überschreiben
            if _darf_aendern(existing, membership):
                report.update.append({"line": line, "data": data, "task": existing})
            else:
                report.skip.append({"line": line, "reason": "keine Berechtigung"})
                report.warnings.append(
                    f"Zeile {line}: Aufgabe {existing.id} existiert, keine Bearbeitungsberechtigung — übersprungen."
                )
            continue

        # Duplikat-Regel: gleicher Titel bereits vorhanden (DB oder Datei)
        if title_key in existing_titles or title_key in seen_in_file:
            report.skip.append({"line": line, "reason": "Duplikat"})
            continue

        seen_in_file.add(title_key)
        report.create.append({"line": line, "data": data})

    # Hinweis zu Labels schon in der Vorschau, nach denselben Regeln und in derselben Reihenfolge wie beim Schreiben
    # (erst neue, dann bestehende Aufgaben). Vorn im Bericht, damit ihn die Kürzung der Warnungen nicht trifft.
    labels = _LabelResolver(organization, membership, dry_run=True)
    for entry in [*report.create, *report.update]:
        labels.resolve(entry["data"].get("labels") or [])
    hinweis = labels.warning()
    if hinweis:
        report.warnings.insert(0, hinweis)
    return report


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------


#: Höchstens so viele neue Labels legt ein Import im gemeinsamen Katalog der Organisation an
MAX_NEW_LABELS_PER_IMPORT = 50


class _LabelResolver:
    """
    Labels per Name auflösen (Cache über den ganzen Import).

    Der Katalog gehört der ganzen Organisation: Unbekannte Namen legt der Import nur für Mitglieder an, die Labels
    verwalten dürfen (``tasks.manage``, wie die Label-Verwaltung selbst), und höchstens
    ``MAX_NEW_LABELS_PER_IMPORT`` je Import. Andere Namen bleiben weg und stehen in ``skipped``.
    """

    def __init__(
        self, organization: Organization, membership: Membership | None = None, *, dry_run: bool = False
    ) -> None:
        self.organization = organization
        self.cache: dict[str, TaskLabel | None] = dict(selectors.labels_by_name(organization))
        self.may_create = membership is not None and membership.has_permission("tasks.manage")
        #: Vorschau: dieselben Regeln in derselben Reihenfolge, aber ohne Labels anzulegen
        self.dry_run = dry_run
        self.created = 0
        self.skipped: dict[str, str] = {}

    def resolve(self, names: list[str]) -> list[TaskLabel]:
        resolved = []
        for name in names:
            key = name.lower()
            if key not in self.cache:
                if not self.may_create or self.created >= MAX_NEW_LABELS_PER_IMPORT:
                    self.skipped.setdefault(key, name)
                    continue
                self.cache[key] = (
                    None
                    if self.dry_run
                    else TaskLabel.objects.create(organization=self.organization, name=name, color="blue")
                )
                self.created += 1
            label = self.cache[key]
            if label is not None:
                resolved.append(label)
        return resolved

    def complete(self, names: list[str]) -> bool:
        """Lassen sich alle Namen auflösen (vorhanden oder in diesem Import angelegt)?"""
        return all(name.lower() in self.cache for name in names)

    def warning(self) -> str:
        """Hinweis für den Bericht zu nicht übernommenen Labels (leer: alle übernommen)."""
        if not self.skipped:
            return ""
        names = sorted(self.skipped.values(), key=str.lower)
        shown = ", ".join(names[:10]) + (" …" if len(names) > 10 else "")
        if not self.may_create:
            reason = "neue Labels legt an, wer Labels verwalten darf"
        else:
            reason = f"höchstens {MAX_NEW_LABELS_PER_IMPORT} neue Labels je Import"
        return f"{len(names)} unbekannte Labels nicht übernommen ({reason}): {shown}"


def _create_from_row(
    organization: Organization, membership: Membership, data: Row, position: int, labels: _LabelResolver, now: Any
) -> Task:
    task = Task.objects.create(
        organization=organization,
        title=data["title"],
        description=data["description"],
        status=data["status"],
        priority=data["priority"],
        visibility=data["visibility"],
        due_date=data["due_date"],
        is_completed=data["is_completed"],
        completed_at=now if data["is_completed"] else None,
        position=position,
        created_by=membership,
        assigned_to=data["assigned_to"] or membership,
    )
    resolved = labels.resolve(data["labels"])
    if resolved:
        task.labels.set(resolved)
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
    log_activity(task, membership, "created")
    return task


def _update_from_row(task: Task, membership: Membership, data: Row, labels: _LabelResolver, now: Any) -> Task:
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
        resolved = labels.resolve(data["labels"])
        if labels.complete(data["labels"]):
            task.labels.set(resolved)
        else:
            # Nicht alle Namen ließen sich auflösen (kein Recht, neue anzulegen, oder Obergrenze): Die bisherigen
            # Labels der Aufgabe bleiben, die aufgelösten kommen hinzu
            task.labels.add(*resolved)
    if old_status != task.status:
        if task.status == "done":
            log_activity(task, membership, "completed")
        elif old_status == "done":
            log_activity(task, membership, "reopened")
    return task


@transaction.atomic
def apply_import(organization: Organization, membership: Membership, report: ImportReport) -> None:
    """Schreibt die klassifizierten Zeilen in die Datenbank (eine Transaktion)."""
    labels = _LabelResolver(organization, membership)
    position_counters = selectors.position_counters(organization)
    now = timezone.now()

    for entry in report.create:
        data = entry["data"]
        status = data["status"]
        _create_from_row(organization, membership, data, position_counters[status], labels, now)
        position_counters[status] += 1

    # Erneut prüfen, und zwar am aktuellen Stand der Datenbank: Seit dem Einteilen kann jemand die Aufgabe privat
    # gestellt oder gelöscht haben. Die Zeilensperre hält den geprüften Stand bis zum Schreiben fest.
    updated = []
    for entry in report.update:
        task = Task.objects.select_for_update().filter(pk=entry["task"].pk, organization=organization).first()
        if task is None or not _darf_aendern(task, membership):
            report.skip.append({"line": entry["line"], "reason": "keine Berechtigung"})
            report.warnings.append(
                f"Zeile {entry['line']}: Aufgabe {entry['task'].pk} ließ sich nicht ändern "
                "(keine Bearbeitungsberechtigung mehr) – übersprungen."
            )
            continue
        entry["task"] = task
        _update_from_row(task, membership, entry["data"], labels, now)
        updated.append(entry)
    report.update[:] = updated
