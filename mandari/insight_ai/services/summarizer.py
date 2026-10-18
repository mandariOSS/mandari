# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Summary service for OParl documents.

Generates AI-powered multi-perspective summaries of municipal documents.

Der Dienst liest nur den gespeicherten Text der Anlagen; er lädt keine Dateien und erkennt keinen Text
(Issue #919, ADR Dokumentkette, Abschnitt 9; Prüfung ``insight_ai/tests/test_keine_dateien.py``). Fehlt der
Text, weil die Erkennung der Anlagen noch aussteht, meldet er das (``TextPendingError``, die Ansicht zeigt einen
Hinweis) und plant die Erkennung dieser Anlagen mit Vorrang ein – aber nur, wenn Aufträge im Worker laufen
(``TASKS_BACKEND=journal``) und der Worker auch den Text erkennt (``TEXT_EXTRACTION_RUNNER=worker``). Mit dem
sofort ausführenden Backend liefe die Erkennung sonst in der Webanfrage; mit ``ingestor`` erkennt der OCR-Worker
des Ingestors den Text, ein Auftrag wäre ein zweiter Weg zur Quelle.

Neuer Text verwirft eine gespeicherte Zusammenfassung (Abonnement ``insight.zusammenfassung``,
``insight_ai/abonnement.py``); erzeugt wird sie wie bisher erst auf Abruf. Kommt während der Erstellung neuer Text
hinzu, wird das Ergebnis ausgegeben, aber nicht gespeichert.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.utils import timezone

from apps.common.db_connections import release_idle_thread_connections
from insight_ai.providers import get_insight_provider
from insight_ai.providers.base import STANDARD_MAX_AUSGABE, ChatMessage

from .prompts import PAPER_SUMMARY_SYSTEM_PROMPT, build_paper_summary_user_prompt

if TYPE_CHECKING:
    from insight_core.models import OParlPaper

logger = logging.getLogger(__name__)

#: Höchstens so viele Anlagen je Anfrage werden mit Vorrang zur Texterkennung eingeplant (Last der Warteschlange ocr)
MAX_PRIORITY_EXTRACTIONS: Final = 3
#: Priorität dieser Aufträge: vor denen aus Zeitplan und Ereignissen (Standard 0), damit der Text bald vorliegt
EXTRACTION_PRIORITY: Final = 80
#: Stände der Texterkennung, in denen der Text noch kommen kann
TEXT_PENDING_STATUSES: Final = ("pending", "processing")
#: Schalter der Quelle für den Dateiabruf (gleich ``file_cache.FILE_DOWNLOADS_KEY``): ``false`` = nie abrufen
FILE_DOWNLOADS_KEY: Final = "file_downloads"
#: Von der Obergrenze des Dokument-Caches verdrängt (#961, gleich ``file_cache_limit.EVICTED``): niemand holt die
#: Anlage von selbst, ihr Text kommt erst nach einem ausdrücklichen Abruf (Vorschau, ``cache_files --verdraengte``)
EVICTED: Final = "evicted"


class SummaryError(Exception):
    """Base exception for summary generation errors."""

    pass


class NoTextContentError(SummaryError):
    """Raised when no text content is available for summarization."""

    pass


class APINotConfiguredError(SummaryError):
    """Raised when the AI API is not properly configured."""

    pass


class SummaryRevokedError(SummaryError):
    """Raised when the paper or one of its files was withdrawn while the summary was generated."""

    pass


class TextPendingError(SummaryError):
    """Noch kein Text, aber die Texterkennung mindestens einer Anlage steht aus: später erneut versuchen."""


class SummaryService:
    """
    Service for generating AI summaries of OParl documents.

    Nutzt den KI-Endpunkt des Bürgerportals aus der zentralen KI-Konfiguration (``get_insight_provider``).
    Liest nur gespeicherten Text; lädt und erkennt nichts selbst (siehe Moduldokumentation).
    """

    def __init__(self, provider: Any = None) -> None:
        """
        Initialize the summary service.

        Args:
            provider: Optional AI provider. Standard: ``get_insight_provider()`` (KI-Einstellungen).
        """
        self.provider = provider or get_insight_provider()

    def generate_summary(self, paper: "OParlPaper", save: bool = True) -> str:
        """
        Generate a summary for an OParl paper.

        Uses only the stored text of the files; never downloads or recognizes text itself.

        Args:
            paper: OParlPaper instance to summarize
            save: Whether to save the summary to the paper

        Returns:
            Generated summary text

        Raises:
            TextPendingError: No text yet, but text recognition of a file is pending (planned with priority)
            NoTextContentError: If no text content is available
            APINotConfiguredError: If the API is not configured
            SummaryError: For other generation errors
        """
        # Check if API is available
        if not self.provider.is_available():
            raise APINotConfiguredError(
                "KI im Bürgerportal nicht eingerichtet (Admin → KI-Einstellungen, nur freigegebene Endpunkte)."
            )

        # Stand zu Beginn: Wird der Vorgang oder eine dieser Anlagen während der Erstellung
        # zurückgenommen, darf das Ergebnis weder gespeichert noch ausgegeben werden
        started = (bool(paper.deleted), list(self._current_files(paper).values_list("pk", flat=True)))
        # Neuer Text ab hier verwirft die Zusammenfassung (Abonnement); ein Ergebnis aus dem älteren Text wird
        # deshalb nicht gespeichert
        started_at = timezone.now()

        # Nur gespeicherter Text; der Dienst lädt und erkennt nichts selbst
        text_content = self._collect_text_content(paper)

        if not text_content:
            if self._text_pending(paper):
                raise TextPendingError("Die Texterkennung der Anlagen steht noch aus.")
            raise NoTextContentError(
                "Keine Textinhalte verfügbar. Das Dokument enthält keine "
                "Dateien oder die Textextraktion ist fehlgeschlagen."
            )

        # Collect metadata
        body_name = None
        if paper.body:
            body_name = paper.body.name or paper.body.short_name

        # Get organizations from consultations via meetings
        organizations = set()
        try:
            from insight_core.models import OParlMeeting, withdrawn_q

            # Get meeting external IDs from consultations (ohne von Session zurückgenommene)
            meeting_ext_ids = (
                paper.consultations.exclude(withdrawn_q()).values_list("meeting_external_id", flat=True).distinct()[:10]
            )

            # Find meetings and their organizations
            meetings = (
                OParlMeeting.objects.filter(external_id__in=[m for m in meeting_ext_ids if m])
                .exclude(withdrawn_q())
                .prefetch_related("organizations")
            )

            for meeting in meetings:
                for org in meeting.organizations.all():
                    if org.name:
                        organizations.add(org.name)
        except Exception as e:
            logger.debug(f"Could not get organizations: {e}")

        # Build prompts
        system_prompt = PAPER_SUMMARY_SYSTEM_PROMPT
        user_prompt = build_paper_summary_user_prompt(
            paper_name=paper.name or "Unbekannt",
            paper_type=paper.paper_type,
            reference=paper.reference,
            date=str(paper.date) if paper.date else None,
            text_content=text_content,
            body_name=body_name,
            organizations=list(organizations) if organizations else None,
        )

        # Create messages
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]

        try:
            logger.info(f"Generating summary for paper {paper.id} ({paper.reference})")

            # Der KI-Aufruf dauert Minuten: Datenbankverbindung solange an den Pool zurückgeben
            release_idle_thread_connections()

            # Modelle mit Reasoning brauchen viel Platz vor der eigentlichen Antwort: Obergrenze aus der
            # KI-Konfiguration (AISettings.insight_max_output_tokens)
            max_tokens = getattr(self.provider, "max_output_tokens", 0) or STANDARD_MAX_AUSGABE
            response = self.provider.chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
            )

            summary = response.content

            logger.info(
                f"Summary generated for {paper.id}: "
                f"{response.input_tokens} input, {response.output_tokens} output tokens"
            )

            # Während der Erstellung zurückgenommen (Vorgang oder Anlage)? Dann nichts ausgeben
            if self._withdrawn_since(paper, started):
                raise SummaryRevokedError("Vorgang oder Anlage wurde während der Erstellung zurückgenommen.")

            # Save to paper if requested – nicht, wenn inzwischen neuer Text vorliegt: Das Abonnement hätte die
            # Zusammenfassung verworfen, sie stammt aus dem älteren Text
            if save:
                if self._text_changed_since(paper, started_at):
                    logger.info(f"Summary for paper {paper.id} not saved: new text since the start")
                else:
                    paper.summary = summary
                    paper.save(update_fields=["summary"])
                    logger.info(f"Summary saved to paper {paper.id}")

            return summary

        except (NoTextContentError, APINotConfiguredError, SummaryRevokedError):
            raise
        except Exception as e:
            logger.exception(f"Summary generation failed for paper {paper.id}: {e}")
            # Feste Meldung: Anbieter-Ausnahmen können Adressen, Schlüssel oder Interna enthalten
            raise SummaryError(
                "Die Zusammenfassung konnte gerade nicht erstellt werden. Bitte später erneut versuchen."
            ) from e

    def _collect_text_content(self, paper: "OParlPaper") -> str:
        """
        Gespeicherten Text aller aktuellen Anlagen zusammenführen (ohne Abruf, ohne Erkennung).

        Args:
            paper: OParlPaper instance

        Returns:
            Combined text content from all files
        """
        texts = []
        for file in self._current_files(paper):
            if file.text_content and file.text_content.strip():
                file_name = file.name or file.file_name or "Dokument"
                texts.append(f"### {file_name}\n{file.text_content.strip()}")
        return "\n\n---\n\n".join(texts)

    def _text_pending(self, paper: "OParlPaper") -> bool:
        """
        Steht die Texterkennung einer aktuellen Anlage noch aus? Dann ihre Erkennung mit Vorrang einplanen.

        Eingeplant werden höchstens ``MAX_PRIORITY_EXTRACTIONS`` wartende Anlagen (``pending``) mit abgelegtem
        Inhalt (``local_status = ok``) und nur unter den Bedingungen aus der Moduldokumentation; sonst bleibt es beim
        Hinweis. Der Auftrag liest nur aus der Ablage (``hub/ris/erkennung.py``); Anlagen ohne abgelegten Inhalt holt
        zuerst der Abruf, danach plant sie der Zeitplan ein. Ein Fehler beim Einplanen ändert den Hinweis nicht.
        Anlagen in Bearbeitung (``processing``) sind schon beansprucht. Hat die Quelle den Dateiabruf abgeschaltet
        (Dokumente nur hinter einer Prüfung für Menschen), kommt kein Text: dann ``False``. Ebenso zählen von der
        Obergrenze verdrängte Anlagen (``evicted``, #961) nicht: Ihr Text kommt nicht von selbst, ein Dauerhinweis
        „Texterkennung steht aus“ wäre falsch.
        """
        if self._downloads_disabled(paper):
            return False
        offen = [
            file
            for file in self._current_files(paper)
            .filter(text_extraction_status__in=TEXT_PENDING_STATUSES)
            .exclude(local_status=EVICTED)
            .only("id", "download_url", "access_url", "text_extraction_status", "local_status")
            .order_by("created_at", "id")
            if file.download_url or file.access_url
        ]
        if not offen:
            return False
        wartend = [file.pk for file in offen if file.text_extraction_status == "pending" and file.local_status == "ok"]
        if wartend and priority_extraction_enabled():
            try:
                plan_priority_extraction(wartend[:MAX_PRIORITY_EXTRACTIONS])
            except Exception:
                logger.exception("Texterkennung für Vorgang %s nicht mit Vorrang eingeplant", paper.pk)
        return True

    @staticmethod
    def _downloads_disabled(paper: "OParlPaper") -> bool:
        """Hat die Quelle des Vorgangs den Dateiabruf abgeschaltet (``sync_config["file_downloads"] = false``)?"""
        source = getattr(paper.body, "source", None) if paper.body_id else None
        config = getattr(source, "sync_config", None)
        return isinstance(config, dict) and config.get(FILE_DOWNLOADS_KEY) is False

    @staticmethod
    def _text_changed_since(paper: "OParlPaper", started_at: datetime) -> bool:
        """Liegt für eine Anlage des Vorgangs seit ``started_at`` neuer Text vor?"""
        return paper.files.filter(text_extraction_status="completed", text_extracted_at__gt=started_at).exists()

    @staticmethod
    def _withdrawn_since(paper: "OParlPaper", started: tuple[bool, list]) -> bool:
        """Vorgang oder eine der verwendeten Anlagen seit Beginn gelöscht, zurückgenommen oder gesperrt?"""
        from django.db.models import Q

        from insight_core.models import OParlFile, OParlPaper

        was_deleted, file_ids = started
        return (
            not was_deleted and OParlPaper.objects.filter(pk=paper.pk, deleted=True).exists()
        ) or OParlFile.objects.filter(pk__in=file_ids).filter(
            Q(deleted=True) | Q(source_missing_since__isnull=False)
        ).exists()

    @staticmethod
    def _current_files(paper: "OParlPaper"):
        """
        Anlagen, die in die Zusammenfassung einfließen dürfen: nur nicht gelöschte und nicht gesperrte.

        Zurückgenommene Anlagen (in Session nicht-öffentlich gestellt oder gelöscht), in der
        Quelle gelöschte und dort nicht mehr abrufbare Dateien (Löschabgleich, #787) gehören nicht mehr
        zum Vorgang; ihr Text darf nicht über eine öffentlich abrufbare Zusammenfassung weiterleben.
        """
        return paper.files.filter(deleted=False, source_missing_since__isnull=True)

    def is_available(self) -> bool:
        """
        Check if the summary service is available.

        Returns:
            True if the AI provider is properly configured
        """
        return self.provider.is_available()


def priority_extraction_enabled() -> bool:
    """
    Darf die Zusammenfassung die Texterkennung mit Vorrang einplanen?

    Nur, wenn der Auftrag ``file.extract_text`` im Journal landet (``TASKS_BACKEND=journal``; das sofort ausführende
    Backend erkennte den Text in der Webanfrage) und der Worker den Text erkennt (``TEXT_EXTRACTION_RUNNER=worker``;
    mit ``ingestor`` macht das der OCR-Worker des Ingestors).
    """
    if str(getattr(settings, "TEXT_EXTRACTION_RUNNER", "ingestor")).strip().lower() != "worker":
        return False
    from django.tasks import task_backends

    from apps.events.tasks_backend import JournalBackend
    from insight_core.background_tasks import file_extract_text

    return isinstance(task_backends[file_extract_text.backend], JournalBackend)


def plan_priority_extraction(file_ids: list[Any]) -> int:
    """
    Reiht ``file.extract_text`` je Datei mit Vorrang ein (``EXTRACTION_PRIORITY``); Rückgabe: Zahl der Aufträge.

    Nur aufrufen, wenn ``priority_extraction_enabled()``. Wartet für eine Datei schon ein Auftrag mit Vorrang (ein
    früherer Klick), entsteht kein zweiter. Einen gewöhnlichen wartenden Auftrag überholt der neue; welcher zuerst
    beansprucht, erkennt, der andere endet ohne Wirkung.
    """
    from apps.events.models import Task, TaskStatus
    from insight_core.background_tasks import file_extract_text

    if not file_ids:
        return 0
    schon_vorrang: set[str] = set()
    for args in Task.objects.filter(
        task_path=file_extract_text.module_path, status=TaskStatus.WARTEND, priority__gte=EXTRACTION_PRIORITY
    ).values_list("args", flat=True):
        werte = args.get("args") if isinstance(args, dict) else None
        if werte:
            schon_vorrang.add(str(werte[0]))
    vorrang = file_extract_text.using(priority=EXTRACTION_PRIORITY)
    eingereiht = 0
    for file_id in file_ids:
        if str(file_id) in schon_vorrang:
            continue
        vorrang.enqueue(str(file_id))
        eingereiht += 1
    if eingereiht:
        logger.info("Texterkennung: %d Anlagen für eine Zusammenfassung mit Vorrang eingereiht", eingereiht)
    return eingereiht
