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

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.core.cache import cache
from django.db.models import Count, QuerySet
from django.utils import timezone

from ..models import (
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    PublicQuestion,
)
from .personen_sichtbarkeit import nur_oeffentliche

if TYPE_CHECKING:
    from ..models import OParlBody

logger = logging.getLogger(__name__)

#: Gültigkeitsdauer der Kennzahlen.
STATS_TTL_SECONDS = 10 * 60

#: Vorberechneter Stand der öffentlichen Zahlen für ``/api/stats/`` und ``/api/stats/bodies/`` (Issue #1478)
API_STAND_KEY = "insight:api-stats:stand:v1"
#: Der Zeitplan ``oeffentliche_zahlen_fortschreiben`` schreibt ihn alle zehn Minuten neu. Die Frist begrenzt das Alter
#: der Zahlen, wenn der Worker steht oder Web und Worker keinen gemeinsamen Cache haben: Dann zählt ein Abruf je Frist.
API_STAND_TTL_SECONDS = 30 * 60

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


def _counts_by_body(
    model: type[OParlPaper] | type[OParlOrganization] | type[OParlMeeting] | type[OParlPerson],
) -> dict[str, int]:
    bestand: QuerySet[Any] = model.objects.filter(deleted=False)
    if model is OParlPerson:
        # Gezählt wird, was das Portal zeigt: Personen mit laufendem Amt (Issue #1560)
        bestand = nur_oeffentliche(OParlPerson.objects.all())
    rows = bestand.values("body").annotate(n=Count("id"))
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
            "persons": nur_oeffentliche(OParlPerson.objects.exclude(body__is_listed=False)).count(),
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
            "persons": nur_oeffentliche(OParlPerson.objects.filter(body=body)).count(),
            "meetings": OParlMeeting.objects.filter(body=body, deleted=False).count(),
            "papers": OParlPaper.objects.filter(body=body, deleted=False).count(),
            "files": OParlFile.objects.filter(paper__body=body, deleted=False).count(),
            "public_questions": PublicQuestion.objects.filter(body=body, status="published").count(),
        }

    return _cached(f"body:{body.pk}", compute)


# ---------------------------------------------------------------------------
# Öffentliche Zahlen für /api/stats/ und /api/stats/bodies/ (Issue #1478)
# ---------------------------------------------------------------------------


def _api_stand_berechnen() -> dict[str, Any]:
    """Gremien, Personen, Sitzungen und Vorgänge je Kommune: vier Abfragen, je eine über die ganze Tabelle."""
    return {
        "ermittelt_am": timezone.now().isoformat(timespec="seconds"),
        "je_kommune": {
            "organizations": _counts_by_body(OParlOrganization),
            "persons": _counts_by_body(OParlPerson),
            "meetings": _counts_by_body(OParlMeeting),
            "papers": _counts_by_body(OParlPaper),
        },
    }


#: Die vier Zählungen eines Stands
_API_ARTEN = ("organizations", "persons", "meetings", "papers")


def _api_stand_brauchbar(stand: object) -> bool:
    """Alle vier Zählungen je Kommune sind da; leere Zählungen sind gültig (Installation ohne Daten)."""
    if not isinstance(stand, dict):
        return False
    je_kommune = stand.get("je_kommune")
    return isinstance(je_kommune, dict) and all(isinstance(je_kommune.get(art), dict) for art in _API_ARTEN)


def api_stand(*, refresh: bool = False) -> dict[str, Any]:
    """
    Zuletzt ermittelte Zahlen je Kommune für ``/api/stats/`` und ``/api/stats/bodies/``, ohne im Abruf zu zählen.

    Die Zählungen laufen über die größten Tabellen des Portals (in Produktion rund 590.000 Vorgänge) und brauchten
    im Abruf 3 bis 5 s, unter Last über 10 s (Issue #1478). Der Zeitplan ``oeffentliche_zahlen_fortschreiben``
    schreibt den Stand alle zehn Minuten (``refresh=True``). Fehlt er (erster Start, geleerter Cache), zählt der
    erste Abruf einmal selbst und legt ihn ab.

    Lässt sich der Stand nicht ablegen, bekommt ein Abruf trotzdem seine Zahlen (Warnung im Protokoll). Mit
    ``refresh=True`` geht der Fehler an den Aufrufer: Der Lauf des Zeitplans gilt dann als fehlgeschlagen, statt
    still einen alten Stand stehen zu lassen.

    Ein Abruf ohne Stand legt seine Zählung nur ab, wenn inzwischen kein anderer Stand da ist (``cache.add``): Hat
    der Zeitplan während der Zählung einen neueren abgelegt, bleibt der stehen, und seine Frist verlängert sich nicht.
    """
    vorhanden: Any = None
    if not refresh:
        try:
            vorhanden = cache.get(API_STAND_KEY)
        except Exception:  # noqa: BLE001 – ohne Cache zählt der Abruf selbst
            logger.warning("Stand der öffentlichen Zahlen nicht lesbar", exc_info=True)
        if _api_stand_brauchbar(vorhanden):
            stand: dict[str, Any] = vorhanden
            return stand
    stand = _api_stand_berechnen()
    try:
        if refresh or vorhanden is not None:
            # Zeitplan: immer fortschreiben. Abruf mit unbrauchbarem Wert im Cache: ihn ersetzen.
            cache.set(API_STAND_KEY, stand, API_STAND_TTL_SECONDS)
        else:
            cache.add(API_STAND_KEY, stand, API_STAND_TTL_SECONDS)
    except Exception:
        if refresh:
            raise
        logger.warning("Stand der öffentlichen Zahlen nicht speicherbar", exc_info=True)
    return stand


def api_gesamt(stand: dict[str, Any], gelistet: set[str]) -> dict[str, int]:
    """
    Summen über die gelisteten Kommunen (``gelistet``: Kennungen aus ``OParlBody.objects.listed()`` als Text): Die
    Demo-Kommune und als gelöscht markierte Kommunen zählen nicht mit, wie in der Zahl der Kommunen daneben.

    Bewusst eine Positivliste: Der Stand kann bis zum nächsten Lauf noch Zahlen einer Kommune enthalten, die
    inzwischen gelöscht, als gelöscht markiert oder aus der Liste genommen wurde. Sie zählen sofort nicht mehr mit.
    """
    return {
        art: sum(anzahl for kommune, anzahl in zahlen.items() if kommune in gelistet)
        for art, zahlen in stand["je_kommune"].items()
    }
