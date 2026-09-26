# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AI Security Module for Motion Assistant.

Provides security measures for AI integration:
- Input sanitization to prevent prompt injection
- Output filtering to prevent XSS and malicious content
- Rate limiting to prevent abuse
"""

import html
import re

from django.core.cache import cache


class AIInputSanitizer:
    """
    Sanitizes user input before sending to AI models.

    Detects and neutralizes prompt injection attempts.
    """

    # Patterns that may indicate prompt injection
    INJECTION_PATTERNS = [
        r"ignore\s+(previous|all|the|your)?\s*(instructions|prompt|rules)",
        r"disregard\s+(the\s+)?(above|previous)",
        r"you\s+are\s+now",
        r"act\s+as",
        r"pretend\s+(to\s+be|you\s+are)",
        r"forget\s+(all\s+)?(previous\s+|your\s+)?instructions",
        r"new\s+instruction(s)?:",
        r"system\s+prompt:",
        r"\[INST\]",
        r"\[/INST\]",
        r"<\|.*?\|>",
        r"###\s*(system|human|assistant)",
        r"<\|(system|user|assistant)\|>",
        r"override\s+.*instructions",
        r"bypass\s+.*restrictions",
        r"jailbreak",
        r"DAN\s*mode",
    ]

    MAX_INPUT_LENGTH = 50000  # 50k characters max

    @classmethod
    def sanitize(cls, text: str, log_attempts: bool = True) -> str:
        """
        Sanitize text input before sending to AI.

        Args:
            text: Raw user input
            log_attempts: Whether to log detected injection attempts

        Returns:
            Sanitized text safe for AI processing
        """
        if not text:
            return ""

        # Truncate overly long input
        text = text[: cls.MAX_INPUT_LENGTH]

        # Check for and neutralize injection patterns
        for pattern in cls.INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                if log_attempts:
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(f"Potential prompt injection detected: pattern '{pattern}'")
                # Replace with innocuous text
                text = re.sub(pattern, "[eingabe entfernt]", text, flags=re.IGNORECASE)

        return text

    @classmethod
    def validate_action(cls, action: str) -> bool:
        """
        Validate that an AI action is in the allowed list.

        Args:
            action: The requested AI action

        Returns:
            True if action is allowed, False otherwise
        """
        ALLOWED_ACTIONS = {
            "improve",
            "shorten",
            "expand",
            "check",
            "suggestions",
            "formalize",
            "title",
            "summary",
            "translate",
        }
        return action.lower() in ALLOWED_ACTIONS


class AIOutputFilter:
    """
    Filters AI output before displaying to user.

    Prevents XSS and other malicious content in AI responses.
    """

    # Patterns that should never appear in output
    FORBIDDEN_PATTERNS = [
        r"<script[^>]*>",
        r"javascript:",
        r"on\w+\s*=",
        r"data:\s*text/html",
        r"<iframe[^>]*>",
        r"<object[^>]*>",
        r"<embed[^>]*>",
        r"<link[^>]*>",
    ]

    # Tags that are safe to keep after escaping
    SAFE_TAGS = [
        "strong",
        "b",
        "em",
        "i",
        "u",
        "br",
        "p",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "blockquote",
        "pre",
        "code",
        "span",
        "div",
    ]

    @classmethod
    def filter(cls, text: str, allow_html: bool = True) -> str:
        """
        Filter AI output for safe display.

        Args:
            text: Raw AI output
            allow_html: Whether to allow basic HTML formatting

        Returns:
            Filtered text safe for display
        """
        if not text:
            return ""

        # Check for forbidden patterns first
        for pattern in cls.FORBIDDEN_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(f"Forbidden pattern in AI output: {pattern}")
                text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        if not allow_html:
            # Full HTML escape if no HTML allowed
            return html.escape(text)

        # Escape HTML first
        text = html.escape(text)

        # Re-enable safe formatting tags
        for tag in cls.SAFE_TAGS:
            # Opening tags (with optional attributes for some)
            text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>")
            text = text.replace(f"&lt;{tag.upper()}&gt;", f"<{tag}>")
            # Closing tags
            text = text.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
            text = text.replace(f"&lt;/{tag.upper()}&gt;", f"</{tag}>")

        return text

    @classmethod
    def sanitize_for_json(cls, text: str) -> str:
        """
        Sanitize text for inclusion in JSON responses.

        Args:
            text: Text to sanitize

        Returns:
            JSON-safe string
        """
        if not text:
            return ""

        # Remove null bytes and other control characters
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


class AIRateLimiter:
    """
    Rate limiting for AI requests to prevent abuse.

    Uses Redis/cache backend for distributed rate limiting.
    """

    LIMITS = {
        "per_minute": 10,
        "per_hour": 100,
        "per_day": 500,
    }

    TTL_SECONDS = {
        "per_minute": 60,
        "per_hour": 3600,
        "per_day": 86400,
    }

    @classmethod
    def check_limit(cls, user_id: int, organization_id: str = None) -> tuple[bool, str]:
        """
        Check if user has exceeded rate limits.

        Args:
            user_id: The user's ID
            organization_id: Optional organization ID for org-level limits

        Returns:
            Tuple of (allowed: bool, message: str)
        """
        for period, limit in cls.LIMITS.items():
            key = cls._get_key(user_id, period)
            count = cache.get(key, 0)

            if count >= limit:
                return False, cls._get_limit_message(period)

        return True, ""

    @classmethod
    def increment(cls, user_id: int, organization_id: str = None) -> None:
        """
        Increment rate limit counters for a user.

        Args:
            user_id: The user's ID
            organization_id: Optional organization ID
        """
        for period, ttl in cls.TTL_SECONDS.items():
            key = cls._get_key(user_id, period)
            try:
                cache.incr(key)
            except ValueError:
                # Key doesn't exist yet, create it
                cache.set(key, 1, ttl)

    @classmethod
    def get_remaining(cls, user_id: int, period: str = "per_minute") -> int:
        """
        Get remaining requests for a given period.

        Args:
            user_id: The user's ID
            period: Time period to check

        Returns:
            Number of remaining requests
        """
        key = cls._get_key(user_id, period)
        count = cache.get(key, 0)
        limit = cls.LIMITS.get(period, 10)
        return max(0, limit - count)

    @classmethod
    def _get_key(cls, user_id: int, period: str) -> str:
        """Generate cache key for rate limiting."""
        return f"ai_rate_limit:{user_id}:{period}"

    @classmethod
    def _get_limit_message(cls, period: str) -> str:
        """Get user-friendly rate limit message."""
        messages = {
            "per_minute": "Zu viele Anfragen. Bitte warten Sie eine Minute.",
            "per_hour": "Stündliches Limit erreicht. Bitte versuchen Sie es später.",
            "per_day": "Tägliches Limit erreicht. Bitte versuchen Sie es morgen wieder.",
        }
        return messages.get(period, "Rate limit erreicht.")
