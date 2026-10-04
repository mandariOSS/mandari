# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt nach RFC 9309 – Auswertung ohne Netzzugriff, gemeinsam für Ingestor und Django.

Warum eigener Parser? ``urllib.robotparser`` kennt die Platzhalter ``*`` und ``$`` nicht und wertet
Regeln in Dateireihenfolge statt nach der längsten Übereinstimmung aus. ``Disallow: /*.pdf$`` (sperrt nur
Dokumente, die Schnittstelle bleibt frei) bliebe damit wirkungslos.

Regeln (RFC 9309):

- Gruppen: Alle Gruppen, deren ``User-agent`` unserem Produkt-Token entspricht (ohne Groß-/Kleinschreibung),
  gelten gemeinsam; gibt es keine, gelten alle ``*``-Gruppen; gibt es auch die nicht, ist alles erlaubt.
- Übereinstimmung: Die längste passende Regel entscheidet; sind ``Allow`` und ``Disallow`` gleich lang,
  gewinnt ``Allow``. ``*`` steht für beliebig viele Zeichen, ``$`` am Ende verankert das Pfadende.
  Pfad und Abfrage (``?…``) werden verglichen, Groß-/Kleinschreibung zählt.
- Abruf: 2xx wird ausgewertet; 4xx (auch 401/403/406) heißt „nicht vorhanden“, also alles erlaubt;
  5xx, 408, 429 oder ein Netzfehler heißt „nicht erreichbar“: Abrufe werden zurückgestellt, bis ein Abruf
  der robots.txt gelingt (429 behandeln wir wie 5xx: Ein Host, der uns bremst, gibt damit keine Freigabe).
  „Nicht erreichbar“ ist eine Störung, keine Sperre: Wer die Prüfung nutzt, stellt den Abruf zurück und
  versucht es später erneut, statt ihn als gesperrt zu überspringen (:attr:`Decision.unreachable`).
- ``/robots.txt`` selbst ist immer erlaubt; ausgewertet werden höchstens 500 KiB.

Abrufe unterscheiden zwei Arten, weil viele Systeme nur Dokumente sperren: ``api`` (OParl-JSON, HTML-Seiten
der Scraper) und ``files`` (Datei-Downloads). Die robots.txt gilt für beide gleich; die Art zählt für die
Ausnahmen je Quelle (``sync_config["robots_override"]``, siehe :func:`robots_override`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from .crawler import PRODUCT_TOKEN

#: Höchstens so viele Bytes einer robots.txt werden ausgewertet (RFC 9309 verlangt mindestens 500 KiB)
MAX_BYTES = 500 * 1024

#: Gültigkeit eines erfolgreich abgerufenen Ergebnisses (RFC 9309: höchstens 24 Stunden)
CACHE_SECONDS = 24 * 3600

#: Nach „nicht erreichbar“ (5xx, Netzfehler) wird nach dieser Zeit erneut abgerufen
RETRY_UNREACHABLE_SECONDS = 15 * 60

#: Zustände einer robots.txt
STATE_PARSED = "parsed"  # abgerufen und ausgewertet
STATE_UNAVAILABLE = "unavailable"  # 4xx: gilt als nicht vorhanden, alles erlaubt
STATE_UNREACHABLE = "unreachable"  # 5xx, 408, 429 oder Netzfehler: Abrufe zurückstellen
#: Zustand einer Entscheidung, die eine Ausnahme der Quelle getroffen hat (ohne Blick in die robots.txt)
STATE_OVERRIDE = "override"

#: Statuscodes, die wie ein Serverfehler als „nicht erreichbar“ gelten (Zeitüberschreitung, Ratenlimit)
_UNREACHABLE_CLIENT_CODES = frozenset({408, 429})

#: Arten von Abrufen
KIND_API = "api"
KIND_FILES = "files"

#: Anfang der Fehlertexte gesperrter Abrufe (alle Texte von :attr:`Decision.reason`). Danach suchen
#: ``robots_report --requeue`` und ``robots_override``, um nach einer Freigabe neu einzureihen.
SKIP_ERROR_PREFIX = "robots.txt"

#: Ausnahme je Quelle in ``OParlSource.sync_config``
ROBOTS_OVERRIDE_KEY = "robots_override"
SCOPE_ALL = "all"
OVERRIDE_SCOPES = (KIND_API, KIND_FILES, SCOPE_ALL)
#: Ein Vermerk muss nachvollziehbar sein (wer hat wann was freigegeben), ein Stichwort reicht nicht
MIN_NOTE_LENGTH = 10
MAX_NOTE_LENGTH = 500

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
# Zeichen, die beim Vergleich unverändert bleiben: reservierte Zeichen (RFC 3986), Platzhalter und bereits
# kodierte Folgen; alles andere außerhalb von ASCII wird kodiert
_SAFE = "/?#[]@!$&'()*+,;=:%"


def _normalize(value: str) -> str:
    """
    Prozentkodierung vereinheitlichen (RFC 9309, Abschnitt 2.2.2): Zeichen außerhalb von US-ASCII werden
    (UTF-8) kodiert, kodierte nicht reservierte Zeichen dekodiert, Hex-Ziffern großgeschrieben.
    """

    def _decode(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PERCENT.sub(_decode, quote(value, safe=_SAFE))


def _target(url_or_path: str) -> str:
    """Pfad mit Abfrage einer URL (oder eines Pfads), normalisiert; leerer Pfad ist ``/``."""
    parts = urlsplit(url_or_path)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _normalize(path)


@dataclass(frozen=True)
class Rule:
    """Eine ``Allow``- oder ``Disallow``-Zeile."""

    allow: bool
    pattern: str
    _regex: re.Pattern[str] = field(repr=False, compare=False)

    @classmethod
    def build(cls, allow: bool, raw: str) -> Rule:
        pattern = _normalize(raw if raw.startswith(("/", "*")) else f"/{raw}")
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else "")
        return cls(allow=allow, pattern=pattern, _regex=re.compile(regex, re.DOTALL))

    def matches(self, target: str) -> bool:
        return self._regex.match(target) is not None

    def __str__(self) -> str:
        return f"{'Allow' if self.allow else 'Disallow'}: {self.pattern}"


@dataclass(frozen=True)
class Decision:
    """
    Ergebnis einer Prüfung; ``rule`` nennt die entscheidende Zeile (für Protokoll und Bericht).

    Drei Fälle für den Aufrufer: erlaubt (``allowed``), gesperrt (``blocked``: die robots.txt untersagt den
    Abruf, überspringen bis zu einer Freigabe) und nicht erreichbar (``unreachable``: zurückstellen und später
    erneut versuchen, kein Befund gegen die Quelle).
    """

    allowed: bool
    state: str
    rule: str = ""
    #: HTTP-Status des Abrufs der robots.txt bei „nicht erreichbar“ (``None``: Netzfehler)
    status_code: int | None = None

    @property
    def unreachable(self) -> bool:
        """Die robots.txt war nicht erreichbar: Abruf zurückstellen, nicht als gesperrt werten."""
        return not self.allowed and self.state == STATE_UNREACHABLE

    @property
    def blocked(self) -> bool:
        """Die robots.txt untersagt den Abruf."""
        return not self.allowed and self.state != STATE_UNREACHABLE

    @property
    def reason(self) -> str:
        """Kurzer fester Text für Protokolle und Fehlermeldungen (ohne Ausnahmetexte)."""
        if self.state == STATE_UNREACHABLE:
            cause = f"HTTP {self.status_code}" if self.status_code else "Netzfehler"
            return f"robots.txt nicht erreichbar ({cause}), Abruf zurückgestellt"
        if self.allowed:
            return "robots.txt erlaubt den Abruf"
        return f"robots.txt sperrt den Abruf ({self.rule})" if self.rule else "robots.txt sperrt den Abruf"


@dataclass(frozen=True)
class RobotsTxt:
    """Ausgewertete robots.txt eines Hosts (Gruppen je User-Agent)."""

    state: str
    groups: tuple[tuple[frozenset[str], tuple[Rule, ...]], ...] = ()
    status_code: int | None = None

    @classmethod
    def parse(cls, text: str | bytes, status_code: int | None = 200) -> RobotsTxt:
        if isinstance(text, bytes):
            text = text[:MAX_BYTES].decode("utf-8", errors="replace")
        else:
            text = text[:MAX_BYTES]
        text = text.lstrip("﻿")
        groups: list[tuple[set[str], list[Rule]]] = []
        agents: set[str] | None = None
        rules: list[Rule] = []
        in_rules = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "").replace("_", "-")
            value = value.strip()
            if key in ("user-agent", "useragent"):
                if agents is None or in_rules:
                    agents = set()
                    rules = []
                    groups.append((agents, rules))
                    in_rules = False
                token = value.split("/", 1)[0].strip().lower()
                if token:
                    agents.add(token)
            elif key in ("allow", "disallow"):
                if agents is None:
                    continue  # Regeln ohne vorangehenden User-agent gehören zu keiner Gruppe
                in_rules = True
                if value:  # leeres Disallow: keine Regel
                    rules.append(Rule.build(key == "allow", value))
            # Andere Felder (Sitemap, Crawl-delay, …) beenden keine Gruppe und werden ignoriert
        return cls(
            state=STATE_PARSED,
            groups=tuple((frozenset(a), tuple(r)) for a, r in groups),
            status_code=status_code,
        )

    @classmethod
    def from_response(cls, status_code: int | None, body: str | bytes | None = None) -> RobotsTxt:
        """
        Ergebnis eines Abrufs von ``/robots.txt`` (``status_code`` nach Weiterleitungen; ``None`` bei
        Netzfehler oder Zeitüberschreitung). 408 und 429 zählen wie 5xx als „nicht erreichbar“.
        """
        if status_code is None or status_code >= 500 or status_code in _UNREACHABLE_CLIENT_CODES:
            return cls(state=STATE_UNREACHABLE, status_code=status_code)
        if 200 <= status_code < 300:
            return cls.parse(body or "", status_code=status_code)
        # 3xx (zu viele Weiterleitungen) und 4xx: gilt als nicht vorhanden
        return cls(state=STATE_UNAVAILABLE, status_code=status_code)

    def _rules_for(self, token: str) -> list[Rule]:
        token = token.lower()
        named = [rule for agents, rules in self.groups if token in agents for rule in rules]
        if named or any(token in agents for agents, _ in self.groups):
            return named
        return [rule for agents, rules in self.groups if "*" in agents for rule in rules]

    def decide(self, url_or_path: str, tokens: Iterable[str] = (PRODUCT_TOKEN,)) -> Decision:
        """Darf die Adresse abgerufen werden? Bei mehreren Tokens müssen alle erlaubt sein."""
        if self.state == STATE_UNREACHABLE:
            return Decision(allowed=False, state=self.state, status_code=self.status_code)
        if self.state == STATE_UNAVAILABLE:
            return Decision(allowed=True, state=self.state)
        target = _target(url_or_path)
        if target == "/robots.txt":
            return Decision(allowed=True, state=self.state)
        for token in dict.fromkeys(t for t in tokens if t):
            best: Rule | None = None
            for rule in self._rules_for(token):
                if not rule.matches(target):
                    continue
                if (
                    best is None
                    or len(rule.pattern) > len(best.pattern)
                    or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
                ):
                    best = rule
            if best is not None and not best.allow:
                return Decision(allowed=False, state=self.state, rule=str(best))
        return Decision(allowed=True, state=self.state)


def robots_url(url: str) -> str:
    """Adresse der robots.txt zum Host einer URL (Schema, Host und Port bleiben)."""
    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}/robots.txt"


def host_key(url: str) -> str:
    """Schlüssel für Zwischenspeicher: Schema und Host (mit Port), kleingeschrieben."""
    parts = urlsplit(url)
    return f"{(parts.scheme or 'https').lower()}://{parts.netloc.lower()}"


@dataclass(frozen=True)
class RobotsOverride:
    """Ausnahme einer Quelle von der robots.txt (nur mit nachvollziehbarem Vermerk)."""

    scope: str
    note: str

    def covers(self, kind: str) -> bool:
        return self.scope == SCOPE_ALL or self.scope == kind

    def decision(self) -> Decision:
        """Entscheidung „erlaubt per Ausnahme“ (ohne Blick in die robots.txt)."""
        return Decision(allowed=True, state=STATE_OVERRIDE, rule=f"Ausnahme der Quelle ({self.scope})")


def _override_parts(sync_config: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    if not isinstance(sync_config, Mapping):
        return None, None
    raw = sync_config.get(ROBOTS_OVERRIDE_KEY)
    if raw is None:
        return None, None
    if not isinstance(raw, Mapping):
        return None, "Ausnahme ist kein Objekt (erwartet: scope, note)"
    scope = raw.get("scope", SCOPE_ALL)
    if scope not in OVERRIDE_SCOPES:
        return None, f"Ausnahme mit unbekanntem Bereich (erlaubt: {', '.join(OVERRIDE_SCOPES)})"
    note = raw.get("note")
    if not isinstance(note, str) or len(note.strip()) < MIN_NOTE_LENGTH:
        return None, "Ausnahme ohne Vermerk (Pflicht: wer hat was freigegeben), daher unwirksam"
    return raw, None


def robots_override(sync_config: Any) -> RobotsOverride | None:
    """
    Ausnahme aus ``sync_config["robots_override"]``, z. B.
    ``{"scope": "files", "note": "Freigabe der Stelle per E-Mail vom …, offizielle Anfrage läuft"}``.

    ``scope``: ``api`` (Schnittstelle), ``files`` (Dateien) oder ``all`` (Standard). Ohne Vermerk
    (mindestens zehn Zeichen) gilt die Ausnahme nicht; :func:`robots_override_problem` nennt den Grund.
    """
    raw, _problem = _override_parts(sync_config)
    if raw is None:
        return None
    return RobotsOverride(scope=str(raw.get("scope", SCOPE_ALL)), note=str(raw["note"]).strip()[:MAX_NOTE_LENGTH])


def robots_override_problem(sync_config: Any) -> str | None:
    """Warum eine eingetragene Ausnahme nicht gilt (``None``: keine oder gültige Ausnahme)."""
    return _override_parts(sync_config)[1]
