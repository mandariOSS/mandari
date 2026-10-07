# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verweise anderer Module auf RIS-Daten vor dem Löschen erkennen (Issue #421).

Arbeitsdaten anderer Module hängen an RIS-Objekten: Sitzungsvorbereitungen, Notizen, Positionen und
Redebeiträge an Sitzungen und TOPs, Kommentare an Vorlagen, Anmerkungen an Dateien, geteilte Anträge an
der Kommune, Mandanten-Verknüpfungen an Gremien und Personen. Ein Teil davon ist ``on_delete=CASCADE``:
Djangos Löschung würde diese Daten still mitentfernen. Bei ``SET_NULL`` und M2M verlören sie ihre
Verknüpfung.

Die Prüfung ist generisch und folgt den Relationen der Modelle (``_meta``): Innerhalb von insight_core geht
sie der Lösch-Kaskade nach (eine Sitzung nimmt ihre TOPs und Dateien mit), jede Relation aus einem anderen
Modul zählt als Verweis – unabhängig von ``on_delete``. Neue Verweise anderer Module sind damit ohne
Anpassung abgedeckt. Gepflegte Daten in insight_core selbst (``GEPFLEGTE_MODELLE``) zählen ebenso.

Alles läuft als Datenbankabfrage mit Unterabfragen; es werden keine Objekte in den Speicher geladen, auch
nicht für eine ganze Kommune.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import Any

from django.db import models
from django.db.models import Q, QuerySet
from django.db.models.fields.reverse_related import ForeignObjectRel

# Apps, deren Daten gemeinsam mit den RIS-Objekten gelöscht werden dürfen. insight_sync verweist nur mit
# Protokollen (SyncLog, SET_NULL) auf Quellen; das sind Betriebsdaten, keine Arbeitsdaten.
RIS_APP_LABELS = frozenset({"insight_core", "insight_sync"})
#: Gepflegte Daten in diesen Apps, die wie Arbeitsdaten anderer Module zählen: Sie entstehen nicht aus der Quelle und
#: kämen mit ihr nicht wieder. Fraktionszuordnungen (Issue #916) sind zum Teil von Hand gepflegt.
GEPFLEGTE_MODELLE = frozenset({"insight_core.PersonFraktion"})

ModelType = type[models.Model]


@dataclass(frozen=True)
class ExternalReference:
    """Anzahl der Datensätze eines fremden Modells, die auf die geprüften RIS-Objekte verweisen."""

    label: str  # z. B. "work.MeetingPreparation"
    name: str  # z. B. "Sitzungsvorbereitungen"
    count: int

    def __str__(self) -> str:
        return f"{self.name} ({self.label}): {self.count}"


def _reverse_relations(model: ModelType) -> list[ForeignObjectRel]:
    """Alle Relationen, die auf ``model`` zeigen – auch solche mit ``related_name="+"``.

    Automatisch erzeugte M2M-Zwischentabellen entfallen; sie werden über ihre ManyToManyRel erfasst.
    """
    relations = []
    for field in model._meta.get_fields(include_hidden=True):
        if not isinstance(field, ForeignObjectRel):
            continue
        if field.related_model._meta.auto_created:
            continue
        relations.append(field)
    return relations


def _is_foreign(model: ModelType) -> bool:
    return model._meta.app_label not in RIS_APP_LABELS or model._meta.label in GEPFLEGTE_MODELLE


def _cascades(rel: ForeignObjectRel) -> bool:
    """Löscht Django beim Löschen des Ziels auch die Datensätze dieser Relation?"""
    return not rel.many_to_many and rel.on_delete is models.CASCADE


def _referencing(
    model: ModelType, queryset: QuerySet[Any], path: frozenset[ModelType]
) -> Iterator[tuple[ForeignObjectRel, QuerySet[Any]]]:
    """(Relation eines fremden Moduls, betroffene RIS-Objekte) – entlang der Lösch-Kaskade in insight_core."""
    for rel in _reverse_relations(model):
        related = rel.related_model
        if _is_foreign(related):
            yield rel, queryset
        elif _cascades(rel) and related not in path:
            children = related._base_manager.filter(**{f"{rel.field.name}__in": queryset})
            yield from _referencing(related, children, path | {related})


def external_references(queryset: QuerySet[Any]) -> list[ExternalReference]:
    """Zählt je fremdem Modell die Datensätze, die auf die Objekte des Querysets verweisen.

    Mitgezählt werden Verweise auf Objekte, die die Lösch-Kaskade mitnähme (z. B. TOPs einer Sitzung).
    Jeder fremde Datensatz zählt einmal, auch wenn er über mehrere Wege erreicht wird.
    """
    model = queryset.model
    conditions: dict[ModelType, list[Q]] = {}
    for rel, affected in _referencing(model, queryset, frozenset({model})):
        conditions.setdefault(rel.related_model, []).append(Q(**{f"{rel.field.name}__in": affected}))

    references = []
    for related, parts in conditions.items():
        count = related._base_manager.filter(reduce(or_, parts)).distinct().count()
        if count:
            references.append(
                ExternalReference(
                    label=related._meta.label,
                    name=str(related._meta.verbose_name_plural),
                    count=count,
                )
            )
    return sorted(references, key=lambda ref: ref.label)


def referenced_condition(model: ModelType, path: frozenset[ModelType] | None = None) -> Q | None:
    """Bedingung für Objekte von ``model``, auf die (direkt oder über die Kaskade) fremde Daten verweisen.

    ``None``, wenn auf das Modell überhaupt kein fremdes Modul verweisen kann.
    """
    path = path or frozenset({model})
    parts = []
    for rel in _reverse_relations(model):
        related = rel.related_model
        name = rel.field.name
        if _is_foreign(related):
            referencing = related._base_manager.filter(**{f"{name}__isnull": False})
        elif _cascades(rel) and related not in path:
            child_condition = referenced_condition(related, path | {related})
            if child_condition is None:
                continue
            referencing = related._base_manager.filter(child_condition).filter(**{f"{name}__isnull": False})
        else:
            continue
        parts.append(Q(pk__in=referencing.values(f"{name}__pk")))
    return reduce(or_, parts) if parts else None


def split_by_references(queryset: QuerySet[Any]) -> tuple[QuerySet[Any], QuerySet[Any]]:
    """Teilt ein Queryset in (löschbar, durch Verweise geschützt)."""
    condition = referenced_condition(queryset.model)
    if condition is None:
        return queryset, queryset.none()
    protected = queryset.filter(condition)
    return queryset.exclude(pk__in=protected.values("pk")), protected
