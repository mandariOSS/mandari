# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vermerk „filtert nicht“ je Liste und je Quelle (Issue #1041).

**Je Liste** (``sync_config["unfiltered_lists"]``: Adresse der Liste -> ``reason``, ``checked_at``): Lehnt eine Liste
den Filter ``modified_since`` ab (HTTP 400, 401 oder 403 auf die gefilterte erste Seite) oder antwortet sie nur auf
die gefilterte Anfrage mit HTTP 404 bzw. einem OParl-Fehlerobjekt, liest der Client diese eine Liste ohne Filter. Alle
übrigen Listen der Quelle bleiben gefiltert, auch mit ``carry_modified_since``. Der inkrementelle Lauf bricht eine
ungefilterte Liste wie jede andere nach Seiten ohne Änderung ab. Liegt die letzte Prüfung ``UNFILTERED_LIST_RECHECK``
(einen Tag) zurück, fragt der nächste Lauf die Liste wieder mit Filter an: Filtert der Server sie jetzt, endet der
Vermerk; lehnt er weiter ab, gilt der Vermerk ab dann wieder einen Tag.

**Je Server** (``sync_config["unfiltered_hosts"]``: Host -> ``reason``, ``checked_at``) nur, wenn die Quelle den Filter
nachweislich ganz ignoriert: Der Server lässt ihn aus den Links der Liste weg (OParl 1.0, more! rubin auf gremien.info,
``modified_since_dropped``), oder die Prüfung nach einem Vollabgleich findet ihn wirkungslos. Dann laufen alle Listen
dieses Servers in dieser Quelle ohne Filter; ``carry_modified_since`` hebt den Vermerk auf. Neu geprüft wird er nach
jedem Vollabgleich (``SyncOrchestrator._recheck_modified_since``).

Beide Vermerke gehören der Quelle, bei der sie entstanden. Der Orchestrator lädt sie zu Beginn eines Laufs aus ihrer
``sync_config``, gibt sie dem Client der Quelle mit und speichert am Ende nur, was sich geändert hat.

Bis Issue #1041 galt jeder Befund für den ganzen Server und prozessweit für alle Quellen
(``sync_config["modified_since_unsupported_hosts"]``, Issue #22): Lehnte bei der Stadt Hamm eine einzige Liste den
Filter ab, liefen alle Listen der Stadt ohne Filter (HTTP 504 bei Vorlagen und Sitzungen), und über den gemeinsamen
Zwischenspeicher landete der Server der Bezirksregierung Köln in der ``sync_config`` anderer Quellen. Diese Einträge
übernimmt ``FilterNotes.from_sync_config`` einmal und löscht beim Speichern den alten Schlüssel (idempotent: ohne
alten Schlüssel gibt es nichts zu übernehmen). Ein Server bleibt nur vermerkt, wenn die Quelle den Filter nachweislich
ganz ignoriert: Die letzte Prüfung dieser Quelle (``modified_since_check``) fand für ihn ``ignored``, oder die Quelle
spricht OParl 1.0 und es ist ihr eigener Server. Alle anderen Einträge fallen weg: Server anderer Quellen gehören dort
hin, und eine Liste, die den Filter ablehnt, vermerkt der nächste Lauf mit einer abgelehnten Anfrage je Liste neu.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

#: Schlüssel in ``sync_config``: Listen ohne Filter (Adresse -> ``reason``, ``checked_at``)
UNFILTERED_LISTS_KEY = "unfiltered_lists"
#: Schlüssel in ``sync_config``: Server, deren Filter die Quelle ganz ignoriert (Host -> ``reason``, ``checked_at``)
UNFILTERED_HOSTS_KEY = "unfiltered_hosts"
#: Bisheriger Schlüssel (bis Issue #1041): Server ohne Filter, Liste von Hosts, über alle Quellen vereinigt
LEGACY_HOSTS_KEY = "modified_since_unsupported_hosts"
#: Ergebnis der letzten Prüfung des Filters nach einem Vollabgleich (``checked_at``, ``host``, ``result``)
MODIFIED_SINCE_CHECK_KEY = "modified_since_check"
#: Ergebnis der Prüfung „Quelle nimmt den Filter an, liefert aber ungefiltert“ (``MODIFIED_SINCE_IGNORED`` im Client)
_CHECK_IGNORED = "ignored"
#: Nach dieser Zeit fragt der inkrementelle Lauf eine vermerkte Liste wieder mit Filter an
UNFILTERED_LIST_RECHECK = timedelta(days=1)


def _stamp(value: Any) -> datetime | None:
    """Zeitpunkt aus ISO 8601 (ohne Zone als UTC), sonst ``None``."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


@dataclass
class FilterNote:
    """Ein Vermerk: Grund und Zeitpunkt der letzten Prüfung (``None``: ungültig, sofort neu zu prüfen)."""

    reason: str
    checked_at: datetime | None

    @classmethod
    def parse(cls, entry: Any) -> FilterNote:
        """Eintrag aus ``sync_config``; Ungültiges gilt als vermerkt und sofort neu zu prüfen."""
        if isinstance(entry, Mapping):
            reason = entry.get("reason")
            return cls(reason if isinstance(reason, str) else "", _stamp(entry.get("checked_at")))
        return cls("", _stamp(entry))

    def to_entry(self) -> dict[str, str]:
        entry = {"reason": self.reason}
        if self.checked_at is not None:
            entry["checked_at"] = self.checked_at.astimezone(UTC).isoformat(timespec="seconds")
        return entry

    def due(self, now: datetime | None = None) -> bool:
        """Ist die nächste Prüfung fällig (letzte vor mindestens einem Tag, ungültig oder in der Zukunft)?"""
        if self.checked_at is None:
            return True
        age = (now or datetime.now(UTC)) - self.checked_at
        return not timedelta(0) <= age < UNFILTERED_LIST_RECHECK


def _entries(sync_config: Mapping[str, Any], key: str) -> dict[str, FilterNote]:
    stored = sync_config.get(key)
    if not isinstance(stored, Mapping):
        return {}
    return {name: FilterNote.parse(entry) for name, entry in stored.items() if isinstance(name, str) and name}


def _legacy_hosts(
    sync_config: Mapping[str, Any], source_url: str, oparl_version: str | None
) -> dict[str, FilterNote] | None:
    """
    Einträge aus ``modified_since_unsupported_hosts``, die als Vermerk je Server bleiben (siehe Moduldokumentation);
    ``None``, wenn es den alten Schlüssel nicht gibt.
    """
    if LEGACY_HOSTS_KEY not in sync_config:
        return None
    stored = sync_config.get(LEGACY_HOSTS_KEY)
    hosts = {host for host in stored if isinstance(host, str) and host} if isinstance(stored, list) else set()
    check = sync_config.get(MODIFIED_SINCE_CHECK_KEY)
    check = check if isinstance(check, Mapping) else {}
    own_host = urlparse(source_url).netloc
    kept: dict[str, FilterNote] = {}
    for host in sorted(hosts):
        if check.get("host") == host and check.get("result") == _CHECK_IGNORED:
            checked = _stamp(check.get("checked_at")) or datetime.now(UTC)
            kept[host] = FilterNote("Prüfung nach Vollabgleich: Filter ignoriert (übernommen)", checked)
        elif oparl_version == "1.0" and host == own_host:
            kept[host] = FilterNote("OParl 1.0, Filter ignoriert (übernommen)", datetime.now(UTC))
    return kept


@dataclass
class FilterNotes:
    """
    Vermerke „filtert nicht“ einer Quelle (siehe Moduldokumentation) mit den Änderungen seit dem Laden.

    Ohne Quelle (Werkzeuge, Tests) beginnt der Client mit leeren Vermerken, die nur so lange gelten wie er.
    """

    #: Listen ohne Filter: Adresse der Liste, wie der Orchestrator sie an ``fetch_list`` gibt
    lists: dict[str, FilterNote] = field(default_factory=dict)
    #: Server, deren Filter die Quelle ganz ignoriert
    hosts: dict[str, FilterNote] = field(default_factory=dict)
    #: Die ``sync_config`` trägt noch den alten Schlüssel ``modified_since_unsupported_hosts``: beim Speichern löschen
    legacy: bool = False
    #: Geänderte Einträge seit dem Laden bzw. Speichern (``None``: gelöscht)
    changed_lists: dict[str, dict[str, str] | None] = field(default_factory=dict)
    changed_hosts: dict[str, dict[str, str] | None] = field(default_factory=dict)

    @classmethod
    def from_sync_config(cls, sync_config: Any, source_url: str, oparl_version: str | None = None) -> FilterNotes:
        """Vermerke der Quelle aus ihrer ``sync_config``; alte Einträge je Server werden dabei übernommen."""
        if not isinstance(sync_config, Mapping):
            return cls()
        notes = cls(
            lists=_entries(sync_config, UNFILTERED_LISTS_KEY), hosts=_entries(sync_config, UNFILTERED_HOSTS_KEY)
        )
        legacy = _legacy_hosts(sync_config, source_url, oparl_version)
        if legacy is not None:
            notes.legacy = True
            for host, note in legacy.items():
                if host not in notes.hosts:
                    notes.hosts[host] = note
                    notes.changed_hosts[host] = note.to_entry()
        return notes

    @property
    def dirty(self) -> bool:
        """Gibt es etwas zu speichern?"""
        return self.legacy or bool(self.changed_lists) or bool(self.changed_hosts)

    def saved(self) -> None:
        """Nach dem Speichern: nichts mehr offen."""
        self.legacy = False
        self.changed_lists.clear()
        self.changed_hosts.clear()

    def host_ignored(self, host: str, *, carry: bool = False) -> bool:
        """Ignoriert die Quelle den Filter auf diesem Server ganz (und hebt ``carry_modified_since`` das nicht auf)?"""
        return not carry and host in self.hosts

    def unfiltered(self, list_url: str, host: str, *, carry: bool = False) -> bool:
        """Ist die Liste als „filtert nicht“ vermerkt, gleich ob eine Prüfung fällig ist?"""
        return list_url in self.lists or self.host_ignored(host, carry=carry)

    def skip_filter(self, list_url: str, host: str, *, carry: bool = False, now: datetime | None = None) -> bool:
        """Liste jetzt ohne Filter anfragen? Nein, sobald die Prüfung eines Vermerks je Liste fällig ist."""
        if self.host_ignored(host, carry=carry):
            return True
        note = self.lists.get(list_url)
        return note is not None and not note.due(now)

    def note_list(self, list_url: str, reason: str, now: datetime | None = None) -> None:
        """Liste als „filtert nicht“ vermerken bzw. den Vermerk mit dieser Prüfung erneuern."""
        note = FilterNote(reason, now or datetime.now(UTC))
        self.lists[list_url] = note
        self.changed_lists[list_url] = note.to_entry()

    def clear_list(self, list_url: str) -> bool:
        """Liste filtert wieder: Vermerk löschen. ``True``, wenn es einen gab."""
        if self.lists.pop(list_url, None) is None:
            return False
        self.changed_lists[list_url] = None
        return True

    def note_host(self, host: str, reason: str, now: datetime | None = None) -> None:
        """Server vermerken: Die Quelle ignoriert dort den Filter ganz."""
        note = FilterNote(reason, now or datetime.now(UTC))
        self.hosts[host] = note
        self.changed_hosts[host] = note.to_entry()

    def clear_host(self, host: str) -> bool:
        """Server filtert wieder: Vermerk löschen. ``True``, wenn es einen gab."""
        if self.hosts.pop(host, None) is None:
            return False
        self.changed_hosts[host] = None
        return True
