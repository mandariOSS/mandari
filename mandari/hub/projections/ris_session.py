# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Projektor für Session-Mandanten (Issue #536): der RIS-Bestand aus den Ereignissen von mandari Session.

Heute gelangen Session-Daten auf zwei Wegen in den RIS-Bestand: Der Spiegel (``insight_sync.session_mirror``) bzw.
der Ingestor ruft die eigene OParl-Schnittstelle ab, und Session schreibt Rücknahmen und Nummern direkt hinein.
Künftig schreibt ihn nur dieses Abonnement (``docs/adr/20260929-ereignistechnik-postgres.md``, Punkt 7). Bis zum
Umschalten (Issue #537) läuft es im **Schattenbetrieb**: Es schreibt dieselben Zeilen in die Schatten-Quelle
(``hub.projections.models.RisSchatten``) neben dem Bestand, und ``manage.py ris_projektor_schatten vergleichen``
zeigt, ob die Ereignisse alles abdecken, was der Spiegel übernimmt.

**Eine Serialisierung.** Ein Ereignis ist nur Auslöser. Jedes Objekt entsteht aus dem **aktuellen** Stand in
Session mit der Abbildung der Schnittstelle (``hub.ris.mapping.session``, dieselben Querysets und dieselbe Auswahl
des Öffentlichen wie ``apps.session.api.oparl``), und daraus die Zeilen mit denselben Spalten wie beim Spiegel
(``hub.ris.uebernahme``). Was die Schnittstelle nicht ausliefert, gelangt nie in die Schatten-Quelle: keine
nichtöffentliche Sitzung (vom veröffentlichten Termin nur der Termin), kein nichtöffentlicher Tagesordnungspunkt,
keine Vorlage im Entwurf, keine Anlage ohne öffentliches Bezugsobjekt, nichts vor der Freischaltung der
Schnittstelle. Die Drehscheibe importiert Session nicht; Session reicht diese Sicht als ``Quelle`` herein
(``apps.session.ris_projektion``).

**Ablauf je Batch:**

1. Nur Ereignisse von Session-Mandanten (``session:<uuid>``) und nur der gewählten
   (``RIS_SESSION_PROJECTOR_TENANTS``, leer = alle); Ereignisse des Ingestors betreffen fremde Quellen. Einen
   Mandanten, den der Spiegel nicht übernimmt (nicht im Bürgerportal veröffentlicht), lässt er wie der Spiegel
   unverändert (``Quelle.uebernommen``).
2. Kennung des Ereignisses -> Objekt in Session (``Quelle.index``): Sitzung (auch ihr Ort und ihre Niederschrift),
   Tagesordnungspunkt (auch seine Abstimmung, über ``agenda_item`` der Nutzlast), Vorlage, Beratung, Anlage.
3. Jedes betroffene Objekt neu abbilden und wie der Spiegel zerlegen (``zerlegen``): Eine Sitzung bringt ihre
   öffentlichen Tagesordnungspunkte und Anlagen mit, eine Vorlage ihre Anlagen und Beratungen, eine Person ihre
   Mitgliedschaften. Liefert die Schnittstelle ein Objekt nicht (mehr) aus oder gibt es es nicht mehr, wird seine
   Zeile als gelöscht markiert – mit dem Grund aus ``ris.object.depublished``, sonst dem der Quelle.
4. Schreiben in derselben Transaktion wie der Cursor (``transactional=True``): Der Effekt tritt genau einmal ein, und
   eine Wiederholung schreibt denselben Stand noch einmal (Idempotenz).

**Meldungen.** Nach dem Umschalten meldet die Übernahme jede Änderung am Bestand als eigenes ``ris.*``-Ereignis in
derselben Transaktion, mit der Kennung des Bestands und der Kommune als ``body_id`` (Voraussetzung für Änderungsfeed
und Snapshot des Aggregators, Kommentar in #536). Dafür liefert das Schreiben je Zeile, was sich geändert hat
(``Uebernahme``); ``melden`` ist die Stelle, an der #537 die Ereignisse schreibt. Im Schatten meldet der Projektor
**nichts**: Die Schatten-Quelle ist kein Bestand, ihre Ereignisse erreichten sonst Suchindex, Änderungsfeed und
Benachrichtigungen. Er zählt nur (``mandari_ris_projector_rows_total``).

**Vollbau.** Ein neues Abonnement beginnt am Ende des Journals. Den Stand davor schreibt ``aufbauen`` einmal aus
Session (alle Listen der Schnittstelle in der Reihenfolge des Spiegels: Körperschaft mit Wahlperioden, Gremien,
Personen mit Mitgliedschaften, Sitzungen, Vorlagen). Für Körperschaft, Wahlperioden, Gremien, Personen und
Mitgliedschaften meldet Session noch keine Ereignisse; ihre Änderungen nach dem Vollbau zeigt der Vergleich.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, Protocol

from django.conf import settings
from django.db import transaction
from prometheus_client import Counter

from apps.events import Delivery
from apps.events.models import Event, Operation
from hub.ris import retraction, uebernahme
from hub.ris.canonical import Objekt

from .models import RisSchatten

logger = logging.getLogger(__name__)

NAME: Final = "ris.session_projektor"
#: Was Session meldet (``hub.ris.session_events``) und den Bestand ändern kann
TYPES: Final = (
    "ris.meeting.*",
    "ris.agendaitem.*",
    "ris.voting.*",
    "ris.resolution.*",
    "ris.protocol.*",
    "ris.paper.*",
    "ris.consultation.*",
    "ris.file.*",
    "ris.object.depublished",
)
BATCH: Final = 200
QUEUE: Final = "default"

AUS: Final = "aus"
SCHATTEN: Final = "schatten"
MODES: Final = (AUS, SCHATTEN)

#: Objekttyp des Journals -> Art der Session-Objekte, über deren Kennungen es aufgelöst wird
_ARTEN: Final[Mapping[str, str]] = {
    "Meeting": "meeting",
    "Location": "location",
    "AgendaItem": "agendaitem",
    "Voting": "agendaitem",
    "Paper": "paper",
    "Consultation": "consultation",
    "File": "file",
}
#: Reihenfolge der Abbildung in einem Batch – wie der inkrementelle Spiegel: erst Sitzungen und Vorlagen mit ihren
#: Einbettungen, dann die eigenen Listen der Tagesordnungspunkte, Beratungen und Anlagen
REIHENFOLGE: Final = ("meeting", "paper", "agendaitem", "consultation", "file")
#: Dateien, die eine Sitzung außer ihren Anlagen einbettet (Einladung, Niederschrift)
_SITZUNGSDATEIEN: Final = ("invitation", "resultsProtocol", "verbatimProtocol")

_EVENTS = Counter(
    "mandari_ris_projector_events_total",
    "Ereignisse am RIS-Projektor für Session-Mandanten nach Ergebnis",
    ["result"],
)
_ROWS = Counter(
    "mandari_ris_projector_rows_total",
    "Zeilen der Schatten-Quelle des RIS-Projektors nach Objekttyp und Ergebnis",
    ["type", "result"],
)


# =============================================================================
# Schalter
# =============================================================================


def modus() -> str:
    """Schalter ``RIS_SESSION_PROJECTOR``; Unbekanntes gilt als ``aus``."""
    wert = str(getattr(settings, "RIS_SESSION_PROJECTOR", AUS) or AUS).strip().lower()
    return wert if wert in MODES else AUS


def mandanten() -> frozenset[uuid.UUID] | None:
    """Gewählte Session-Mandanten (``RIS_SESSION_PROJECTOR_TENANTS``); ``None`` = alle."""
    gewaehlt = frozenset(uuid.UUID(str(wert)) for wert in getattr(settings, "RIS_SESSION_PROJECTOR_TENANTS", []) or [])
    return gewaehlt or None


def session_mandant(tenant_ref: str) -> uuid.UUID | None:
    """Kennung des Session-Mandanten aus ``session:<uuid>``; andere Erzeuger ``None``."""
    art, _, kennung = (tenant_ref or "").partition(":")
    if art != "session":
        return None
    try:
        return uuid.UUID(kennung)
    except ValueError:
        return None


# =============================================================================
# Quelle: Session-Objekte in der Abbildung der Schnittstelle
# =============================================================================


class Quelle(Protocol):
    """Was der Projektor von einem Session-Mandanten braucht (Session reicht es herein)."""

    @property
    def mandant(self) -> uuid.UUID:
        """Kennung des Session-Mandanten."""
        ...

    @property
    def uebernommen(self) -> bool:
        """
        Übernimmt der heutige Weg den Mandanten in den RIS-Bestand (Veröffentlichung im Bürgerportal bei
        freigeschalteter Schnittstelle)? Sonst bleibt auch die Schatten-Quelle, wie sie ist.
        """
        ...

    def kennung(self, adresse: str) -> uuid.UUID:
        """Kennung im RIS-Bestand zur Adresse eines Objekts (kanonisch, wie im Journal und beim Spiegel)."""
        ...

    def adresse(self, art: str, pk: Any) -> str:
        """Adresse des Session-Objekts ``pk`` der Art ``art`` in der Schnittstelle."""
        ...

    def kommune(self) -> str:
        """Adresse der Körperschaft der Schnittstelle (Bezug der Zeilen auf ihre Kommune)."""
        ...

    def index(self, arten: Collection[str]) -> dict[uuid.UUID, tuple[str, Any]]:
        """Kennung -> (Art, Schlüssel) aller vorhandenen Objekte der Arten (``location``: die Sitzung)."""
        ...

    def objekte(self, art: str, pks: Collection[Any]) -> dict[Any, Objekt]:
        """Die Objekte, die die Schnittstelle ausliefert, in ihrer Abbildung; alle übrigen fehlen."""
        ...

    def grund(self, art: str, pk: Any) -> str:
        """Grund, aus dem ein vorhandenes Objekt nicht (mehr) ausgeliefert wird (``hub.ris.retraction.REASONS``)."""
        ...

    def vollstaendig(self) -> Iterator[tuple[str, Objekt]]:
        """Alle ausgelieferten Objekte in der Reihenfolge des Spiegels (Vollbau)."""
        ...


QuelleFuer = Callable[[uuid.UUID], Quelle | None]


# =============================================================================
# Zerlegen: ein OParl-Objekt -> Zeilen des Bestands
# =============================================================================


@dataclass(frozen=True)
class Zeile:
    """Eine Zeile des Bestands: Typ, Adresse, Spalten und Bezüge (Adressen), vergleichbar normiert."""

    typ: str
    external_id: str
    spalten: dict[str, Any]
    verweise: dict[str, Any]
    oparl_modified: datetime | None


def wert(value: Any) -> Any:
    """
    Wert einer Spalte JSON-fähig und vergleichbar: Zeitpunkte in UTC, Daten als ``yyyy-mm-dd``; leerer Text, leere
    Listen und leere Objekte gelten als fehlend (wie bei ``hub.ris.canonical.clean``).
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str | list | dict) and not value:
        return None
    if isinstance(value, list):
        return [wert(eintrag) for eintrag in value]
    if isinstance(value, dict):
        return {schluessel: wert(eintrag) for schluessel, eintrag in value.items()}
    return value


def _zeile(typ: str, objekt: Mapping[str, Any], verweise: Mapping[str, Any]) -> Zeile | None:
    adresse = objekt.get("id")
    if not isinstance(adresse, str) or not adresse:
        return None
    spalten = {name: wert(inhalt) for name, inhalt in uebernahme.FUNKTIONEN[typ](objekt).items()}
    return Zeile(
        typ=typ,
        external_id=adresse,
        spalten=spalten,
        verweise={name: wert(inhalt) for name, inhalt in verweise.items()},
        oparl_modified=uebernahme.parse_dt(objekt.get("modified")),
    )


def _eingebettet(inhalt: Any) -> list[Mapping[str, Any]]:
    return [eintrag for eintrag in inhalt or [] if isinstance(eintrag, Mapping)] if isinstance(inhalt, list) else []


def _erste(inhalt: Any) -> str | None:
    """Erster Verweis einer Liste (bzw. der Verweis selbst), wie der Spiegel ihn für Anlagen nimmt."""
    if isinstance(inhalt, str):
        return inhalt or None
    if isinstance(inhalt, list):
        return next((eintrag for eintrag in inhalt if isinstance(eintrag, str) and eintrag), None)
    return None


def zerlegen(art: str, objekt: Mapping[str, Any], kommune: str) -> list[Zeile]:
    """
    Ein OParl-Objekt in die Zeilen zerlegen, die der Spiegel daraus in den Bestand übernimmt
    (``insight_sync.session_mirror``, ``_upsert_*``): dieselben Einbettungen und dieselben Bezüge.

    ``kommune`` ist die Adresse der Körperschaft; der Spiegel ordnet jede Zeile der Kommune zu, deren Listen er
    gerade liest. Eine Anlage aus der eigenen Liste übernimmt er nur mit Vorlage oder Sitzung (Anlagen allein an
    einem Tagesordnungspunkt nicht).
    """
    zeilen: list[Zeile] = []

    def hinzu(typ: str, eintrag: Mapping[str, Any], **verweise: Any) -> None:
        zeile = _zeile(typ, eintrag, verweise)
        if zeile is not None:
            zeilen.append(zeile)

    adresse = objekt.get("id")
    if art == "body":
        hinzu("body", objekt)
        for term in _eingebettet(objekt.get("legislativeTerm")):
            hinzu("legislativeterm", term, body=kommune)
    elif art in ("organization", "legislativeterm"):
        hinzu(art, objekt, body=kommune)
    elif art == "person":
        hinzu("person", objekt, body=kommune)
        for mitgliedschaft in _eingebettet(objekt.get("membership")):
            hinzu("membership", mitgliedschaft, person=adresse, organization=mitgliedschaft.get("organization"))
    elif art == "membership":
        hinzu("membership", objekt, person=objekt.get("person"), organization=objekt.get("organization"))
    elif art == "meeting":
        gremien = sorted({ref for ref in objekt.get("organization") or [] if isinstance(ref, str)})
        hinzu("meeting", objekt, body=kommune, organizations=gremien)
        for punkt in _eingebettet(objekt.get("agendaItem")):
            hinzu("agendaitem", punkt, meeting=adresse)
        dateien = _eingebettet(objekt.get("auxiliaryFile"))
        dateien += [objekt[name] for name in _SITZUNGSDATEIEN if isinstance(objekt.get(name), Mapping)]
        for datei in dateien:
            hinzu("file", datei, body=kommune, paper=None, meeting=adresse)
    elif art == "agendaitem":
        hinzu("agendaitem", objekt, meeting=objekt.get("meeting"))
    elif art == "paper":
        hinzu("paper", objekt, body=kommune)
        haupt = objekt.get("mainFile")
        dateien = ([haupt] if isinstance(haupt, Mapping) else []) + _eingebettet(objekt.get("auxiliaryFile"))
        for datei in dateien:
            hinzu("file", datei, body=kommune, paper=adresse, meeting=None)
        for beratung in _eingebettet(objekt.get("consultation")):
            hinzu("consultation", beratung, body=kommune, paper=adresse)
    elif art == "consultation":
        hinzu("consultation", objekt, body=kommune, paper=objekt.get("paper"))
    elif art == "file":
        vorlage, sitzung = _erste(objekt.get("paper")), _erste(objekt.get("meeting"))
        if vorlage is not None or sitzung is not None:
            hinzu("file", objekt, body=kommune, paper=vorlage, meeting=sitzung)
    else:
        raise ValueError("Unbekannte Art eines Session-Objekts.")
    return zeilen


# =============================================================================
# Schreiben in die Schatten-Quelle
# =============================================================================


@dataclass(frozen=True)
class Uebernahme:
    """
    Eine Änderung an einer Zeile – nach dem Umschalten (Issue #537) der Anlass eines ``ris.*``-Ereignisses.

    ``neu``: Die Zeile gab es nicht oder sie war gelöscht. ``geaendert``: Namen der geänderten Spalten und Bezüge.
    """

    typ: str
    kennung: uuid.UUID
    operation: str
    neu: bool = False
    geaendert: tuple[str, ...] = ()
    grund: str | None = None


def unterschiede(
    spalten_a: Mapping[str, Any],
    verweise_a: Mapping[str, Any],
    spalten_b: Mapping[str, Any],
    verweise_b: Mapping[str, Any],
) -> tuple[str, ...]:
    """Namen der Spalten und Bezüge, in denen sich zwei Zeilen unterscheiden (fehlend gilt wie ``None``)."""
    geaendert = {name for name in set(spalten_a) | set(spalten_b) if spalten_a.get(name) != spalten_b.get(name)}
    geaendert |= {name for name in set(verweise_a) | set(verweise_b) if verweise_a.get(name) != verweise_b.get(name)}
    return tuple(sorted(geaendert))


def _unterschiede(alt: RisSchatten, zeile: Zeile) -> tuple[str, ...]:
    return unterschiede(alt.spalten, alt.verweise, zeile.spalten, zeile.verweise)


class Schatten:
    """Schreibt Zeilen eines Mandanten in die Schatten-Quelle (in der laufenden Transaktion)."""

    def __init__(self, mandant: uuid.UUID, kennung: Callable[[str], uuid.UUID], seq: int | None = None) -> None:
        self.mandant = mandant
        self.kennung = kennung
        self.seq = seq

    def schreiben(self, zeilen: Iterable[Zeile]) -> list[Uebernahme]:
        """Zeilen anlegen bzw. ändern; bei gleicher Adresse gilt die letzte. Unveränderte bleiben unberührt."""
        je_kennung: dict[uuid.UUID, Zeile] = {}
        for zeile in zeilen:
            je_kennung[self.kennung(zeile.external_id)] = zeile
        if not je_kennung:
            return []
        vorhanden = {
            zeile.bestand_id: zeile
            for zeile in RisSchatten.objects.filter(mandant=self.mandant, bestand_id__in=list(je_kennung))
        }
        ergebnis: list[Uebernahme] = []
        schreiben: list[RisSchatten] = []
        for kennung, zeile in je_kennung.items():
            alt = vorhanden.get(kennung)
            if alt is None or alt.deleted:
                ergebnis.append(Uebernahme(zeile.typ, kennung, Operation.UPSERT, neu=True))
            else:
                geaendert = _unterschiede(alt, zeile)
                if geaendert:
                    ergebnis.append(Uebernahme(zeile.typ, kennung, Operation.UPSERT, geaendert=geaendert))
                elif alt.oparl_modified == zeile.oparl_modified and alt.external_id == zeile.external_id:
                    _ROWS.labels(zeile.typ, "unveraendert").inc()
                    continue
            schreiben.append(
                RisSchatten(
                    mandant=self.mandant,
                    typ=zeile.typ,
                    bestand_id=kennung,
                    external_id=zeile.external_id,
                    spalten=zeile.spalten,
                    verweise=zeile.verweise,
                    deleted=False,
                    deletion_reason=None,
                    oparl_modified=zeile.oparl_modified,
                    seq=self.seq,
                )
            )
        RisSchatten.objects.bulk_create(
            schreiben,
            update_conflicts=True,
            unique_fields=["mandant", "bestand_id"],
            update_fields=[
                "typ",
                "external_id",
                "spalten",
                "verweise",
                "deleted",
                "deletion_reason",
                "oparl_modified",
                "seq",
                "updated_at",
            ],
        )
        return ergebnis

    def entfernen(self, gruende: Mapping[uuid.UUID, str]) -> list[Uebernahme]:
        """
        Zeilen als gelöscht markieren, mit Grund; ohne Spalten und Bezüge (nichts Zurückgenommenes bleibt stehen).

        Wie die Rücknahme im Bestand (``hub.ris.retraction.retract``) gilt der erste Grund: Eine schon gelöschte Zeile
        bleibt, wie sie ist. Eine Zeile, die es nicht gibt, wird nicht angelegt.
        """
        if not gruende:
            return []
        zeilen = list(RisSchatten.objects.filter(mandant=self.mandant, bestand_id__in=list(gruende), deleted=False))
        for zeile in zeilen:
            zeile.deleted = True
            zeile.deletion_reason = gruende[zeile.bestand_id]
            zeile.spalten = {}
            zeile.verweise = {}
            zeile.seq = self.seq
        RisSchatten.objects.bulk_update(zeilen, ["deleted", "deletion_reason", "spalten", "verweise", "seq"])
        return [
            Uebernahme(zeile.typ, zeile.bestand_id, Operation.DELETE, grund=zeile.deletion_reason) for zeile in zeilen
        ]


def melden(uebernahmen: Iterable[Uebernahme]) -> int:
    """
    Änderungen der Übernahme melden; Zahl der geschriebenen Ereignisse.

    Im Schattenbetrieb meldet der Projektor nichts und zählt nur. Mit dem Umschalten (Issue #537) schreibt er hier
    je Änderung am Bestand ein ``ris.*``-Ereignis in derselben Transaktion – Kennung des Bestands, Quelle als
    Mandant (``source:<uuid>``), Kommune als ``body_id``, wie ``hub.ris.retraction`` und der Ingestor
    (``ingestor/src/storage/ris_events.py``).
    """
    for eintrag in uebernahmen:
        art = "neu" if eintrag.neu else "geaendert"
        _ROWS.labels(eintrag.typ, "geloescht" if eintrag.operation == Operation.DELETE else art).inc()
    return 0


# =============================================================================
# Zustellung
# =============================================================================


def _voting_punkt(event: Event) -> uuid.UUID | None:
    """Kennung des Tagesordnungspunkts einer Abstimmung (Nutzlast ``agenda_item``)."""
    ref = (event.payload or {}).get("agenda_item")
    try:
        return uuid.UUID(str(ref)) if ref else None
    except ValueError:
        return None


def projizieren(quelle: Quelle, events: Sequence[Event], *, seq: int | None = None) -> list[Uebernahme]:
    """Die Objekte, die die Ereignisse eines Mandanten nennen, neu abbilden und in die Schatten-Quelle schreiben."""
    gruende: dict[uuid.UUID, str] = {}
    kennungen: list[uuid.UUID] = []
    for event in events:
        if event.type == retraction.DEPUBLISHED:
            grund = (event.payload or {}).get("reason")
            if grund in retraction.REASONS:
                gruende.setdefault(event.aggregate_id, str(grund))
        kennung = _voting_punkt(event) if event.aggregate_type == "Voting" else event.aggregate_id
        if event.aggregate_type in _ARTEN and kennung is not None:
            kennungen.append(kennung)
        else:
            _EVENTS.labels("ohne_objekt").inc()

    arten = {_ARTEN[event.aggregate_type] for event in events if event.aggregate_type in _ARTEN}
    index = quelle.index(arten) if kennungen else {}
    ziele: dict[str, set[Any]] = {}
    weg: set[uuid.UUID] = set()
    for kennung in kennungen:
        treffer = index.get(kennung)
        if treffer is None:
            weg.add(kennung)
        else:
            ziele.setdefault(treffer[0], set()).add(treffer[1])

    zeilen: list[Zeile] = []
    entfernen: dict[uuid.UUID, str] = {}
    kommune = quelle.kommune()
    for art in REIHENFOLGE:
        pks = ziele.get(art)
        if not pks:
            continue
        objekte = quelle.objekte(art, pks)
        for pk in sorted(pks, key=str):
            objekt = objekte.get(pk)
            if objekt is not None:
                zeilen.extend(zerlegen(art, objekt, kommune))
                continue
            kennung = quelle.kennung(quelle.adresse(art, pk))
            entfernen[kennung] = gruende.get(kennung) or quelle.grund(art, pk)
    for kennung in weg:
        # Gibt es das Objekt in Session nicht mehr, entfernt das die Zeile, sofern es eine gibt
        entfernen.setdefault(kennung, gruende.get(kennung, retraction.REASON_DELETED_AT_SOURCE))

    schatten = Schatten(quelle.mandant, quelle.kennung, seq)
    ergebnis = schatten.schreiben(zeilen)
    geschrieben = {quelle.kennung(zeile.external_id) for zeile in zeilen}
    ergebnis += schatten.entfernen({k: g for k, g in entfernen.items() if k not in geschrieben})
    return ergebnis


def verarbeiten(events: list[Event], delivery: Delivery, quelle_fuer: QuelleFuer) -> None:
    """
    Handler des Abonnements (Session bindet ``quelle_fuer``): Ereignisse je Mandant projizieren.

    Es gibt vorerst nur das Schattenziel; auch ein Abonnement, das in der Datenbank auf ``aktiv`` steht, schreibt
    nur die Schatten-Quelle, bis das Umschalten (Issue #537) den Bestand als Ziel bringt.
    """
    gewaehlt = mandanten()
    je_mandant: dict[uuid.UUID, list[Event]] = {}
    for event in events:
        mandant = session_mandant(event.tenant_ref)
        if mandant is None:
            _EVENTS.labels("anderer_erzeuger").inc()
        elif gewaehlt is not None and mandant not in gewaehlt:
            _EVENTS.labels("nicht_gewaehlt").inc()
        else:
            je_mandant.setdefault(mandant, []).append(event)
    for mandant, liste in je_mandant.items():
        quelle = quelle_fuer(mandant)
        if quelle is None:
            _EVENTS.labels("mandant_unbekannt").inc(len(liste))
            continue
        if not quelle.uebernommen:
            # Der Spiegel übernimmt den Mandanten nicht (nicht im Bürgerportal veröffentlicht): Bestand und
            # Schatten-Quelle bleiben unverändert
            _EVENTS.labels("nicht_veroeffentlicht").inc(len(liste))
            continue
        seq = max((event.seq for event in liste if event.seq is not None), default=None)
        melden(projizieren(quelle, liste, seq=seq))
        _EVENTS.labels("verarbeitet").inc(len(liste))
        delivery.alive()


# =============================================================================
# Vollbau
# =============================================================================


@dataclass
class Vollbau:
    """Ergebnis eines Vollbaus: Zeilen je Typ."""

    mandant: uuid.UUID
    zeilen: dict[str, int]
    trocken: bool

    @property
    def gesamt(self) -> int:
        return sum(self.zeilen.values())


def aufbauen(quelle: Quelle, *, trocken: bool = False, stapel: int = 500) -> Vollbau:
    """
    Schatten-Quelle eines Mandanten einmal vollständig aus Session bauen (vorher seine Zeilen entfernen).

    Geschrieben wird in Stapeln, je Stapel eine Transaktion: Ein großer Mandant hält so weder Sperren noch Speicher
    lange fest. Läuft das Abonnement gleichzeitig, schreiben beide den aktuellen Stand; was zuletzt kommt, gilt.
    """
    zaehler: dict[str, int] = {}
    gesehen: set[str] = set()
    if not trocken:
        RisSchatten.objects.filter(mandant=quelle.mandant).delete()
    schatten = Schatten(quelle.mandant, quelle.kennung)
    kommune = quelle.kommune()
    puffer: list[Zeile] = []

    def leeren() -> None:
        for zeile in puffer:
            # Eine Anlage kann zweimal vorkommen (an der Sitzung und an der Vorlage); gezählt wird sie einmal
            if zeile.external_id not in gesehen:
                gesehen.add(zeile.external_id)
                zaehler[zeile.typ] = zaehler.get(zeile.typ, 0) + 1
        if not trocken:
            with transaction.atomic():
                schatten.schreiben(puffer)
        puffer.clear()

    for art, objekt in quelle.vollstaendig():
        puffer.extend(zerlegen(art, objekt, kommune))
        if len(puffer) >= stapel:
            leeren()
    leeren()
    return Vollbau(mandant=quelle.mandant, zeilen=dict(sorted(zaehler.items())), trocken=trocken)
