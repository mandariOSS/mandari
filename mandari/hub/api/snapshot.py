# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Snapshot einer Kommune mit Cursor-Übergabe – Einstieg in den Änderungsfeed
(``docs/adr/20260929-aenderungsfeed-format.md``).

``GET …/snapshot`` liefert den Gesamtstand einer Kommune als NDJSON: je Zeile ein JSON-Objekt. Die
erste Zeile nennt den ``snapshot_cursor`` und die Zahl der folgenden Zeilen (``objects``), alle weiteren
sind die Objekte der Kommune, wie sie die externen Listen ausgeben (Body, Gremien, Personen, Sitzungen,
Vorlagen, Orte – mit ihren Einbettungen). Danach liest der Abnehmer ``changes?after=<snapshot_cursor>``.
Beide Ausgaben der offenen Schnittstelle geben den Snapshot mit dieser einen Funktion aus
(``snapshot_response``).

**Übergabe ohne Verlust:** Der ``snapshot_cursor`` wird festgehalten, *bevor* das erste Objekt gelesen
wird. Er ist der Stand des Feeds in diesem Moment. Was sich danach ändert, steht im Feed hinter dem
Cursor – gleichgültig, ob der Snapshot die Änderung schon enthält oder nicht. Ein Objekt kann deshalb
nach dem Snapshot noch einmal als ``upsert`` erscheinen; Abnehmer verarbeiten das idempotent.

**Nur Öffentliches:** Der Snapshot enthält, was die Listen der Ausgabe enthalten, ohne Gelöschtes. Der
Cursor ist der des neuesten *öffentlichen* Ereignisses der Kommune: Er lässt nicht erkennen, ob oder
wann Nichtöffentliches geschah.

**Vollständigkeit:** Ein abgebrochener Abruf ist unabhängig von der Übertragung erkennbar: Es folgen
weniger Zeilen, als ``objects`` nennt. ``Content-Length`` allein genügt dafür nicht – ein Proxy, der
komprimiert, lässt sie weg.

**Betrieb:**

- Der Snapshot wird vollständig in eine temporäre Datei geschrieben, bevor die Übertragung beginnt.
  So liest nur die Anfrage selbst die Datenbank – ihre Verbindung geht am Ende der Anfrage sicher
  zurück (``apps.common.db_connections``) – und ein langsamer Abnehmer hält keine Verbindung fest.
  Bis die Datei fertig ist, fließt kein Byte; bei sehr großen Kommunen muss das Leerlauf-Zeitlimit
  vorgeschalteter Proxys das zulassen.
- Übertragen wird in Blöcken, unter ASGI über einen asynchronen Iterator (sonst läse Django die ganze
  Datei in den Speicher).
- Höchstens ``OPARL_SNAPSHOT_PARALLEL`` Snapshots entstehen gleichzeitig (weitere Anfragen: ``503`` mit
  ``Retry-After``), und je Client-Adresse höchstens einer (sonst ``429`` mit ``Retry-After``). Ein
  Client kann die Plätze also nicht allein belegen – auch nicht mit abgebrochenen Abrufen, deren
  Aufbau noch läuft.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
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
from hub.api.http import client_ip, error_response
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


@dataclass(frozen=True)
class _Slots:
    """Belegte Plätze einer Anfrage: ein gemeinsamer und der ihrer Client-Adresse, mit eigenem Zeichen."""

    token: str
    keys: tuple[str, ...]


def _release(slots: _Slots) -> None:
    """
    Plätze freigeben – nur, solange sie noch diese Anfrage tragen. Ist ein Platz nach ``_SLOT_SECONDS``
    verfallen und inzwischen neu vergeben, bleibt der andere Abruf unberührt.
    """
    for key in slots.keys:
        if cache.get(key) == slots.token:
            cache.delete(key)


def _acquire(client: str) -> _Slots | HttpResponse:
    """Den Platz der Client-Adresse und einen gemeinsamen Platz belegen; sonst die Absage."""
    token = uuid.uuid4().hex
    own = f"oparl_api:snapshot:client:{client}"
    if not cache.add(own, token, timeout=_SLOT_SECONDS):
        return _busy(429, "Für diese Adresse entsteht bereits ein Snapshot. Bitte danach erneut abrufen.")
    for number in range(parallel()):
        key = f"oparl_api:snapshot:{number}"
        if cache.add(key, token, timeout=_SLOT_SECONDS):
            return _Slots(token, (own, key))
    _release(_Slots(token, (own,)))
    return _busy(503, "Es entstehen gerade zu viele Snapshots gleichzeitig. Bitte später erneut abrufen.")


def _busy(status: int, message: str) -> HttpResponse:
    response = error_response(status, message)
    response["Retry-After"] = str(_RETRY_AFTER)
    response["Cache-Control"] = "no-store"
    return response


# =============================================================================
# Übertragung
# =============================================================================


def _blocks(first: bytes, spool: IO[bytes]) -> Iterator[bytes]:
    try:
        yield first
        while block := spool.read(BLOCK_SIZE):
            yield block
    finally:
        spool.close()


async def _async_blocks(first: bytes, spool: IO[bytes]) -> AsyncIterator[bytes]:
    """Blöcke der Datei für ASGI; das Lesen blockiert den Ereignis-Loop nicht."""
    try:
        yield first
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
    Feed (``changes``), Zeitpunkt des Cursors (``created``) und die Zahl der folgenden Zeilen
    (``objects``); danach je Zeile ein Objekt.

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

    slots = _acquire(client_ip(request))
    if isinstance(slots, HttpResponse):
        return slots

    spool: IO[bytes] = tempfile.TemporaryFile()  # noqa: SIM115 – die Antwort schließt die Datei nach der Übertragung
    try:
        body = snapshot.body()
        spool.write(_line(body))
        count = 1
        for data in objects(snapshot):
            spool.write(_line(data))
            count += 1
        size = spool.tell()
        spool.seek(0)
    except BaseException:
        spool.close()
        raise
    finally:
        _release(slots)

    first = _line(
        {
            "snapshot_cursor": cursor,
            "body": body.get("id"),
            "changes": f"{feed.url}?after={cursor}",
            "created": iso(created),
            "objects": count,
        }
    )
    content = _async_blocks(first, spool) if isinstance(request, ASGIRequest) else _blocks(first, spool)
    response = StreamingHttpResponse(content, content_type=CONTENT_TYPE)
    response["Content-Length"] = str(len(first) + size)
    _headers(response, cursor)
    return response
