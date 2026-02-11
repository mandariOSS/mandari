# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Pre-LLM content filters for the chat service.

Three filter stages, all executed BEFORE the LLM call:
1. PII detection (email, phone, IBAN, etc.)
2. Spam/abuse detection (length, duplicates, gibberish)
3. Prompt injection detection (role override attempts)
"""

import re
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# PII Patterns
# =============================================================================

PII_PATTERNS = [
    # Email addresses
    (re.compile(r"[\w.+-]+@[\w.-]+\.\w{2,}", re.IGNORECASE), "E-Mail-Adresse"),
    # German phone numbers
    (re.compile(r"(\+49|0)\s?\d{2,4}[\s/\-]?\d{3,}", re.IGNORECASE), "Telefonnummer"),
    # German postal codes + city (5 digits followed by word)
    (re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][a-zäöüß]+", re.UNICODE), "Postadresse"),
    # IBAN
    (re.compile(r"[A-Z]{2}\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{0,2}"), "IBAN"),
    # Credit card numbers
    (re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"), "Kreditkartennummer"),
]

# =============================================================================
# Prompt Injection Patterns
# =============================================================================

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+.{0,20}instructions", re.IGNORECASE),
    re.compile(r"forget\s+.{0,20}rules", re.IGNORECASE),
    re.compile(r"new\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now", re.IGNORECASE),
    re.compile(r"act\s+as\b", re.IGNORECASE),
    re.compile(r"pretend\s+to\s+be", re.IGNORECASE),
    re.compile(r"ignore\s+previous", re.IGNORECASE),
    re.compile(r"disregard\s+.{0,20}(instructions|rules|prompt)", re.IGNORECASE),
    re.compile(r"\boverride\b", re.IGNORECASE),
    re.compile(r"\bDAN\b"),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"\bbypass\b", re.IGNORECASE),
]


def _check_pii(message: str) -> dict | None:
    """Check for personally identifiable information."""
    for pattern, pii_type in PII_PATTERNS:
        if pattern.search(message):
            logger.info(f"PII blocked: {pii_type} detected")
            return {
                "blocked": True,
                "reason": "pii",
                "filter_result": "pii_blocked",
                "message": (
                    "Bitte senden Sie keine persönlichen Daten wie E-Mail-Adressen, "
                    "Telefonnummern oder Bankdaten über den Chat. "
                    "Stellen Sie Ihre Frage ohne persönliche Informationen."
                ),
            }
    return None


def _check_spam(message: str, session_key: str) -> dict | None:
    """Check for spam/abuse patterns."""
    # Too short
    if len(message.strip()) < 2:
        return {
            "blocked": True,
            "reason": "spam",
            "filter_result": "spam_blocked",
            "message": "Bitte geben Sie eine vollständige Frage ein.",
        }

    # Too long
    if len(message) > 2000:
        return {
            "blocked": True,
            "reason": "spam",
            "filter_result": "spam_blocked",
            "message": "Ihre Nachricht ist zu lang. Bitte kürzen Sie sie auf maximal 2000 Zeichen.",
        }

    # No letters at all (only special chars/numbers)
    if not re.search(r"[a-zA-ZäöüÄÖÜß]", message):
        return {
            "blocked": True,
            "reason": "spam",
            "filter_result": "spam_blocked",
            "message": "Bitte geben Sie eine verständliche Frage ein.",
        }

    # Check for exact duplicates in recent messages (same session)
    if session_key:
        from insight_core.models import ChatUsage

        recent = list(
            ChatUsage.objects.filter(
                session_key=session_key,
                filter_result="passed",
            )
            .order_by("-created_at")
            .values_list("message", flat=True)[:3]
        )
        if message.strip() in [m.strip() for m in recent]:
            return {
                "blocked": True,
                "reason": "spam",
                "filter_result": "spam_blocked",
                "message": "Sie haben diese Frage bereits gestellt. Bitte formulieren Sie Ihre Frage anders.",
            }

    return None


def _check_injection(message: str) -> dict | None:
    """Check for prompt injection attempts."""
    for pattern in INJECTION_PATTERNS:
        if pattern.search(message):
            logger.warning(f"Injection attempt blocked: {pattern.pattern}")
            return {
                "blocked": True,
                "reason": "injection",
                "filter_result": "injection_blocked",
                "message": "Diese Anfrage kann nicht verarbeitet werden.",
            }
    return None


def check_message(message: str, session_key: str = "") -> dict:
    """
    Run all content filters on a message.

    Checks PII, spam, and injection filters in order.

    Args:
        message: The user message to check
        session_key: Django session key for duplicate detection

    Returns:
        {"blocked": False} if all checks pass, or
        {"blocked": True, "reason": str, "filter_result": str, "message": str}
    """
    # 1. PII check
    result = _check_pii(message)
    if result:
        return result

    # 2. Spam check
    result = _check_spam(message, session_key)
    if result:
        return result

    # 3. Injection check
    result = _check_injection(message)
    if result:
        return result

    return {"blocked": False}
