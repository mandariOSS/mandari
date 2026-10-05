# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schattenbetrieb des Suchindex: Vollbau, Vergleich mit dem Live-Index, Stand und Aufräumen (Issue #526).

Bedient wird das von ``manage.py suchindex_schatten``; die Funktionen hier geben Daten zurück, die
Ausgabe macht der Befehl.

- ``build``: baut den Schattenindex für die gewählten Kommunen einmal aus dem Bestand, mit externer
  Version gleich der höchsten Folgenummer **vor** dem Lesen. Danach eintreffende Ereignisse haben
  höhere Folgenummern und gewinnen; ältere verlieren gegen den Vollbau. Die Obergrenze prüft der
  Plan (``plan_build``) gegen den Schattenindex **danach**: was schon darin liegt und der Vollbau nicht
  ersetzt (andere Kommunen bzw. Indizes), plus die Dokumente des Vollbaus.
- ``compare``: je Index und Kommune die Zahl der Dokumente im Bestand (Datenbank), im Live- und im
  Schattenindex, fehlende und überzählige Dokumente des Schattenindex und eine Stichprobe gemeinsamer
  Dokumente mit den Feldern, die abweichen. Dazu, ob die Abbildungen (Felder) gleich sind. Weicht ein
  Feld ab, entscheidet der aktuelle Bestand, welche Seite veraltet ist (``schatten_veraltet``): Der
  Live-Index zieht abhängige Felder (Kontext von Dateien, Gremien) nur beim Speichern in Django nach,
  das Abonnement mit jedem Ereignis (Issue #821). Veraltet ist der Schatten nur bei offenem Rückstand
  oder einer echten Lücke im Abonnement.
- ``status``: Schalter, Abonnement (Zustand, Cursor, Rückstand, geparkte Ereignisse) und Größe der
  Schattenindizes samt Speicher von Elasticsearch.
- ``drop``: löscht die Schattenindizes, auf Wunsch auch das Abonnement (Cursor und Geparktes). Ob das
  gerade geht, sagen ``writes_shadow`` und ``subscription_running``; der Befehl hält den Eingriff im
  Sicherheitsprotokoll fest.
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Now

from apps.events.dispatch import head_seq
from apps.events.models import Event, ParkedEvent, Subscription, SubscriptionState
from apps.events.registry import type_filter
from insight_core.services.search_projection import count_documents, iter_all_documents, iter_documents
from insight_search import abonnement
from insight_search.indices import INDEXES, ensure_shadow_indices, shadow_name

#: Kennungen je Seite beim Lesen aller Dokumente einer Kommune
_SEITE: Final = 2000
_SCROLL: Final = "2m"
#: Höchstens so viele Kommunen ermittelt der Vergleich aus dem Schattenindex selbst
_MAX_KOMMUNEN: Final = 1000


# --- Stand --------------------------------------------------------------------------------------


@dataclass
class SubscriptionStatus:
    vorhanden: bool
    zustand: str | None = None
    cursor: int | None = None
    hoechste_folgenummer: int = 0
    offen: int = 0
    rueckstand_sekunden: float = 0.0
    geparkt: dict[str, int] = field(default_factory=dict)


@dataclass
class IndexStatus:
    index: str
    name: str
    vorhanden: bool
    dokumente: int = 0
    bytes: int = 0


@dataclass
class Status:
    schalter: str
    kommunen: list[str]
    obergrenze: int
    abonnement: SubscriptionStatus
    indizes: list[IndexStatus] = field(default_factory=list)
    heap_belegt_bytes: int | None = None
    heap_max_bytes: int | None = None
    fehler: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def subscription_status() -> SubscriptionStatus:
    """Zustand, Cursor und Rückstand des Abonnements ``suchindex`` (auch wenn es nicht registriert ist)."""
    hoechste = head_seq()
    zeile = (
        Subscription.objects.filter(name=abonnement.NAME)
        .annotate(jetzt=Now())
        .values_list("state", "cursor_seq", "jetzt")
        .first()
    )
    if zeile is None:
        return SubscriptionStatus(vorhanden=False, hoechste_folgenummer=hoechste)
    zustand, cursor, jetzt = zeile
    offen = Event.objects.filter(seq__gt=cursor).filter(type_filter(abonnement.TYPES))
    aeltestes: datetime | None = offen.order_by("seq").values_list("recorded_at", flat=True).first()
    geparkt = {
        str(state): int(anzahl)
        for state, anzahl in ParkedEvent.objects.filter(subscription=abonnement.NAME)
        .values_list("state")
        .annotate(anzahl=Count("id"))
        .order_by()
    }
    return SubscriptionStatus(
        vorhanden=True,
        zustand=str(zustand),
        cursor=int(cursor),
        hoechste_folgenummer=hoechste,
        offen=offen.count(),
        rueckstand_sekunden=max((jetzt - aeltestes).total_seconds(), 0.0) if aeltestes is not None else 0.0,
        geparkt=geparkt,
    )


def status(es: Any) -> Status:
    """Stand von Schalter, Abonnement und Schattenindizes; Elasticsearch-Fehler landen in ``fehler``."""
    ergebnis = Status(
        schalter=settings.SEARCH_INDEX_SUBSCRIPTION,
        kommunen=list(settings.SEARCH_INDEX_SHADOW_BODIES),
        obergrenze=int(settings.SEARCH_INDEX_SHADOW_MAX_DOCS),
        abonnement=subscription_status(),
    )
    try:
        for index in INDEXES:
            name = shadow_name(index)
            if not es.indices.exists(index=name):
                ergebnis.indizes.append(IndexStatus(index=index, name=name, vorhanden=False))
                continue
            dokumente = int(es.count(index=name)["count"])
            stats = es.indices.stats(index=name, metric="store")
            groesse = int(stats["_all"]["primaries"]["store"]["size_in_bytes"])
            ergebnis.indizes.append(
                IndexStatus(index=index, name=name, vorhanden=True, dokumente=dokumente, bytes=groesse)
            )
        knoten = es.nodes.stats(metric="jvm")["nodes"]
        ergebnis.heap_belegt_bytes = sum(int(k["jvm"]["mem"]["heap_used_in_bytes"]) for k in knoten.values())
        ergebnis.heap_max_bytes = sum(int(k["jvm"]["mem"]["heap_max_in_bytes"]) for k in knoten.values())
    except Exception as exc:  # noqa: BLE001 – der Stand der Datenbank soll trotzdem erscheinen
        ergebnis.fehler = type(exc).__name__
    return ergebnis


# --- Vollbau ------------------------------------------------------------------------------------


@dataclass
class BuildPlan:
    kommunen: list[uuid.UUID]
    indizes: list[str]
    dokumente: dict[str, int]
    version: int
    #: Schattendokumente, die der Vollbau nicht ersetzt (andere Kommunen bzw. Indizes)
    bleibend: int = 0

    @property
    def gesamt(self) -> int:
        return sum(self.dokumente.values())

    @property
    def danach(self) -> int:
        """Ungefähre Zahl aller Schattendokumente nach dem Vollbau (gegen die Obergrenze geprüft)."""
        return self.bleibend + self.gesamt


def remaining_shadow_documents(es: Any, kommunen: Iterable[uuid.UUID], indizes: Iterable[str]) -> int:
    """Schattendokumente, die ein Vollbau dieser Auswahl nicht ersetzt.

    Alle Schattendokumente abzüglich derer der gewählten Kommunen in den gewählten Indizes (ohne
    Kommunen: alle Dokumente dieser Indizes). Was der Vollbau ersetzt, zählt so nicht doppelt; Dokumente
    der Auswahl, die es im Bestand nicht mehr gibt, bleiben zwar liegen, zählen hier aber nicht mit.
    """
    kennungen = [str(kommune) for kommune in kommunen]
    ersetzt = 0
    for index in indizes:
        name = shadow_name(index)
        if not es.indices.exists(index=name):
            continue
        if kennungen:
            ersetzt += int(es.count(index=name, query={"terms": {"body_id": kennungen}})["count"])
        else:
            ersetzt += int(es.count(index=name)["count"])
    return max(abonnement.shadow_document_count(es) - ersetzt, 0)


def plan_build(es: Any, kommunen: Iterable[uuid.UUID], indizes: Iterable[str]) -> BuildPlan:
    """Zählt, was der Vollbau schreiben würde und was danach im Schattenindex liegt.

    Die Version ist die höchste Folgenummer **vor** dem Lesen.
    """
    version = head_seq()
    liste = list(kommunen)
    gewaehlt = list(indizes)
    return BuildPlan(
        kommunen=liste,
        indizes=gewaehlt,
        dokumente={index: count_documents(index, liste) for index in gewaehlt},
        version=version,
        bleibend=remaining_shadow_documents(es, liste, gewaehlt),
    )


def build(es: Any, plan: BuildPlan) -> dict[str, abonnement.Tally]:
    """Schreibt die Dokumente des Plans in den Schattenindex (externe Version ``plan.version``)."""
    ensure_shadow_indices(es)
    ergebnis: dict[str, abonnement.Tally] = {}
    for index in plan.indizes:
        tally = abonnement.Tally()
        name = shadow_name(index)
        operationen = (
            abonnement.Operation(name, str(dokument["id"]), plan.version, dokument)
            for dokument in iter_all_documents(index, plan.kommunen)
        )
        abonnement.write(es, operationen, tally)
        tally.count_to("schatten")
        ergebnis[index] = tally
    return ergebnis


# --- Vergleich ----------------------------------------------------------------------------------


@dataclass
class BodyComparison:
    kommune: str
    bestand: int
    live: int
    schatten: int
    fehlt: int
    ueberzaehlig: int
    stichprobe: int
    abweichend: int
    felder: dict[str, int]
    beispiele_fehlt: list[str]
    beispiele_ueberzaehlig: list[str]
    beispiele_abweichend: list[str]
    #: Abweichende Felder, in denen der Schatten nicht dem aktuellen Bestand entspricht (sonst ist der
    #: Live-Index veraltet); ``BESTAND_FEHLT``: Das Dokument gehört nicht mehr in den Index
    schatten_veraltet: dict[str, int] = field(default_factory=dict)

    @property
    def gleich(self) -> bool:
        return not (self.fehlt or self.ueberzaehlig or self.abweichend)


@dataclass
class IndexComparison:
    index: str
    abbildung_abweichend: list[str]
    kommunen: list[BodyComparison] = field(default_factory=list)


def ids(es: Any, name: str, kommune: str) -> set[str]:
    """Alle Kennungen einer Kommune in einem Index (Scroll, ohne Inhalte)."""
    if not es.indices.exists(index=name):
        return set()
    gefunden: set[str] = set()
    antwort = es.search(
        index=name,
        query={"term": {"body_id": kommune}},
        source=False,
        size=_SEITE,
        sort=["_doc"],
        scroll=_SCROLL,
    )
    scroll_id = antwort.get("_scroll_id")
    try:
        while antwort["hits"]["hits"]:
            gefunden.update(treffer["_id"] for treffer in antwort["hits"]["hits"])
            if not scroll_id:
                break
            antwort = es.scroll(scroll_id=scroll_id, scroll=_SCROLL)
            scroll_id = antwort.get("_scroll_id") or scroll_id
    finally:
        if scroll_id:
            es.clear_scroll(scroll_id=scroll_id)
    return gefunden


def bodies_in(es: Any, name: str) -> list[str]:
    """Kommunen mit Dokumenten in einem Index (Aggregation über ``body_id``)."""
    if not es.indices.exists(index=name):
        return []
    antwort = es.search(index=name, size=0, aggs={"kommunen": {"terms": {"field": "body_id", "size": _MAX_KOMMUNEN}}})
    return [str(eintrag["key"]) for eintrag in antwort["aggregations"]["kommunen"]["buckets"]]


def _normalisiert(wert: Any) -> Any:
    """Listen einfacher Werte ohne Reihenfolge vergleichen (Gremien kommen ungeordnet aus der Datenbank)."""
    if isinstance(wert, list) and all(isinstance(teil, str | int | float | bool) or teil is None for teil in wert):
        return sorted(wert, key=lambda teil: (teil is None, str(teil)))
    if isinstance(wert, dict):
        return {schluessel: _normalisiert(teil) for schluessel, teil in wert.items()}
    return wert


def differing_fields(live: dict[str, Any], schatten: dict[str, Any]) -> list[str]:
    """Felder, deren Wert abweicht oder nur auf einer Seite steht (sortiert)."""
    return sorted(
        schluessel
        for schluessel in live.keys() | schatten.keys()
        if _normalisiert(live.get(schluessel)) != _normalisiert(schatten.get(schluessel))
        or (schluessel in live) != (schluessel in schatten)
    )


#: Feldname in ``schatten_veraltet`` für ein Dokument, das nicht mehr in den Index gehört
BESTAND_FEHLT: Final = "(nicht im Bestand)"


def _bestand(index: str, kennungen: Iterable[str]) -> dict[str, dict[str, Any] | None]:
    """Aktuelle Dokumente aus dem Bestand (wie das Abonnement sie baut); ``None``: gehört nicht in den Index."""
    gueltig: list[uuid.UUID] = []
    for kennung in kennungen:
        try:
            gueltig.append(uuid.UUID(kennung))
        except ValueError:
            continue
    return {str(kennung): dokument for kennung, dokument in iter_documents(index, gueltig)}


def stale_shadow_fields(live: dict[str, Any], schatten: dict[str, Any], bestand: dict[str, Any] | None) -> list[str]:
    """Abweichende Felder, in denen der Schatten nicht dem aktuellen Bestand entspricht."""
    if bestand is None:
        return [BESTAND_FEHLT]
    return [
        feld
        for feld in differing_fields(live, schatten)
        if _normalisiert(schatten.get(feld)) != _normalisiert(bestand.get(feld))
        or (feld in schatten) != (feld in bestand)
    ]


def _quellen(es: Any, name: str, kennungen: list[str]) -> dict[str, dict[str, Any]]:
    if not kennungen:
        return {}
    antwort = es.mget(index=name, ids=kennungen)
    return {doc["_id"]: doc.get("_source") or {} for doc in antwort["docs"] if doc.get("found")}


def _mappings(es: Any, name: str) -> dict[str, Any]:
    if not es.indices.exists(index=name):
        return {}
    antwort: dict[str, Any] = dict(es.indices.get_mapping(index=name))
    eintrag: dict[str, Any] = next(iter(antwort.values()), {})
    return dict(eintrag.get("mappings", {}).get("properties", {}))


def mapping_differences(es: Any, index: str) -> list[str]:
    """Felder, deren Abbildung zwischen Live- und Schattenindex abweicht (sortiert)."""
    live = _mappings(es, index)
    schatten = _mappings(es, shadow_name(index))
    if not live or not schatten:
        return []
    return sorted(feld for feld in live.keys() | schatten.keys() if live.get(feld) != schatten.get(feld))


def compare_body(
    es: Any, index: str, kommune: str, *, sample: int, examples: int, rng: random.Random
) -> BodyComparison:
    """Vergleich eines Index für eine Kommune."""
    live = ids(es, index, kommune)
    schatten = ids(es, shadow_name(index), kommune)
    fehlt = sorted(live - schatten)
    ueberzaehlig = sorted(schatten - live)
    gemeinsam = sorted(live & schatten)
    auswahl = rng.sample(gemeinsam, min(sample, len(gemeinsam)))
    live_quellen = _quellen(es, index, auswahl)
    schatten_quellen = _quellen(es, shadow_name(index), auswahl)
    felder: Counter[str] = Counter()
    abweichend: list[str] = []
    for kennung in auswahl:
        unterschiede = differing_fields(live_quellen.get(kennung, {}), schatten_quellen.get(kennung, {}))
        if unterschiede:
            abweichend.append(kennung)
            felder.update(unterschiede)
    veraltet: Counter[str] = Counter()
    bestand_quellen = _bestand(index, abweichend) if abweichend else {}
    for kennung in abweichend:
        veraltet.update(
            stale_shadow_fields(
                live_quellen.get(kennung, {}), schatten_quellen.get(kennung, {}), bestand_quellen.get(kennung)
            )
        )
    try:
        bestand = count_documents(index, [uuid.UUID(kommune)])
    except ValueError:
        bestand = 0
    return BodyComparison(
        kommune=kommune,
        bestand=bestand,
        live=len(live),
        schatten=len(schatten),
        fehlt=len(fehlt),
        ueberzaehlig=len(ueberzaehlig),
        stichprobe=len(auswahl),
        abweichend=len(abweichend),
        felder=dict(sorted(felder.items())),
        beispiele_fehlt=fehlt[:examples],
        beispiele_ueberzaehlig=ueberzaehlig[:examples],
        beispiele_abweichend=sorted(abweichend)[:examples],
        schatten_veraltet=dict(sorted(veraltet.items())),
    )


def compare(
    es: Any,
    *,
    indizes: Iterable[str] = INDEXES,
    kommunen: Iterable[str] = (),
    sample: int = 20,
    examples: int = 5,
    seed: int | None = None,
) -> Iterator[IndexComparison]:
    """Vergleich je Index und Kommune.

    Ohne gewählte Kommunen die aus ``SEARCH_INDEX_SHADOW_BODIES``. Ist auch die leer, gehören alle
    Kommunen in den Schattenindex; verglichen werden dann die aus Live- **und** Schattenindex, damit
    auch eine Kommune auffällt, die im Schattenindex ganz fehlt.
    """
    rng = random.Random(seed)  # noqa: S311 – Stichprobe, kein Geheimnis
    gewaehlt = [str(kommune).lower() for kommune in kommunen] or list(settings.SEARCH_INDEX_SHADOW_BODIES)
    for index in indizes:
        vergleich = IndexComparison(index=index, abbildung_abweichend=mapping_differences(es, index))
        alle = [] if gewaehlt else sorted(set(bodies_in(es, index)) | set(bodies_in(es, shadow_name(index))))
        for kommune in gewaehlt or alle:
            vergleich.kommunen.append(compare_body(es, index, kommune, sample=sample, examples=examples, rng=rng))
        yield vergleich


# --- Aufräumen ----------------------------------------------------------------------------------


def subscription_running(stand: SubscriptionStatus) -> bool:
    """Wird das Abonnement zugestellt (laut Schalter registriert, angelegt und nicht pausiert)?"""
    return (
        settings.SEARCH_INDEX_SUBSCRIPTION != "aus" and stand.vorhanden and stand.zustand != SubscriptionState.PAUSIERT
    )


def writes_shadow(stand: SubscriptionStatus) -> bool:
    """Schreibt das Abonnement in den Schattenindex (Schalter ``schatten`` oder Zustand ``schatten``)?"""
    return subscription_running(stand) and (
        settings.SEARCH_INDEX_SUBSCRIPTION == "schatten" or stand.zustand == SubscriptionState.SCHATTEN
    )


@dataclass
class DropResult:
    indizes: list[str]
    abonnement_entfernt: bool = False
    cursor: int | None = None
    geparkt_entfernt: int = 0


def drop(es: Any, *, subscription: bool) -> DropResult:
    """Löscht die Schattenindizes und auf Wunsch das Abonnement (Cursor und geparkte Ereignisse).

    Das Abonnement wird in der laufenden Transaktion entfernt: Der Aufrufer schreibt den Eintrag im
    Sicherheitsprotokoll in derselben Transaktion.
    """
    geloescht = [shadow_name(index) for index in INDEXES if es.indices.exists(index=shadow_name(index))]
    if geloescht:
        es.indices.delete(index=",".join(geloescht))
    ergebnis = DropResult(indizes=geloescht)
    if subscription:
        with transaction.atomic():
            ergebnis.cursor = (
                Subscription.objects.filter(name=abonnement.NAME).values_list("cursor_seq", flat=True).first()
            )
            ergebnis.geparkt_entfernt = ParkedEvent.objects.filter(subscription=abonnement.NAME).delete()[0]
            ergebnis.abonnement_entfernt = bool(Subscription.objects.filter(name=abonnement.NAME).delete()[0])
    return ergebnis
