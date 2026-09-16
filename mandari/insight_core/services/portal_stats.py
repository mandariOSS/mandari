# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zwischengespeicherte Kennzahlen für die Portal-Startseite.

Die Startseite unter ``/insight/`` zeigt Zählwerte über die größten Tabellen des
Portals (Vorgänge, Dateien, Sitzungen). Ohne Cache zählt Postgres sie bei jedem
Seitenaufruf neu; auf dem Produktivbestand kostet das je Aufruf rund 180 ms und
trifft ausgerechnet die Seite, die Suchmaschinen und Erstbesucher zuerst laden.

Die Zahlen ändern sich nur, wenn der Ingestor neue Daten schreibt. Sie ein paar
Minuten lang aus dem Cache zu liefern, ist für die Anzeige unerheblich und nimmt
die Last vollständig von der Datenbank.

Der Ingestor läuft in einem eigenen Prozess und kann den Cache nicht leeren —
deshalb ist die Laufzeit (``STATS_TTL_SECONDS``) die verlässliche Grenze.
``invalidate_portal_stats`` leert ihn zusätzlich sofort, wenn ein Sync innerhalb
von Django durchläuft.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db.models import Count

from ..models import (
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    PublicQuestion,
)

if TYPE_CHECKING:
    from ..models import OParlBody

#: Gültigkeitsdauer der Kennzahlen.
STATS_TTL_SECONDS = 10 * 60

_CACHE_PREFIX = "insight:portal-stats"
_VERSION_KEY = f"{_CACHE_PREFIX}:version"


def _version() -> int:
    """Laufende Nummer aller Kennzahl-Schlüssel (macht das Leeren billig)."""
    version = cache.get(_VERSION_KEY)
    if version is None:
        cache.set(_VERSION_KEY, 1, None)
        return 1
    return int(version)


def invalidate_portal_stats() -> None:
    """Verwirft alle zwischengespeicherten Kennzahlen."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, 1, None)


def _cached[T](name: str, compute: Callable[[], T]) -> T:
    key = f"{_CACHE_PREFIX}:{_version()}:{name}"
    zwischenstand: T | None = cache.get(key)
    if zwischenstand is not None:
        return zwischenstand
    value = compute()
    cache.set(key, value, STATS_TTL_SECONDS)
    return value


def _counts_by_body(model: type[OParlPaper] | type[OParlOrganization] | type[OParlMeeting]) -> dict[str, int]:
    rows = model.objects.filter(deleted=False).values("body").annotate(n=Count("id"))
    return {str(row["body"]): int(row["n"]) for row in rows}


def counts_by_body() -> dict[str, dict[str, int]]:
    """Vorgänge, Gremien und Sitzungen je Kommune (Schlüssel: Kommunen-ID als Text)."""

    def compute() -> dict[str, dict[str, int]]:
        return {
            "papers": _counts_by_body(OParlPaper),
            "organizations": _counts_by_body(OParlOrganization),
            "meetings": _counts_by_body(OParlMeeting),
        }

    return _cached("counts-by-body", compute)


def overview_stats() -> dict[str, int]:
    """Gesamtzahlen über alle gelisteten Kommunen (Auswahlseite)."""

    def compute() -> dict[str, int]:
        # Nicht gelistete Kommunen (z. B. die Demo-Kommune) zählen nicht mit,
        # sonst weicht die Summe von der angezeigten Kommunenliste ab.
        return {
            "organizations": OParlOrganization.objects.filter(deleted=False).exclude(body__is_listed=False).count(),
            "persons": OParlPerson.objects.filter(deleted=False).exclude(body__is_listed=False).count(),
            "meetings": OParlMeeting.objects.filter(deleted=False).exclude(body__is_listed=False).count(),
            "papers": OParlPaper.objects.filter(deleted=False).exclude(body__is_listed=False).count(),
            "files": OParlFile.objects.filter(deleted=False).exclude(body__is_listed=False).count(),
        }

    return _cached("overview", compute)


def body_stats(body: OParlBody) -> dict[str, int]:
    """Kennzahlen einer einzelnen Kommune."""

    def compute() -> dict[str, int]:
        return {
            "organizations": OParlOrganization.objects.filter(body=body, deleted=False).count(),
            "persons": OParlPerson.objects.filter(body=body, deleted=False).count(),
            "meetings": OParlMeeting.objects.filter(body=body, deleted=False).count(),
            "papers": OParlPaper.objects.filter(body=body, deleted=False).count(),
            "files": OParlFile.objects.filter(paper__body=body, deleted=False).count(),
            "public_questions": PublicQuestion.objects.filter(body=body, status="published").count(),
        }

    return _cached(f"body:{body.pk}", compute)
