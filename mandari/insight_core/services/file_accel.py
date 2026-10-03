# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslieferung lokaler Dokumentkopien über den Webserver (``X-Accel-Redirect``, Issue #785).

Django prüft Zugriff und Sperre und antwortet ohne Dateiinhalt mit einer internen Weiterleitung.
Die Bytes liefert der Webserver aus der Ablage (Caddy: ``reverse_proxy`` mit ``handle_response``
und ``file_server``, siehe ``Caddyfile`` und docs/FILE_CACHE.md). Damit gibt es Range-Anfragen
(``206``), ``ETag``, ``Last-Modified`` und bedingte Anfragen, und kein Anwendungs-Thread ist mit
dem Download belegt.

Sicherheit:

* Die Schutzkopfzeilen aus ``file_delivery`` (Typ, Anzeigeart, Dateiname, ``nosniff``,
  CSP-Sandbox) stehen in der Antwort von Django; Caddy übernimmt genau diese per
  ``copy_response_headers`` und bestimmt den Typ nie nach der Dateiendung. Sonst würde eine
  HTML- oder SVG-Anlage im gemeinsamen Ursprung von Insight, Work und Session ausgeführt.
* Weitergeleitet werden nur Pfade, die nach Auflösen aller Verweise unterhalb der Ablage liegen
  und nur aus unverfänglichen Zeichen bestehen. Alles andere liefert Django wie bisher selbst aus.
* Caddy wertet die Kopfzeile nur in der Antwort von Django aus (``handle_response``); eine von
  außen mitgeschickte Kopfzeile bewirkt nichts, und der interne Pfad ist von außen nicht erreichbar.

Abgeschaltet (``FILE_ACCEL_REDIRECT=false``, Standard) bleibt alles wie bisher: Django streamt die
Datei per ``FileResponse``. Einschalten erst, wenn der Webserver die Ablage lesen kann und den
Block aus dem ``Caddyfile`` hat – sonst kommen leere Antworten an.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse

from . import file_cache, file_delivery

#: Präfix der internen Pfade; muss zum Matcher im Caddyfile passen
ACCEL_PREFIX = "/_mandari/dateien/"
ACCEL_HEADER = "X-Accel-Redirect"

#: Erlaubte Pfadteile unterhalb der Ablage: Verzeichnis der Kommune, Jahr, Datei-ID mit Endung bzw.
#: Hash. Keine versteckten Dateien, kein ``..``, keine Zeichen mit Sonderbedeutung in Adressen.
_PFADTEIL = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,254}")


def enabled() -> bool:
    """Liefert der Webserver lokale Kopien aus (``FILE_ACCEL_REDIRECT``)?"""
    return bool(getattr(settings, "FILE_ACCEL_REDIRECT", False))


def internal_path(path: Path | str) -> str | None:
    """
    Interner Pfad für den Webserver oder ``None``, wenn die Datei nicht sicher unter der Ablage liegt.

    Maßgeblich ist der aufgelöste Pfad (Verweise verfolgt), relativ zur ebenso aufgelösten Wurzel
    der Ablage. Fehlt die Datei, gibt es keinen Pfad – Django antwortet dann selbst.
    """
    try:
        root = file_cache.cache_root().resolve(strict=True)
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if not parts or not all(_PFADTEIL.fullmatch(part) and part not in {".", ".."} for part in parts):
        return None
    return ACCEL_PREFIX + "/".join(parts)


def response(
    path: Path | str, content_type: str | None, filename: str | None, *, download: bool
) -> HttpResponse | None:
    """
    Antwort mit interner Weiterleitung und allen Schutzkopfzeilen, oder ``None``, wenn der Weg
    abgeschaltet ist oder der Pfad nicht weitergeleitet werden darf.
    """
    if not enabled():
        return None
    target = internal_path(path)
    if target is None:
        return None
    result = HttpResponse(b"")
    file_delivery.apply(result, content_type, filename, download=download)
    result[ACCEL_HEADER] = target
    return result
