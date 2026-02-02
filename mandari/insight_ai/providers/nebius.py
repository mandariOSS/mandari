# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nebius TokenFactory AI Provider.

Uses OpenAI-compatible API with Kimi K2 Thinking model.
"""

import logging
from typing import Optional

from openai import OpenAI

from .base import AbstractAIProvider, ChatMessage, ChatResponse

logger = logging.getLogger(__name__)


class NebiusProvider(AbstractAIProvider):
    """
    Nebius TokenFactory provider using OpenAI-compatible API.

    Models (by priority):
    1. moonshotai/Kimi-K2-Thinking (256k context, reasoning)
    2. thudm/GLM-4.5 (128k context, fallback)
    """

    BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
    PRIMARY_MODEL = "moonshotai/Kimi-K2-Thinking"
    FALLBACK_MODEL = "thudm/GLM-4.5"

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the Nebius provider.

        Args:
            api_key: Optional API key. If not provided, reads from SiteSettings.
        """
        self._api_key = api_key
        self._client: Optional[OpenAI] = None
        self._model = self.PRIMARY_MODEL

    def _get_api_key(self) -> str:
        """Get API key from parameter or SiteSettings."""
        if self._api_key:
            return self._api_key

        from apps.common.models import SiteSettings
        return SiteSettings.get_nebius_api_key()

    def _get_client(self) -> OpenAI:
        """Get or create OpenAI client."""
        if self._client is None:
            api_key = self._get_api_key()
            if not api_key:
                raise ValueError(
                    "Nebius API Key nicht konfiguriert. "
                    "Setzen Sie NEBIUS_API_KEY als Umgebungsvariable oder in den Systemeinstellungen."
                )

            self._client = OpenAI(
                base_url=self.BASE_URL,
                api_key=api_key,
            )

        return self._client

    def is_available(self) -> bool:
        """Check if the provider is properly configured."""
        try:
            api_key = self._get_api_key()
            return bool(api_key and api_key.strip())
        except Exception:
            return False

    @property
    def model_name(self) -> str:
        """Return the model name being used."""
        return self._model

    def chat_completion(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> ChatResponse:
        """
        Generate a chat completion using Nebius API.

        Args:
            messages: List of ChatMessage objects
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0.0 - 1.0)

        Returns:
            ChatResponse with generated content and token usage
        """
        client = self._get_client()

        # Convert ChatMessage to dict format
        message_dicts = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]

        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=message_dicts,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            # Extract response content
            content = response.choices[0].message.content or ""

            # Extract token usage
            usage = response.usage
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0
            total_tokens = usage.total_tokens if usage else 0

            logger.info(
                f"Nebius completion: {input_tokens} input, {output_tokens} output tokens"
            )

            return ChatResponse(
                content=content,
                model=self._model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )

        except Exception as e:
            logger.error(f"Nebius API error: {e}")

            # Try fallback model if primary fails
            if self._model == self.PRIMARY_MODEL:
                logger.info(f"Trying fallback model: {self.FALLBACK_MODEL}")
                self._model = self.FALLBACK_MODEL
                return self.chat_completion(messages, max_tokens, temperature)

            raise
