# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AI Providers for Mandari Insight.

Abstraction layer for different AI API providers.
"""

from .base import AbstractAIProvider
from .nebius import NebiusProvider

__all__ = ["AbstractAIProvider", "NebiusProvider"]
