# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsfeed je Kommune – eine kompatible Erweiterung von OParl 1.1
(``docs/adr/20260929-aenderungsfeed-format.md``).

``GET …/changes?after=<cursor>&limit=<n>`` liefert, was sich seit dem Cursor geändert hat: je Änderung
einen Eintrag ``{cursor, operation, type, id, modified, reason?}`` ohne Inhalte. Die Inhalte holt der
Abnehmer über die Adresse des Objekts (``id``). Beide Ausgaben der offenen Schnittstelle geben den Feed
mit dieser einen Funktion aus (``changes_response``); eine Ausgabe nennt nur ihre Kommune und die
Adressen ihrer Objekte (``Feed``).

**Herkunft:** Der Feed ist eine Sicht auf das Journal der Ereignistechnik (``apps.events``). Er liest
die Ereignisse ``ris.*`` der Kommune in der Reihenfolge ihrer Folgenummer ``seq``; die vergibt der
Sequenzierer erst nach dem Commit, deshalb überspringt ein Leser mit ``seq > cursor`` nichts.

**Nur Öffentliches:**

- Gelesen werden ausschließlich Ereignisse der Sichtbarkeit ``oeffentlich``. Alle anderen erscheinen
  weder als Eintrag noch sonst in der Antwort.
- Der Cursor der Antwort rückt nur mit ausgegebenen Einträgen vor. Geschieht Nichtöffentliches, bleibt
  die Antwort auf dieselbe Anfrage Byte für Byte gleich – auch ihr ``ETag``. Wer den Feed beobachtet,
  erkennt daran weder, dass etwas geschehen ist, noch wann.
- Der Cursor ist verschlüsselt (AES-SIV, Schlüssel aus ``SECRET_KEY`` abgeleitet): Abnehmer sehen keine
  Folgenummern und damit keine Lücken zwischen ihnen.
- Eine Ausgabe nennt nur Adressen von Objekten, die öffentlich sind oder es waren
  (``Feed.addresses``). Ein Ereignis ohne solche Adresse ergibt keinen Eintrag, und der Cursor rückt
  nicht darüber hinaus.

**Objekte:** Ein Eintrag nennt ein Objekt, das die Ausgabe unter einer Adresse ausliefert. Ändert sich
etwas ohne eigene Adresse (eine Abstimmung), nennt der Eintrag das Objekt, das es ausgibt (den
Tagesordnungspunkt, ``CARRIERS``).

**Operationen:** ``upsert`` (neu oder geändert), ``delete`` (entfernt oder nicht mehr öffentlich, mit
``reason`` ``quelle_geloescht``, ``zurueckgenommen`` oder ``nichtoeffentlich``) und ``redact`` (Inhalte
sind aus Kopien zu entfernen, ``reason`` ``datenschutz``). Maßgeblich ist der Vertrag des Ereignisses
(``hub/contracts/schemas``): ``ris.object.depublished`` ist immer ``delete`` bzw. ``redact``.

**Aufbewahrung:** Ein Cursor trägt den Tag, an dem er ausgegeben wurde. Er gilt
``OPARL_CHANGES_RETENTION_DAYS`` Tage (mindestens 30); jede Antwort – auch eine leere – gibt einen
frischen Cursor aus. Ein älterer Cursor ergibt ``410 Gone`` mit einer Fehlerbeschreibung nach RFC 9457
und dem Verweis auf den Snapshot. So lange muss das Journal seine Zeilen mindestens behalten; alles, was
nach der Ausgabe eines gültigen Cursors geschah, ist dann noch vorhanden.

**Schalter:** ``OPARL_CHANGES_ENABLED`` (Standard aus). Solange die Erzeuger der Ereignisse einer
Installation nicht laufen, wäre der Feed leer und würde Abnehmern vortäuschen, es habe sich nichts
geändert.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import struct
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse

from apps.events.models import Event, Operation, Visibility
from hub.api.http import BadRequestError, json_response
from hub.commands.problems import Problem
from hub.ris.canonical import TYPE_SCHEMA, Objekt, iso, schema_type

logger = logging.getLogger(__name__)

#: Ereignisse des kanonischen RIS-Modells; nur sie erscheinen im Feed
TYPE_PREFIX: Final = "ris."
#: Rücknahme eines Objekts: laut Vertrag immer ``delete`` bzw. ``redact``
DEPUBLISHED: Final = "ris.object.depublished"
#: Gründe einer Rücknahme (``delete``) und der Grund für ``redact``
DELETE_REASONS: Final[frozenset[str]] = frozenset({"quelle_geloescht", "zurueckgenommen", "nichtoeffentlich"})
REDACT_REASON: Final = "datenschutz"

#: Kanonischer Objekttyp der Ereignishülle -> Objekttyp in den Adressen der Schnittstelle
KINDS: Final[dict[str, str]] = {name: kind for kind, name in TYPE_SCHEMA.items() if kind != "system"}
#: Objekttypen ohne eigene Adresse: Ihre Änderung erscheint am Objekt, das sie ausgibt –
#: (Objekttyp des tragenden Objekts, Feld der Nutzlast mit dessen Kennung). Eine Abstimmung steht am
#: Tagesordnungspunkt. Fehlt die Kennung des tragenden Objekts, gibt es keinen Eintrag.
CARRIERS: Final[dict[str, tuple[str, str]]] = {"Voting": ("agendaitem", "agenda_item")}

#: Einträge je Seite: Vorgabe und Obergrenze
DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 1000
#: So viele Ereignisse liest eine Anfrage höchstens, wenn Ereignisse ohne Adresse übersprungen werden:
#: das Zehnfache der Seite, mindestens 1000 – und nach dem ersten Schritt in Schritten dieser Größe
_MAX_EXAMINED_FACTOR: Final = 10
_MIN_EXAMINED: Final = 1000
_SKIP_BATCH: Final = 100

#: Zusage der Spezifikation: Cursor gelten mindestens so lange
MIN_RETENTION_DAYS: Final = 30
DEFAULT_RETENTION_DAYS: Final = 90

#: Adressen der Objekte eines Typs: Kennungen -> Adresse (``id``). Fehlt eine Kennung, ist das Objekt in
#: dieser Ausgabe nicht öffentlich (gewesen) und bekommt keinen Eintrag.
Addresses = Callable[[str, Collection[uuid.UUID]], Mapping[uuid.UUID, str]]


@dataclass(frozen=True)
class Feed:
    """
    Der Änderungsfeed einer Kommune in einer Ausgabe.

    - ``body_id``: kanonische Kennung der Kommune, wie sie die Ereignisse im Journal tragen
    - ``url``: Adresse des Feeds, ``snapshot_url``: Adresse des Snapshots (Einstieg und Wiedereinstieg)
    - ``addresses``: Adressen der Objekte dieser Ausgabe
    """

    body_id: uuid.UUID
    url: str
    snapshot_url: str
    addresses: Addresses


# =============================================================================
# Einstellungen
# =============================================================================


def enabled() -> bool:
    """Ist der Änderungsfeed in dieser Installation eingeschaltet (``OPARL_CHANGES_ENABLED``)?"""
    return bool(getattr(settings, "OPARL_CHANGES_ENABLED", False))


def retention_days() -> int:
    """Gültigkeit eines Cursors in Tagen (``OPARL_CHANGES_RETENTION_DAYS``, mindestens 30)."""
    days = int(getattr(settings, "OPARL_CHANGES_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    if days < MIN_RETENTION_DAYS:
        raise ImproperlyConfigured(
            f"OPARL_CHANGES_RETENTION_DAYS muss mindestens {MIN_RETENTION_DAYS} sein (Zusage des Änderungsfeeds)."
        )
    return days


def today() -> date:
    """Heutiger Tag (UTC); Cursor tragen ihn als Ausgabetag."""
    return datetime.now(UTC).date()


# =============================================================================
# Cursor
# =============================================================================


@dataclass(frozen=True)
class Cursor:
    """Stand eines Abnehmers: letzte gesehene Folgenummer und der Tag, an dem der Cursor ausgegeben wurde."""

    seq: int
    day: date


class CursorExpiredError(Exception):
    """Der Cursor ist nicht (mehr) gültig; der Abnehmer steigt über den Snapshot wieder ein."""


_TOKEN = re.compile(r"[A-Za-z0-9_-]{38}")
_LAYOUT: Final = struct.Struct(">QI")  # Folgenummer, Ausgabetag (Ordinalzahl)
_KEY_INFO: Final = b"mandari.hub.api.changes.cursor.v1"


@lru_cache(maxsize=8)
def _cipher(secret: str) -> AESSIV:
    """Deterministische, authentifizierte Verschlüsselung mit einem aus ``secret`` abgeleiteten Schlüssel."""
    key = HKDF(algorithm=hashes.SHA256(), length=64, salt=None, info=_KEY_INFO).derive(secret.encode("utf-8"))
    return AESSIV(key)


def _associated(body_id: uuid.UUID) -> list[bytes]:
    # Ein Cursor gilt nur für die Kommune, für die er ausgegeben wurde
    return [b"changes", body_id.bytes]


def encode_cursor(body_id: uuid.UUID, seq: int, day: date) -> str:
    """
    Opaker Cursor. Gleiche Eingaben ergeben denselben Cursor (stabile Antworten und ``ETag``); ohne den
    Schlüssel der Installation lässt er weder Folgenummer noch Tag erkennen.
    """
    token = _cipher(settings.SECRET_KEY).encrypt(_LAYOUT.pack(seq, day.toordinal()), _associated(body_id))
    return base64.urlsafe_b64encode(token).rstrip(b"=").decode("ascii")


def decode_cursor(body_id: uuid.UUID, token: str) -> Cursor:
    """
    Cursor lesen. ``BadRequestError``, wenn ``token`` keiner ist; ``CursorExpiredError``, wenn er nicht
    von dieser Installation für diese Kommune ausgegeben wurde (etwa nach einem Schlüsselwechsel).
    """
    if not _TOKEN.fullmatch(token):
        raise BadRequestError("Parameter 'after': kein gültiger Cursor. Cursor stammen aus einer Antwort des Feeds.")
    try:
        raw = base64.urlsafe_b64decode(token + "==")
    except (binascii.Error, ValueError):
        raise BadRequestError(
            "Parameter 'after': kein gültiger Cursor. Cursor stammen aus einer Antwort des Feeds."
        ) from None
    for secret in (settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", ())):
        try:
            seq, ordinal = _LAYOUT.unpack(_cipher(secret).decrypt(raw, _associated(body_id)))
            return Cursor(seq=seq, day=date.fromordinal(ordinal))
        except (InvalidTag, struct.error, ValueError, OverflowError):
            continue
    raise CursorExpiredError


# =============================================================================
# Journal lesen
# =============================================================================


def events(body_id: uuid.UUID) -> QuerySet[Event]:
    """Öffentliche, nummerierte Ereignisse ``ris.*`` einer Kommune zu Objekten, die der Feed nennen kann."""
    return Event.objects.filter(
        body_id=body_id,
        visibility=Visibility.OEFFENTLICH,
        seq__isnull=False,
        type__startswith=TYPE_PREFIX,
        aggregate_type__in=[*KINDS, *CARRIERS],
    )


def _subject(event: Event) -> tuple[str, uuid.UUID] | None:
    """
    Objekt, das ein Eintrag nennt: (Objekttyp der Schnittstelle, Kennung). Für Typen ohne eigene Adresse
    das tragende Objekt; ``None``, wenn das Ereignis es nicht nennt.
    """
    kind = KINDS.get(event.aggregate_type)
    if kind is not None:
        return kind, event.aggregate_id
    carrier, field = CARRIERS[event.aggregate_type]
    payload = event.payload if isinstance(event.payload, dict) else {}
    try:
        return carrier, uuid.UUID(str(payload.get(field)))
    except ValueError:
        return None


def head(body_id: uuid.UUID) -> int:
    """Folgenummer des neuesten Ereignisses im Feed der Kommune (0: noch keines)."""
    newest = events(body_id).order_by("-seq").values_list("seq", flat=True).first()
    return newest or 0


def _oldest_in_journal() -> tuple[int, datetime] | None:
    """Älteste nummerierte Zeile des Journals über alle Kommunen: (Folgenummer, erfasst am)."""
    row = Event.objects.filter(seq__isnull=False).order_by("seq").values_list("seq", "recorded_at").first()
    if row is None or row[0] is None:
        return None
    return row[0], row[1]


def _beginning_missing() -> bool:
    """
    Fehlt der Anfang des Journals (aufgeräumt)? Dann kann niemand „von vorn“ lesen, ohne etwas zu
    verpassen – der Einstieg ist der Snapshot.
    """
    oldest = _oldest_in_journal()
    return oldest is not None and oldest[0] > 1


def _missing_since(cursor: Cursor) -> bool:
    """
    Sicherheitsnetz gegen ein Journal, das kürzer aufbewahrt als der Feed zusagt: Fehlen Zeilen zwischen
    dem Cursor und der ältesten vorhandenen Zeile, und ist diese jünger als der Cursor, kann seit seiner
    Ausgabe etwas aufgeräumt worden sein, das der Abnehmer nie gesehen hat.
    """
    oldest = _oldest_in_journal()
    if oldest is None or cursor.seq >= oldest[0] - 1:
        return False
    return oldest[1].astimezone(UTC).date() > cursor.day


def _operation(event: Event) -> tuple[str, str | None]:
    """Operation und Grund eines Eintrags nach dem Vertrag des Ereignisses."""
    if event.aggregate_type in CARRIERS:
        # Das tragende Objekt besteht weiter, was auch mit dem getragenen geschieht: Es hat sich geändert
        return Operation.UPSERT.value, None
    payload = event.payload if isinstance(event.payload, dict) else {}
    reason = payload.get("reason")
    if event.operation == Operation.REDACT or reason == REDACT_REASON:
        return Operation.REDACT.value, REDACT_REASON
    if event.operation == Operation.DELETE or event.type == DEPUBLISHED:
        return Operation.DELETE.value, reason if reason in DELETE_REASONS else None
    return Operation.UPSERT.value, None


def _read(feed: Feed, after: int, limit: int, day: date) -> tuple[list[Objekt], int]:
    """
    Höchstens ``limit`` Einträge nach ``after`` in aufsteigender Reihenfolge und die Folgenummer des
    letzten ausgegebenen Eintrags (``after``, wenn es keinen gibt).

    Ereignisse, deren Objekt in dieser Ausgabe keine Adresse hat, werden übersprungen. Der Stand rückt
    nur mit ausgegebenen Einträgen vor.
    """
    entries: list[Objekt] = []
    position = last = after
    examined = 0
    budget = max(limit * _MAX_EXAMINED_FACTOR, _MIN_EXAMINED)
    size = limit
    while len(entries) < limit and examined < budget:
        batch = list(events(feed.body_id).filter(seq__gt=position).order_by("seq")[: min(size, budget - examined)])
        if not batch:
            break
        examined += len(batch)
        subjects = [_subject(event) for event in batch]
        by_kind: dict[str, set[uuid.UUID]] = {}
        for subject in subjects:
            if subject is not None:
                by_kind.setdefault(subject[0], set()).add(subject[1])
        addresses = {kind: feed.addresses(kind, ids) for kind, ids in by_kind.items()}
        for event, subject in zip(batch, subjects, strict=True):
            if subject is None or event.seq is None:
                continue
            kind, object_id = subject
            address = addresses[kind].get(object_id)
            if address is None:
                continue
            operation, reason = _operation(event)
            entry: Objekt = {
                "cursor": encode_cursor(feed.body_id, event.seq, day),
                "operation": operation,
                "type": schema_type(kind),
                "id": address,
                "modified": iso(event.occurred_at),
            }
            if reason:
                entry["reason"] = reason
            entries.append(entry)
            last = event.seq
            if len(entries) == limit:
                break
        if len(batch) < size:
            break
        position = batch[-1].seq or position
        # Es wurde übersprungen: in größeren Schritten weiterlesen
        size = max(limit - len(entries), _SKIP_BATCH)
    if len(entries) < limit and examined >= budget:
        # Nur Protokoll: Die Antwort verrät übersprungene Ereignisse nicht
        logger.error(
            "Änderungsfeed: %s Ereignisse ohne öffentliche Adresse übersprungen (Kommune %s)",
            examined - len(entries),
            feed.body_id,
        )
    return entries, last


# =============================================================================
# Antwort
# =============================================================================


def _limit(request: HttpRequest) -> int:
    raw = request.GET.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        raise BadRequestError(f"Parameter 'limit': '{raw}' ist keine gültige Anzahl.") from None
    if limit < 1:
        raise BadRequestError("Parameter 'limit': mindestens 1.")
    return min(limit, MAX_LIMIT)


def _link(feed: Feed, cursor: str | None, request: HttpRequest) -> str:
    """Adresse einer Seite: ``after`` und – wenn der Abnehmer es geschickt hat – ``limit``."""
    params = []
    if cursor:
        params.append(f"after={cursor}")
    if "limit" in request.GET:
        params.append(f"limit={_limit(request)}")
    return f"{feed.url}?{'&'.join(params)}" if params else feed.url


def expired_response(request: HttpRequest, feed: Feed) -> HttpResponse:
    """``410 Gone`` nach RFC 9457 mit dem Verweis auf den Snapshot."""
    problem = Problem(
        status=410,
        kind="cursor-abgelaufen",
        detail=(
            "Der Cursor ist nicht mehr gültig: Er ist älter als die Aufbewahrung des Änderungsfeeds oder stammt "
            "nicht aus diesem Feed. Bitte den Snapshot abrufen und mit dessen Cursor fortsetzen."
        ),
        extensions={"snapshot": feed.snapshot_url},
    )
    response = json_response(problem.to_dict(instance=request.path), status=410)
    response["Content-Type"] = "application/problem+json; charset=utf-8"
    response["Cache-Control"] = "no-store"
    return response


def changes_response(request: HttpRequest, feed: Feed) -> HttpResponse:
    """
    Eine Seite des Änderungsfeeds: ``data`` (Einträge in aufsteigender Reihenfolge), ``cursor`` (Stand für
    die nächste Anfrage) und ``links`` (``self``, ``next``, ``snapshot``).

    Eine leere Seite bedeutet: Der Abnehmer ist aktuell. Ihren Cursor übernimmt er trotzdem – er ist
    frisch ausgegeben und gilt wieder die volle Aufbewahrungszeit.
    """
    limit = _limit(request)
    days = retention_days()
    day = today()
    token = request.GET.get("after")
    if token:
        try:
            cursor = decode_cursor(feed.body_id, token)
        except CursorExpiredError:
            return expired_response(request, feed)
        if (day - cursor.day).days > days or _missing_since(cursor):
            return expired_response(request, feed)
        after = cursor.seq
    else:
        # Ohne Cursor von vorn – nur solange der Anfang des Journals noch da ist
        if _beginning_missing():
            return expired_response(request, feed)
        after = 0

    entries, last = _read(feed, after, limit, day)
    position = encode_cursor(feed.body_id, last, day)
    return json_response(
        {
            "data": entries,
            "cursor": position,
            "links": {
                "self": _link(feed, token or None, request),
                "next": _link(feed, position, request),
                "snapshot": feed.snapshot_url,
            },
        }
    )
