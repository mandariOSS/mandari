# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AI Providers for Mandari Insight.

Abstraction layer for different AI API providers.
"""

from .base import AbstractAIProvider
from .nebius import NebiusProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = ["AbstractAIProvider", "NebiusProvider", "OpenAICompatibleProvider", "chat_provider"]


def chat_provider() -> OpenAICompatibleProvider:
    """
    Anbieter des KI-Assistenten im Bürgerportal: die einzige Stelle, an der er gewählt wird.

    Die Werkzeugrunden setzen nur eine OpenAI-kompatible Schnittstelle voraus. Bis die Anbieterwahl aus Issue #950
    umgesetzt ist, bleibt es der bisherige Anbieter des KI-Assistenten.
    """
    return NebiusProvider()
