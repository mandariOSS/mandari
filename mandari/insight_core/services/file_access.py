# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugriffsprotokoll der Dokumentablage (Issue #786).

Jeder Abruf über die Dateivorschau zählt einmal: je Tag, Kommune, Ergebnis (Treffer aus der lokalen
Kopie, Abruf bei der Quelle, nicht ausgeliefert, gesperrt) und Altersklasse des Dokuments. Es gibt
keine Adressen, keine Kennungen und keine einzelnen Dokumente im Protokoll, nur Zähler.

PDF-Betrachter laden große Dokumente in Teilen (Range-Anfragen), sobald der Webserver sie ausliefert
(#785). Gezählt wird nur die erste Anfrage eines Abrufs: ohne ``Range`` oder mit einem Bereich ab
Byte 0. Folgeanfragen zählen weder als Abruf noch mit ihrer Größe.

Das Zählen darf die Auslieferung nie verhindern: Fehler werden nur protokolliert.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.http import HttpResponseBase
from django.utils import timezone

logger = logging.getLogger(__name__)

HIT = "hit"
MISS = "miss"
FAILED = "failed"
BLOCKED = "blocked"

#: Markierung an einer Antwort, die ein Dokument bewusst nicht ausliefert (zurückgenommen, gesperrt)
BLOCKED_ATTRIBUTE = "mandari_file_blocked"


def counts_as_access(request: Any) -> bool:
    """
    Zählt diese Anfrage als eigener Abruf? Ja ohne ``Range`` und für einen Bereich ab Byte 0.

    Folgeanfragen eines PDF-Betrachters (``bytes=65536-…``, auch Bereiche vom Ende ``bytes=-500``)
    gehören zu einem Abruf, der schon gezählt ist. Eine unlesbare ``Range`` ignoriert der Webserver und
    liefert die ganze Datei: Die zählt.

    Ausgewertet wird nur der Beginn des ersten Bereichs (``bytes=<start>-…``), mit einfachen
    Zeichenkettenoperationen in linearer Zeit, denn die Kopfzeile kommt vom Client.
    """
    header = (getattr(request, "META", None) or {}).get("HTTP_RANGE", "")
    if not header:
        return True
    unit, has_equals, ranges = header.partition("=")
    if not has_equals or unit.strip().lower() != "bytes":
        return True
    start, has_dash, _ = ranges.split(",", 1)[0].partition("-")
    if not has_dash:
        return True
    start = start.strip()
    if not start:
        return False  # Bereich vom Ende (``bytes=-500``)
    if not (start.isascii() and start.isdigit()):
        return True
    return int(start) == 0


def age_class(file_obj: Any, now: datetime | None = None) -> str:
    """Altersklasse eines Dokuments nach Datum, sonst Anlage in der Quelle bzw. bei uns."""
    when = (
        getattr(file_obj, "file_date", None)
        or getattr(file_obj, "oparl_created", None)
        or getattr(file_obj, "created_at", None)
    )
    if when is None:
        return "unknown"
    age = (now or timezone.now()) - when
    if age < timedelta(days=30):
        return "d30"
    if age < timedelta(days=365):
        return "d365"
    if age < timedelta(days=3 * 365):
        return "y3"
    return "older"


def outcome_of(response: HttpResponseBase) -> str:
    """Ergebnis einer Antwort der Dateivorschau."""
    if getattr(response, BLOCKED_ATTRIBUTE, False):
        return BLOCKED
    cache = response.get("X-Mandari-Cache", "")
    if response.status_code == 200 and cache == "hit":
        return HIT
    if response.status_code == 200 and cache == "miss":
        return MISS
    return FAILED


def mark_blocked(response: HttpResponseBase) -> HttpResponseBase:
    """Antwort als bewusste Sperre kennzeichnen (zählt als „gesperrt“, nicht als Fehler)."""
    setattr(response, BLOCKED_ATTRIBUTE, True)
    return response


def _count(file_obj: Any, outcome: str, day: date | None) -> None:
    from ..models import OParlFileAccessDay

    key = {
        "day": day or timezone.localdate(),
        "body_id": getattr(file_obj, "body_id", None),
        "outcome": outcome,
        "age_class": age_class(file_obj),
    }
    size = int(getattr(file_obj, "local_size", None) or getattr(file_obj, "size", None) or 0)
    size = size if outcome in (HIT, MISS) else 0
    if OParlFileAccessDay.objects.filter(**key).update(count=F("count") + 1, bytes=F("bytes") + size):
        return
    try:
        with transaction.atomic():
            OParlFileAccessDay.objects.create(**key, count=1, bytes=size)
    except IntegrityError:
        # Ein paralleler Abruf hat die Zeile eben angelegt
        OParlFileAccessDay.objects.filter(**key).update(count=F("count") + 1, bytes=F("bytes") + size)


def record(file_obj: Any, outcome: str, *, day: date | None = None) -> None:
    """Einen Abruf zählen; jeder Fehler beim Zählen wird nur protokolliert, nie weitergereicht."""
    try:
        _count(file_obj, outcome, day)
    except Exception:  # Zählfehler (Datenbank, unerwartete Werte) verhindern die Auslieferung nie
        logger.warning("Dokumentabruf %s konnte nicht gezählt werden", getattr(file_obj, "id", "?"), exc_info=True)


def record_response(file_obj: Any, response: HttpResponseBase, request: Any = None) -> HttpResponseBase:
    """
    Ergebnis aus der Antwort ableiten, zählen und die Antwort unverändert zurückgeben.

    Mit ``request`` zählen Folgeanfragen eines Abrufs in Teilen nicht (``counts_as_access``).
    """
    try:
        if request is None or counts_as_access(request):
            record(file_obj, outcome_of(response))
    except Exception:
        logger.warning("Dokumentabruf %s konnte nicht gezählt werden", getattr(file_obj, "id", "?"), exc_info=True)
    return response


def summary(days: int = 30) -> dict[str, Any]:
    """Abrufe der letzten ``days`` Tage je Ergebnis und Altersklasse, dazu die Trefferquote."""
    from ..models import OParlFileAccessDay

    since = timezone.localdate() - timedelta(days=days - 1)
    rows = OParlFileAccessDay.objects.filter(day__gte=since)
    by_outcome = {
        row["outcome"]: {"count": row["n"] or 0, "bytes": row["b"] or 0}
        for row in rows.values("outcome").annotate(n=Sum("count"), b=Sum("bytes"))
    }
    by_age = {
        row["age_class"]: row["n"] or 0
        for row in rows.filter(~Q(outcome=BLOCKED)).values("age_class").annotate(n=Sum("count"))
    }
    hits = by_outcome.get(HIT, {}).get("count", 0)
    delivered = hits + by_outcome.get(MISS, {}).get("count", 0)
    return {
        "days": days,
        "by_outcome": by_outcome,
        "by_age": by_age,
        "hit_rate": round(hits / delivered * 100, 1) if delivered else None,
    }
