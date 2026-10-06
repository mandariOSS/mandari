# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endgültiges Löschen in den Fraktionssitzungen nur mit ausdrücklicher Freigabe (Issue #897).

Sven (06.10.2026): „Löschen darf man nur mit expliziter Freigabe. Es sollte aber möglich sein.“

- **Sitzungen und Sitzungsreihen** löscht nur, wer das Recht ``faction.delete`` hat (bestehendes Rechtesystem).
- Vor dem Löschen nennt Work die Folgen mit Zahlen. Sie stammen aus derselben Kaskade, die Django beim Löschen
  abarbeitet (``Collector``), damit nichts unerwähnt mitverschwindet – auch Daten anderer Bereiche, die an der
  Sitzung hängen.
- Gelöscht wird erst nach einer ausdrücklichen Eingabe: Datum oder Titel der Sitzung, Name der Reihe, Nummer oder
  Titel des TOPs. Die Oberfläche gibt den Knopf erst dann frei (``frontend/alpine/delete-confirmation.ts``), der
  Server prüft die Eingabe beim Absenden noch einmal mit derselben Normalisierung.
- Der Audit-Eintrag des gelöschten Objekts hält die Zahlen fest (``_audit_delete_changes``, siehe ``audit.py``).
- **TOPs**: Rechte wie bisher (Verwaltung der Tagesordnung). Eine Eingabe verlangt Work nur, wenn mit dem TOP
  Inhalte verschwinden (Unterpunkte, Protokolleinträge, Beschluss, Anhänge); ein leerer TOP geht wie bisher.

Einen Papierkorb gibt es bewusst nicht; die schonende Alternative ist das Absagen der Sitzung bzw. das Pausieren
der Reihe.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import router, transaction
from django.db.models import Model, ProtectedError, QuerySet, RestrictedError
from django.db.models.deletion import Collector
from django.utils import timezone

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionAgendaItem, FactionMeeting, FactionMeetingSchedule

#: Recht für das endgültige Löschen von Sitzungen und Sitzungsreihen
DELETE_PERMISSION = "faction.delete"

#: Feste Meldungen (nie Ausnahmetexte in HTTP-Antworten)
NOT_ALLOWED = (
    "Keine Berechtigung zum Löschen. Das Recht „Fraktionssitzungen löschen“ vergibt, wer die Rollen verwaltet."
)
NOT_CONFIRMED_MEETING = "Bitte zur Bestätigung das Datum oder den Titel der Sitzung eingeben."
NOT_CONFIRMED_SCHEDULE = "Bitte zur Bestätigung den Namen der Sitzungsreihe eingeben."
NOT_CONFIRMED_ITEM = "Bitte zur Bestätigung die Nummer oder den Titel des TOPs eingeben."
BLOCKED = "Löschen nicht möglich: Andere Daten verweisen noch auf diesen Eintrag."

#: Was mit verschwindet: Bezeichnung je Modell (``app_label.modelname``); andere Modelle mit ihrem Pluralnamen
DELETED_LABELS: dict[str, str] = {
    "work.factionagendaitem": "Tagesordnungspunkte",
    "work.factionattendance": "Anwesenheiten und Zusagen",
    "work.factionprotocolentry": "Protokolleinträge",
    "work.factiondecision": "Beschlüsse",
    "work.factionagendaitemattachment": "Unterlagen (Anhänge an TOPs)",
    "work.factionmeetingexception": "Ausnahmezeiträume",
    "work.factionsuspensionrule": "Ausfallregeln",
}
#: Bezeichnung der Unterpunkte, wenn ein TOP selbst gelöscht wird
SUBITEMS_LABEL = "Unterpunkte"

#: Was erhalten bleibt und nur den Bezug verliert (``SET_NULL``): (Modell, Feld) → Bezeichnung. Übrige
#: Rückverweise (Vorsitzung, Genehmigungs-TOP der Folgesitzung …) sind interne Zeiger und werden nicht genannt.
KEPT_LABELS: dict[tuple[str, str], str] = {
    ("work.task", "related_faction_meeting"): "Aufgaben",
    ("work.task", "related_faction_agenda_item"): "Aufgaben",
    ("work.factionmeeting", "schedule"): "Termine der Reihe",
}


class DeletionError(Exception):
    """Löschen abgelehnt; die Views antworten mit festen Meldungen dieses Moduls."""


class DeletionNotAllowedError(DeletionError):
    """Das Recht ``faction.delete`` fehlt."""


class DeletionNotConfirmedError(DeletionError):
    """Die ausdrückliche Eingabe fehlt oder passt nicht."""


class DeletionBlockedError(DeletionError):
    """Geschützte Verweise (``PROTECT``/``RESTRICT``) verhindern das Löschen."""


@dataclass(frozen=True)
class ImpactLine:
    """Eine Zeile der Folgen: Bezeichnung und Anzahl."""

    label: str
    count: int


@dataclass(frozen=True)
class DeletionImpact:
    """Folgen des Löschens: was mit verschwindet und was bleibt, aber den Bezug verliert."""

    deleted: tuple[ImpactLine, ...] = ()
    kept: tuple[ImpactLine, ...] = ()

    @property
    def has_content(self) -> bool:
        """Verschwinden mit dem Objekt weitere Daten?"""
        return bool(self.deleted)

    def audit_changes(self) -> dict[str, dict[str, int]]:
        """Zahlen für den Audit-Eintrag im Format der Änderungshistorie (vorher → nachher)."""
        changes: dict[str, dict[str, int]] = {}
        for line in self.deleted:
            changes[f"{line.label} (mitgelöscht)"] = {"alt": line.count, "neu": 0}
        for line in self.kept:
            changes[f"{line.label} (Bezug entfernt)"] = {"alt": line.count, "neu": 0}
        return changes


# -- Rechte und Eingabe ---------------------------------------------------------------------------------------


def can_delete(membership: Membership | None) -> bool:
    """Darf die Person Sitzungen und Sitzungsreihen endgültig löschen (``faction.delete``)?"""
    return membership is not None and bool(membership.has_permission(DELETE_PERMISSION))


def normalize(value: Any) -> str:
    """Eingabe vergleichbar machen: Leerraum zusammenfassen, Kleinschreibung (wie ``normalizeConfirmation`` im Frontend)."""
    return " ".join(str(value or "").split()).lower()


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def meeting_phrases(meeting: FactionMeeting) -> list[str]:
    """Angenommene Eingaben für eine Sitzung: Datum (TT.MM.JJJJ, auch ohne führende Nullen) oder Titel."""
    day = timezone.localtime(meeting.start).date()
    return _unique([f"{day:%d.%m.%Y}", f"{day.day}.{day.month}.{day.year}", normalize(meeting.title)])


def schedule_phrases(schedule: FactionMeetingSchedule) -> list[str]:
    """Angenommene Eingabe für eine Sitzungsreihe: ihr Name."""
    return _unique([normalize(schedule.name)])


def item_phrases(item: FactionAgendaItem) -> list[str]:
    """Angenommene Eingaben für einen TOP: Nummer (z. B. „3.1“) oder Titel."""
    return _unique([normalize(item.number), normalize(item.title)])


def phrases_json(phrases: list[str]) -> str:
    """Erwartete Eingaben als JSON für ``data-phrases`` am Formular (Django maskiert das Attribut)."""
    return json.dumps(phrases, ensure_ascii=False)


def confirmed(value: Any, phrases: list[str]) -> bool:
    """Passt die Eingabe zu einer der erwarteten Angaben?"""
    entered = normalize(value)
    return bool(entered) and entered in phrases


# -- Folgen ermitteln -----------------------------------------------------------------------------------------


def _pks(objs: Any) -> set[Any]:
    """Primärschlüssel einer Teilmenge aus der Kaskade (Queryset oder Liste von Objekten)."""
    if isinstance(objs, QuerySet):
        return set(objs.values_list("pk", flat=True))
    return {obj.pk for obj in objs}


def _deleted_label(model: type[Model], root: Model) -> str:
    if model is type(root) and model._meta.label_lower == "work.factionagendaitem":
        return SUBITEMS_LABEL
    return DELETED_LABELS.get(model._meta.label_lower, str(model._meta.verbose_name_plural))


def _through_label(model: type[Model]) -> str | None:
    """Verknüpfungstabelle einer Mehrfachbeziehung (z. B. TOP ↔ Antrag): Bezeichnung des Feldes."""
    for related in model._meta.get_fields():
        target = getattr(getattr(related, "remote_field", None), "model", None)
        if not isinstance(target, type) or not issubclass(target, Model):
            continue
        for m2m in target._meta.many_to_many:
            if m2m.remote_field.through is model:
                return str(m2m.verbose_name)
    return None


class _Tally:
    """Zählt Datensätze je Bezeichnung (Schlüssel: Modell und Primärschlüssel, damit nichts doppelt zählt)."""

    def __init__(self, root: Model) -> None:
        self.root = root
        self.deleted: dict[str, set[tuple[str, Any]]] = defaultdict(set)
        self.kept: dict[str, set[tuple[str, Any]]] = defaultdict(set)

    def removed(self, model: type[Model], pks: set[Any]) -> None:
        if model is type(self.root):
            pks = pks - {self.root.pk}
        if not pks:
            return
        key = model._meta.label_lower
        if model._meta.auto_created:
            # Verknüpfung einer Mehrfachbeziehung: Das verknüpfte Dokument bleibt, nur der Bezug verschwindet
            label = _through_label(model)
            if label:
                self.kept[label].update((key, pk) for pk in pks)
            return
        self.deleted[_deleted_label(model, self.root)].update((key, pk) for pk in pks)

    def detached(self, model: type[Model], field_name: str, pks: set[Any]) -> None:
        label = KEPT_LABELS.get((model._meta.label_lower, field_name))
        if label is not None and pks:
            self.kept[label].update((model._meta.label_lower, pk) for pk in pks)

    def impact(self) -> DeletionImpact:
        return DeletionImpact(
            deleted=tuple(
                ImpactLine(label, len(keys)) for label, keys in _ordered(self.deleted, DELETED_LABELS.values())
            ),
            kept=tuple(ImpactLine(label, len(keys)) for label, keys in _ordered(self.kept, KEPT_LABELS.values())),
        )


def impact_of(obj: Model) -> DeletionImpact:
    """
    Folgen des Löschens von ``obj`` aus der Kaskade, die Django beim Löschen abarbeitet.

    Verknüpfungen von Mehrfachbeziehungen (TOP ↔ Antrag/Dokument, TOP ↔ RIS-Vorlage) zählen als „Bezug entfernt“:
    Es verschwindet nur der Bezug, nicht das Dokument. Löst ein geschützter Verweis eine Sperre aus, meldet
    :class:`DeletionBlockedError` das.
    """
    collector = Collector(using=router.db_for_write(type(obj), instance=obj), origin=obj)
    try:
        collector.collect([obj])
    except (ProtectedError, RestrictedError) as exc:
        raise DeletionBlockedError(BLOCKED) from exc

    tally = _Tally(obj)
    for model, instances in collector.data.items():
        tally.removed(model, {instance.pk for instance in instances})
    for qs in collector.fast_deletes:
        tally.removed(qs.model, _pks(qs))
    for (field, _value), objs_list in collector.field_updates.items():
        for objs in objs_list:
            tally.detached(field.model, field.name, _pks(objs))
    return tally.impact()


def _ordered(groups: dict[str, set[Any]], known: Iterable[str]) -> list[tuple[str, set[Any]]]:
    """Bekannte Bezeichnungen in fester Reihenfolge, danach die übrigen alphabetisch."""
    order = [SUBITEMS_LABEL, *_unique(known)]
    rank = {label: position for position, label in enumerate(order)}
    return sorted(groups.items(), key=lambda pair: (rank.get(pair[0], len(order)), pair[0]))


def upcoming_meetings(schedule: FactionMeetingSchedule) -> int:
    """Noch nicht begonnene Termine der Reihe, die nicht abgesagt sind (bleiben beim Löschen der Reihe bestehen)."""
    return int(schedule.meetings.filter(start__gte=timezone.now()).exclude(status="cancelled").count())


# -- Löschen --------------------------------------------------------------------------------------------------


def _delete_with_audit(obj: Model, impact: DeletionImpact) -> None:
    # Der post_delete-Empfänger der Änderungshistorie übernimmt die Zahlen in den Eintrag des Objekts
    obj._audit_delete_changes = impact.audit_changes()  # type: ignore[attr-defined]
    obj.delete()


@transaction.atomic
def delete_meeting(meeting: FactionMeeting, membership: Membership | None, confirmation: Any) -> DeletionImpact:
    """Sitzung endgültig löschen: nur mit ``faction.delete`` und passender Eingabe (Datum oder Titel)."""
    if not can_delete(membership):
        raise DeletionNotAllowedError(NOT_ALLOWED)
    if not confirmed(confirmation, meeting_phrases(meeting)):
        raise DeletionNotConfirmedError(NOT_CONFIRMED_MEETING)
    impact = impact_of(meeting)
    _delete_with_audit(meeting, impact)
    return impact


@transaction.atomic
def delete_schedule(
    schedule: FactionMeetingSchedule, membership: Membership | None, confirmation: Any
) -> DeletionImpact:
    """Sitzungsreihe endgültig löschen: nur mit ``faction.delete`` und ihrem Namen; die Termine bleiben bestehen."""
    if not can_delete(membership):
        raise DeletionNotAllowedError(NOT_ALLOWED)
    if not confirmed(confirmation, schedule_phrases(schedule)):
        raise DeletionNotConfirmedError(NOT_CONFIRMED_SCHEDULE)
    impact = impact_of(schedule)
    _delete_with_audit(schedule, impact)
    return impact


def deletable_item(meeting: FactionMeeting, item_id: Any) -> FactionAgendaItem | None:
    """TOP einer Sitzung, der sich löschen lässt (der Genehmigungs-TOP nicht); ungültige Kennung → ``None``."""
    from .models import FactionAgendaItem

    try:
        return FactionAgendaItem.objects.filter(id=item_id, meeting=meeting, is_approval_item=False).first()
    except (ValidationError, ValueError):
        return None


@transaction.atomic
def delete_agenda_item(item: FactionAgendaItem, confirmation: Any) -> DeletionImpact:
    """TOP löschen; verschwinden Inhalte mit, nur mit passender Eingabe (Nummer oder Titel)."""
    impact = impact_of(item)
    if impact.has_content and not confirmed(confirmation, item_phrases(item)):
        raise DeletionNotConfirmedError(NOT_CONFIRMED_ITEM)
    _delete_with_audit(item, impact)
    return impact


@dataclass(frozen=True)
class ScheduleDeletion:
    """Angaben für den Löschdialog einer Sitzungsreihe in den Einstellungen."""

    impact: DeletionImpact | None
    phrases_json: str
    upcoming: int
    input_id: str


def with_deletion_details(schedules: Iterable[FactionMeetingSchedule]) -> list[FactionMeetingSchedule]:
    """
    Sitzungsreihen mit den Angaben für ihren Löschdialog (``schedule.deletion``): Folgen, erwartete Eingabe und
    die Zahl der kommenden Termine, die beim Löschen der Reihe bestehen bleiben. Eine Sperre durch geschützte
    Verweise zeigt der Dialog statt der Folgen an (``impact`` ist dann ``None``).
    """
    result: list[FactionMeetingSchedule] = []
    for schedule in schedules:
        try:
            impact: DeletionImpact | None = impact_of(schedule)
        except DeletionBlockedError:
            impact = None
        # Anhängen wie bei anderen Listen der Einstellungen; Django-Vorlagen lesen keine Wörterbücher per Variable
        schedule.deletion = ScheduleDeletion(  # type: ignore[attr-defined]
            impact=impact,
            phrases_json=phrases_json(schedule_phrases(schedule)),
            upcoming=upcoming_meetings(schedule),
            input_id=f"delete-schedule-confirmation-{schedule.pk}",
        )
        result.append(schedule)
    return result
