# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Summary service for OParl documents.

Generates AI-powered multi-perspective summaries of municipal documents.
Includes on-demand text extraction from PDFs if text_content is not available.
"""

import logging
from typing import TYPE_CHECKING

from apps.common.db_connections import release_idle_thread_connections
from insight_ai.providers import get_insight_provider
from insight_ai.providers.base import STANDARD_MAX_AUSGABE, ChatMessage

from .prompts import PAPER_SUMMARY_SYSTEM_PROMPT, build_paper_summary_user_prompt

if TYPE_CHECKING:
    from insight_core.models import OParlFile, OParlPaper

logger = logging.getLogger(__name__)

#: Höchstens so viele Anlagen werden bei Bedarf heruntergeladen und ausgelesen (Dauer, OCR-Kosten)
MAX_ON_DEMAND_EXTRACTIONS = 3


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


class SummaryService:
    """
    Service for generating AI summaries of OParl documents.

    Nutzt den KI-Endpunkt des Bürgerportals aus der zentralen KI-Konfiguration (``get_insight_provider``).
    Automatically extracts text from PDFs on-demand if needed.
    """

    def __init__(self, provider=None, *, pace_max_wait: float | None = None):
        """
        Initialize the summary service.

        Args:
            provider: Optional AI provider. Standard: ``get_insight_provider()`` (KI-Einstellungen).
            pace_max_wait: Höchstwartezeit auf die Drossel je Host beim Nachladen von Dokumenten. In einer
                Web-Anfrage immer setzen: Ohne freien Zeitpunkt bricht die Erstellung dann mit der Bitte um
                einen neuen Versuch ab, statt die Anfrage lange schlafen zu lassen.
        """
        self.provider = provider or get_insight_provider()
        self.pace_max_wait = pace_max_wait

    def generate_summary(self, paper: "OParlPaper", save: bool = True) -> str:
        """
        Generate a summary for an OParl paper.

        Automatically extracts text from files if not already available.

        Args:
            paper: OParlPaper instance to summarize
            save: Whether to save the summary to the paper

        Returns:
            Generated summary text

        Raises:
            NoTextContentError: If no text content is available after extraction
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

        # Collect text content from all files (with on-demand extraction)
        text_content = self._collect_text_content_with_extraction(paper)

        if not text_content:
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

            # Save to paper if requested
            if save:
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

    def _collect_text_content_with_extraction(self, paper: "OParlPaper") -> str:
        """
        Collect text content from all files, extracting on-demand if needed.

        If a file doesn't have text_content, attempts to download and extract it.

        Args:
            paper: OParlPaper instance

        Returns:
            Combined text content from all files
        """
        texts = []
        files_to_extract = []

        # First pass: collect existing text and identify files needing extraction
        for file in self._current_files(paper):
            if file.text_content and file.text_content.strip():
                file_name = file.name or file.file_name or "Dokument"
                texts.append(f"### {file_name}\n{file.text_content.strip()}")
            elif file.download_url or file.access_url:
                files_to_extract.append(file)

        # Second pass: extract text from files that need it
        if files_to_extract and not texts:
            files_to_extract = files_to_extract[:MAX_ON_DEMAND_EXTRACTIONS]
            logger.info(f"Extracting text from {len(files_to_extract)} files on-demand")
            for file in files_to_extract:
                extracted_text = self._extract_text_from_file(file)
                if extracted_text:
                    file_name = file.name or file.file_name or "Dokument"
                    texts.append(f"### {file_name}\n{extracted_text}")

        return "\n\n---\n\n".join(texts)

    def _extract_text_from_file(self, file: "OParlFile") -> str:
        """
        Extract text from a single file on-demand.

        Downloads the file and extracts text using OCR if needed.
        Saves the extracted text to the file object.

        Args:
            file: OParlFile instance

        Returns:
            Extracted text or empty string on failure
        """
        from insight_core.services import robots
        from insight_core.services.document_extraction import (
            DocumentDownloadError,
            RobotsUnreachableError,
            SourceBusyError,
            download_and_extract,
        )
        from insight_core.services.file_cache import download_headers

        url = file.download_url or file.access_url
        if not url:
            logger.warning(f"File {file.id} has no download URL")
            return ""

        try:
            logger.info(f"Extracting text from file {file.id}: {url}")

            # Einstellungen der Quelle (robots-Ausnahme, Download-Header samt User-Agent) noch mit Verbindung
            # lesen; Download und OCR dauern, die Datenbankverbindung geht solange an den Pool zurück
            sync_config = robots.sync_config_of(file)
            extra_headers = download_headers(file.body)
            release_idle_thread_connections()
            result = download_and_extract(
                url=url,
                mime_type=file.mime_type,
                original_name=file.file_name or file.name or "",
                timeout=120.0,
                extra_headers=extra_headers,
                sync_config=sync_config,
                max_wait=self.pace_max_wait,
                # Öffentliche RIS-Datei: externe Texterkennung zulässig (nur mit Endpunkt aus KI_ERLAUBTE_HOSTS)
                allow_external=True,
            )

            if result.text and result.text.strip():
                # Save extracted text to file for future use
                file.text_content = result.text
                file.save(update_fields=["text_content"])
                logger.info(f"Extracted {len(result.text)} chars from file {file.id} (OCR: {result.ocr_performed})")
                return result.text

            logger.warning(f"No text extracted from file {file.id}")
            return ""

        except (SourceBusyError, RobotsUnreachableError) as e:
            # Vorübergehend: nicht als „kein Text“ werten, sondern um einen neuen Versuch bitten
            logger.info(f"File {file.id} not fetched now: {e}")
            raise SummaryError(
                "Die Zusammenfassung konnte gerade nicht erstellt werden. Bitte später erneut versuchen."
            ) from e
        except DocumentDownloadError as e:
            logger.warning(f"Failed to download file {file.id}: {e}")
            return ""
        except Exception as e:
            logger.exception(f"Failed to extract text from file {file.id}: {e}")
            return ""

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
