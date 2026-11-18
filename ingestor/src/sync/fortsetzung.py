# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fortsetzung eines Vollabgleichs, den das Zeitfenster der Quelle abbricht.

Die Stadt Köln erlaubt Abrufe nur von 23 bis 1 Uhr (``sync_window``). Bei einer Anfrage je Sekunde liest der
Vollabgleich in diesen zwei Stunden Gremien, Personen, Sitzungen und Vorlagen ganz, von Tagesordnungspunkten, Dateien
und Beratungen aber nur die ersten gut tausend Seiten. Weil er nie fertig wurde, blieb er fällig
(``last_successful_full_sync`` leer) und begann jede Nacht wieder bei Seite 1: Er kam nie weiter, und die Quelle stand
dauerhaft auf „seither mit Lücken“.

Jetzt setzt der nächste Vollabgleich fort (``sync_config["full_sync_resume"]``, je Liste):

- **Abgebrochen** (``{"page": …, "ids": […], "at": …}``): Endet das Zeitfenster mitten in einer Liste, merkt sich der
  Lauf die zuletzt ganz gelesene Seite und ihre Kennungen. Der nächste Vollabgleich holt zuerst die Änderungen der
  Liste seit ``at`` nach (die schon gelesenen Seiten können sich seitdem geändert haben) und liest sie dann ab dieser
  Seite bis zum Ende, mit einer Seite Überlappung. Trägt die Seite keine der gemerkten Kennungen mehr, hat sich die
  Liste um mehr als eine Seite verschoben: Dann liest er sie ganz.
- **Gelesen** (``{"done": true, "at": …}``): Listen, die ein Lauf des Durchgangs schon ganz gelesen hat, liest der
  nächste Vollabgleich nur noch mit ``modified_since`` ab ``at`` abzüglich des Sicherheitsabstands. Sie bleiben so
  aktuell, ohne die Zeit des Fensters zu verbrauchen. Scheitert dieser Änderungsabgleich, kommt die Liste ganz.

``at`` ist der Beginn des Laufs, in dem die Liste zuerst gelesen wurde: Alles, was sich danach ändert, holt der
Änderungsabgleich nach. Ein Durchgang beginnt mit dem ersten Abbruch am Fensterende; ohne Abbruch schreibt der
Vollabgleich nichts. Er endet, sobald ein Lauf vollständig ist: Dann ist jede Liste innerhalb des Durchgangs ganz
gelesen und seither mit ihren Änderungen nachgeführt, ``last_successful_full_sync`` rückt vor, und die Einträge werden
gelöscht. Einträge, die älter als ``RESUME_MAX_AGE`` sind, gelten nicht mehr: Die Liste wird dann wieder ganz gelesen,
damit ein Durchgang nicht über Wochen aus alten Teilen zusammengesetzt wird. Scheitert eine Liste aus anderem Grund als
dem Fensterende, fällt ihr Eintrag weg, und der nächste Vollabgleich liest sie ganz. Eine Liste, die es bei der Quelle
nicht gibt, gilt nicht als gelesen. Abgleiche in Zeitscheiben (``full_sync_slices``) haben ihren eigenen Fortschritt
und nutzen die Fortsetzung nicht.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

#: So lange gilt ein Eintrag; danach liest der Vollabgleich die Liste wieder ganz
RESUME_MAX_AGE = timedelta(days=14)
#: Höchstens so viele Kennungen der Fortsetzungsseite werden gespeichert
MAX_PAGE_IDS = 100

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


def _ids(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(value for value in values if isinstance(value, str) and value)[:MAX_PAGE_IDS]


@dataclass(frozen=True)
class ResumeEntry:
    """
    Stand einer Liste im Durchgang: ganz gelesen (``done``) oder abgebrochen nach Seite ``page`` mit deren Kennungen
    ``ids``. ``at``: Beginn des Laufs, in dem der Durchgang die Liste zuerst gelesen hat.
    """

    at: datetime
    page: str | None = None
    done: bool = False
    ids: tuple[str, ...] = ()

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
            ids = raw.get("ids")
            return cls(at=at, page=page, ids=_ids(ids) if isinstance(ids, list) else ())
        return None

    def to_entry(self) -> dict[str, Any]:
        if self.done:
            return {"done": True, "at": _iso(self.at)}
        entry: dict[str, Any] = {"page": self.page, "at": _iso(self.at)}
        if self.ids:
            entry["ids"] = list(self.ids)
        return entry


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
    #: Beginn dieses Laufs (``at`` neuer Einträge)
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Sicherheitsabstand des Änderungsfensters (``modified_since_margin_minutes``)
    margin: timedelta = timedelta(0)
    #: Stand aus früheren Läufen des Durchgangs
    entries: dict[str, ResumeEntry] = field(default_factory=dict)
    #: In diesem Lauf ganz gelesene Listen (ganz oder ab der Fortsetzungsseite)
    finished: set[str] = field(default_factory=set)
    #: In diesem Lauf am Fensterende abgebrochene Listen -> zuletzt ganz gelesene Seite und ihre Kennungen
    cut: dict[str, tuple[str | None, tuple[str, ...]]] = field(default_factory=dict)
    #: In diesem Lauf aus anderem Grund gescheiterte Listen: Eintrag fällt weg, der nächste Lauf liest sie ganz
    failed: set[str] = field(default_factory=set)
    #: Alle Einträge der Quelle in ``sync_config``, auch zu alte (zum Aufräumen)
    stored: set[str] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        source_url: str,
        stored: Mapping[str, Any],
        now: datetime | None = None,
        margin: timedelta = timedelta(0),
    ) -> ResumeRun:
        """``stored``: Einträge aus ``sync_config["full_sync_resume"]`` (``full_sync_resume_of``)."""
        now = now or datetime.now(UTC)
        names = {name for name in stored if isinstance(name, str)}
        return cls(
            source_url=source_url, started=now, margin=margin, entries=resume_entries_of(stored, now), stored=names
        )

    def plan(self, list_url: str) -> tuple[str, ResumeEntry | None]:
        """
        Wie der Vollabgleich die Liste liest: ``FULL`` ganz, ``RESUME`` ab der Fortsetzungsseite des Eintrags,
        ``CHANGES`` nur mit ``modified_since`` (in diesem Durchgang schon ganz gelesen).
        """
        entry = self.entries.get(list_url)
        if entry is None:
            return FULL, None
        return (CHANGES if entry.done else RESUME), entry

    def since(self, list_url: str) -> datetime | None:
        """Änderungsfenster einer Liste im Durchgang: Beginn ihres ersten Laufs abzüglich Sicherheitsabstand."""
        entry = self.entries.get(list_url)
        return entry.at - self.margin if entry is not None else None

    def note_finished(self, list_url: str) -> None:
        self.finished.add(list_url)
        self.cut.pop(list_url, None)
        self.failed.discard(list_url)

    def note_cut(self, list_url: str, last_page: str | None, ids: Iterable[Any] = ()) -> None:
        """
        Liste am Fensterende abgebrochen. ``last_page``: zuletzt ganz gelesene Folgeseite dieses Laufs mit ihren
        Kennungen ``ids``. Ohne sie (keine Folgeseite gelesen) bleibt eine frühere Fortsetzungsseite stehen; die erste
        Seite selbst ist keine Fortsetzung.
        """
        if last_page is None or last_page == list_url:
            previous = self.entries.get(list_url)
            if previous is not None and not previous.done:
                self.cut[list_url] = (previous.page, previous.ids)
            else:
                self.cut[list_url] = (None, ())
            return
        self.cut[list_url] = (last_page, _ids(ids))

    def note_failed(self, list_url: str) -> None:
        """Liste aus anderem Grund als dem Fensterende gescheitert: Der nächste Vollabgleich liest sie ganz."""
        if list_url not in self.finished and list_url not in self.cut:
            self.failed.add(list_url)

    def active(self) -> bool:
        """Läuft ein Durchgang (früherer Abbruch oder Abbruch in diesem Lauf)?"""
        return bool(self.entries) or any(page is not None for page, _ids_ in self.cut.values())

    def changes(self, complete: bool, now: datetime | None = None) -> dict[str, dict[str, Any] | None]:
        """
        Einträge für ``sync_config["full_sync_resume"]`` nach dem Lauf (``None`` löscht). Vollständig: alle weg. Sonst,
        wenn ein Durchgang läuft: ganz gelesene Listen als gelesen, abgebrochene mit ihrer Seite, gescheiterte und zu
        alte Einträge fallen weg. ``at`` bleibt beim Beginn des ersten Laufs der Liste im Durchgang.
        """
        if complete:
            return dict.fromkeys(sorted(self.stored))
        result: dict[str, dict[str, Any] | None] = {
            name: None for name in sorted(self.stored) if name not in self.entries
        }
        if not self.active():
            return result
        for list_url in sorted(self.failed):
            if list_url in self.entries:
                result[list_url] = None
        for list_url in sorted(self.finished):
            previous = self.entries.get(list_url)
            at = previous.at if previous is not None else self.started
            result[list_url] = ResumeEntry(at=at, done=True).to_entry()
        for list_url, (page, ids) in sorted(self.cut.items()):
            if page is None:
                result[list_url] = None
                continue
            previous = self.entries.get(list_url)
            at = previous.at if previous is not None else self.started
            result[list_url] = ResumeEntry(at=at, page=page, ids=ids).to_entry()
        return result
