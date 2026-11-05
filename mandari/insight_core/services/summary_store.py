# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gespeicherte KI-Zusammenfassungen der Vorgänge (``OParlPaper.summary``) beim Eigentümer des RIS-Bestands.

Erzeugt werden Zusammenfassungen auf Abruf (``insight_ai.services.summarizer``) und, mit der Claude API, vorab im
Message Batch (``insight_ai.services.zusammenfassung_batch``: Auswahl ``ohne_zusammenfassung``, Übernahme
``speichern``); verworfen werden sie, wenn sich ihre Grundlage ändert: bei Rücknahme einer Anlage
(``SourceDeletionModel._forget_summaries``), im Löschabgleich (``file_reconcile``) und nach neuem Text einer Anlage
(Abonnement ``insight.zusammenfassung``, Issue #919). Abonnement und Batch liegen bei den KI-Funktionen und
schreiben über diese Funktionen, nicht selbst in den Bestand (``scripts/check_ris_access_ratchet.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet

from ..models import OParlFile, OParlPaper

#: Länge des gespeicherten Anbieterhinweises (``OParlPaper.summary_hinweis``)
HINWEIS_LAENGE = 300


def discard_for_files(file_ids: Iterable[uuid.UUID], *, dry_run: bool = False) -> int:
    """
    Verwirft die Zusammenfassungen der Vorgänge dieser Dateien; Rückgabe: ihre Zahl (mit ``dry_run`` nur gezählt).

    Dateien ohne Vorgang (Sitzungsdokumente), unbekannte Kennungen und Vorgänge ohne Zusammenfassung zählen nicht.
    Ein ``UPDATE`` je Aufruf; idempotent.
    """
    kennungen = list(file_ids)
    if not kennungen:
        return 0
    vorgaenge = OParlFile.objects.filter(pk__in=kennungen, paper_id__isnull=False).values("paper_id")
    betroffen = OParlPaper.objects.filter(pk__in=vorgaenge, summary__isnull=False)
    return betroffen.count() if dry_run else betroffen.update(summary=None, summary_hinweis="")


def _aktuelle_dateien_mit_text() -> QuerySet[OParlFile]:
    """Anlagen, deren gespeicherter Text in eine Zusammenfassung einfließen darf (wie ``SummaryService``)."""
    return OParlFile.objects.filter(
        deleted=False, source_missing_since__isnull=True, text_content__isnull=False
    ).exclude(text_content="")


def ohne_zusammenfassung(*, seit: date, limit: int, ausser: Iterable[Any] = ()) -> list[OParlPaper]:
    """
    Vorgänge für eine Zusammenfassung vorab (Batch): neueste zuerst, höchstens ``limit``.

    Nur Vorgänge ohne Zusammenfassung, nicht gelöscht, aus einer gelisteten Kommune, mit Datum ab ``seit`` und mit
    mindestens einer aktuellen Anlage mit gespeichertem Text; ``ausser`` (Kennungen) entfällt. Kommunen, deren
    Veröffentlichung pausiert oder zurückgenommen ist (``publication``), fallen ganz heraus: Ihre Seiten antworten
    mit 503 bzw. 410, also geht auch nichts an einen KI-Anbieter.
    """
    from insight_core import publication

    gesperrt = [kennung for kennung, stand in publication.states().items() if stand.paused or stand.withdrawn]
    mit_text = _aktuelle_dateien_mit_text().filter(paper_id=OuterRef("pk"))
    auswahl = (
        OParlPaper.objects.filter(
            summary__isnull=True, deleted=False, date__gte=seit, body__deleted=False, body__is_listed=True
        )
        .filter(Exists(mit_text))
        .exclude(pk__in=list(ausser))
        .exclude(body_id__in=gesperrt)
        .select_related("body", "body__source")
        .order_by("-date", "-pk")
    )
    return list(auswahl[: max(0, limit)])


def vorgaenge(kennungen: Iterable[Any]) -> QuerySet[OParlPaper]:
    """Vorgänge zu diesen Kennungen (etwa aus einem Batch), mit Kommune und Quelle."""
    return OParlPaper.objects.filter(pk__in=list(kennungen)).select_related("body", "body__source")


def speichern(
    vorgang_id: Any, zusammenfassung: str, hinweis: str, *, seit: datetime, dateien: Iterable[Any] = ()
) -> bool:
    """
    Speichert eine vorab erstellte Zusammenfassung, wenn sie noch passt; Rückgabe: gespeichert?

    Nicht gespeichert wird, wenn der Vorgang gelöscht ist oder schon eine Zusammenfassung hat, wenn eine der
    verwendeten Anlagen (``dateien``) inzwischen gelöscht oder in der Quelle verschwunden ist oder wenn seit ``seit``
    (Beginn der Erstellung) neuer Text zu einer Anlage vorliegt. Der Vorgang ist dabei gesperrt: Eine gleichzeitige
    Rücknahme wartet und verwirft die Zusammenfassung danach (``_forget_summaries``).
    """
    datei_ids = [kennung for kennung in dateien if kennung]
    with transaction.atomic():
        vorgang = OParlPaper.objects.select_for_update().filter(pk=vorgang_id).only("pk", "deleted", "summary").first()
        if vorgang is None or vorgang.deleted or vorgang.summary is not None:
            return False
        if (
            datei_ids
            and OParlFile.objects.filter(pk__in=datei_ids)
            .filter(Q(deleted=True) | Q(source_missing_since__isnull=False))
            .exists()
        ):
            return False
        if OParlFile.objects.filter(
            paper_id=vorgang_id, text_extraction_status="completed", text_extracted_at__gt=seit
        ).exists():
            return False
        OParlPaper.objects.filter(pk=vorgang_id).update(
            summary=zusammenfassung, summary_hinweis=(hinweis or "")[:HINWEIS_LAENGE]
        )
    return True
