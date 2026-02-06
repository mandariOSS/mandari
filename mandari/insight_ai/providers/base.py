# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abstract base class for AI providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChatMessage:
    """A single chat message."""

    role: str  # "system", "user", or "assistant"
    content: str


@dataclass
class ChatResponse:
    """Response from a chat completion."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


class AbstractAIProvider(ABC):
    """
    Abstract base class for AI providers.

    Implementations should support OpenAI-compatible chat completions.
    """

    @abstractmethod
    def chat_completion(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> ChatResponse:
        """
        Generate a chat completion.

        Args:
            messages: List of ChatMessage objects
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (0.0 - 1.0)

        Returns:
            ChatResponse with generated content and token usage
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if the provider is properly configured.

        Returns:
            True if API key is configured and valid
        """
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return the model name being used."""
        pass
