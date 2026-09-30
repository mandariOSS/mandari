# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugangsdaten aus Verbindungs-URLs entfernen, bevor Texte ausgegeben oder geloggt werden.

Verbindungs-URLs für Datenbank, Redis, Elasticsearch oder den OTLP-Collector tragen im Betrieb oft
Benutzer und Passwort (``redis://:<passwort>@redis:6379``). Auch Fehlermeldungen von Bibliotheken
enthalten solche URLs (httpx nennt bei ``raise_for_status`` die vollständige Adresse).

Eine Stelle für alle Ausgabewege:

- ``mask_credentials()`` ersetzt Benutzer/Passwort in URLs und Werte von Passwort-/Token-Parametern
  durch ``***``.
- ``MaskingConsole`` wendet das auf jede Rich-Konsolenausgabe an; alle Module nutzen sie statt
  ``rich.console.Console``.
- Die Log-Formatter in ``src/observability.py`` maskieren Meldung und Traceback jedes Log-Eintrags.
"""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console

MASK = "***"

# Benutzerangaben einer URL: alles zwischen "schema://" und dem letzten "@" vor dem ersten "/" bzw.
# Leerraum. Gierig bis zum letzten "@", damit auch ein unkodiertes "@" im Passwort nichts übrig lässt.
_URL_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/]+@")

# Parameter mit geheimen Werten, in URLs (?password=…) wie in DSN-Schreibweise (password=…).
_SECRET_PARAM = re.compile(
    r"(?P<key>\b(?:password|passwd|pwd|secret|token|access_token|api_key|apikey)=)[^&\s#'\"]+",
    re.IGNORECASE,
)


def mask_credentials(text: str) -> str:
    """Text ohne Zugangsdaten aus URLs: Benutzer/Passwort und Passwort-Parameter werden zu ``***``.

    Beispiele::

        redis://:geheim@redis:6379/0            -> redis://***@redis:6379/0
        postgresql://mandari:geheim@db/mandari  -> postgresql://***@db/mandari
        rediss://redis:6380?password=geheim     -> rediss://redis:6380?password=***
    """
    if not text:
        return text
    masked = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", text)
    return _SECRET_PARAM.sub(lambda m: f"{m.group('key')}{MASK}", masked)


class MaskingConsole(Console):
    """Rich-Konsole, die Zugangsdaten in ausgegebenen Texten maskiert (siehe ``mask_credentials``)."""

    def print(self, *objects: Any, **kwargs: Any) -> None:
        super().print(*(mask_credentials(o) if isinstance(o, str) else o for o in objects), **kwargs)
