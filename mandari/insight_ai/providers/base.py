# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abstract base class for AI providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: Obergrenze der Antwortlänge, wenn die Konfiguration keine nennt (wie AISettings.insight_max_output_tokens):
#: dokumentierte maximale Antwortlänge von openai/gpt-oss-120b bei STACKIT AI Model Serving
STANDARD_MAX_AUSGABE = 8192


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


@dataclass
class ToolCall:
    """Aufruf eines Werkzeugs, wie ihn das Modell anfordert (OpenAI-kompatibles Function Calling)."""

    id: str
    name: str
    #: Argumente als JSON-Text, so wie das Modell sie liefert (kann ungültig sein)
    arguments: str


@dataclass
class ToolChatResponse:
    """Antwort einer Runde mit Werkzeugen: Text oder Werkzeugaufrufe, dazu der Verbrauch."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Nachricht des Assistenten für den Verlauf der nächsten Runde (mit ``tool_calls``)
    message: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ToolChatProvider(Protocol):
    """Anbieter mit Runden mit Werkzeugen (``OpenAIKompatiblerProvider``; in Tests ein Ersatz ohne Netz)."""

    def is_available(self) -> bool: ...

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        max_tokens: int = 1500,
        temperature: float = 0.3,
        timeout: float = 120.0,
    ) -> ToolChatResponse: ...


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

    @property
    def max_output_tokens(self) -> int:
        """Obergrenze der Antwortlänge je Aufruf (aus der KI-Konfiguration, sonst der Standard)."""
        return STANDARD_MAX_AUSGABE
