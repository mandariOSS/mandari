# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Summary service for OParl documents.

Generates AI-powered multi-perspective summaries of municipal documents.
"""

import logging
from typing import TYPE_CHECKING

from insight_ai.providers import NebiusProvider
from insight_ai.providers.base import ChatMessage
from .prompts import PAPER_SUMMARY_SYSTEM_PROMPT, build_paper_summary_user_prompt

if TYPE_CHECKING:
    from insight_core.models import OParlPaper

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


class SummaryService:
    """
    Service for generating AI summaries of OParl documents.

    Uses Nebius TokenFactory with Kimi K2 Thinking model.
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

        Args:
            paper: OParlPaper instance to summarize
            save: Whether to save the summary to the paper

        Returns:
            Generated summary text

        Raises:
            NoTextContentError: If no text content is available
            APINotConfiguredError: If the API is not configured
            SummaryError: For other generation errors
        """
        # Check if API is available
        if not self.provider.is_available():
            raise APINotConfiguredError(
                "KI-API nicht konfiguriert. Bitte setzen Sie NEBIUS_API_KEY "
                "als Umgebungsvariable oder in den Systemeinstellungen."
            )

        # Collect text content from all files
        text_content = self._collect_text_content(paper)

        if not text_content:
            raise NoTextContentError(
                "Keine Textinhalte verfügbar. Das Dokument enthält keine "
                "extrahierten Texte aus den angehängten Dateien."
            )

        # Build prompts
        system_prompt = PAPER_SUMMARY_SYSTEM_PROMPT
        user_prompt = build_paper_summary_user_prompt(
            paper_name=paper.name or "Unbekannt",
            paper_type=paper.paper_type,
            reference=paper.reference,
            date=str(paper.date) if paper.date else None,
            text_content=text_content,
        )

        # Create messages
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]

        try:
            logger.info(f"Generating summary for paper {paper.id} ({paper.reference})")

            # Call AI provider
            response = self.provider.chat_completion(
                messages=messages,
                max_tokens=1500,
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

    def _collect_text_content(self, paper: "OParlPaper") -> str:
        """
        Collect text content from all files attached to a paper.

        Args:
            paper: OParlPaper instance

        Returns:
            Combined text content from all files
        """
        texts = []

        for file in paper.files.all():
            if file.text_content and file.text_content.strip():
                # Add file name as header
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
