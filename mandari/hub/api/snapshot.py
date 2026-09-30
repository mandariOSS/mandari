# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Snapshot einer Kommune mit Cursor-Übergabe – Einstieg in den Änderungsfeed
(``docs/adr/20260929-aenderungsfeed-format.md``).

``GET …/snapshot`` liefert den Gesamtstand einer Kommune als NDJSON: je Zeile ein JSON-Objekt. Die
erste Zeile nennt den ``snapshot_cursor``, alle weiteren sind die Objekte der Kommune, wie sie die
externen Listen ausgeben (Body, Gremien, Personen, Sitzungen, Vorlagen, Orte – mit ihren Einbettungen).
Danach liest der Abnehmer ``changes?after=<snapshot_cursor>``. Beide Ausgaben der offenen Schnittstelle
geben den Snapshot mit dieser einen Funktion aus (``snapshot_response``).

**Übergabe ohne Verlust:** Der ``snapshot_cursor`` wird festgehalten, *bevor* das erste Objekt gelesen
wird. Er ist der Stand des Feeds in diesem Moment. Was sich danach ändert, steht im Feed hinter dem
Cursor – gleichgültig, ob der Snapshot die Änderung schon enthält oder nicht. Ein Objekt kann deshalb
nach dem Snapshot noch einmal als ``upsert`` erscheinen; Abnehmer verarbeiten das idempotent.

**Nur Öffentliches:** Der Snapshot enthält, was die Listen der Ausgabe enthalten, ohne Gelöschtes. Der
Cursor ist der des neuesten *öffentlichen* Ereignisses der Kommune: Er lässt nicht erkennen, ob oder
wann Nichtöffentliches geschah.

**Betrieb:**

- Der Snapshot wird vollständig in eine temporäre Datei geschrieben, bevor die Übertragung beginnt.
  So liest nur die Anfrage selbst die Datenbank – ihre Verbindung geht am Ende der Anfrage sicher
  zurück (``apps.common.db_connections``) – und ein langsamer Abnehmer hält keine Verbindung fest.
  Die Antwort trägt ``Content-Length``: Ein abgebrochener Abruf fällt auf.
- Übertragen wird in Blöcken, unter ASGI über einen asynchronen Iterator (sonst läse Django die ganze
  Datei in den Speicher).
- Höchstens ``OPARL_SNAPSHOT_PARALLEL`` Snapshots entstehen gleichzeitig; weitere Anfragen bekommen
  ``503`` mit ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any, Final

from django.conf import settings
from django.core.cache import cache
from django.core.handlers.asgi import ASGIRequest
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase

from hub.api import changes
from hub.api.http import error_response
from hub.ris.canonical import Objekt, iso

#: Objekte je Abfrage beim Lesen des Bestands
PAGE_SIZE: Final = 200
#: Blockgröße der Übertragung
BLOCK_SIZE: Final = 2**16
CONTENT_TYPE: Final = "application/x-ndjson; charset=utf-8"
#: Kopfzeile der Antwort mit dem Cursor (steht auch in der ersten Zeile des Inhalts)
CURSOR_HEADER: Final = "Snapshot-Cursor"

#: So lange gilt ein belegter Platz höchstens, falls ein Prozess ihn nicht mehr freigibt (Sekunden)
_SLOT_SECONDS: Final = 900
_RETRY_AFTER: Final = 30


@dataclass(frozen=True)
class Section:
    """
    Eine Objektart des Snapshots.

    - ``queryset``: die sichtbaren Objekte (ohne Gelöschtes), mit vorgeladenen Beziehungen
    - ``render``: eine Seite auf kanonische Objekte abbilden (ein Aufruf je Seite, damit Verweise
      gesammelt aufgelöst werden können)
    """

    queryset: QuerySet[Any]
    render: Callable[[list[Any]], list[Objekt]]


@dataclass(frozen=True)
class Snapshot:
    """Der Snapshot einer Kommune in einer Ausgabe: ihr Feed, ihr Body und ihre Objekte."""

    feed: changes.Feed
    body: Callable[[], Objekt]
    sections: Sequence[Section]


def parallel() -> int:
    """Snapshots, die gleichzeitig entstehen dürfen (``OPARL_SNAPSHOT_PARALLEL``)."""
    return max(1, int(getattr(settings, "OPARL_SNAPSHOT_PARALLEL", 2)))


# =============================================================================
# Bestand lesen
# =============================================================================


def _pages(queryset: QuerySet[Any]) -> Iterator[list[Any]]:
    """
    Alle Objekte in Seiten, fortlaufend nach Kennung statt nach Position: Ändert sich der Bestand
    während des Lesens, rutscht kein Objekt zwischen zwei Seiten durch.
    """
    ordered = queryset.order_by("pk")
    last: Any = None
    while True:
        page = list((ordered if last is None else ordered.filter(pk__gt=last))[:PAGE_SIZE])
        if not page:
            return
        yield page
        if len(page) < PAGE_SIZE:
            return
        last = page[-1].pk


def objects(snapshot: Snapshot) -> Iterator[Objekt]:
    """Alle Objekte der Kommune nach dem Body, wie die externen Listen sie ausgeben."""
    for section in snapshot.sections:
        for page in _pages(section.queryset):
            yield from section.render(page)


def _line(data: Objekt) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


# =============================================================================
# Begrenzung gleichzeitiger Snapshots
# =============================================================================


def _acquire() -> str | None:
    """Einen der Plätze belegen; ``None``, wenn alle belegt sind."""
    for number in range(parallel()):
        key = f"oparl_api:snapshot:{number}"
        if cache.add(key, 1, timeout=_SLOT_SECONDS):
            return key
    return None


# =============================================================================
# Übertragung
# =============================================================================


def _blocks(spool: IO[bytes]) -> Iterator[bytes]:
    try:
        while block := spool.read(BLOCK_SIZE):
            yield block
    finally:
        spool.close()


async def _async_blocks(spool: IO[bytes]) -> AsyncIterator[bytes]:
    """Blöcke der Datei für ASGI; das Lesen blockiert den Ereignis-Loop nicht."""
    try:
        while block := await asyncio.to_thread(spool.read, BLOCK_SIZE):
            yield block
    finally:
        spool.close()


def _headers(response: HttpResponseBase, cursor: str) -> None:
    response[CURSOR_HEADER] = cursor
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Expose-Headers"] = f"{CURSOR_HEADER}, Content-Length"
    # Ein Gesamtabzug gehört in keinen Zwischenspeicher
    response["Cache-Control"] = "no-store"


def snapshot_response(request: HttpRequest, snapshot: Snapshot) -> HttpResponseBase:
    """
    Snapshot als NDJSON. Erste Zeile: ``snapshot_cursor``, Adresse des Body, fertige Adresse für den
    Feed (``changes``) und Zeitpunkt; danach je Zeile ein Objekt.

    ``HEAD`` liefert nur die Kopfzeilen mit dem Cursor, ohne den Bestand zu lesen.
    """
    feed = snapshot.feed
    # Vor dem Lesen: Stand des Feeds festhalten. Alles, was sich ab jetzt ändert, steht dahinter.
    cursor = changes.encode_cursor(feed.body_id, changes.head(feed.body_id), changes.today())
    created = datetime.now(UTC)

    if request.method == "HEAD":
        head = HttpResponse(content_type=CONTENT_TYPE)
        _headers(head, cursor)
        return head

    slot = _acquire()
    if slot is None:
        busy = error_response(503, "Es entstehen gerade zu viele Snapshots gleichzeitig. Bitte später erneut abrufen.")
        busy["Retry-After"] = str(_RETRY_AFTER)
        busy["Cache-Control"] = "no-store"
        return busy

    spool: IO[bytes] = tempfile.TemporaryFile()  # noqa: SIM115 – die Antwort schließt die Datei nach der Übertragung
    try:
        body = snapshot.body()
        spool.write(
            _line(
                {
                    "snapshot_cursor": cursor,
                    "body": body.get("id"),
                    "changes": f"{feed.url}?after={cursor}",
                    "created": iso(created),
                }
            )
        )
        spool.write(_line(body))
        for data in objects(snapshot):
            spool.write(_line(data))
        size = spool.tell()
        spool.seek(0)
    except BaseException:
        spool.close()
        raise
    finally:
        cache.delete(slot)

    content = _async_blocks(spool) if isinstance(request, ASGIRequest) else _blocks(spool)
    response = StreamingHttpResponse(content, content_type=CONTENT_TYPE)
    response["Content-Length"] = str(size)
    _headers(response, cursor)
    return response
