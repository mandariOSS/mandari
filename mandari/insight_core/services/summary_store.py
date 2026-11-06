# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gespeicherte KI-Zusammenfassungen der Vorgänge (``OParlPaper.summary``) beim Eigentümer des RIS-Bestands.

Erzeugt werden Zusammenfassungen auf Abruf (``insight_ai.services.summarizer``) und, mit der Claude API, vorab im
Message Batch (``insight_ai.services.zusammenfassung_batch``: Auswahl ``fuer_tagesbatch`` und ``tagesbatch_wartet``,
Übernahme ``speichern``); verworfen werden sie, wenn sich ihre Grundlage ändert: bei Rücknahme einer Anlage
(``SourceDeletionModel._forget_summaries``), im Löschabgleich (``file_reconcile``) und nach neuem Text einer Anlage
(Abonnement ``insight.zusammenfassung``, Issue #919). Abonnement und Batch liegen bei den KI-Funktionen und
schreiben über diese Funktionen, nicht selbst in den Bestand (``scripts/check_ris_access_ratchet.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
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


def kommune_finden(schluessel: str) -> Any:
    """
    Kommune zu Kennung (UUID) oder eindeutigem Teil des Namens (für ``manage.py tagesbatch``); ``LookupError``,
    wenn keine oder mehrere passen.
    """
    from ..models import OParlBody

    try:
        return OParlBody.objects.get(pk=uuid.UUID(schluessel), deleted=False)
    except (ValueError, OParlBody.DoesNotExist):
        pass
    treffer = list(OParlBody.objects.filter(name__icontains=schluessel, deleted=False)[:5])
    genau = [kommune for kommune in treffer if (kommune.name or "").casefold() == schluessel.casefold()]
    if len(genau) == 1:
        return genau[0]
    if len(treffer) != 1:
        namen = ", ".join(kommune.name or str(kommune.pk) for kommune in treffer) or "keine"
        raise LookupError(f"„{schluessel}“ passt nicht eindeutig: {namen}")
    return treffer[0]


def fuer_tagesbatch(kommunen: Mapping[Any, date], *, limit: int) -> list[OParlPaper]:
    """
    Vorgänge ausgewählter Kommunen für den Tagesbatch (Issue #1220): neueste zuerst, höchstens ``limit``; nur ohne
    Zusammenfassung, nicht gelöscht, aus einer gelisteten Kommune, je Kommune ab ihrem Stichtag (``kommunen``: Kennung
    der Kommune → Datum) und mit mindestens einer aktuellen Anlage mit gespeichertem Text. Kommunen, deren
    Veröffentlichung pausiert oder zurückgenommen ist (``publication``), fallen heraus: Ihre Seiten antworten mit 503
    bzw. 410, also geht auch nichts an einen KI-Anbieter. Ohne Kommunen keine Vorgänge. Ob der Text aller
    Anlagen schon vorliegt, prüft ``tagesbatch_wartet`` je Vorgang.
    """
    from insight_core import publication

    if not kommunen:
        return []
    gesperrt = [
        uuid.UUID(kennung) for kennung, stand in publication.states().items() if stand.paused or stand.withdrawn
    ]
    auswahl_kommunen = Q()
    for kommune, ab in kommunen.items():
        auswahl_kommunen |= Q(body_id=kommune, date__gte=ab)
    mit_text = _aktuelle_dateien_mit_text().filter(paper_id=OuterRef("pk"))
    auswahl = (
        OParlPaper.objects.filter(auswahl_kommunen)
        .filter(summary__isnull=True, deleted=False, body__deleted=False, body__is_listed=True)
        .filter(Exists(mit_text))
        .exclude(body_id__in=gesperrt)
        .select_related("body", "body__source")
        .order_by("-date", "-pk")
    )
    return list(auswahl[: max(0, limit)])


#: Stände der Texterkennung, in denen der Text noch kommt
_TEXT_AUSSTEHEND = ("pending", "processing")
#: Von der Obergrenze des Dokument-Caches verdrängt: der Text kommt nicht von selbst (wie ``SummaryService``)
_VERDRAENGT = "evicted"
#: Fehlertexte eines gescheiterten Abrufs (``hub.ris.erkennung.DOWNLOAD_ERROR_PREFIX``): wird nachgearbeitet
_ABRUFFEHLER = "Download"


def _abruf_wird_nachgearbeitet(paper: OParlPaper) -> bool:
    """Reiht die tägliche Nacharbeit gescheiterte Abrufe dieses Vorgangs noch neu ein (Alter des Vorgangs)?"""
    from datetime import timedelta

    from django.utils import timezone

    from hub.ris.erkennung import NACHARBEIT_TAGE

    tag = paper.date.date() if isinstance(paper.date, datetime) else paper.date
    if tag is None:
        return False
    return tag >= timezone.localdate() - timedelta(days=NACHARBEIT_TAGE)


def tagesbatch_wartet(paper: OParlPaper, *, verortung: bool) -> str:
    """
    Worauf ein Vorgang vor dem Tagesbatch noch wartet; leer: bereit (Texterkennung ist Pflicht, Issue #1220).

    - ``texterkennung``: Die Erkennung einer aktuellen Anlage steht aus oder läuft (verdrängte Anlagen zählen nicht).
    - ``abruf``: Eine Anlage ist am Abruf gescheitert; ``abrufe_nacharbeiten`` reiht sie täglich neu ein, solange der
      Vorgang höchstens ``hub.ris.erkennung.NACHARBEIT_TAGE`` alt ist. Danach hält sie ihn nicht mehr auf.
    - ``verortung``: Mit ``verortung`` muss der automatische Verortungslauf nach dem letzten Text gelaufen sein, damit
      Claude die gefundenen Orte prüft und nicht einen alten Stand. Ist er älter, wird der Vorgang dafür wieder auf
      ``pending`` gesetzt (der Zeitplan ``verortung_automatisch`` holt ihn ab), außer die Orte stammen von der KI
      oder die KI fand keine (``no_locations``): Die ersetzte der automatische Lauf nicht, sie gelten als aktuell.

    Anlagen, die dauerhaft keinen Text liefern (zu groß, nicht unterstützt, unlesbar), halten nichts auf.
    """
    from django.db.models import Max

    dateien = OParlFile.objects.filter(paper=paper, deleted=False, source_missing_since__isnull=True)
    if dateien.filter(text_extraction_status__in=_TEXT_AUSSTEHEND).exclude(local_status=_VERDRAENGT).exists():
        return "texterkennung"
    if _abruf_wird_nachgearbeitet(paper) and (
        dateien.filter(text_extraction_status="failed", text_extraction_error__startswith=_ABRUFFEHLER).exists()
    ):
        return "abruf"
    if not verortung or paper.georef_status == "no_locations" or "ai" in (paper.georef_method or ""):
        return ""
    if paper.georef_status == "pending":
        return "verortung"
    letzter_text = dateien.aggregate(letzter=Max("text_extracted_at"))["letzter"]
    if paper.georef_extracted_at is None or (letzter_text is not None and paper.georef_extracted_at < letzter_text):
        OParlPaper.objects.filter(pk=paper.pk).exclude(georef_status="pending").update(georef_status="pending")
        return "verortung"
    return ""


def abrufe_nacharbeiten(kommunen: Mapping[Any, date]) -> int:
    """
    Am Abruf gescheiterte Anlagen der Kommunen im Tagesbatch zurück in die Dokumentkette
    (``hub.ris.erkennung.nacharbeiten`` mit Auswahl); Rückgabe: Zahl der zurückgesetzten Anlagen. Die KI-Funktionen
    laden selbst nichts (``insight_ai/tests/test_keine_dateien.py``), deshalb liegt der Aufruf hier.
    """
    from hub.ris.erkennung import NACH_ABRUF, NACH_ERKENNUNG, NACH_WARTET, nacharbeiten

    if not kommunen:
        return 0
    gesamt = nacharbeiten(ausfuehren=True, kommunen=kommunen).gesamt()
    return sum(gesamt.get(art, 0) for art in (NACH_ERKENNUNG, NACH_ABRUF, NACH_WARTET))


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
