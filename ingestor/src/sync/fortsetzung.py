# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fortsetzung eines Vollabgleichs, den das Zeitfenster der Quelle abbricht.

Die Stadt Köln erlaubt Abrufe nur von 23 bis 1 Uhr (``sync_window``). Bei einer Anfrage je Sekunde liest der
Vollabgleich in diesen zwei Stunden Gremien, Personen, Sitzungen und Vorlagen ganz, von Tagesordnungspunkten, Dateien
und Beratungen aber nur die ersten gut tausend Seiten. Weil er nie fertig wurde, blieb er fällig
(``last_successful_full_sync`` leer) und begann jede Nacht wieder bei Seite 1: Er kam nie weiter, und die Quelle stand
dauerhaft auf „seither mit Lücken“.

Jetzt setzt der nächste Vollabgleich fort (``sync_config["full_sync_resume"]``, je Liste):

- **Abgebrochen** (``{"page": …, "at": …}``): Endet das Zeitfenster mitten in einer Liste, merkt sich der Lauf die
  zuletzt ganz gelesene Seite. Der nächste Vollabgleich liest die Liste ab dieser Seite (eine Seite Überlappung, falls
  die Quelle Einträge nach vorn verschiebt) bis zum Ende.
- **Gelesen** (``{"done": true, "at": …}``): Listen, die ein Lauf des Durchgangs schon ganz gelesen hat, liest der
  nächste Vollabgleich nur noch mit ``modified_since`` (Änderungsfenster der Kommune wie im inkrementellen Lauf). Sie
  bleiben so aktuell, ohne die Zeit des Fensters zu verbrauchen.

Ein Durchgang beginnt mit dem ersten Abbruch am Fensterende; ohne Abbruch schreibt der Vollabgleich nichts. Er endet,
sobald ein Lauf vollständig ist: Dann ist jede Liste innerhalb des Durchgangs ganz gelesen und seither mit ihren
Änderungen nachgeführt, ``last_successful_full_sync`` rückt vor, und die Einträge werden gelöscht. Einträge, die älter
als ``RESUME_MAX_AGE`` sind, gelten nicht mehr: Die Liste wird dann wieder ganz gelesen, damit ein Durchgang nicht
über Wochen aus alten Teilen zusammengesetzt wird. Abgleiche in Zeitscheiben (``full_sync_slices``) haben ihren
eigenen Fortschritt und nutzen die Fortsetzung nicht.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

#: So lange gilt ein Eintrag; danach liest der Vollabgleich die Liste wieder ganz
RESUME_MAX_AGE = timedelta(days=14)

#: Wie der Vollabgleich eine Liste liest
FULL = "full"
RESUME = "resume"
CHANGES = "changes"


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ResumeEntry:
    """Stand einer Liste im Durchgang: ganz gelesen (``done``) oder abgebrochen nach Seite ``page``."""

    at: datetime
    page: str | None = None
    done: bool = False

    @classmethod
    def from_entry(cls, raw: Any) -> ResumeEntry | None:
        if not isinstance(raw, Mapping):
            return None
        at = _stamp(raw.get("at"))
        if at is None:
            return None
        if raw.get("done") is True:
            return cls(at=at, done=True)
        page = raw.get("page")
        if isinstance(page, str) and page.startswith(("http://", "https://")):
            return cls(at=at, page=page)
        return None

    def to_entry(self) -> dict[str, Any]:
        if self.done:
            return {"done": True, "at": _iso(self.at)}
        return {"page": self.page, "at": _iso(self.at)}


def resume_entries_of(stored: Mapping[str, Any], now: datetime | None = None) -> dict[str, ResumeEntry]:
    """Gültige Einträge aus ``sync_config["full_sync_resume"]``; ungültige und zu alte fallen weg."""
    now = now or datetime.now(UTC)
    entries: dict[str, ResumeEntry] = {}
    for list_url, raw in stored.items():
        entry = ResumeEntry.from_entry(raw)
        if isinstance(list_url, str) and list_url and entry is not None and now - entry.at <= RESUME_MAX_AGE:
            entries[list_url] = entry
    return entries


@dataclass
class ResumeRun:
    """Fortsetzung in einem Vollabgleich einer Quelle (geteilt von allen Kommunen und Listen)."""

    source_url: str
    #: Stand aus früheren Läufen des Durchgangs
    entries: dict[str, ResumeEntry] = field(default_factory=dict)
    #: In diesem Lauf ganz gelesene Listen (ganz oder ab der Fortsetzungsseite)
    finished: set[str] = field(default_factory=set)
    #: In diesem Lauf am Fensterende abgebrochene Listen -> zuletzt ganz gelesene Seite (``None``: keine)
    cut: dict[str, str | None] = field(default_factory=dict)
    #: Alle Einträge der Quelle in ``sync_config``, auch zu alte (zum Aufräumen)
    stored: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, source_url: str, stored: Mapping[str, Any], now: datetime | None = None) -> ResumeRun:
        """``stored``: Einträge aus ``sync_config["full_sync_resume"]`` (``full_sync_resume_of``)."""
        names = {name for name in stored if isinstance(name, str)}
        return cls(source_url=source_url, entries=resume_entries_of(stored, now), stored=names)

    def plan(self, list_url: str) -> tuple[str, str | None]:
        """
        Wie der Vollabgleich die Liste liest: ``(FULL, None)`` ganz, ``(RESUME, Seite)`` ab der Fortsetzungsseite,
        ``(CHANGES, None)`` nur mit ``modified_since`` (in diesem Durchgang schon ganz gelesen).
        """
        entry = self.entries.get(list_url)
        if entry is None:
            return FULL, None
        if entry.done:
            return CHANGES, None
        return RESUME, entry.page

    def note_finished(self, list_url: str) -> None:
        self.finished.add(list_url)
        self.cut.pop(list_url, None)

    def note_cut(self, list_url: str, last_page: str | None) -> None:
        """
        Liste am Fensterende abgebrochen. ``last_page``: zuletzt ganz gelesene Seite dieses Laufs. Ohne sie (keine
        Seite gelesen) bleibt eine frühere Fortsetzungsseite stehen; die erste Seite selbst ist keine Fortsetzung.
        """
        if last_page is None or last_page == list_url:
            previous = self.entries.get(list_url)
            last_page = previous.page if previous is not None and not previous.done else None
        self.cut[list_url] = last_page

    def active(self) -> bool:
        """Läuft ein Durchgang (früherer Abbruch oder Abbruch in diesem Lauf)?"""
        return bool(self.entries) or any(page is not None for page in self.cut.values())

    def changes(self, complete: bool, now: datetime | None = None) -> dict[str, dict[str, Any] | None]:
        """
        Einträge für ``sync_config["full_sync_resume"]`` nach dem Lauf (``None`` löscht). Vollständig: alle weg. Sonst,
        wenn ein Durchgang läuft: ganz gelesene Listen als gelesen (mit dem Zeitpunkt ihres ersten Eintrags),
        abgebrochene mit ihrer Seite; zu alte Einträge fallen weg.
        """
        if complete:
            return dict.fromkeys(sorted(self.stored))
        now = now or datetime.now(UTC)
        result: dict[str, dict[str, Any] | None] = {
            name: None for name in sorted(self.stored) if name not in self.entries
        }
        if not self.active():
            return result
        for list_url in sorted(self.finished):
            previous = self.entries.get(list_url)
            at = previous.at if previous is not None and previous.done else now
            result[list_url] = ResumeEntry(at=at, done=True).to_entry()
        for list_url, page in sorted(self.cut.items()):
            if page is None:
                result[list_url] = None
                continue
            previous = self.entries.get(list_url)
            at = previous.at if previous is not None and not previous.done else now
            result[list_url] = ResumeEntry(at=at, page=page).to_entry()
        return result
