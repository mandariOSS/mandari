"""
Gemeinsame Obergrenzen für Anwendung und Ingestor.

``MAX_TEXT_CHARS``: Höchstens so viele Zeichen erkannten Texts speichern beide je Datei (``text_content``). Eine
kleine Datei kann sehr viel Text ergeben, und jeder Leseweg müsste ihn sonst ganz verarbeiten. Lange Ratsdokumente
wie Haushaltspläne erreichen gut 1 Mio. Zeichen; die Grenze lässt dafür Raum.
"""

from typing import Final

MAX_TEXT_CHARS: Final = 5_000_000


def truncation_note(limit: int | None = None) -> str:
    """Vermerk für einen an der Obergrenze gekürzten Text (ohne Inhalte)."""
    grenze = MAX_TEXT_CHARS if limit is None else limit
    return f"Text nach {grenze:,} Zeichen gekürzt (Obergrenze je Datei)".replace(",", ".")


def limit_text(text: str) -> tuple[str, str | None]:
    """Text auf ``MAX_TEXT_CHARS`` Zeichen kürzen. Zweiter Wert: Vermerk, wenn gekürzt wurde, sonst ``None``."""
    grenze = MAX_TEXT_CHARS
    if len(text) <= grenze:
        return text, None
    return text[:grenze], truncation_note(grenze)
