# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreibende Anwendungsfälle für das Aufgaben-Modul (Issue #160, Service-Layer).

Views parsen die Anfrage, prüfen Berechtigungen und rufen hier hinein; die
Funktionen kapseln Statuswechsel, Positionen, Aktivitätsprotokoll und
Benachrichtigungen. Pfade mit mehreren Schreibzugriffen laufen in
``transaction.atomic``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Membership, Organization
from apps.work.files import remove_files_on_commit

from . import ereignisse, selectors
from .activity import log_activity, log_field_change
from .models import Task, TaskActivity, TaskAttachment, TaskChecklistItem, TaskLabel, TaskShare

logger = logging.getLogger(__name__)

VALID_STATUSES = ("todo", "in_progress", "done")
VALID_VISIBILITIES = ("private", "shared", "organization")
#: Felder, die das Panel-Formular ändert (``forms.TaskPanelForm``); nur sie schreibt :func:`apply_panel_update`
PANEL_FELDER = ("title", "description", "status", "priority", "due_date", "assigned_to", "visibility")


class InvalidTaskStatusError(ValueError):
    """Unbekannter Spaltenstatus."""


def _hub() -> Any:
    """NotificationHub spät importieren (Zyklus work.notifications → work.tasks) und untypisiert durchreichen."""
    from apps.work.notifications.services import NotificationHub

    return NotificationHub


def _erledigt(task: Task, membership: Membership) -> None:
    """Erledigung melden (Datendrehscheibe) bzw. wie bisher direkt benachrichtigen."""
    if not ereignisse.melden(ereignisse.ERLEDIGT, task, membership):
        _hub().notify_task_completed(task, membership)


def can_edit_task(task: Task, membership: Membership) -> bool:
    """
    Ersteller, Zuständige (zugewiesene Person und weitere Zuständige, Issue #1462) und Mitglieder mit
    ``tasks.manage`` – letztere nur bei Aufgaben, die sie sehen dürfen.

    Aufgaben aus einem nichtöffentlichen TOP ändert nur, wer den TOP heute sehen darf (``Task.noe_gesperrt``): Wer sie
    nicht mehr öffnen kann, hakt sie auch über Board und Sitzungsansicht nicht mehr ab.
    """
    if task.noe_gesperrt(membership):
        return False
    if task.created_by == membership or task.assigned_to == membership:
        return True
    if membership.has_permission("tasks.manage") and task.can_access(membership):
        return True
    # Zuletzt, weil es eine Abfrage kosten kann (die Listen laden die weiteren Zuständigen vor)
    return task.ist_zustaendig(membership)


NICHT_MEHR_AENDERBAR = "Diese Aufgabe lässt sich nicht mehr ändern."


def _aktueller_stand(task: Task, membership: Membership) -> Task:
    """
    Aufgabe unter Zeilensperre neu lesen und das Bearbeitungsrecht am jetzigen Stand prüfen.

    Nur innerhalb einer Transaktion aufrufen. Das Objekt aus dem View kann überholt sein: Hat die erstellende Person
    die Aufgabe inzwischen privat gestellt oder neu zugewiesen, führt die laufende Aktion nichts mehr aus
    (``PermissionDenied``, Antwort 403). Die Sperre hält den geprüften Stand bis zum Schreiben fest.
    """
    aktuell = Task.objects.select_for_update().filter(pk=task.pk).first()
    if aktuell is None or not can_edit_task(aktuell, membership):
        raise PermissionDenied(NICHT_MEHR_AENDERBAR)
    return aktuell


def _auf_aktuellen_stand(task: Task, membership: Membership) -> None:
    """
    Wie :func:`_aktueller_stand`, bringt aber das übergebene Objekt selbst auf den Stand der Datenbank: Die
    Statusaktionen arbeiten so mit den jetzigen Werten weiter, und der Aufrufer sieht das Ergebnis am selben Objekt.
    """
    _aktueller_stand(task, membership)
    task.refresh_from_db()


def weitere_zustaendige_ids(task: Task) -> set[Any]:
    """Kennungen der weiteren Zuständigen, wie sie jetzt in der Datenbank stehen."""
    return set(Task.weitere_zustaendige.through.objects.filter(task_id=task.pk).values_list("membership_id", flat=True))


def _weitere_bereinigt(task: Task, personen: Iterable[Membership]) -> list[Membership]:
    """
    Weitere Zuständige, wie sie gespeichert werden: Mitglieder der Organisation der Aufgabe, ohne Gastzugänge, ohne
    die zugewiesene Person (sie steht in ``assigned_to``) und ohne Doppelte.
    """
    bereinigt: dict[Any, Membership] = {}
    for person in personen:
        if person.organization_id != task.organization_id or person.is_guest or person.pk == task.assigned_to_id:
            continue
        bereinigt[person.pk] = person
    return list(bereinigt.values())


def weitere_gespeichert(task: Task, auswahl: Iterable[Membership]) -> list[Membership]:
    """
    Was aus der Auswahl eines Panels als weitere Zuständige gespeichert ist: die Auswahl ohne die zugewiesene Person
    und ohne Unzulässige (:func:`_weitere_bereinigt`). Das Panel merkt sich diesen Stand; eine Person, die es zwar
    angekreuzt zeigt, die aber gerade zugewiesen ist, gilt beim nächsten Speichern wieder als neu gewählt.
    """
    return _weitere_bereinigt(task, auswahl)


def _weitere_nach_stand(task: Task, auswahl: Iterable[Membership], stand: set[str]) -> list[Membership]:
    """
    Auswahl eines Panels auf den jetzigen Stand der Datenbank anwenden. ``stand`` sind die Kennungen, die das Panel
    zuletzt als gewählt kannte: Hinzu kommt, wen es neu gewählt hat; heraus fällt, wen es abgewählt hat. Wen es noch
    nicht kannte (inzwischen von anderen hinzugefügt), bleibt; wen andere inzwischen herausgenommen haben, kommt nicht
    zurück.
    """
    gewaehlt = {str(person.pk): person for person in auswahl}
    # Frisch gelesen (order_by umgeht einen vorgeladenen Bestand): Der Aufrufer hält die Zeile der Aufgabe gesperrt
    bleiben = [
        person
        for person in task.weitere_zustaendige.order_by("pk")
        if str(person.pk) in gewaehlt or str(person.pk) not in stand
    ]
    vorhanden = {str(person.pk) for person in bleiben}
    neu = [person for kennung, person in gewaehlt.items() if kennung not in stand and kennung not in vorhanden]
    return [*bleiben, *neu]


def _zuweisung_melden(task: Task, person: Membership, membership: Membership, *, created: bool) -> None:
    """„Aufgabe zugewiesen“ an eine zuständige Person (Datendrehscheibe bzw. wie bisher direkt), nie an sich selbst."""
    uebernommen = ereignisse.melden(ereignisse.ZUGEWIESEN, task, membership, assignee=person.pk, created=created)
    if not uebernommen and person != membership:
        _hub().notify_task_assigned(task, person, membership)


def _sync_completion(task: Task, *, previous_status: str | None = None) -> None:
    """Hält ``is_completed``/``completed_at`` konsistent zum Status."""
    if previous_status is None:
        if task.status == "done" and not task.is_completed:
            task.is_completed = True
            task.completed_at = timezone.now()
        elif task.status != "done" and task.is_completed:
            task.is_completed = False
            task.completed_at = None
        return
    if task.status == "done" and previous_status != "done":
        task.is_completed = True
        task.completed_at = timezone.now()
    elif task.status != "done" and previous_status == "done":
        task.is_completed = False
        task.completed_at = None


# ---------------------------------------------------------------------------
# Anlegen, Sichtbarkeit, Labels, Protokoll-Import
# ---------------------------------------------------------------------------


@transaction.atomic
def create_task(
    task: Task,
    organization: Organization,
    membership: Membership,
    *,
    related_motion_id: str | None = None,
    related_agenda_item: Any = None,
    weitere_zustaendige: Iterable[Membership] = (),
) -> Task:
    """
    Speichert eine über das Formular vorbereitete Aufgabe (``form.save(commit=False)``).

    Setzt Organisation, Ersteller, Standard-Zuweisung, optionale Dokument-Verknüpfung und
    die Position am Spaltenende; protokolliert und benachrichtigt bei Fremdzuweisung.
    ``weitere_zustaendige``: zusätzliche Zuständige neben der zugewiesenen Person (Issue #1462); jede wird
    benachrichtigt wie die zugewiesene.
    ``related_agenda_item``: TOP aus der Sitzungsvorbereitung (#856), vom Aufrufer bereits an
    die Org-Grenze gebunden; die Sitzung wird mit verknüpft.
    """
    task.organization = organization
    task.created_by = membership
    if not task.assigned_to:
        task.assigned_to = membership
    if related_motion_id:
        motion = selectors.find_motion(organization, membership, related_motion_id)
        if motion is not None:
            task.related_motion = motion
    if related_agenda_item is not None:
        task.related_agenda_item = related_agenda_item
        task.related_meeting_id = related_agenda_item.meeting_id
    task.position = selectors.next_position(organization, task.status)
    task.save()
    weitere = _weitere_bereinigt(task, weitere_zustaendige)
    if weitere:
        task.weitere_zustaendige.set(weitere)
    log_activity(task, membership, "created")
    if task.assigned_to:
        uebernommen = ereignisse.melden(
            ereignisse.ZUGEWIESEN, task, membership, assignee=task.assigned_to_id, created=True
        )
        if not uebernommen and task.assigned_to != membership:
            _hub().notify_task_assigned(task, task.assigned_to, membership)
    for person in weitere:
        _zuweisung_melden(task, person, membership, created=True)
    return task


def _org_member_ids(organization_id: Any, raw_ids: Sequence[str]) -> list[Any]:
    """IDs aktiver Mitgliedschaften (ohne Gäste) der Organisation aus einer Formularliste; Fremdes fällt weg."""
    parsed = []
    for raw_id in raw_ids:
        try:
            parsed.append(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError, AttributeError):
            continue
    return list(
        Membership.objects.filter(
            id__in=parsed, organization_id=organization_id, is_active=True, is_guest=False
        ).values_list("id", flat=True)
    )


def reset_shares_on_reshare(task: Task, old_visibility: str) -> None:
    """
    Aufgabe ohne Freigabedialog (Panel, Dateiimport) auf „geteilt“ gestellt: frühere Empfänger entfallen.

    Freigabezeilen zählen nur bei Sichtbarkeit „geteilt“ (``selectors.visible_tasks``, ``Task.can_access``). Wechselt
    eine Aufgabe über Panel oder Import auf eine andere Sichtbarkeit, bleiben sie ohne Wirkung stehen; ein
    veraltetes Panel kann so keine neuen Freigaben löschen. Wird die Aufgabe später auf demselben Weg wieder
    geteilt, bekämen die früheren Empfänger ungefragt wieder Zugriff: Deshalb beginnt sie dann ohne Empfänger,
    sie werden im Freigabedialog gewählt (``update_visibility``).
    """
    if task.visibility == "shared" and old_visibility != "shared":
        TaskShare.objects.filter(task=task).delete()


def _protocol_entry_taken(entry: Any, organization: Organization) -> bool:
    """
    Protokolleintrag bis zum Ende der Transaktion sperren und prüfen, ob es zu ihm schon eine Aufgabe gibt.

    Beide Wege, die aus einem Eintrag eine Aufgabe machen (Sitzungsansicht und Übernahme im Aufgabenbereich),
    gehen hier durch: Gleichzeitige Aufrufe laufen nacheinander, der zweite sieht die Aufgabe des ersten.
    """
    type(entry).objects.select_for_update().filter(pk=entry.pk).first()
    return Task.objects.filter(organization=organization, related_protocol_entry=entry).exists()


@transaction.atomic
def task_from_protocol_entry(entry: Any, membership: Membership, *, title: str, description: str) -> Task | None:
    """
    Aufgabe aus einem Protokolleintrag der Fraktionssitzung anlegen; ``None``, wenn es zu dem Eintrag schon eine gibt.

    Der Eintrag wird für die Dauer der Transaktion gesperrt: Zwei gleichzeitige Aufrufe legen nacheinander an und
    der zweite sieht die Aufgabe des ersten.
    """
    if _protocol_entry_taken(entry, entry.meeting.organization):
        return None
    return Task.objects.create(
        organization=entry.meeting.organization,
        title=title,
        description=description,
        assigned_to=entry.action_assignee,
        due_date=entry.action_due_date,
        created_by=membership,
        related_faction_meeting=entry.meeting,
        related_protocol_entry=entry,
    )


@transaction.atomic
def update_visibility(task: Task, membership: Membership, visibility: str, share_with_ids: Sequence[str]) -> None:
    """
    Sichtbarkeit setzen und Freigaben angleichen (nur bei ``shared`` bleiben Freigaben bestehen). Das
    Bearbeitungsrecht gilt am jetzigen Stand der Datenbank (:func:`_aktueller_stand`).
    """
    _aktueller_stand(task, membership)
    if visibility in VALID_VISIBILITIES:
        task.visibility = visibility
        task.save(update_fields=["visibility"])
    if visibility == "shared":
        # Nur aktive Mitglieder (keine Gastzugänge) derselben Organisation
        member_ids = _org_member_ids(task.organization_id, share_with_ids)
        TaskShare.objects.filter(task=task).exclude(membership_id__in=member_ids).delete()
        for member_id in member_ids:
            TaskShare.objects.get_or_create(task=task, membership_id=member_id, defaults={"shared_by": membership})
    else:
        TaskShare.objects.filter(task=task).delete()


def delete_personal_tasks(membership: Membership) -> int:
    """
    Persönliche Aufgaben eines Mitglieds löschen (Issue #420, :func:`selectors.personal_tasks`).

    Wird beim Entfernen der Mitgliedschaft aufgerufen. Alle anderen Aufgaben bleiben erhalten; der
    Verweis auf das Mitglied wird geleert. Die Dateien der Anhänge verschwinden nach erfolgreicher
    Transaktion mit (Issue #1237). Liefert die Anzahl gelöschter Aufgaben.
    """
    task_ids = list(selectors.personal_tasks(membership).values_list("pk", flat=True))
    files = [attachment.file for attachment in TaskAttachment.objects.filter(task_id__in=task_ids)]
    # Wie in ``delete_task``: Nachrichten zu Aufgaben aus nichtöffentlichen TOPs vorher kennzeichnen. Sie können bei
    # anderen liegen, etwa bei einer Vertretung, an die weitergeleitet wurde.
    nichtoeffentlich = (
        Task.objects.filter(pk__in=task_ids)
        .filter(selectors.noe_top_q())
        .select_related("related_faction_agenda_item__parent")
    )
    for task in nichtoeffentlich:
        _hub().aufgabe_wird_geloescht(task)
    Task.objects.filter(pk__in=task_ids).delete()
    remove_files_on_commit(files)
    return len(task_ids)


def save_label(organization: Organization, label: TaskLabel) -> TaskLabel:
    """Label der Organisation zuordnen und speichern."""
    label.organization = organization
    label.save()
    return label


def delete_label(label: TaskLabel) -> None:
    """Label löschen (M2M-Zuordnungen fallen mit)."""
    label.delete()


@transaction.atomic
def import_protocol_entries(organization: Organization, membership: Membership, entry_ids: Iterable[str]) -> int:
    """
    Aufgaben aus Protokolleinträgen (Typ ``action``) anlegen; liefert die Anzahl.

    Schon übernommene oder erledigte Einträge werden übersprungen – ein erneutes Absenden legt
    keine doppelten Aufgaben an. Die Aufgabe merkt sich ihre Herkunft (``related_protocol_entry``).
    """
    imported_ids, legacy_keys = selectors.protocol_import_state(organization)
    created = 0
    for entry_id in entry_ids:
        entry = selectors.find_protocol_entry(organization, membership, entry_id, action_only=True)
        if (
            entry is None
            or entry.action_completed
            or selectors.is_protocol_entry_imported(entry, legacy_keys, imported_ids)
        ):
            continue
        imported_ids.add(entry.id)
        # Unter der Sperre des Eintrags erneut prüfen: Der Stand oben kann vor einer gleichzeitigen Übernahme liegen
        if _protocol_entry_taken(entry, organization):
            continue
        task = Task.objects.create(
            organization=organization,
            title=entry.content[:500] if entry.content else "Protokoll-Aufgabe",
            created_by=membership,
            assigned_to=entry.action_assignee or membership,
            due_date=entry.action_due_date,
            status="todo",
            priority="medium",
            position=selectors.next_position(organization, "todo"),
            related_faction_meeting=entry.meeting,
            related_protocol_entry=entry,
        )
        log_activity(task, membership, "created")
        created += 1
    return created


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


@transaction.atomic
def quick_add_task(
    organization: Organization, membership: Membership, *, title: str, status: str, priority: str
) -> Task:
    """Schnelleingabe: Aufgabe am Spaltenende anlegen, dem Ersteller zuweisen, protokollieren."""
    task = Task.objects.create(
        organization=organization,
        title=title,
        status=status,
        priority=priority,
        position=selectors.next_position(organization, status),
        created_by=membership,
        assigned_to=membership,
    )
    log_activity(task, membership, "created")
    return task


@transaction.atomic
def move_task(task: Task, membership: Membership, *, new_status: str, new_position: int) -> Task:
    """
    Drag-and-drop: Aufgabe in Spalte/Position verschieben.

    Aktualisiert Erledigt-Flags, protokolliert Statuswechsel, benachrichtigt bei
    Erledigung und sortiert Ziel- und Quellspalte lückenlos nach. Geprüft und geschrieben wird am jetzigen Stand
    (:func:`_auf_aktuellen_stand`), und zwar nur Status, Position und Erledigung: Sichtbarkeit und Zuweisung
    bleiben, wie sie in der Datenbank stehen.
    """
    if new_status not in VALID_STATUSES:
        raise InvalidTaskStatusError(new_status)
    _auf_aktuellen_stand(task, membership)
    organization = task.organization
    old_status = task.status
    task.status = new_status
    task.position = new_position
    _sync_completion(task, previous_status=old_status)
    task.save(update_fields=["status", "position", "is_completed", "completed_at", "updated_at"])

    if old_status != new_status:
        if new_status == "done":
            log_activity(task, membership, "completed")
            _erledigt(task, membership)
        elif old_status == "done":
            log_activity(task, membership, "reopened")
        else:
            labels = dict(Task.STATUS_CHOICES)
            log_field_change(
                task,
                membership,
                "status",
                labels.get(old_status, old_status),
                labels.get(new_status, new_status),
                "status_changed",
            )

    for idx, other in enumerate(selectors.column_tasks(organization, new_status, exclude_id=task.id)):
        correct_pos = idx if idx < new_position else idx + 1
        if other.position != correct_pos:
            Task.objects.filter(id=other.id).update(position=correct_pos)

    if old_status != new_status:
        for idx, other in enumerate(selectors.column_tasks(organization, old_status)):
            if other.position != idx:
                Task.objects.filter(id=other.id).update(position=idx)
    return task


@transaction.atomic
def set_status(task: Task, membership: Membership, new_status: str) -> Task:
    """
    Status ohne Protokoll setzen (Board-Aktion ``update_status``); geprüft und geschrieben wird am jetzigen Stand,
    und zwar nur Status und Erledigung (wie :func:`move_task`).
    """
    _auf_aktuellen_stand(task, membership)
    old_status = task.status
    task.status = new_status
    if new_status == "done" and old_status != "done":
        task.is_completed = True
        task.completed_at = timezone.now()
    elif new_status != "done":
        task.is_completed = False
        task.completed_at = None
    task.save(update_fields=["status", "is_completed", "completed_at", "updated_at"])
    return task


@transaction.atomic
def toggle_completion(
    task: Task, membership: Membership, *, reopen_status: str = "in_progress", record_activity: bool = True
) -> Task:
    """
    Erledigt-Status umschalten.

    Board-Karte: ``reopen_status="todo"`` ohne Protokoll; Panel: ``in_progress`` mit
    Protokolleintrag und Benachrichtigung bei Erledigung. Geprüft und geschrieben wird am jetzigen Stand, und zwar
    nur Status und Erledigung (wie :func:`move_task`).
    """
    _auf_aktuellen_stand(task, membership)
    if task.is_completed:
        task.is_completed = False
        task.completed_at = None
        task.status = reopen_status
        if record_activity:
            log_activity(task, membership, "reopened")
    else:
        task.is_completed = True
        task.completed_at = timezone.now()
        task.status = "done"
        if record_activity:
            log_activity(task, membership, "completed")
    task.save(update_fields=["status", "is_completed", "completed_at", "updated_at"])
    if task.is_completed and record_activity:
        _erledigt(task, membership)
    return task


# ---------------------------------------------------------------------------
# Panel: Formular, Kommentare, Anhänge, Checkliste, Labels
# ---------------------------------------------------------------------------


def capture_old_values(task: Task) -> dict[str, Any]:
    """
    Alte Werte für das Aktivitätsprotokoll festhalten.

    Muss vor ``form.is_valid()`` aufgerufen werden, weil ein ModelForm die Instanz
    bereits bei der Validierung überschreibt.
    """
    weitere = list(Membership.objects.filter(mitzustaendige_aufgaben=task).select_related("user") if task.pk else [])
    return {
        "status": task.get_status_display(),
        "priority": task.get_priority_display(),
        "due_date": str(task.due_date) if task.due_date else "—",
        "assigned_to": task.assigned_to.user.get_display_name() if task.assigned_to else "—",
        "visibility": task.get_visibility_display(),
        "weitere": _namen(weitere),
        "status_raw": task.status,
        "priority_raw": task.priority,
        "due_date_raw": task.due_date,
        "assigned_to_raw": task.assigned_to_id,
        "visibility_raw": task.visibility,
        "weitere_raw": frozenset(m.pk for m in weitere),
    }


def _namen(personen: Iterable[Membership]) -> str:
    """Namen für den Verlauf, alphabetisch; ohne Personen ein Strich."""
    namen = sorted((m.user.get_display_name() for m in personen), key=str.lower)
    return ", ".join(namen) if namen else "—"


def _log_changes(task: Task, membership: Membership, old: dict[str, Any]) -> None:
    """Feldänderungen protokollieren und Betroffene benachrichtigen."""
    hub = _hub()
    changes: list[tuple[str, str, str, str]] = []
    if old["status_raw"] != task.status:
        if task.status == "done":
            log_activity(task, membership, "completed")
            _erledigt(task, membership)
        elif old["status_raw"] == "done":
            log_activity(task, membership, "reopened")
        else:
            changes.append(("status", old["status"], task.get_status_display(), "status_changed"))

    assignee_changed = old["assigned_to_raw"] != task.assigned_to_id
    if assignee_changed and task.assigned_to:
        uebernommen = ereignisse.melden(
            ereignisse.ZUGEWIESEN, task, membership, assignee=task.assigned_to_id, created=False
        )
        if not uebernommen and task.assigned_to != membership:
            hub.notify_task_assigned(task, task.assigned_to, membership)
    if old["priority_raw"] != task.priority:
        changes.append(("priority", old["priority"], task.get_priority_display(), "priority_changed"))
    if old["due_date_raw"] != task.due_date:
        changes.append(("due_date", old["due_date"], str(task.due_date) if task.due_date else "—", "due_date_changed"))
    if assignee_changed:
        new_assigned = task.assigned_to.user.get_display_name() if task.assigned_to else "—"
        changes.append(("assigned_to", old["assigned_to"], new_assigned, "assigned"))
    # Weitere Zuständige (Issue #1462): wer neu dazukommt, erfährt es wie die zugewiesene Person
    alt_weitere = set(old.get("weitere_raw", ()))
    neu_weitere = weitere_zustaendige_ids(task)
    if alt_weitere != neu_weitere:
        personen = {m.pk: m for m in Membership.objects.filter(pk__in=neu_weitere).select_related("user")}
        changes.append(
            (
                "weitere_zustaendige",
                f"Weitere Zuständige: {old.get('weitere', '—')}",
                f"Weitere Zuständige: {_namen(personen.values())}",
                "assigned",
            )
        )
        for kennung in sorted(neu_weitere - alt_weitere, key=str):
            if kennung in personen:
                _zuweisung_melden(task, personen[kennung], membership, created=False)
    if old["visibility_raw"] != task.visibility:
        changes.append(("visibility", old["visibility"], task.get_visibility_display(), "visibility_changed"))

    for field_name, old_val, new_val, activity_type in changes:
        log_field_change(task, membership, field_name, old_val, new_val, activity_type)


@transaction.atomic
def apply_panel_update(
    task: Task,
    membership: Membership,
    old_values: dict[str, Any],
    *,
    weitere_zustaendige: Iterable[Membership] | None = None,
    weitere_stand: set[str] | None = None,
) -> Task:
    """
    Vom Panel-Formular geänderte Aufgabe (``form.save(commit=False)``) speichern.

    Gleicht die Erledigt-Flags an den Status an, protokolliert Feldänderungen und
    benachrichtigt Betroffene. ``old_values`` stammt aus :func:`capture_old_values`.
    ``weitere_zustaendige``: die neue Auswahl der weiteren Zuständigen; ``None`` lässt sie, wie sie sind (ein Panel
    von vor der Funktion sendet das Feld nicht mit). ``weitere_stand``: die Auswahl, die das Panel zuletzt kannte;
    damit gilt nur der Unterschied (:func:`_weitere_nach_stand`), ohne Angabe ersetzt die Auswahl den Bestand.

    Das Bearbeitungsrecht gilt am jetzigen Stand der Datenbank (:func:`_aktueller_stand`), nicht an dem, den der View
    gelesen hat. Geschrieben werden nur die Felder des Formulars und die Erledigung.
    """
    aktuell = _aktueller_stand(task, membership)
    # Erledigung und Verlauf gehen vom jetzigen Stand der Datenbank aus, nicht von dem beim Öffnen des Panels: Ein
    # überholtes Panel schriebe sonst einen alten Erledigungszeitpunkt zurück oder trüge die Erledigung noch einmal ein.
    task.is_completed = aktuell.is_completed
    task.completed_at = aktuell.completed_at
    _sync_completion(task)
    task.save(update_fields=[*PANEL_FELDER, "is_completed", "completed_at", "updated_at"])
    # Ausgangswert ist die Sichtbarkeit in der Datenbank, nicht die beim Öffnen des Panels gelesene: Hat inzwischen
    # jemand im Freigabedialog Empfänger gewählt, bleiben sie stehen.
    reset_shares_on_reshare(task, aktuell.visibility)
    alt = {**old_values, **capture_old_values(aktuell)}
    if weitere_zustaendige is not None:
        auswahl = list(weitere_zustaendige)
        if weitere_stand is not None:
            auswahl = _weitere_nach_stand(task, auswahl, weitere_stand)
        task.weitere_zustaendige.set(_weitere_bereinigt(task, auswahl))
    elif task.assigned_to is not None and task.assigned_to.pk in alt["weitere_raw"]:
        # Eine der weiteren Zuständigen ist jetzt die zugewiesene Person: Sie steht nicht doppelt
        task.weitere_zustaendige.remove(task.assigned_to)
    _log_changes(task, membership, alt)
    return task


@transaction.atomic
def add_comment(task: Task, membership: Membership, content: str) -> TaskActivity:
    """Kommentar als Aktivität anlegen und Beteiligte benachrichtigen."""
    activity = log_activity(task, membership, "comment", content=content)
    if not ereignisse.melden(ereignisse.KOMMENTIERT, task, membership, comment=activity.id):
        _hub().notify_task_comment(task, activity, membership)
    return activity


@transaction.atomic
def delete_task(task: Task) -> None:
    """
    Aufgabe mit allen abhängigen Objekten löschen.

    Benachrichtigungen zur Aufgabe bleiben gespeichert. Gehört sie zu einem nichtöffentlichen TOP, werden sie vorher
    gekennzeichnet, damit sie für nicht Vereidigte verborgen bleiben (``NotificationHub.aufgabe_wird_geloescht``).
    """
    _hub().aufgabe_wird_geloescht(task)
    task.delete()


@transaction.atomic
def add_attachment(
    task: Task, membership: Membership, attachment: TaskAttachment, uploaded: UploadedFile[Any]
) -> TaskAttachment:
    """Anhang (aus ``form.save(commit=False)``) mit Metadaten speichern und protokollieren."""
    import mimetypes

    attachment.task = task
    attachment.uploaded_by = membership
    attachment.filename = uploaded.name or ""
    attachment.mime_type = (
        uploaded.content_type or mimetypes.guess_type(uploaded.name or "")[0] or "application/octet-stream"
    )
    attachment.file_size = uploaded.size or 0
    attachment.save()
    log_activity(task, membership, "attachment_added", details={"filename": uploaded.name})
    return attachment


@transaction.atomic
def remove_attachment(task: Task, membership: Membership, attachment: TaskAttachment) -> str:
    """Anhang samt Datei löschen; liefert den Dateinamen für die Rückmeldung."""
    filename = attachment.filename
    attachment.file.delete(save=False)
    attachment.delete()
    log_activity(task, membership, "attachment_removed", details={"filename": filename})
    return filename


@transaction.atomic
def add_checklist_item(task: Task, membership: Membership, item: TaskChecklistItem) -> TaskChecklistItem:
    """Checklistenpunkt (aus ``form.save(commit=False)``) am Ende anhängen und protokollieren."""
    item.task = task
    item.position = task.checklist_items.count()
    item.save()
    log_activity(task, membership, "checklist_item_added", details={"title": item.title})
    return item


@transaction.atomic
def toggle_checklist_item(task: Task, membership: Membership, item: TaskChecklistItem) -> TaskChecklistItem:
    """Checklistenpunkt abhaken bzw. wieder öffnen und protokollieren."""
    item.is_completed = not item.is_completed
    item.save(update_fields=["is_completed"])
    activity_type = "checklist_item_completed" if item.is_completed else "checklist_item_unchecked"
    log_activity(task, membership, activity_type, details={"title": item.title})
    return item


def delete_checklist_item(item: TaskChecklistItem) -> None:
    """Checklistenpunkt löschen."""
    item.delete()


@transaction.atomic
def reorder_checklist(task: Task, order: Sequence[Any]) -> None:
    """Checklisten-Reihenfolge übernehmen (nur Punkte dieser Aufgabe)."""
    for idx, item_id in enumerate(order):
        TaskChecklistItem.objects.filter(id=item_id, task=task).update(position=idx)


@transaction.atomic
def toggle_label(task: Task, membership: Membership, label: TaskLabel) -> bool:
    """Label an-/abwählen; liefert ``True``, wenn es hinzugefügt wurde."""
    if selectors.has_label(task, label.id):
        task.labels.remove(label)
        log_activity(task, membership, "label_removed", details={"label": label.name})
        return False
    task.labels.add(label)
    log_activity(task, membership, "label_added", details={"label": label.name})
    return True
