# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nebius TokenFactory AI Provider.

Uses OpenAI-compatible API with Kimi K2 Thinking model.
"""

import json
import logging
from typing import Optional

import httpx

from .base import AbstractAIProvider, ChatMessage, ChatResponse

logger = logging.getLogger(__name__)


class NebiusProvider(AbstractAIProvider):
    """
    Nebius TokenFactory provider using direct HTTP requests.

    Uses httpx instead of OpenAI SDK to properly handle
    Kimi K2 Thinking's reasoning_content field.

    Models (by priority):
    1. moonshotai/Kimi-K2-Thinking (256k context, reasoning)
    2. thudm/GLM-4.5 (128k context, fallback)
    """

    BASE_URL = "https://api.tokenfactory.nebius.com/v1/chat/completions"
    PRIMARY_MODEL = "moonshotai/Kimi-K2-Thinking"
    FALLBACK_MODEL = "thudm/GLM-4.5"

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the Nebius provider.

        Args:
            api_key: Optional API key. If not provided, reads from SiteSettings.
        """
        self._api_key = api_key
        self._model = self.PRIMARY_MODEL

    def _get_api_key(self) -> str:
        """Get API key from parameter or SiteSettings."""
        if self._api_key:
            return self._api_key

        from apps.common.models import SiteSettings
        return SiteSettings.get_nebius_api_key()

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

        Uses direct HTTP requests to properly handle Kimi K2's
        reasoning_content field.

        Args:
            messages: List of ChatMessage objects
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0.0 - 1.0)

        Returns:
            ChatResponse with generated content and token usage
        """
        api_key = self._get_api_key()
        if not api_key:
            raise ValueError(
                "Nebius API Key nicht konfiguriert. "
                "Setzen Sie NEBIUS_API_KEY als Umgebungsvariable oder in den Systemeinstellungen."
            )

        # Convert ChatMessage to dict format
        message_dicts = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]

        # Kimi K2 Thinking recommends temperature=1.0
        actual_temp = 1.0 if "Thinking" in self._model else temperature

        payload = {
            "model": self._model,
            "messages": message_dicts,
            "max_tokens": max_tokens,
            "temperature": actual_temp,
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            # Use httpx with extended timeout for thinking models
            timeout = httpx.Timeout(300.0, connect=30.0)  # 5 min for response

            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    self.BASE_URL,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

            # Parse JSON response
            data = response.json()
            logger.debug(f"Raw API response keys: {list(data.keys())}")

            # Extract message from response
            choices = data.get("choices", [])
            if not choices:
                raise ValueError("No choices in API response")

            message = choices[0].get("message", {})
            logger.debug(f"Message keys: {list(message.keys())}")

            # Extract content from the response
            # Kimi K2 Thinking: content has the answer, reasoning_content has the thinking
            content = message.get("content") or ""

            # Log reasoning length for debugging (but don't use it as content)
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if reasoning:
                logger.debug(f"Thinking process: {len(reasoning)} chars")

            # If content is still empty, something went wrong
            if not content:
                logger.warning(
                    f"Empty content! This usually means max_tokens was too low. "
                    f"Reasoning length: {len(reasoning)}, Message keys: {list(message.keys())}"
                )

            # Extract token usage
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)

            logger.info(
                f"Nebius completion: {input_tokens} input, {output_tokens} output tokens, "
                f"content length: {len(content)}"
            )

            return ChatResponse(
                content=content,
                model=self._model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"Nebius API HTTP error: {e.response.status_code} - {e.response.text[:500]}")

            # Try fallback model if primary fails
            if self._model == self.PRIMARY_MODEL:
                logger.info(f"Trying fallback model: {self.FALLBACK_MODEL}")
                self._model = self.FALLBACK_MODEL
                return self.chat_completion(messages, max_tokens, temperature)

            raise ValueError(f"API-Fehler: {e.response.status_code}") from e

        except Exception as e:
            logger.error(f"Nebius API error: {e}")

            # Try fallback model if primary fails
            if self._model == self.PRIMARY_MODEL:
                logger.info(f"Trying fallback model: {self.FALLBACK_MODEL}")
                self._model = self.FALLBACK_MODEL
                return self.chat_completion(messages, max_tokens, temperature)

            raise
