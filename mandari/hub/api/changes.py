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
  nicht darüber hinaus – mit einer Ausnahme: Findet eine Anfrage unter ``max(limit × 10, 1000)``
  gelesenen Ereignissen nicht genug Einträge für ihre Seite, rückt der Cursor bis zum zuletzt
  gelesenen vor. Sonst bliebe der Feed hinter einem solchen Block für immer stehen. Die Antwort
  verrät dann nur, dass viele als öffentlich gemeldete Ereignisse ohne Adresse geschehen sind.

**Objekte:** Ein Eintrag nennt ein Objekt, das die Ausgabe unter einer Adresse ausliefert. Ändert sich
etwas ohne eigene Adresse (eine Abstimmung), nennt der Eintrag das Objekt, das es ausgibt (den
Tagesordnungspunkt, ``CARRIERS``).

**Operationen:** ``upsert`` (neu oder geändert), ``delete`` (entfernt oder nicht mehr öffentlich, mit
``reason`` ``quelle_geloescht``, ``zurueckgenommen`` oder ``nichtoeffentlich``) und ``redact`` (Inhalte
sind aus Kopien zu entfernen, ``reason`` ``datenschutz``). Maßgeblich ist der Vertrag des Ereignisses
(``hub/contracts/schemas``): ``ris.object.depublished`` ist immer ``delete`` bzw. ``redact``. Ein
``delete`` trägt immer einen Grund; nennt das Ereignis keinen gültigen, gilt ``quelle_geloescht``.

**Aufbewahrung:** Ein Cursor trägt den Tag, an dem er ausgegeben wurde. Er gilt
``OPARL_CHANGES_RETENTION_DAYS`` Tage (mindestens 30); jede Antwort – auch eine leere – gibt einen
frischen Cursor aus. Ein älterer Cursor ergibt ``410 Gone`` mit einer Fehlerbeschreibung nach RFC 9457
und dem Verweis auf den Snapshot. So lange muss das Journal seine Zeilen mindestens behalten; alles, was
nach der Ausgabe eines gültigen Cursors geschah, ist dann noch vorhanden. Ob Zeilen gelöscht wurden,
hält das Aufräumen ausdrücklich fest (``apps.events.pruning``); aus Lücken in den Folgenummern lässt es
sich nicht schließen, denn der Sequenzierer darf Nummern verwerfen.

**Wiederherstellung:** Wird die Datenbank aus einer Sicherung zurückgespielt, fehlen Ereignisse, die
Abnehmer schon gesehen haben. Ein Cursor, dessen Ereignis nicht mehr im Journal steht (und nicht
aufgeräumt wurde), ergibt ``410`` mit dem Verweis auf den Snapshot; die Folgenummern dieser Ereignisse
werden nie wieder vergeben (``apps.events.wiederherstellung``).

**Abschnitte des Bestands:** Ändert sich der Bestand einer Kommune am Journal vorbei – das Bürgerportal
nimmt sie dauerhaft zurück und stellt sie später wieder her –, kann der Feed das nicht nachzeichnen.
Die Ausgabe beginnt dann einen neuen Abschnitt (``Feed.epoch``); er geht in die Verschlüsselung des
Cursors ein, ein Cursor aus einem früheren Abschnitt ergibt ``410`` mit dem Verweis auf den Snapshot.
Ob und wann eine Kommune Feed und Snapshot anbietet, legt die Ausgabe fest (beim Aggregator: nur
veröffentlichte und gelistete Kommunen, ``hub.api.aggregator``).

**Aktualität (Issue #556):** Für eine aus einem Fremd-RIS geerntete Kommune nennt die Antwort unter
``freshness`` den letzten vollständig erfolgreichen Abgleich ihrer Quelle (``synced_at``) und Vollabgleich
(``full_synced_at``); ``null`` heißt: noch keiner. Eine leere Seite heißt „nichts Neues seit diesem Stand
der Quelle“, nicht „nichts Neues in der Quelle“. Session-Mandanten führen ihren Bestand selbst und nennen
das Feld nicht.

**Zwei Formen (Issue #1216):** ``kommparl_changes_response`` gibt den Feed nach dem Profil Änderungsfeed von
kommParl aus (``kommparl:changes`` am Body; Schema ``profiles/changes/ChangeList.json``): Gründe mit den Werten des
Profils (``deleted``, ``withdrawn``, ``nonPublic``, ``privacy``), ``links.next`` nur, solange weitere Einträge
vorliegen, ``modified`` auf die Sekunde, die Aktualität als ``mandari:freshness`` und ein abgelaufener Cursor als
Fehlerobjekt nach OParl 1.1 mit ``kommparl:snapshot``. Ohne ``after`` beginnt sie beim ältesten aufbewahrten
Eintrag, auch nach dem Aufräumen (Abschnitt „Abruf“). ``changes_response`` gibt die ältere Form aus
(``mandari:changes``), unverändert bis zum Wegfall (``MANDARI_FEED_DEPRECATION``); ihre Antworten tragen
``Deprecation``, ``Sunset`` und den Nachfolger im Header ``Link`` (``deprecated``). Cursor gelten in beiden Formen.

**Einbettende Objekte (Issue #1229, nur nach dem Profil):** Ändert sich ein Objekt, das ein anderes intern ausgibt
(OParl 1.1, Kapitel „Schema“), nennt der Feed nach dem Profil auch das einbettende Objekt (Abschnitt „Vollständigkeit
und Reihenfolge“): die Sitzung zu einem Tagesordnungspunkt, die Vorlage zu einer Beratung, Vorlage, Sitzung und
Tagesordnungspunkt zu einer Datei, die Person zu einer Mitgliedschaft, die Kommune zu einer Wahlperiode, die
Sitzungen zu einem Ort – auch über zwei Stufen (Datei im Tagesordnungspunkt in der Sitzung). Welche Einbettungen eine
Ausgabe hat, nennt sie selbst (``Feed.embeds``). Die einbettenden Objekte kommen aus der Nutzlast des Ereignisses
(``PAYLOAD_PARENTS``) und aus der Ausgabe (``Feed.related``), zusammengeführt: Die Nutzlast nennt auch frühere und
gelöschte Bezüge, die Ausgabe die übrigen der Zeile (Rücknahmen älterer Erzeuger, Orte, eine Datei an Vorlage und
Sitzung, deren Ereignis nur eines nennt). Jedes bekommt einen Eintrag ``upsert``, solange die Ausgabe es ausliefert
(``Feed.current``): Wird es mit dem eingebetteten Objekt gelöscht, folgt kein ``upsert`` hinter seinem ``delete``. Ein
Objekt, das zu einem anderen gehört und dessen Kennung trägt (der Sitzungsort einer Sitzung), ändert sich mit ihm und
bekommt einen Eintrag mit derselben Operation; ebenso die Datei, die ein Ereignis als neu nennt (die öffentliche
Fassung der Niederschrift, ``PAYLOAD_COMPANIONS``).

- **Reihenfolge:** Die Einträge eines Ereignisses stehen zusammen, zuerst das geänderte Objekt, dann mitgeänderte,
  dann einbettende, jeweils nach Typ und Kennung. Jeder Eintrag hat seine eigene Stelle: Endet eine Seite innerhalb
  eines Ereignisses, nennt der Cursor zusätzlich das letzte ausgegebene Objekt (``_PART``), und die nächste Seite
  setzt dahinter fort. Der Cursor des letzten Eintrags eines Ereignisses ist derselbe wie bisher.
- **Keine Doppelungen in einer Seite:** Folgt in einer Seite ein gleicher Eintrag (Objekt, Operation, Grund) zum
  selben Objekt, ohne dass dazwischen ein anderer zu ihm steht, entfällt der frühere. Der Abnehmer ruft das Objekt so
  einmal ab, an der spätesten Stelle. Der Cursor der Seite bleibt der ihres letzten Eintrags.
- **Last:** Je gelesenem Abschnitt des Journals eine Abfrage je Typ und Stufe, nicht je Eintrag.

Die ältere Form bleibt, wie sie ist: je Ereignis höchstens ein Eintrag.

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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
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
from django.http.response import HttpResponseBase

from apps.common.api.deprecation import Deprecation
from apps.common.api.problems import Problem
from apps.events import pruning
from apps.events.models import Event, Operation, Visibility
from hub.api import kommparl
from hub.api.http import BadRequestError, json_response, problem_response
from hub.ris.canonical import TYPE_SCHEMA, Objekt, iso, schema_type

logger = logging.getLogger(__name__)

#: Ereignisse des kanonischen RIS-Modells; nur sie erscheinen im Feed
TYPE_PREFIX: Final = "ris."
#: Rücknahme eines Objekts: laut Vertrag immer ``delete`` bzw. ``redact``
DEPUBLISHED: Final = "ris.object.depublished"
#: Gründe einer Rücknahme (``delete``) und der Grund für ``redact``
DELETE_REASONS: Final[frozenset[str]] = frozenset({"quelle_geloescht", "zurueckgenommen", "nichtoeffentlich"})
REDACT_REASON: Final = "datenschutz"
#: Grund eines ``delete``, wenn das Ereignis keinen gültigen nennt (Entfernen ohne Rücknahme)
DEFAULT_DELETE_REASON: Final = "quelle_geloescht"
#: Gründe im Profil Änderungsfeed von kommParl (Abschnitt „Operationen“)
KOMMPARL_REASONS: Final[dict[str, str]] = {
    "quelle_geloescht": "deleted",
    "zurueckgenommen": "withdrawn",
    "nichtoeffentlich": "nonPublic",
    REDACT_REASON: "privacy",
}

#: Abkündigung der älteren Form von Feed und Snapshot (``mandari:changes``, ``mandari:snapshot``): angekündigt mit
#: Issue #1216, Wegfall frühestens nach der Frist für die OParl-API (12 Monate, ``docs/RELEASE_POLITIK.md``)
MANDARI_FEED_DEPRECATION: Final = Deprecation(
    deprecated_at=datetime(2026, 11, 5, tzinfo=UTC),
    sunset=datetime(2027, 12, 1, tzinfo=UTC),
)

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
#: das Zehnfache der Seite, mindestens 1000 – und nach dem ersten Schritt in Schritten dieser Größe.
#: Ist das erschöpft, rückt der Cursor bis zum zuletzt gelesenen Ereignis vor (``_read``).
_MAX_EXAMINED_FACTOR: Final = 10
_MIN_EXAMINED: Final = 1000
_SKIP_BATCH: Final = 100

#: Zusage der Spezifikation: Cursor gelten mindestens so lange
MIN_RETENTION_DAYS: Final = 30
DEFAULT_RETENTION_DAYS: Final = 90

#: Adressen der Objekte eines Typs: Kennungen -> Adresse (``id``). Fehlt eine Kennung, ist das Objekt in
#: dieser Ausgabe nicht öffentlich (gewesen) und bekommt keinen Eintrag.
Addresses = Callable[[str, Collection[uuid.UUID]], Mapping[uuid.UUID, str]]

#: Einbettungen nach OParl 1.1 (Kapitel „Schema“, „Die Objekte“): eingebetteter Typ -> Typen, die ihn intern ausgeben.
#: Welche davon eine Ausgabe tatsächlich hat, nennt sie in ``Feed.embeds``.
OPARL_EMBEDDINGS: Final[dict[str, frozenset[str]]] = {
    "legislativeterm": frozenset({"body"}),
    "membership": frozenset({"person"}),
    "agendaitem": frozenset({"meeting"}),
    "consultation": frozenset({"paper"}),
    "file": frozenset({"meeting", "agendaitem", "paper"}),
    "location": frozenset({"body", "organization", "meeting", "paper"}),
}

#: Felder der Nutzlast, die ein einbettendes Objekt nennen: kanonischer Typ des Ereignisses -> (Feld, Objekttyp).
#: ``ris.object.depublished`` nennt dieselben Felder, wenn der Erzeuger sie kennt (Issue #1229). Eine Abstimmung
#: steht am Tagesordnungspunkt (``CARRIERS``); ihre Sitzung bettet ihn ein.
PAYLOAD_PARENTS: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "AgendaItem": (("meeting", "meeting"), ("previous_meeting", "meeting")),
    "Voting": (("meeting", "meeting"),),
    "Consultation": (("paper", "paper"),),
    "File": (
        ("paper", "paper"),
        ("meeting", "meeting"),
        ("agenda_item", "agendaitem"),
        ("previous_paper", "paper"),
        ("previous_meeting", "meeting"),
        ("previous_agenda_item", "agendaitem"),
    ),
    "Membership": (("person", "person"),),
}

#: Objekte, die ein Ereignis mit seinem eigenen Objekt neu oder geändert meldet: Ereignistyp -> (Feld, Objekttyp).
#: Die öffentliche Fassung einer Niederschrift ist eine neue Datei, die die Sitzung als ``resultsProtocol`` ausgibt.
PAYLOAD_COMPANIONS: Final[dict[str, tuple[tuple[str, str], ...]]] = {"ris.protocol.published": (("file", "file"),)}
#: Objekttypen, zu denen eine Ausgabe mitgeänderte Objekte kennen kann (der Sitzungsort trägt die Kennung der Sitzung)
SHARED_KINDS: Final = frozenset({"meeting"})
#: So viele Stufen von Einbettungen folgt der Feed höchstens (Datei -> Tagesordnungspunkt -> Sitzung braucht zwei)
_MAX_DEPTH: Final = 3


@dataclass(frozen=True)
class Link:
    """
    Ein weiteres Objekt, das eine Änderung betrifft (Profil Änderungsfeed, „Vollständigkeit und Reihenfolge“).

    - ``shared=False``: Es gibt das geänderte Objekt intern aus (einbettendes Objekt). Sein Eintrag ist ``upsert``.
    - ``shared=True``: Es gehört zum geänderten Objekt und ändert sich mit ihm (der Sitzungsort einer Sitzung, der
      deren Kennung trägt). Sein Eintrag hat dieselbe Operation und denselben Grund. ``removed``: Das Objekt gibt es
      nicht mehr (die Sitzung nennt keinen Ort); einen Eintrag ``delete`` gibt es nur, wenn das Ereignis die Änderung
      dieses Felds nennt (``changed``).
    """

    kind: str
    id: uuid.UUID
    shared: bool = False
    removed: bool = False


#: Weitere Objekte zu Objekten eines Typs, die nur die Ausgabe kennt: (Objekttyp, Kennungen) -> je Kennung die Objekte.
#: Eine Abfrage je Aufruf, nie je Objekt.
Related = Callable[[str, Collection[uuid.UUID]], Mapping[uuid.UUID, Sequence[Link]]]


@dataclass(frozen=True)
class Freshness:
    """
    Aktualität einer geernteten Kommune (Issue #556): letzter vollständig erfolgreicher Abgleich bzw.
    Vollabgleich ihrer Quelle (``OParlSource.last_successful_sync``/``last_successful_full_sync``).
    ``None``: noch keiner.
    """

    synced_at: datetime | None
    full_synced_at: datetime | None


@dataclass(frozen=True)
class Feed:
    """
    Der Änderungsfeed einer Kommune in einer Ausgabe.

    - ``body_id``: kanonische Kennung der Kommune, wie sie die Ereignisse im Journal tragen
    - ``url``: Adresse des Feeds, ``snapshot_url``: Adresse des Snapshots (Einstieg und Wiedereinstieg)
    - ``addresses``: Adressen der Objekte dieser Ausgabe
    - ``epoch``: Abschnitt des Bestands, für den Cursor gelten. Ändert sich der Bestand am Journal
      vorbei (Rücknahme einer ganzen Kommune), wechselt die Ausgabe den Abschnitt; Cursor aus einem
      früheren gelten dann als abgelaufen (``410`` mit Verweis auf den Snapshot). Leer: der erste.
    - ``freshness``: Aktualität der Quelle einer geernteten Kommune; ``None`` (Session): nicht ausgegeben

    ``url`` und ``snapshot_url`` sind die Adressen der Form, die eine Antwort ausgibt: nach dem Profil von kommParl
    (``kommparl_changes_response``) oder in der älteren Form (``changes_response``).

    Für die einbettenden Objekte (nur nach dem Profil, Issue #1229):

    - ``embeds``: Einbettungen dieser Ausgabe, eingebetteter Typ -> Typen, die ihn intern ausgeben (aus
      ``OPARL_EMBEDDINGS``). Ohne Angabe nennt der Feed keine einbettenden Objekte.
    - ``related``: einbettende und mitgeänderte Objekte nach dem Stand der Ausgabe (``Related``); der Feed führt sie
      mit denen aus der Nutzlast zusammen
    - ``current``: Adressen der Objekte, die die Ausgabe derzeit ausliefert, ohne Gelöschtes; nur solche bekommen als
      einbettendes Objekt einen Eintrag. ``None``: wie ``addresses``.
    """

    body_id: uuid.UUID
    url: str
    snapshot_url: str
    addresses: Addresses
    epoch: str = ""
    freshness: Freshness | None = None
    embeds: Mapping[str, frozenset[str]] = field(default_factory=dict)
    related: Related | None = None
    current: Addresses | None = None


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
    """
    Stand eines Abnehmers: letzte gesehene Folgenummer und der Tag, an dem der Cursor ausgegeben wurde.

    ``part``: Stelle innerhalb des Ereignisses ``seq``, wenn der Abnehmer erst einen Teil seiner Einträge gesehen hat
    (``_PART``: Art und Rang des Objekts, seine Kennung). Leer: das Ereignis ist ganz gesehen.
    """

    seq: int
    day: date
    part: bytes = b""


class CursorExpiredError(Exception):
    """Der Cursor ist nicht (mehr) gültig; der Abnehmer steigt über den Snapshot wieder ein."""


#: 38 Zeichen: Folgenummer und Tag; 60 Zeichen: zusätzlich die Stelle innerhalb eines Ereignisses (``_PART``)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{38}|[A-Za-z0-9_-]{60}")
_LAYOUT: Final = struct.Struct(">QI")  # Folgenummer, Ausgabetag (Ordinalzahl)
_PART: Final = struct.Struct(">B16s")  # Stelle im Ereignis: Art und Rang des Objekts, Kennung
_KEY_INFO: Final = b"mandari.hub.api.changes.cursor.v1"


@lru_cache(maxsize=8)
def _cipher(secret: str) -> AESSIV:
    """Deterministische, authentifizierte Verschlüsselung mit einem aus ``secret`` abgeleiteten Schlüssel."""
    key = HKDF(algorithm=hashes.SHA256(), length=64, salt=None, info=_KEY_INFO).derive(secret.encode("utf-8"))
    return AESSIV(key)


def _associated(body_id: uuid.UUID, epoch: str) -> list[bytes]:
    # Ein Cursor gilt nur für die Kommune und den Abschnitt ihres Bestands, für die er ausgegeben wurde.
    # Der erste Abschnitt hat keine eigene Angabe: Cursor aus der Zeit vor den Abschnitten gelten weiter.
    return [b"changes", body_id.bytes, *([epoch.encode("utf-8")] if epoch else [])]


def encode_cursor(body_id: uuid.UUID, seq: int, day: date, epoch: str = "", part: bytes = b"") -> str:
    """
    Opaker Cursor. Gleiche Eingaben ergeben denselben Cursor (stabile Antworten und ``ETag``); ohne den
    Schlüssel der Installation lässt er weder Folgenummer noch Tag erkennen. ``part``: Stelle innerhalb des
    Ereignisses (``Cursor.part``); ohne sie ist der Cursor derselbe wie vor Issue #1229.
    """
    if part and len(part) != _PART.size:
        raise ValueError("Stelle innerhalb eines Ereignisses mit falscher Länge")
    plain = _LAYOUT.pack(seq, day.toordinal()) + part
    token = _cipher(settings.SECRET_KEY).encrypt(plain, _associated(body_id, epoch))
    return base64.urlsafe_b64encode(token).rstrip(b"=").decode("ascii")


def decode_cursor(body_id: uuid.UUID, token: str, epoch: str = "") -> Cursor:
    """
    Cursor lesen. ``BadRequestError``, wenn ``token`` keiner ist; ``CursorExpiredError``, wenn er nicht
    von dieser Installation für diese Kommune und diesen Abschnitt ihres Bestands (``Feed.epoch``)
    ausgegeben wurde (etwa nach einem Schlüsselwechsel oder einer Rücknahme der Kommune).
    """
    if not _TOKEN.fullmatch(token):
        raise BadRequestError("Parameter 'after': kein gültiger Cursor. Cursor stammen aus einer Antwort des Feeds.")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        raise BadRequestError(
            "Parameter 'after': kein gültiger Cursor. Cursor stammen aus einer Antwort des Feeds."
        ) from None
    for secret in (settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", ())):
        try:
            plain = _cipher(secret).decrypt(raw, _associated(body_id, epoch))
            if len(plain) not in (_LAYOUT.size, _LAYOUT.size + _PART.size):
                continue
            seq, ordinal = _LAYOUT.unpack(plain[: _LAYOUT.size])
            return Cursor(seq=seq, day=date.fromordinal(ordinal), part=plain[_LAYOUT.size :])
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


def _beginning_missing() -> bool:
    """
    Fehlt der Anfang des Journals (aufgeräumt)? Dann kann niemand „von vorn“ lesen, ohne etwas zu
    verpassen – der Einstieg ist der Snapshot.
    """
    return pruning.horizon() is not None


def _missing_since(cursor: Cursor) -> bool:
    """
    Sicherheitsnetz gegen ein Journal, das kürzer aufbewahrt als der Feed zusagt: Wurden Zeilen hinter
    dem Cursor gelöscht, die nach seiner Ausgabe erfasst worden sein können, hat der Abnehmer sie nie
    gesehen.

    Maßgeblich ist, was das Aufräumen festhält (``apps.events.pruning``), nicht die älteste verbliebene
    Folgenummer: Der Sequenzierer darf Nummern verwerfen, eine Lücke am Anfang heißt nicht, dass etwas
    gelöscht wurde.
    """
    horizon = pruning.horizon()
    if horizon is None or cursor.seq >= horizon.through_seq:
        return False
    return horizon.recorded_before > datetime.combine(cursor.day, time.min, tzinfo=UTC)


def _lost_in_restore(cursor: Cursor) -> bool:
    """
    Zeigt der Cursor auf ein Ereignis, das es nicht mehr gibt, ohne dass aufgeräumt wurde?

    Jeder Cursor nennt die Folgenummer eines Ereignisses, das bei seiner Ausgabe im Journal stand (oder 0),
    und Zeilen löscht nur das Aufräumen, das festgehalten wird. Fehlt das Ereignis oberhalb dessen, wurde
    die Datenbank aus einer Sicherung wiederhergestellt: Der Abnehmer hat Ereignisse gesehen, die es nicht
    mehr gibt, und steigt über den Snapshot neu ein. Ihre Folgenummern werden nie wieder vergeben
    (``apps.events.wiederherstellung``), die Antwort bleibt also ``410``.
    """
    if cursor.seq <= 0:
        return False
    horizon = pruning.horizon()
    if horizon is not None and cursor.seq <= horizon.through_seq:
        return False
    return not Event.objects.filter(seq=cursor.seq).exists()


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
        # Ein ``delete`` nennt immer seinen Grund (ADR); ein Entfernen ohne Rücknahme hat die Quelle veranlasst
        return Operation.DELETE.value, reason if reason in DELETE_REASONS else DEFAULT_DELETE_REASON
    return Operation.UPSERT.value, None


#: Ein Eintrag aus Ereignis, Objekttyp der Adresse, Adresse, Operation, Grund und Cursor
EntryBuilder = Callable[[Event, str, str, str, str | None, str], Objekt]


def _entry(event: Event, kind: str, address: str, operation: str, reason: str | None, cursor: str) -> Objekt:
    """Eintrag in der älteren Form (``mandari:changes``): Gründe auf Deutsch, ``modified`` mit Sekundenbruchteilen."""
    entry: Objekt = {
        "cursor": cursor,
        "operation": operation,
        "type": schema_type(kind),
        "id": address,
        "modified": iso(event.occurred_at, fractions=True),
    }
    if reason:
        entry["reason"] = reason
    return entry


def seconds(moment: datetime) -> str:
    """Zeitpunkt in der Form ``yyyy-mm-ddThh:mm:ss±hh:mm`` (OParl 1.1, Kapitel „JSON-Ausgabe“), in UTC."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def _kommparl_entry(event: Event, kind: str, address: str, operation: str, reason: str | None, cursor: str) -> Objekt:
    """Eintrag nach dem Profil Änderungsfeed von kommParl: Gründe des Profils, ``modified`` auf die Sekunde."""
    entry: Objekt = {
        "cursor": cursor,
        "operation": operation,
        "type": schema_type(kind),
        "id": address,
        "modified": seconds(event.occurred_at),
    }
    if reason:
        entry["reason"] = KOMMPARL_REASONS[reason]
    return entry


@dataclass(frozen=True)
class _Page:
    """Gelesene Einträge, der neue Stand (Folgenummer und Stelle im Ereignis) und ob das Lesebudget erschöpft ist."""

    entries: list[Objekt]
    last: tuple[int, bytes]
    exhausted: bool


@dataclass(frozen=True)
class _Item:
    """
    Ein Eintrag, den ein Ereignis ergeben kann: Objekt, Operation, Grund, seine Stelle im Ereignis (``_PART``) und ob
    es ein einbettendes Objekt ist (dann nur, solange die Ausgabe es ausliefert).
    """

    kind: str
    id: uuid.UUID
    operation: str
    reason: str | None
    part: bytes
    parent: bool = False


#: Art eines Eintrags innerhalb eines Ereignisses: das geänderte Objekt, ein mitgeändertes, ein einbettendes
_PRIMARY, _SHARED, _PARENT = 0, 1, 2
#: Rang der Objekttypen (Reihenfolge der Spezifikation); er ordnet die Einträge eines Ereignisses
_RANK: Final[dict[str, int]] = {kind: rank for rank, kind in enumerate(TYPE_SCHEMA)}


def _part(klass: int, kind: str, object_id: uuid.UUID) -> bytes:
    """Stelle eines Eintrags im Ereignis; als Bytes verglichen erst nach Art, dann Typ, dann Kennung."""
    return _PART.pack(klass << 4 | _RANK[kind], object_id.bytes)


def _uuid(value: object) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _payload_links(event: Event, kind: str, embeds: Mapping[str, frozenset[str]]) -> list[Link]:
    """Einbettende Objekte, die das Ereignis selbst nennt, soweit die Ausgabe sie einbettet."""
    parents = embeds.get(kind, frozenset())
    if not parents:
        return []
    payload = event.payload if isinstance(event.payload, dict) else {}
    links: list[Link] = []
    for name, parent_kind in PAYLOAD_PARENTS.get(event.aggregate_type, ()):
        found = _uuid(payload.get(name))
        if found is not None and parent_kind in parents:
            links.append(Link(parent_kind, found))
    if kind == "legislativeterm" and "body" in parents and event.body_id is not None:
        # Die Wahlperiode gibt die Kommune aus, in deren Journal das Ereignis steht
        links.append(Link("body", event.body_id))
    return links


def _changed_fields(event: Event) -> frozenset[str]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    changed = payload.get("changed")
    return frozenset(str(name) for name in changed) if isinstance(changed, list) else frozenset()


@dataclass
class _Expansion:
    """Was ein Ereignis betrifft: das geänderte Objekt, mitgeänderte und einbettende Objekte."""

    event: Event
    primary: _Item
    shared: list[Link] = field(default_factory=list)
    parents: dict[tuple[str, uuid.UUID], None] = field(default_factory=dict)

    def add_parent(self, link: Link) -> bool:
        """Einbettendes Objekt aufnehmen; ``False``, wenn es schon dazugehört (oder das geänderte Objekt ist)."""
        key = (link.kind, link.id)
        if key == (self.primary.kind, self.primary.id) or key in self.parents:
            return False
        self.parents[key] = None
        return True

    def items(self) -> list[_Item]:
        """Die Einträge des Ereignisses, nach ihrer Stelle geordnet und ohne Doppel."""
        primary = self.primary
        items = [primary]
        seen = {(primary.kind, primary.id)}
        for link in self.shared:
            if (link.kind, link.id) in seen:
                continue
            reason: str | None
            if link.removed:
                # Das mitgeänderte Objekt gibt es nicht mehr – ein Eintrag nur, wenn das Ereignis die Änderung nennt
                if primary.operation != Operation.UPSERT.value or link.kind not in _changed_fields(self.event):
                    continue
                operation, reason = Operation.DELETE.value, DEFAULT_DELETE_REASON
            else:
                operation, reason = primary.operation, primary.reason
            seen.add((link.kind, link.id))
            items.append(_Item(link.kind, link.id, operation, reason, _part(_SHARED, link.kind, link.id)))
        for kind, object_id in self.parents:
            if (kind, object_id) in seen:
                continue
            seen.add((kind, object_id))
            part = _part(_PARENT, kind, object_id)
            items.append(_Item(kind, object_id, Operation.UPSERT.value, None, part, parent=True))
        return sorted(items, key=lambda item: item.part)


def _expand(feed: Feed, batch: Sequence[Event], subjects: Sequence[tuple[str, uuid.UUID] | None]) -> list[list[_Item]]:
    """
    Einträge je Ereignis nach dem Profil: das geänderte Objekt, mitgeänderte Objekte und – über bis zu ``_MAX_DEPTH``
    Stufen – die Objekte, die es intern ausgeben. Je Stufe und Typ höchstens eine Frage an die Ausgabe
    (``Feed.related``), für alle Ereignisse des Abschnitts zusammen.
    """
    expansions: list[_Expansion | None] = []
    #: je Ereignis: Objekte, deren weitere Objekte die Ausgabe nennen soll
    pending: list[list[tuple[str, uuid.UUID]]] = []
    for event, subject in zip(batch, subjects, strict=True):
        if subject is None or event.seq is None:
            expansions.append(None)
            pending.append([])
            continue
        kind, object_id = subject
        operation, reason = _operation(event)
        expansion = _Expansion(event, _Item(kind, object_id, operation, reason, _part(_PRIMARY, kind, object_id)))
        links = _payload_links(event, kind, feed.embeds)
        for link in links:
            expansion.add_parent(link)
        payload = event.payload if isinstance(event.payload, dict) else {}
        for name, companion_kind in PAYLOAD_COMPANIONS.get(event.type, ()):
            if (companion := _uuid(payload.get(name))) is not None:
                expansion.shared.append(Link(companion_kind, companion, shared=True))
        # Die Ausgabe fragen, auch wenn das Ereignis einbettende Objekte nennt: Es nennt nicht immer alle (eine Datei
        # an Vorlage und Sitzung, deren Ereignis nur die Vorlage nennt). ``add_parent`` führt beides ohne Doppel
        # zusammen. Dazu nach mitgeänderten Objekten
        ask = kind in feed.embeds or kind in SHARED_KINDS
        own = [(kind, object_id)] if ask else []
        # Einbettende Objekte, die selbst eingebettet sind (der Tagesordnungspunkt einer Datei): eine Stufe weiter
        pending.append(own + [(link.kind, link.id) for link in links if link.kind in feed.embeds])
        expansions.append(expansion)

    known: dict[tuple[str, uuid.UUID], Sequence[Link]] = {}
    for _depth in range(_MAX_DEPTH):
        wanted: dict[str, set[uuid.UUID]] = {}
        for objects in pending:
            for kind, object_id in objects:
                if (kind, object_id) not in known:
                    wanted.setdefault(kind, set()).add(object_id)
        if feed.related is None or not any(objects for objects in pending):
            break
        for kind, ids in wanted.items():
            found = feed.related(kind, ids)
            for object_id in ids:
                known[(kind, object_id)] = found.get(object_id, ())
        for index, grown in enumerate(expansions):
            if grown is None:
                continue
            following: list[tuple[str, uuid.UUID]] = []
            for kind, object_id in pending[index]:
                is_primary = (kind, object_id) == (grown.primary.kind, grown.primary.id)
                for link in known.get((kind, object_id), ()):
                    if link.shared:
                        # Mitgeändert ist nur, was zum geänderten Objekt selbst gehört
                        if is_primary:
                            grown.shared.append(link)
                        continue
                    embedding = link.kind in feed.embeds.get(kind, frozenset())
                    # Ist das einbettende Objekt selbst eingebettet, folgt die nächste Stufe
                    if embedding and grown.add_parent(link) and link.kind in feed.embeds:
                        following.append((link.kind, link.id))
            pending[index] = following
    return [[] if done is None else done.items() for done in expansions]


def _read(
    feed: Feed,
    after: tuple[int, bytes],
    limit: int,
    day: date,
    build: EntryBuilder = _entry,
    *,
    profile: bool = False,
) -> _Page:
    """
    Höchstens ``limit`` Einträge nach ``after`` (Folgenummer und Stelle im Ereignis) in aufsteigender Reihenfolge und
    der neue Stand: die Stelle des letzten ausgegebenen Eintrags (``after``, wenn es keinen gibt).

    Ereignisse, deren Objekt in dieser Ausgabe keine Adresse hat, werden übersprungen; der Stand rückt
    nur mit ausgegebenen Einträgen vor. Ausnahme: Ist das Lesebudget erschöpft, bevor die Seite voll
    ist, rückt er bis zum zuletzt gelesenen Ereignis vor. Sonst bliebe ein Abnehmer hinter einem Block
    solcher Ereignisse stehen und bekäme nie wieder eine Änderung.

    ``profile``: nach dem Profil von kommParl – mit den einbettenden und mitgeänderten Objekten (``_expand``), ohne
    Doppelungen in der Seite und mit der Stelle im Ereignis. Sonst (ältere Form) je Ereignis höchstens ein Eintrag;
    eine Stelle im Ereignis zählt dann wie das ganze Ereignis.
    """
    seq, start = after if profile else (after[0], b"")
    entries: list[Objekt | None] = []
    #: Adresse -> Stelle ihres letzten Eintrags in ``entries`` (nur nach dem Profil)
    latest: dict[str, int] = {}
    count = 0
    position = seq
    last = (seq, start)
    examined = 0
    budget = max(limit * _MAX_EXAMINED_FACTOR, _MIN_EXAMINED)
    size = limit
    first = True
    while count < limit and examined < budget:
        query = events(feed.body_id).order_by("seq")
        # Endet der Cursor innerhalb eines Ereignisses, stehen dessen übrige Einträge am Anfang
        query = query.filter(seq__gte=position) if first and start else query.filter(seq__gt=position)
        first = False
        batch = list(query[: min(size, budget - examined)])
        if not batch:
            break
        examined += len(batch)
        subjects = [_subject(event) for event in batch]
        expansions = _expand(feed, batch, subjects) if profile else _single(batch, subjects)
        addresses = _addresses(feed, expansions)
        for event, items in zip(batch, expansions, strict=True):
            if event.seq is None:
                continue
            resumed = bool(start) and event.seq == seq
            shown: list[tuple[_Item, str]] = []
            for item in items:
                address = addresses[item.parent].get(item.kind, {}).get(item.id)
                if address is not None and not (resumed and item.part <= start):
                    shown.append((item, address))
            for index, (item, address) in enumerate(shown):
                # Der letzte Eintrag eines Ereignisses trägt den Cursor des ganzen Ereignisses
                part = b"" if index == len(shown) - 1 else item.part
                cursor = encode_cursor(feed.body_id, event.seq, day, feed.epoch, part)
                entry = build(event, item.kind, address, item.operation, item.reason, cursor)
                if profile:
                    earlier = latest.get(address)
                    if earlier is not None and (previous := entries[earlier]) is not None and _same(previous, entry):
                        # Derselbe Eintrag folgt: Der frühere entfällt, der Abnehmer ruft das Objekt hier ab
                        entries[earlier] = None
                        count -= 1
                    latest[address] = len(entries)
                entries.append(entry)
                count += 1
                last = (event.seq, part)
                if count == limit:
                    break
            if count == limit:
                break
        position = batch[-1].seq or position
        if len(batch) < size:
            break
        # Es wurde übersprungen: in größeren Schritten weiterlesen
        size = max(limit - count, _SKIP_BATCH)
    exhausted = count < limit and examined >= budget
    if exhausted:
        # Budget erschöpft: bis zum zuletzt gelesenen Ereignis vorrücken, sonst stünde der Feed hier still
        logger.warning(
            "Änderungsfeed: %s Ereignisse gelesen, nur %s Einträge, Stand rückt darüber hinaus (Kommune %s)",
            examined,
            count,
            feed.body_id,
        )
        last = (position, b"")
    return _Page([entry for entry in entries if entry is not None], last, exhausted)


def _single(batch: Sequence[Event], subjects: Sequence[tuple[str, uuid.UUID] | None]) -> list[list[_Item]]:
    """Ältere Form: je Ereignis nur das Objekt, das es nennt."""
    expansions: list[list[_Item]] = []
    for event, subject in zip(batch, subjects, strict=True):
        if subject is None:
            expansions.append([])
            continue
        operation, reason = _operation(event)
        kind, object_id = subject
        expansions.append([_Item(kind, object_id, operation, reason, _part(_PRIMARY, kind, object_id))])
    return expansions


def _same(previous: Objekt, entry: Objekt) -> bool:
    """Gleicher Eintrag zum selben Objekt: gleiche Operation und gleicher Grund (die Adresse ist dieselbe)."""
    return previous["operation"] == entry["operation"] and previous.get("reason") == entry.get("reason")


def _addresses(feed: Feed, expansions: Sequence[Sequence[_Item]]) -> dict[bool, dict[str, Mapping[uuid.UUID, str]]]:
    """
    Adressen aller Einträge eines Abschnitts, je Typ eine Abfrage: unter ``False`` die geänderten und mitgeänderten
    Objekte (``Feed.addresses``), unter ``True`` die einbettenden, nur solange die Ausgabe sie ausliefert
    (``Feed.current``).
    """
    wanted: dict[bool, dict[str, set[uuid.UUID]]] = {False: {}, True: {}}
    for items in expansions:
        for item in items:
            wanted[item.parent].setdefault(item.kind, set()).add(item.id)
    current = feed.current or feed.addresses
    return {
        False: {kind: feed.addresses(kind, ids) for kind, ids in wanted[False].items()},
        True: {kind: current(kind, ids) for kind, ids in wanted[True].items()},
    }


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


_EXPIRED_DETAIL: Final = (
    "Der Cursor ist nicht mehr gültig: Er ist älter als die Aufbewahrung des Änderungsfeeds, stammt "
    "nicht aus diesem Feed oder ist älter als eine Rücknahme der Kommune. Bitte den Snapshot abrufen "
    "und mit dessen Cursor fortsetzen."
)
_WITHDRAWN_DETAIL: Final = (
    "Die Kommune hat die Veröffentlichung dauerhaft zurückgenommen; ihre Einträge gelten als gelöscht. "
    "Ein vorhandener Cursor gilt nicht mehr. Wird die Kommune wieder veröffentlicht, beginnt der "
    "Abgleich mit dem Snapshot."
)


def expired_response(request: HttpRequest, feed: Feed) -> HttpResponse:
    """``410 Gone`` nach RFC 9457 mit dem Verweis auf den Snapshot (ältere Form)."""
    problem = Problem(
        status=410,
        kind="cursor-abgelaufen",
        detail=_EXPIRED_DETAIL,
        extensions={"snapshot": feed.snapshot_url},
    )
    return problem_response(request, problem)


def withdrawn_response(request: HttpRequest) -> HttpResponse:
    """
    ``410 Gone`` für Feed und Snapshot einer Kommune, die ihre Veröffentlichung dauerhaft zurückgenommen
    hat – ohne Verweis auf den Snapshot, denn den gibt es dann auch nicht. Eine feste Antwort: Sie verrät
    nichts über das Journal der Kommune (ältere Form).
    """
    problem = Problem(status=410, kind="kommune-zurueckgenommen", detail=_WITHDRAWN_DETAIL)
    return problem_response(request, problem)


def _oparl_error(status: int, message: str, debug: str, snapshot_url: str | None = None) -> HttpResponse:
    """Fehlerobjekt nach OParl 1.1, Kapitel „Ausnahmebehandlung“; nie zwischengespeichert."""
    data: dict[str, str] = {"type": kommparl.OPARL_ERROR, "message": message, "debug": debug}
    if snapshot_url:
        data["kommparl:snapshot"] = snapshot_url
    response = json_response(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


def kommparl_expired_response(feed: Feed) -> HttpResponse:
    """
    ``410 Gone`` nach dem Profil Änderungsfeed (Abschnitt „Aufbewahrung“): Fehlerobjekt nach OParl 1.1 mit dem
    Snapshot in ``kommparl:snapshot`` (Schema ``profiles/changes/CursorExpired.json``).
    """
    return _oparl_error(410, _EXPIRED_DETAIL, "cursor-abgelaufen", feed.snapshot_url)


def kommparl_withdrawn_response() -> HttpResponse:
    """``410 Gone`` für Feed und Snapshot nach den Profilen von kommParl, wenn die Kommune zurückgenommen ist."""
    return _oparl_error(410, _WITHDRAWN_DETAIL, "kommune-zurueckgenommen")


_DELETED_DETAIL: Final = (
    "Die Kommune (Body) ist in ihrer Quelle gelöscht. Ein Snapshot enthält keine gelöschten Objekte, deshalb gibt "
    "es keinen; die Löschung nennt der Änderungsfeed."
)


def kommparl_deleted_response() -> HttpResponse:
    """``410 Gone`` für den Snapshot nach dem Profil von kommParl, wenn der Body selbst gelöscht ist."""
    return _oparl_error(410, _DELETED_DETAIL, "kommune-geloescht")


def _oldest_retained() -> tuple[int, bytes]:
    """
    Stand, hinter dem der älteste aufbewahrte Eintrag steht: hinter dem zuletzt Aufgeräumten (``0``, solange nie etwas
    gelöscht wurde).
    """
    horizon = pruning.horizon()
    return (0 if horizon is None else horizon.through_seq, b"")


def _position(
    request: HttpRequest, feed: Feed, expired: Callable[[], HttpResponse], *, profile: bool = False
) -> tuple[int, bytes] | HttpResponse:
    """
    Stand, ab dem gelesen wird (Folgenummer und Stelle im Ereignis): aus dem Cursor ``after`` oder von vorn – oder die
    Antwort ``expired``, wenn der Cursor abgelaufen ist.

    Ohne ``after`` unterscheiden sich die Formen: Nach dem Profil Änderungsfeed (``profile``, Abschnitt „Abruf“)
    beginnt die Antwort beim ältesten Eintrag, den der Server noch aufbewahrt; ``410`` gibt es dort nur für einen
    Cursor, dessen Folgeeinträge fehlen. Die ältere Form antwortet ohne Cursor mit ``410``, sobald der Anfang des
    Journals aufgeräumt ist, und bleibt so.
    """
    days = retention_days()
    token = request.GET.get("after")
    if not token:
        if profile:
            return _oldest_retained()
        # Ohne Cursor von vorn – nur solange der Anfang des Journals noch da ist
        return expired() if _beginning_missing() else (0, b"")
    try:
        cursor = decode_cursor(feed.body_id, token, feed.epoch)
    except CursorExpiredError:
        return expired()
    if (today() - cursor.day).days > days or _missing_since(cursor) or _lost_in_restore(cursor):
        return expired()
    return cursor.seq, cursor.part


def changes_response(request: HttpRequest, feed: Feed) -> HttpResponse:
    """
    Eine Seite des Änderungsfeeds in der älteren Form (``mandari:changes``): ``data`` (Einträge in aufsteigender
    Reihenfolge), ``cursor`` (Stand für die nächste Anfrage) und ``links`` (``self``, ``next``, ``snapshot``).

    Eine leere Seite bedeutet: Der Abnehmer ist aktuell. Ihren Cursor übernimmt er trotzdem – er ist
    frisch ausgegeben und gilt wieder die volle Aufbewahrungszeit.
    """
    limit = _limit(request)
    day = today()
    after = _position(request, feed, lambda: expired_response(request, feed))
    if isinstance(after, HttpResponse):
        return after

    read = _read(feed, after, limit, day)
    position = encode_cursor(feed.body_id, read.last[0], day, feed.epoch)
    page: dict[str, object] = {
        "data": read.entries,
        "cursor": position,
        "links": {
            "self": _link(feed, request.GET.get("after") or None, request),
            "next": _link(feed, position, request),
            "snapshot": feed.snapshot_url,
        },
    }
    if feed.freshness is not None:
        page["freshness"] = {
            "synced_at": iso(feed.freshness.synced_at, fractions=True),
            "full_synced_at": iso(feed.freshness.full_synced_at, fractions=True),
        }
    return json_response(page)


def kommparl_changes_response(request: HttpRequest, feed: Feed) -> HttpResponse:
    """
    Eine Seite des Änderungsfeeds nach dem Profil von kommParl (``profiles/changes/ChangeList.json``): ``data``,
    ``cursor`` und ``links`` mit ``self`` und – nur wenn weitere Einträge vorliegen – ``next``.

    Ob weitere Einträge vorliegen, zeigt ein Eintrag über die Seite hinaus. Es zählen nur Einträge, die diese
    Ausgabe nennt; ``next`` verrät also nichts über Ereignisse ohne Adresse. Ist das Lesebudget erschöpft, nennt die
    Seite ``next`` mit dem vorgerückten Cursor. Eine Seite ohne ``next`` heißt: Der Abnehmer ist aktuell.

    Die Einträge nennen auch einbettende und mitgeänderte Objekte, ohne Doppelungen in der Seite (``_read``).
    """
    limit = _limit(request)
    day = today()
    after = _position(request, feed, lambda: kommparl_expired_response(feed), profile=True)
    if isinstance(after, HttpResponse):
        return after

    read = _read(feed, after, limit + 1, day, _kommparl_entry, profile=True)
    full = len(read.entries) > limit
    entries = read.entries[:limit]
    # Steht ein Eintrag über die Seite hinaus, endet sie hinter ihrem letzten Eintrag
    seq, part = read.last
    position = str(entries[-1]["cursor"]) if full else encode_cursor(feed.body_id, seq, day, feed.epoch, part)
    links = {"self": _link(feed, request.GET.get("after") or None, request)}
    if full or read.exhausted:
        links["next"] = _link(feed, position, request)
    page: dict[str, object] = {"data": entries, "cursor": position, "links": links}
    if feed.freshness is not None:
        page["mandari:freshness"] = {
            "synced_at": iso(feed.freshness.synced_at),
            "full_synced_at": iso(feed.freshness.full_synced_at),
        }
    return json_response(page)


def deprecated(response: HttpResponseBase, successor: str) -> HttpResponseBase:
    """
    Antwort der älteren Form (``mandari:changes``, ``mandari:snapshot``) als abgekündigt kennzeichnen:
    ``Deprecation``, ``Sunset`` und ``Link`` mit dem Nachfolger nach dem kommParl-Profil; auch Browsern lesbar.
    """
    MANDARI_FEED_DEPRECATION.apply(response, successor=successor)
    exposed = [part.strip() for part in str(response.get("Access-Control-Expose-Headers", "")).split(",")]
    for header in ("Deprecation", "Sunset", "Link"):
        if header not in exposed:
            exposed.append(header)
    response["Access-Control-Expose-Headers"] = ", ".join(part for part in exposed if part)
    return response
