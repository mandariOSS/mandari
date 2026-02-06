# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Summary service for OParl documents.

Generates AI-powered multi-perspective summaries of municipal documents.
Includes on-demand text extraction from PDFs if text_content is not available.
"""

import logging
from typing import TYPE_CHECKING

from insight_ai.providers import NebiusProvider
from insight_ai.providers.base import ChatMessage

from .prompts import PAPER_SUMMARY_SYSTEM_PROMPT, build_paper_summary_user_prompt

if TYPE_CHECKING:
    from insight_core.models import OParlFile, OParlPaper

logger = logging.getLogger(__name__)


class SummaryError(Exception):
    """Base exception for summary generation errors."""

    pass


class NoTextContentError(SummaryError):
    """Raised when no text content is available for summarization."""

    pass


class APINotConfiguredError(SummaryError):
    """Raised when the AI API is not properly configured."""

    pass


class TextExtractionError(SummaryError):
    """Raised when text extraction fails."""

    pass


class SummaryService:
    """
    Service for generating AI summaries of OParl documents.

    Uses Nebius TokenFactory with Kimi K2 Thinking model.
    Automatically extracts text from PDFs on-demand if needed.
    """

    def __init__(self, provider=None):
        """
        Initialize the summary service.

        Args:
            provider: Optional AI provider. Defaults to NebiusProvider.
        """
        self.provider = provider or NebiusProvider()

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
                "KI-API nicht konfiguriert. Bitte setzen Sie NEBIUS_API_KEY "
                "als Umgebungsvariable oder in den Systemeinstellungen."
            )

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
            from insight_core.models import OParlMeeting

            # Get meeting external IDs from consultations
            meeting_ext_ids = paper.consultations.values_list("meeting_external_id", flat=True).distinct()[:10]

            # Find meetings and their organizations
            meetings = OParlMeeting.objects.filter(external_id__in=[m for m in meeting_ext_ids if m]).prefetch_related(
                "organizations"
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

            # Call AI provider
            # Kimi K2 Thinking needs high max_tokens - the thinking process
            # can use 10k+ tokens before producing the actual answer
            response = self.provider.chat_completion(
                messages=messages,
                max_tokens=32000,
                temperature=0.3,
            )

            summary = response.content

            logger.info(
                f"Summary generated for {paper.id}: "
                f"{response.input_tokens} input, {response.output_tokens} output tokens"
            )

            # Save to paper if requested
            if save:
                paper.summary = summary
                paper.save(update_fields=["summary"])
                logger.info(f"Summary saved to paper {paper.id}")

            return summary

        except (NoTextContentError, APINotConfiguredError):
            raise
        except Exception as e:
            logger.exception(f"Summary generation failed for paper {paper.id}: {e}")
            raise SummaryError(f"Fehler bei der Zusammenfassung: {str(e)}") from e

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
        for file in paper.files.all():
            if file.text_content and file.text_content.strip():
                file_name = file.name or file.file_name or "Dokument"
                texts.append(f"### {file_name}\n{file.text_content.strip()}")
            elif file.download_url or file.access_url:
                files_to_extract.append(file)

        # Second pass: extract text from files that need it
        if files_to_extract and not texts:
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
        from insight_core.services.document_extraction import (
            DocumentDownloadError,
            download_and_extract,
        )

        url = file.download_url or file.access_url
        if not url:
            logger.warning(f"File {file.id} has no download URL")
            return ""

        try:
            logger.info(f"Extracting text from file {file.id}: {url}")

            result = download_and_extract(
                url=url,
                mime_type=file.mime_type,
                original_name=file.file_name or file.name or "",
                timeout=120.0,
            )

            if result.text and result.text.strip():
                # Save extracted text to file for future use
                file.text_content = result.text
                file.save(update_fields=["text_content"])
                logger.info(f"Extracted {len(result.text)} chars from file {file.id} (OCR: {result.ocr_performed})")
                return result.text

            logger.warning(f"No text extracted from file {file.id}")
            return ""

        except DocumentDownloadError as e:
            logger.warning(f"Failed to download file {file.id}: {e}")
            return ""
        except Exception as e:
            logger.exception(f"Failed to extract text from file {file.id}: {e}")
            return ""

    def _collect_text_content(self, paper: "OParlPaper") -> str:
        """
        Collect existing text content from all files (no extraction).

        Args:
            paper: OParlPaper instance

        Returns:
            Combined text content from all files
        """
        texts = []

        for file in paper.files.all():
            if file.text_content and file.text_content.strip():
                file_name = file.name or file.file_name or "Dokument"
                texts.append(f"### {file_name}\n{file.text_content.strip()}")

        return "\n\n---\n\n".join(texts)

    def is_available(self) -> bool:
        """
        Check if the summary service is available.

        Returns:
            True if the AI provider is properly configured
        """
        return self.provider.is_available()
