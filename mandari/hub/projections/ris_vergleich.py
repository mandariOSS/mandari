# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schatten-Quelle des RIS-Projektors gegen den RIS-Bestand (Issue #536): Stand, Vergleich, Aufräumen.

Der Vergleich stellt je Objekttyp die Zeilen der Schatten-Quelle eines Session-Mandanten neben die Zeilen, die der
heutige Weg (Spiegel bzw. Ingestor über die Session-Schnittstelle, dazu die Rücknahmen aus Session) im Bestand der
Kommune hinterlassen hat. Verbunden wird über die Adresse (``external_id``), unter der der Bestand das Objekt führt.

- **fehlt**: im Bestand sichtbar, in der Schatten-Quelle nicht (oder dort gelöscht);
- **überzählig**: in der Schatten-Quelle sichtbar, im Bestand nicht (oder dort gelöscht);
- **abweichend**: beidseitig sichtbar, aber eine Spalte (``hub.ris.uebernahme.SPALTEN``) oder ein Bezug (Kommune,
  Sitzung, Vorlage, Gremien, Person) unterscheidet sich – oder beidseitig gelöscht mit verschiedenem Grund.

Beidseitig nicht sichtbar (gelöscht bzw. nicht vorhanden) ist gleich. Zeitstempel der Quelle (``modified``) und die
Rohdaten zählen nicht: Ändert sich in Session nur, was das kanonische Modell nicht kennt, gibt es kein Ereignis, der
Spiegel übernimmt aber den neuen Zeitstempel. Werte werden wie in der Schatten-Quelle normiert (``wert``): leerer
Text gilt wie ein fehlender Wert.

Ausgegeben werden nur Zahlen, Feldnamen und Kennungen des Bestands, nie Inhalte.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import Now

from apps.events.dispatch import head_seq
from apps.events.models import Event, ParkedEvent, Subscription, SubscriptionState
from apps.events.registry import type_filter
from hub.ris import uebernahme
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

from . import ris_session
from .models import RisSchatten

#: Reihenfolge der Typen in Ausgaben (wie der Spiegel sie übernimmt)
TYPEN: Final = (
    "body",
    "legislativeterm",
    "organization",
    "person",
    "membership",
    "meeting",
    "agendaitem",
    "paper",
    "consultation",
    "file",
)
#: Bezüge je Typ (Schlüssel in ``RisSchatten.verweise``) -> Abfragepfad der Adresse im Bestand
VERWEISE: Final[Mapping[str, Mapping[str, str]]] = {
    "body": {},
    "legislativeterm": {"body": "body__external_id"},
    "organization": {"body": "body__external_id"},
    "person": {"body": "body__external_id"},
    "membership": {"person": "person__external_id", "organization": "organization__external_id"},
    "meeting": {"body": "body__external_id"},
    "agendaitem": {"meeting": "meeting__external_id"},
    "paper": {"body": "body__external_id"},
    "consultation": {"body": "body__external_id", "paper": "paper__external_id"},
    "file": {"body": "body__external_id", "paper": "paper__external_id", "meeting": "meeting__external_id"},
}


def _bestand(typ: str, kommunen: list[uuid.UUID]) -> QuerySet[Any]:
    """Zeilen des Bestands der Kommunen je Typ (über Fremdschlüssel, wie ``insight_service._entry_querysets``)."""
    if typ == "body":
        return OParlBody.objects.filter(pk__in=kommunen)
    if typ == "legislativeterm":
        return OParlLegislativeTerm.objects.filter(body__in=kommunen)
    if typ == "organization":
        return OParlOrganization.objects.filter(body__in=kommunen)
    if typ == "person":
        return OParlPerson.objects.filter(body__in=kommunen)
    if typ == "membership":
        return OParlMembership.objects.filter(
            Q(organization__body__in=kommunen) | Q(person__body__in=kommunen)
        ).distinct()
    if typ == "meeting":
        return OParlMeeting.objects.filter(body__in=kommunen)
    if typ == "agendaitem":
        return OParlAgendaItem.objects.filter(meeting__body__in=kommunen)
    if typ == "paper":
        return OParlPaper.objects.filter(body__in=kommunen)
    if typ == "consultation":
        return OParlConsultation.objects.filter(Q(body__in=kommunen) | Q(paper__body__in=kommunen)).distinct()
    if typ == "file":
        return OParlFile.objects.filter(
            Q(body__in=kommunen) | Q(paper__body__in=kommunen) | Q(meeting__body__in=kommunen)
        ).distinct()
    raise ValueError("Unbekannter Objekttyp.")


@dataclass(frozen=True)
class Stand:
    """Eine Zeile, normiert: Kennung im Bestand, sichtbar, Grund, Spalten und Bezüge."""

    kennung: str
    sichtbar: bool
    grund: str | None
    spalten: dict[str, Any]
    verweise: dict[str, Any]


def bestand_staende(typ: str, kommunen: list[uuid.UUID]) -> dict[str, Stand]:
    """Zeilen des Bestands der Kommunen je Adresse, normiert wie die Schatten-Quelle."""
    spalten = uebernahme.SPALTEN[typ]
    verweise = VERWEISE[typ]
    zeilen = _bestand(typ, kommunen).values(
        "pk", "external_id", "deleted", "deletion_reason", *spalten, *verweise.values()
    )
    gremien: dict[str, list[str]] = {}
    if typ == "meeting":
        for adresse, gremium in OParlMeeting.objects.filter(body__in=kommunen).values_list(
            "external_id", "organizations__external_id"
        ):
            if gremium:
                gremien.setdefault(adresse, []).append(gremium)
    staende: dict[str, Stand] = {}
    for zeile in zeilen.iterator(chunk_size=2000):
        bezuege = {name: ris_session.wert(zeile[pfad]) for name, pfad in verweise.items()}
        if typ == "meeting":
            bezuege["organizations"] = ris_session.wert(sorted(set(gremien.get(zeile["external_id"], []))))
        staende[zeile["external_id"]] = Stand(
            kennung=str(zeile["pk"]),
            sichtbar=not zeile["deleted"],
            grund=zeile["deletion_reason"],
            spalten={name: ris_session.wert(zeile[name]) for name in spalten},
            verweise=bezuege,
        )
    return staende


def schatten_staende(typ: str, mandant: uuid.UUID) -> dict[str, Stand]:
    """Zeilen der Schatten-Quelle eines Mandanten je Adresse."""
    zeilen = RisSchatten.objects.filter(mandant=mandant, typ=typ).values(
        "bestand_id", "external_id", "deleted", "deletion_reason", "spalten", "verweise"
    )
    return {
        zeile["external_id"]: Stand(
            kennung=str(zeile["bestand_id"]),
            sichtbar=not zeile["deleted"],
            grund=zeile["deletion_reason"],
            spalten=dict(zeile["spalten"] or {}),
            verweise=dict(zeile["verweise"] or {}),
        )
        for zeile in zeilen.iterator(chunk_size=2000)
    }


def abweichende_felder(bestand: Stand, schatten: Stand) -> list[str]:
    """Namen der Spalten und Bezüge, in denen sich zwei sichtbare Zeilen unterscheiden."""
    return list(ris_session.unterschiede(bestand.spalten, bestand.verweise, schatten.spalten, schatten.verweise))


@dataclass
class TypVergleich:
    """Vergleich eines Objekttyps."""

    typ: str
    bestand: int = 0
    schatten: int = 0
    gleich: int = 0
    fehlt: list[str] = field(default_factory=list)
    ueberzaehlig: list[str] = field(default_factory=list)
    abweichend: list[str] = field(default_factory=list)
    felder: Counter[str] = field(default_factory=Counter)

    @property
    def abweichungen(self) -> int:
        return len(self.fehlt) + len(self.ueberzaehlig) + len(self.abweichend)

    def as_dict(self, beispiele: int) -> dict[str, Any]:
        return {
            "bestand": self.bestand,
            "schatten": self.schatten,
            "gleich": self.gleich,
            "fehlt": len(self.fehlt),
            "ueberzaehlig": len(self.ueberzaehlig),
            "abweichend": len(self.abweichend),
            "felder": dict(sorted(self.felder.items())),
            "beispiele": {
                "fehlt": sorted(self.fehlt)[:beispiele],
                "ueberzaehlig": sorted(self.ueberzaehlig)[:beispiele],
                "abweichend": sorted(self.abweichend)[:beispiele],
            },
        }


def vergleiche_typ(typ: str, bestand: Mapping[str, Stand], schatten: Mapping[str, Stand]) -> TypVergleich:
    ergebnis = TypVergleich(
        typ=typ,
        bestand=sum(1 for stand in bestand.values() if stand.sichtbar),
        schatten=sum(1 for stand in schatten.values() if stand.sichtbar),
    )
    for adresse in sorted(set(bestand) | set(schatten)):
        live, schatten_stand = bestand.get(adresse), schatten.get(adresse)
        live_sichtbar = live is not None and live.sichtbar
        schatten_sichtbar = schatten_stand is not None and schatten_stand.sichtbar
        if live_sichtbar and not schatten_sichtbar:
            assert live is not None
            ergebnis.fehlt.append(live.kennung)
        elif schatten_sichtbar and not live_sichtbar:
            assert schatten_stand is not None
            ergebnis.ueberzaehlig.append(schatten_stand.kennung)
        elif live_sichtbar and schatten_sichtbar:
            assert live is not None and schatten_stand is not None
            felder = abweichende_felder(live, schatten_stand)
            if felder:
                ergebnis.abweichend.append(live.kennung)
                ergebnis.felder.update(felder)
            else:
                ergebnis.gleich += 1
        elif live is not None and schatten_stand is not None and live.grund and schatten_stand.grund:
            # Beidseitig gelöscht: Der Grund entscheidet, ob das Portal „zurückgezogen“ zeigt
            if live.grund != schatten_stand.grund:
                ergebnis.abweichend.append(live.kennung)
                ergebnis.felder.update(["deletion_reason"])
    return ergebnis


@dataclass
class Vergleich:
    """Vergleich eines Mandanten über alle Objekttypen."""

    mandant: uuid.UUID
    kommunen: list[uuid.UUID]
    typen: dict[str, TypVergleich]

    @property
    def abweichungen(self) -> int:
        return sum(typ.abweichungen for typ in self.typen.values())

    def as_dict(self, beispiele: int = 5) -> dict[str, Any]:
        return {
            "mandant": str(self.mandant),
            "kommunen": [str(kommune) for kommune in self.kommunen],
            "abweichungen": self.abweichungen,
            "typen": {name: typ.as_dict(beispiele) for name, typ in self.typen.items()},
        }


def kommunen_der_quellen(quellen: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Kommunen (Bodies) der Quellen im Bestand."""
    return sorted(OParlBody.objects.filter(source_id__in=list(quellen)).values_list("pk", flat=True), key=str)


def vergleichen(mandant: uuid.UUID, kommunen: list[uuid.UUID], typen: Iterable[str] = TYPEN) -> Vergleich:
    """Schatten-Quelle des Mandanten mit dem Bestand seiner Kommunen vergleichen (je Typ)."""
    ergebnis: dict[str, TypVergleich] = {}
    for typ in typen:
        ergebnis[typ] = vergleiche_typ(typ, bestand_staende(typ, kommunen), schatten_staende(typ, mandant))
    return Vergleich(mandant=mandant, kommunen=kommunen, typen=ergebnis)


# =============================================================================
# Stand und Aufräumen
# =============================================================================


@dataclass
class AbonnementStand:
    vorhanden: bool
    zustand: str | None = None
    cursor: int | None = None
    hoechste_folgenummer: int = 0
    offen: int = 0
    rueckstand_sekunden: float = 0.0
    geparkt: dict[str, int] = field(default_factory=dict)


def abonnement_stand() -> AbonnementStand:
    """Zustand, Cursor und Rückstand des Abonnements (auch wenn es nicht registriert ist)."""
    hoechste = head_seq()
    zeile = (
        Subscription.objects.filter(name=ris_session.NAME)
        .annotate(jetzt=Now())
        .values_list("state", "cursor_seq", "jetzt")
        .first()
    )
    if zeile is None:
        return AbonnementStand(vorhanden=False, hoechste_folgenummer=hoechste)
    zustand, cursor, jetzt = zeile
    offen = Event.objects.filter(seq__gt=cursor).filter(type_filter(ris_session.TYPES))
    aeltestes: datetime | None = offen.order_by("seq").values_list("recorded_at", flat=True).first()
    geparkt = {
        str(state): int(anzahl)
        for state, anzahl in ParkedEvent.objects.filter(subscription=ris_session.NAME)
        .values_list("state")
        .annotate(anzahl=Count("id"))
        .order_by()
    }
    return AbonnementStand(
        vorhanden=True,
        zustand=str(zustand),
        cursor=int(cursor),
        hoechste_folgenummer=hoechste,
        offen=offen.count(),
        rueckstand_sekunden=max((jetzt - aeltestes).total_seconds(), 0.0) if aeltestes is not None else 0.0,
        geparkt=geparkt,
    )


def zeilen_je_mandant() -> dict[str, dict[str, int]]:
    """Zeilen der Schatten-Quelle je Mandant und Typ (sichtbar)."""
    ergebnis: dict[str, dict[str, int]] = {}
    for mandant, typ, anzahl in (
        RisSchatten.objects.filter(deleted=False)
        .values_list("mandant", "typ")
        .annotate(anzahl=Count("id"))
        .order_by("mandant", "typ")
    ):
        ergebnis.setdefault(str(mandant), {})[str(typ)] = int(anzahl)
    return ergebnis


def wird_zugestellt(stand: AbonnementStand) -> bool:
    """Schreibt das Abonnement noch (laut Schalter registriert, angelegt und nicht pausiert)?"""
    return ris_session.modus() != ris_session.AUS and stand.vorhanden and stand.zustand != SubscriptionState.PAUSIERT


@dataclass
class Entfernt:
    zeilen: int
    abonnement_entfernt: bool = False
    cursor: int | None = None
    geparkt_entfernt: int = 0


def entfernen(mandanten: Iterable[uuid.UUID] | None, *, abonnement: bool) -> Entfernt:
    """
    Zeilen der Schatten-Quelle löschen (``None``: alle) und auf Wunsch das Abonnement (Cursor, geparkte Ereignisse).

    Läuft in der Transaktion des Aufrufers; er schreibt den Eintrag im Sicherheitsprotokoll in derselben.
    """
    zeilen = RisSchatten.objects.all()
    if mandanten is not None:
        zeilen = zeilen.filter(mandant__in=list(mandanten))
    with transaction.atomic():
        ergebnis = Entfernt(zeilen=zeilen.delete()[0])
        if abonnement:
            ergebnis.cursor = (
                Subscription.objects.filter(name=ris_session.NAME).values_list("cursor_seq", flat=True).first()
            )
            ergebnis.geparkt_entfernt = ParkedEvent.objects.filter(subscription=ris_session.NAME).delete()[0]
            ergebnis.abonnement_entfernt = bool(Subscription.objects.filter(name=ris_session.NAME).delete()[0])
    return ergebnis


def iter_typen(angaben: Iterable[str]) -> Iterator[str]:
    """Gewählte Typen in der Reihenfolge von ``TYPEN`` (leer = alle)."""
    gewaehlt = set(angaben)
    for typ in TYPEN:
        if not gewaehlt or typ in gewaehlt:
            yield typ
