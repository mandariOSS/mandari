# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``suchindex``: pflegt den Suchindex aus den Ereignissen der Datendrehscheibe (Issue #526).

Heute schreiben drei Wege den Suchindex: Django-Signale, der Ingestor und ``reindex_elasticsearch``.
Künftig schreibt ihn nur dieses Abonnement (``docs/adr/20260929-ereignistechnik-postgres.md``,
Punkt 7). Bis zum Umschalten (Issue #527) läuft es im **Schattenbetrieb** und schreibt einen
Schattenindex (``schatten-papers`` …, ``insight_search.indices``) mit derselben Abbildung; der
Vergleich mit dem Live-Index zeigt, ob die Ereignisse alles abdecken
(``manage.py suchindex_schatten vergleichen``).

**Schalter** ``SEARCH_INDEX_SUBSCRIPTION`` (``insight_search.subscribers``): ``aus`` (Standard)
registriert nichts; ``schatten`` schreibt nur den Schattenindex, auch wenn das Abonnement in der
Datenbank auf ``aktiv`` steht; ``aktiv`` schreibt den Live-Index, außer das Abonnement steht in der
Datenbank auf ``schatten``.

**Ablauf je Batch:**

1. Aus den Ereignissen die betroffenen Dokumente bestimmen (``affected``): Sitzung, Vorgang, Datei,
   Person, Gremium über ``aggregate_type``. Abhängige Dokumente: Eine Datei oder Beratung betrifft
   zusätzlich ihren Vorgang (Textvorschau, Dateinamen, Gremien), wie heute die Signale; ein Vorgang
   bzw. eine Sitzung die indexierbaren Dateien, die direkt an ihm bzw. ihr hängen (Name und
   Aktenzeichen des Vorgangs, Name und Datum der Sitzung). Nur öffentliche Ereignisse, dazu die
   Texterkennung (``ris.file.text_extracted``, laut Vertrag ``intern``: eine Anreicherung ohne Inhalt;
   das Dokument entsteht wie jedes andere nur aus dem RIS-Bestand). Im Schattenbetrieb nur die
   gewählten Kommunen (``SEARCH_INDEX_SHADOW_BODIES``).

   **Bekannte Lücken** (Issue #821, Voraussetzung für #527): Sitzung über die Beratung → Dateien der
   beratenen Vorgänge (``meeting_name``/``meeting_date``), Tagesordnungspunkt → Dateien
   (``agenda_number``, ``ris.agendaitem.*`` ist nicht abonniert), Gremium → ``organization_names``
   von Sitzungen, Vorgängen und Dateien; für Gremien und Personen gibt es nur Löschmeldungen.
2. Jedes Dokument aus dem **aktuellen** Bestand bauen (``insight_core.services.search_projection``,
   Dokumentbauer wie ``reindex_elasticsearch``). Gehört das Objekt nicht (mehr) in den Index, wird
   sein Dokument gelöscht.
3. Schreiben mit **externer Version gleich Folgenummer** (``version_type=external``): Ein älterer
   Stand verliert gegen einen neueren (Konflikt 409, gezählt als ``stale``). Wiederholte oder
   nachgespielte Ereignisse schaden deshalb nicht (Zustellung mindestens einmal, Idempotenz hier).

**Fehler:** Ist Elasticsearch nicht erreichbar oder überlastet (Verbindung, 429, 5xx), meldet der
Handler ``TargetUnavailableError``; die Zustellung wartet und stellt denselben Batch erneut zu,
nichts wird geparkt. Lehnt Elasticsearch einzelne Dokumente ab (z. B. Feldtyp), wirft er
``DocumentsRejectedError``; die Zustellung parkt dann nur die betroffenen Ereignisse.

**Speicher:** Der Schattenbetrieb lässt sich auf Kommunen begrenzen und hat eine ungefähre
Obergrenze an Dokumenten (``SEARCH_INDEX_SHADOW_MAX_DOCS``, Prüfung je Batch): Ist sie erreicht,
werden vorhandene Dokumente weiter aktualisiert und gelöscht, neue nicht mehr angelegt
(``skipped_limit``). Dateitexte werden einzeln gebaut und in Paketen bis 8 MB gesendet.

**Kennzahlen:** Rückstand ``mandari_events_lag_seconds{subscription="suchindex"}`` (Zustellung),
Ergebnis je Dokument ``mandari_search_subscription_documents_total{target,result}`` (hier).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, fields
from typing import Any, Final, NamedTuple

from django.conf import settings
from prometheus_client import Counter

from apps.events import Delivery, TargetUnavailableError
from apps.events.models import Event, Visibility
from insight_core.services.search_projection import iter_documents, related_file_ids, related_paper_ids
from insight_search.indices import INDEXES, SHADOW_PREFIX, ensure_shadow_indices, shadow_name

logger = logging.getLogger(__name__)

NAME: Final = "suchindex"
TYPES: Final = (
    "ris.meeting.*",
    "ris.paper.*",
    "ris.file.*",
    "ris.consultation.*",
    "ris.object.depublished",
)
#: Ereignisse je Batch: Ein Batch kann ebenso viele Dateitexte bauen
BATCH: Final = 100
QUEUE: Final = "index"

#: Suchindex je kanonischem Objekttyp (``aggregate_type``)
INDEX_BY_AGGREGATE: Final[Mapping[str, str]] = {
    "Meeting": "meetings",
    "Paper": "papers",
    "Person": "persons",
    "Organization": "organizations",
    "File": "files",
}
#: Objekttypen, deren Änderung das Dokument ihres Vorgangs ändert
_PAPER_DEPENDENTS: Final = ("File", "Consultation")
#: Objekttypen, deren Änderung die Dokumente der Dateien ändert, die an ihnen hängen
_FILE_OWNERS: Final = ("Paper", "Meeting")
#: Sichtbarkeiten je Ereignistyp, die den Suchindex betreffen; alle übrigen Typen nur ``oeffentlich``.
#: Die Texterkennung ist laut Vertrag ``intern`` (Anreicherung, Nutzlast ohne Inhalt); das Dokument der
#: Datei entsteht trotzdem nur aus dem RIS-Bestand mit derselben Auswahl wie ``reindex_elasticsearch``.
_VISIBILITY_BY_TYPE: Final[Mapping[str, frozenset[str]]] = {
    "ris.file.text_extracted": frozenset({Visibility.OEFFENTLICH.value, Visibility.INTERN.value}),
}
_PUBLIC: Final = frozenset({Visibility.OEFFENTLICH.value})

#: Höchstens so viele Aktionen bzw. Bytes je Bulk-Anfrage
BULK_ACTIONS: Final = 100
BULK_BYTES: Final = 8 * 1024 * 1024
#: Statuscodes unter 500, bei denen Elasticsearch als Ganzes nicht verfügbar ist (überlastet)
_UNAVAILABLE: Final = frozenset({429})

DOCUMENTS = Counter(
    "mandari_search_subscription_documents_total",
    "Dokumente des Abonnements suchindex je Ziel (schatten, live) und Ergebnis",
    ["target", "result"],
)


class DocumentsRejectedError(Exception):
    """Elasticsearch hat einzelne Dokumente abgelehnt; die Zustellung parkt die betroffenen Ereignisse."""


@dataclass(frozen=True)
class Target:
    """Ein Dokument im Suchindex."""

    index: str
    id: uuid.UUID


class Operation(NamedTuple):
    """Schreiben (``document``) oder Löschen (``None``) eines Dokuments mit externer Version."""

    index_name: str
    id: str
    version: int
    document: dict[str, Any] | None


@dataclass
class Tally:
    """Ergebnis je Dokument (Kennzahl ``mandari_search_subscription_documents_total``)."""

    indexed: int = 0
    deleted: int = 0
    #: Löschen eines Dokuments, das nicht im Index stand (etwa eine Datei ohne erkannten Text)
    absent: int = 0
    stale: int = 0
    skipped_body: int = 0
    skipped_limit: int = 0
    rejected: int = 0
    unavailable: int = 0

    def as_dict(self) -> dict[str, int]:
        return {feld.name: getattr(self, feld.name) for feld in fields(self)}

    def count_to(self, target: str) -> None:
        for ergebnis, anzahl in self.as_dict().items():
            if anzahl:
                DOCUMENTS.labels(target=target, result=ergebnis).inc(anzahl)


# --- Elasticsearch ------------------------------------------------------------------------------

_client_lock = threading.Lock()
_client: Any = None


def client() -> Any:
    """Elasticsearch-Client des Prozesses (threadsicher, einmal angelegt)."""
    global _client
    with _client_lock:
        if _client is None:
            from elasticsearch import Elasticsearch

            _client = Elasticsearch(settings.ELASTICSEARCH_URL, request_timeout=60)
        return _client


def _unavailable(exc: BaseException) -> bool:
    """Ist das Ziel als Ganzes nicht erreichbar (Verbindung, Zeitüberschreitung, Überlast)?"""
    from elastic_transport import TransportError
    from elasticsearch import ApiError

    if isinstance(exc, TransportError):
        return True
    if isinstance(exc, ApiError):
        status = int(getattr(getattr(exc, "meta", None), "status", 0) or 0)
        return status in _UNAVAILABLE or status >= 500
    return False


def index_name(index: str, *, shadow: bool) -> str:
    return shadow_name(index) if shadow else index


# --- betroffene Dokumente -----------------------------------------------------------------------


def _uuid(wert: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(wert))
    except (TypeError, ValueError, AttributeError):
        return None


def _seq(ereignis: Event) -> int:
    if ereignis.seq is None:  # Zugestellt werden nur nummerierte Ereignisse
        raise ValueError("Ereignis ohne Folgenummer")
    return ereignis.seq


def relevant(ereignis: Event) -> bool:
    """Betrifft das Ereignis seiner Sichtbarkeit nach den Suchindex (``_VISIBILITY_BY_TYPE``)?"""
    return str(ereignis.visibility) in _VISIBILITY_BY_TYPE.get(ereignis.type, _PUBLIC)


def affected(events: Iterable[Event], bodies: frozenset[str] | None = None) -> tuple[dict[Target, int], int]:
    """Betroffene Dokumente mit der höchsten Folgenummer, die sie betrifft; dazu die Zahl übergangener Ereignisse.

    ``bodies``: nur Ereignisse dieser Kommunen (Kennungen klein geschrieben); ``None`` = alle.
    Nichtöffentliche Ereignisse betreffen den Suchindex nicht (``relevant``).
    """
    ziele: dict[Target, int] = {}
    uebergangen = 0
    abhaengige: dict[str, dict[uuid.UUID, int]] = {}
    besitzer: dict[str, dict[uuid.UUID, int]] = {}

    def merken(ziel: Target, seq: int) -> None:
        if ziele.get(ziel, -1) < seq:
            ziele[ziel] = seq

    def vormerken(je_typ: dict[str, dict[uuid.UUID, int]], ereignis: Event, seq: int) -> None:
        objekte = je_typ.setdefault(ereignis.aggregate_type, {})
        objekte[ereignis.aggregate_id] = max(objekte.get(ereignis.aggregate_id, -1), seq)

    for ereignis in events:
        if not relevant(ereignis):
            continue
        if bodies is not None and (ereignis.body_id is None or str(ereignis.body_id) not in bodies):
            uebergangen += 1
            continue
        seq = _seq(ereignis)
        index = INDEX_BY_AGGREGATE.get(ereignis.aggregate_type)
        if index is not None:
            merken(Target(index, ereignis.aggregate_id), seq)
        if ereignis.aggregate_type in _PAPER_DEPENDENTS:
            nutzlast = ereignis.payload if isinstance(ereignis.payload, dict) else {}
            vorgang = _uuid(nutzlast.get("paper"))
            if vorgang is not None:
                merken(Target("papers", vorgang), seq)
            vormerken(abhaengige, ereignis, seq)
        if ereignis.aggregate_type in _FILE_OWNERS:
            vormerken(besitzer, ereignis, seq)

    # Der Vorgang laut Bestand (die Nutzlast nennt ihn nicht immer, etwa bei einer Rücknahme)
    for aggregate_type, objekte in abhaengige.items():
        for objekt, vorgang in related_paper_ids(aggregate_type, objekte).items():
            merken(Target("papers", vorgang), objekte[objekt])
    # Die Dateien eines Vorgangs bzw. einer Sitzung
    for aggregate_type, objekte in besitzer.items():
        for datei, objekt in related_file_ids(aggregate_type, objekte).items():
            merken(Target("files", datei), objekte[objekt])
    return ziele, uebergangen


# --- Schreiben ----------------------------------------------------------------------------------


def _aktion(operation: Operation) -> tuple[dict[str, Any], str | None]:
    kopf = {
        "_index": operation.index_name,
        "_id": operation.id,
        "version": operation.version,
        "version_type": "external",
    }
    if operation.document is None:
        return {"delete": kopf}, None
    return {"index": kopf}, json.dumps(operation.document, ensure_ascii=False, default=str)


def write(es: Any, operations: Iterable[Operation], tally: Tally) -> None:
    """Sendet die Operationen in Paketen (Anzahl und Größe begrenzt) und zählt das Ergebnis je Dokument."""
    paket: list[Any] = []
    anzahl = groesse = 0
    for operation in operations:
        kopf, quelle = _aktion(operation)
        paket.append(kopf)
        if quelle is not None:
            paket.append(quelle)
            groesse += len(quelle)
        anzahl += 1
        if anzahl >= BULK_ACTIONS or groesse >= BULK_BYTES:
            _senden(es, paket, tally)
            paket, anzahl, groesse = [], 0, 0
    if paket:
        _senden(es, paket, tally)


def _senden(es: Any, paket: list[Any], tally: Tally) -> None:
    antwort = es.bulk(operations=paket)
    for eintrag in antwort["items"]:
        aktion, info = next(iter(eintrag.items()))
        status = int(info.get("status", 0))
        if 200 <= status < 300:
            if aktion == "delete":
                tally.deleted += 1
            else:
                tally.indexed += 1
        elif aktion == "delete" and status == 404:
            tally.absent += 1  # war nicht (mehr) im Index
        elif status == 409:
            tally.stale += 1  # neuerer Stand schon im Index (externe Version)
        elif status in _UNAVAILABLE or status >= 500:
            tally.unavailable += 1
        else:
            tally.rejected += 1
            fehler = info.get("error") or {}
            # Nur Fehlerart, nie die Meldung: Sie kann Ausschnitte des Dokuments enthalten
            logger.warning(
                "Suchindex: Dokument abgelehnt (%s/%s, Status %s, %s)",
                info.get("_index"),
                info.get("_id"),
                status,
                fehler.get("type") if isinstance(fehler, dict) else "unbekannt",
            )


# --- Obergrenze des Schattenindex -----------------------------------------------------------------


def shadow_document_count(es: Any) -> int:
    """Dokumente in allen Schattenindizes."""
    return int(es.count(index=f"{SHADOW_PREFIX}*", allow_no_indices=True)["count"])


def _vorhandene(es: Any, name: str, ids: list[uuid.UUID]) -> set[str]:
    antwort = es.mget(index=name, ids=[str(kennung) for kennung in ids], source=False)
    return {doc["_id"] for doc in antwort["docs"] if doc.get("found")}


def _operationen(
    es: Any,
    ziele: Mapping[Target, int],
    *,
    shadow: bool,
    bodies: frozenset[str] | None,
    full: bool,
    tally: Tally,
) -> Iterator[Operation]:
    for index in INDEXES:
        ids = [ziel.id for ziel in ziele if ziel.index == index]
        if not ids:
            continue
        name = index_name(index, shadow=shadow)
        erlaubt = _vorhandene(es, name, ids) if full else None
        for kennung, dokument in iter_documents(index, ids):
            version = ziele[Target(index, kennung)]
            if dokument is None:
                yield Operation(name, str(kennung), version, None)
            elif bodies is not None and str(dokument.get("body_id") or "").lower() not in bodies:
                tally.skipped_body += 1
            elif erlaubt is not None and str(kennung) not in erlaubt:
                tally.skipped_limit += 1
            else:
                yield Operation(name, str(kennung), version, dokument)


# --- Handler ------------------------------------------------------------------------------------


def shadow_bodies() -> frozenset[str] | None:
    """Kommunen des Schattenbetriebs (``SEARCH_INDEX_SHADOW_BODIES``); ``None`` = alle."""
    kommunen = frozenset(str(kennung).lower() for kennung in settings.SEARCH_INDEX_SHADOW_BODIES)
    return kommunen or None


def _mit_ziel(aufruf: Callable[[], None]) -> None:
    """Führt ``aufruf`` aus; ein nicht erreichbares Elasticsearch wird zu ``TargetUnavailableError``."""
    try:
        aufruf()
    except TargetUnavailableError:
        raise
    except Exception as exc:
        if _unavailable(exc):
            raise TargetUnavailableError("Elasticsearch nicht erreichbar") from exc
        raise


def suchindex(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements ``suchindex`` (idempotent über die externe Version)."""
    shadow = delivery.shadow or settings.SEARCH_INDEX_SUBSCRIPTION != "aktiv"
    bodies = shadow_bodies() if shadow else None
    ziele, uebergangen = affected(events, bodies)
    tally = Tally(skipped_body=uebergangen)
    ziel = "schatten" if shadow else "live"
    if not ziele:
        tally.count_to(ziel)
        return

    def schreiben() -> None:
        es = client()
        full = False
        if shadow:
            if not es.indices.exists(index=",".join(shadow_name(index) for index in INDEXES)):
                ensure_shadow_indices(es)
            obergrenze = int(settings.SEARCH_INDEX_SHADOW_MAX_DOCS)
            full = bool(obergrenze) and shadow_document_count(es) >= obergrenze
        write(es, _operationen(es, ziele, shadow=shadow, bodies=bodies, full=full, tally=tally), tally)

    try:
        _mit_ziel(schreiben)
    finally:
        tally.count_to(ziel)
    if tally.skipped_limit:
        logger.warning(
            "Suchindex (Schatten): Obergrenze von %s Dokumenten erreicht, %s neue Dokumente nicht angelegt",
            settings.SEARCH_INDEX_SHADOW_MAX_DOCS,
            tally.skipped_limit,
        )
    if tally.unavailable:
        raise TargetUnavailableError("Elasticsearch überlastet oder nicht verfügbar")
    if tally.rejected:
        raise DocumentsRejectedError(f"{tally.rejected} Dokumente abgelehnt")
