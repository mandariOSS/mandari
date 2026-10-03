# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt der Ratsinformationssysteme für die Dateiabrufe des Löschabgleichs (Issue #787).

Die robots.txt einer Quelle ist für unsere automatischen Abrufe verbindlich. Manche Quellen erlauben
die OParl-Schnittstelle, sperren aber die Dokumente, typischerweise mit ``Disallow: /*.pdf$``. Das ist
ein maschinenlesbarer Nutzungsvorbehalt (§ 44b Abs. 3 UrhG); unsere Abrufe der Dateien unterbleiben
dann. Die Standardbibliothek (``urllib.robotparser``) kennt weder ``*`` noch ``$``, deshalb hier ein
kleiner Abgleich nach RFC 9309:

* Gruppen nach ``User-agent``; maßgeblich ist die Gruppe unseres Produktnamens, sonst ``*``. Eine
  ``User-agent``-Zeile nach einer Zeile der Gruppe (Regel, ``Crawl-delay`` oder eine andere Angabe)
  beginnt eine neue Gruppe, wie in ``urllib.robotparser``. ``Sitemap`` gilt für die ganze Datei und
  gehört zu keiner Gruppe.
* ``Allow``/``Disallow`` mit ``*`` (beliebige Zeichen) und ``$`` (Ende); die längste passende Regel
  gewinnt, bei gleicher Länge ``Allow``.
* Keine robots.txt (4xx) bedeutet erlaubt; ist sie nicht abrufbar (5xx, Netzfehler), unterbleibt der
  Abruf, bis sie wieder zu lesen ist.

Eine Ausnahme je Quelle steht in ``sync_config["robots_override"]`` mit Bereich und Pflicht-Vermerk,
z. B. ``{"scope": "files", "note": "Zustimmung liegt vor, Anfrage läuft"}`` – dasselbe Format wie für
die übrigen Abrufe der Quelle. Für Dateien gelten die Bereiche ``files`` und ``all``; ohne Vermerk von
mindestens zehn Zeichen gibt es keine Ausnahme.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Schlüssel in ``OParlSource.sync_config``: Ausnahme mit Bereich und Vermerk
EXCEPTION_KEY = "robots_override"
#: Bereiche einer Ausnahme, die Dateiabrufe abdecken
FILE_SCOPES = ("files", "all")
#: Ein Vermerk muss nachvollziehbar sein (wer hat was freigegeben), ein Stichwort reicht nicht
MIN_NOTE_LENGTH = 10
#: Produktnamen, unter denen wir in einer robots.txt angesprochen werden (kleingeschrieben)
PRODUCT_TOKENS = ("mandari-file-cache", "mandari")
CACHE_SECONDS = 24 * 3600
#: Bei nicht lesbarer robots.txt nach kurzer Zeit erneut versuchen
ERROR_CACHE_SECONDS = 3600
MAX_BYTES = 512 * 1024

ALLOWED = "erlaubt"
DISALLOWED = "gesperrt"
UNREACHABLE = "nicht_lesbar"


@dataclass
class Rules:
    """Regeln der für uns maßgeblichen Gruppe einer robots.txt."""

    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    #: robots.txt war nicht lesbar: alles gesperrt
    unreachable: bool = False


def _pattern(path: str) -> re.Pattern[str]:
    anchored = path.endswith("$")
    body = path[:-1] if anchored else path
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def _match_length(rule: str, path: str) -> int:
    """Länge der Regel, wenn sie auf ``path`` passt, sonst -1."""
    if not rule:
        return -1
    return len(rule) if _pattern(rule).match(path) else -1


def parse(text: str) -> Rules:
    """Regeln der Gruppe für unseren Produktnamen, sonst der Gruppe ``*``."""
    groups: dict[str, Rules] = {}
    current: list[str] = []
    # Kam seit der letzten User-agent-Zeile eine andere Zeile? Dann beginnt die nächste eine neue Gruppe.
    group_closed = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if group_closed:
                current = []
                group_closed = False
            agent = value.lower()
            current.append(agent)
            groups.setdefault(agent, Rules())
            continue
        if key == "sitemap":
            # Gilt für die ganze Datei, gehört zu keiner Gruppe
            continue
        group_closed = True
        if key in ("allow", "disallow") and current:
            for agent in current:
                (groups[agent].allow if key == "allow" else groups[agent].disallow).append(value)
    for token in PRODUCT_TOKENS:
        if token in groups:
            return groups[token]
    return groups.get("*", Rules())


def is_allowed(rules: Rules, url: str) -> bool:
    """Darf ``url`` nach diesen Regeln abgerufen werden?"""
    if rules.unreachable:
        return False
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    best_allow = max((_match_length(rule, path) for rule in rules.allow), default=-1)
    best_disallow = max((_match_length(rule, path) for rule in rules.disallow), default=-1)
    return best_disallow < 0 or best_allow >= best_disallow


def _robots_url(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def _fetch(robots_url: str, client: httpx.Client) -> tuple[Rules, int]:
    try:
        with client.stream("GET", robots_url) as response:
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                return Rules(), CACHE_SECONDS
            if response.status_code != 200:
                return Rules(unreachable=True), ERROR_CACHE_SECONDS
            data = b""
            for chunk in response.iter_bytes(64 * 1024):
                data += chunk
                if len(data) > MAX_BYTES:
                    break
    except httpx.HTTPError as exc:
        logger.info("robots.txt %s nicht lesbar: %s", robots_url, type(exc).__name__)
        return Rules(unreachable=True), ERROR_CACHE_SECONDS
    return parse(data[:MAX_BYTES].decode("utf-8", errors="replace")), CACHE_SECONDS


def rules_for(url: str, client: httpx.Client) -> Rules:
    """Regeln für den Host von ``url`` (je Host einen Tag zwischengespeichert)."""
    robots_url = _robots_url(url)
    if robots_url is None:
        return Rules(unreachable=True)
    key = f"robots:{robots_url}"
    cached = cache.get(key)
    if isinstance(cached, Rules):
        return cached
    rules, seconds = _fetch(robots_url, client)
    cache.set(key, rules, seconds)
    return rules


def exception_note(source: object) -> str:
    """Vermerk einer Ausnahme der Quelle für Dateiabrufe (leer = keine gültige Ausnahme)."""
    config = getattr(source, "sync_config", None)
    raw = config.get(EXCEPTION_KEY) if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("scope", "all") not in FILE_SCOPES:
        return ""
    note = raw.get("note")
    note = note.strip() if isinstance(note, str) else ""
    return note if len(note) >= MIN_NOTE_LENGTH else ""


def file_fetch_status(source: object, url: str, client: httpx.Client) -> str:
    """``erlaubt``, ``gesperrt`` oder ``nicht_lesbar`` für einen automatischen Abruf von ``url``."""
    if exception_note(source):
        return ALLOWED
    rules = rules_for(url, client)
    if rules.unreachable:
        return UNREACHABLE
    return ALLOWED if is_allowed(rules, url) else DISALLOWED
