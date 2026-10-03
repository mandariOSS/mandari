# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugriffsprotokoll der Dokumentablage (Issue #786).

Jeder Abruf über die Dateivorschau zählt einmal: je Tag, Kommune, Ergebnis (Treffer aus der lokalen
Kopie, Abruf bei der Quelle, nicht ausgeliefert, gesperrt) und Altersklasse des Dokuments. Es gibt
keine Adressen, keine Kennungen und keine einzelnen Dokumente im Protokoll, nur Zähler.

Das Zählen darf die Auslieferung nie verhindern: Fehler werden nur protokolliert.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from django.db import DatabaseError, IntegrityError, transaction
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


def record(file_obj: Any, outcome: str, *, day: date | None = None) -> None:
    """Einen Abruf zählen; Fehler beim Zählen werden nur protokolliert."""
    from ..models import OParlFileAccessDay

    key = {
        "day": day or timezone.localdate(),
        "body_id": getattr(file_obj, "body_id", None),
        "outcome": outcome,
        "age_class": age_class(file_obj),
    }
    size = int(getattr(file_obj, "local_size", None) or getattr(file_obj, "size", None) or 0)
    size = size if outcome in (HIT, MISS) else 0
    try:
        if OParlFileAccessDay.objects.filter(**key).update(count=F("count") + 1, bytes=F("bytes") + size):
            return
        try:
            with transaction.atomic():
                OParlFileAccessDay.objects.create(**key, count=1, bytes=size)
        except IntegrityError:
            # Ein paralleler Abruf hat die Zeile eben angelegt
            OParlFileAccessDay.objects.filter(**key).update(count=F("count") + 1, bytes=F("bytes") + size)
    except DatabaseError:
        logger.warning("Dokumentabruf %s konnte nicht gezählt werden", getattr(file_obj, "id", "?"), exc_info=True)


def record_response(file_obj: Any, response: HttpResponseBase) -> HttpResponseBase:
    """Ergebnis aus der Antwort ableiten, zählen und die Antwort unverändert zurückgeben."""
    record(file_obj, outcome_of(response))
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
