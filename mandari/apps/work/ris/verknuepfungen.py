# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work-Daten folgen ihrem Tagesordnungspunkt bzw. ihrer Vorlage über Neuveröffentlichungen (Issue #547).

Ablauf (Zeitplan ``ris_verknuepfungen_abgleichen`` im Worker, Befehl ``ris_verknuepfungen_abgleichen``):

1. **Anker pflegen:** Für jedes RIS-Objekt, an dem Work-Daten einer Organisation hängen (``VERKNUEPFUNGEN``), hat
   die Organisation einen ``RisAnker`` mit der fachlichen Kennung (``hub.ris.neuveroeffentlichung``). Neue
   Verknüpfungen mit einem Tagesordnungspunkt erfasst ein Signal sofort, damit die Kennung den Stand beim
   Verknüpfen trägt; der Abgleich holt fehlende Anker nach und räumt Anker ohne Verknüpfung weg.
2. **Prüfen:** Sitzungen, an denen sich seit der letzten Prüfung etwas geändert hat und die seit ``RUHEZEIT``
   ruhen, und Vorlagen, die sich geändert haben oder entfallen sind. Bei unveränderten Sitzungen gilt die Kennung
   weiter als bestätigt (``bestaetigt_am``).
3. **Umhängen:** Hat ein Punkt bzw. eine Vorlage genau einen Nachfolger, wandern die Datensätze der Organisation
   dorthin (nur der Fremdschlüssel; verschlüsselte Inhalte bleiben unberührt, ``updated_at`` auch). Es wandern nur
   Datensätze, die zum beschriebenen Stand gehören. Am alten Objekt bleiben – als ``zurueckgelassen`` und nie
   wieder automatisch umgehängt –
   - Datensätze, deren Gegenstück am Ziel schon steht (private Notiz, Redebeitrag, Position derselben Person bzw.
     Organisation; es wird nichts zusammengeführt),
   - an Tagesordnungspunkten, deren Zeile noch auf der Tagesordnung steht, Datensätze, die nach der letzten
     Bestätigung angelegt wurden: Sie können schon den neuen Inhalt der Zeile meinen,
   - Datensätze, deren Umzug jemand zurückgedreht hat (``zurueckdrehen``).
   Jeder Umzug steht mit den Kennungen der Datensätze in ``RisNeuzuordnung``. Danach beschreibt der Anker das, was
   am Objekt heute steht; ein zweiter Lauf bewegt nichts mehr.
4. **Nicht zuordnen statt falsch zuordnen:** Steht unter der alten Kennung inzwischen ein anderer Punkt und gibt
   es keinen eindeutigen Nachfolger, bleibt alles, wo es ist; die Vorbereitung zeigt „Nicht zugeordnet“ mit dem
   früheren Titel, ebenso an Punkten mit Zurückgelassenem.

Mandantentrennung: Anker, Entscheidung, Umhängen, Protokoll und Hinweis gelten je Organisation; die Regeln lesen
nur den RIS-Bestand. Organisation und Autor eines Datensatzes bleiben, ein Nachfolger liegt immer in derselben
Sitzung bzw. Kommune.

``WORK_RIS_RELINK``: ``aus`` (Standard) tut nichts, ``probe`` pflegt nur Anker und meldet, was geschähe, ``aktiv``
hängt um. Rückweg: ``zurueckdrehen`` bzw. Befehl ``ris_neuzuordnung_zurueckdrehen``.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any, cast

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, models, transaction
from django.db.models.signals import post_save
from django.utils import timezone

from hub.ris import neuveroeffentlichung as ris_neu
from hub.ris.neuveroeffentlichung import TopKennung, TopStand, VorlagenKennung, Zuordnung

from .models import RisAnker, RisNeuzuordnung

logger = logging.getLogger(__name__)

#: Eine Sitzung wird erst geprüft, wenn sie so lange unverändert ist (kein halber Stand mitten im Abruf).
RUHEZEIT = timedelta(minutes=10)
#: Entfallene oder mehrdeutige Vorlagen sucht der Abgleich höchstens so oft erneut (Suche über die Drucksachennummer).
NEUPRUEFUNG = timedelta(hours=6)
#: Sitzungen je Lesevorgang
STAPEL = 50
_SPERRE = "work:ris_verknuepfungen_abgleichen"
_SPERRE_SEKUNDEN = 30 * 60

MODI = ("aus", "probe", "aktiv")

#: je Verknüpfung (``Verknuepfung.name``) die Kennungen von Datensätzen
Datensaetze = dict[str, list[str]]


@dataclass(frozen=True)
class Verknuepfung:
    """Ein Feld in Work, das auf einen Tagesordnungspunkt bzw. eine Vorlage zeigt."""

    modell: str
    feld: str
    #: Weg von der Tabelle zur Organisation des Datensatzes
    organisation: str = "organization_id"
    #: am Ziel höchstens ein Datensatz mit diesen Werten (sonst bleibt der Datensatz am alten Objekt)
    eindeutig: tuple[str, ...] = ()
    #: die Eindeutigkeit steht als Bedingung in der Datenbank (gilt auch für Zwischenstände beim Umhängen)
    db_eindeutig: bool = False
    #: nullbare Spalte aus ``eindeutig``, über die eine Zeile beim Umhängen kurz geparkt wird (``NULL`` fällt aus der
    #: Bedingung in der Datenbank). Dann zählt trotz Bedingung nur der Endzustand: Tausch und Ring gehen auf.
    parken: str = ""
    m2m: bool = False

    @property
    def name(self) -> str:
        return f"{self.modell}.{self.feld}"

    @property
    def angelegt(self) -> str | None:
        """Spalte mit dem Zeitpunkt der Anlage (Zwischentabellen kennen keinen)."""
        return None if self.m2m else "created_at"

    def tabelle(self) -> tuple[type[models.Model], str, tuple[str, ...]]:
        """(Tabelle, Spalte mit der Kennung des RIS-Objekts, Spalten der Eindeutigkeit)."""
        model = apps.get_model(self.modell)
        feld = cast(Any, model._meta.get_field(self.feld))
        if self.m2m:
            through = cast(type[models.Model], feld.remote_field.through)
            return through, f"{feld.m2m_reverse_field_name()}_id", (f"{feld.m2m_field_name()}_id",)
        return model, cast(str, feld.attname), self.eindeutig

    def organisation_von(self, instance: models.Model) -> Any:
        """Organisation eines gespeicherten Datensatzes (entlang ``organisation``)."""
        wert: Any = instance
        for teil in self.organisation.split("__"):
            wert = getattr(wert, teil, None)
            if wert is None:
                return None
        return wert


#: Verknüpfungen mit Tagesordnungspunkten. Neue Felder gehören hierher (Test ``test_alle_verknuepfungen_erfasst``).
TOP_VERKNUEPFUNGEN = (
    Verknuepfung("work.AgendaItemPosition", "agenda_item", eindeutig=("organization_id",), parken="organization_id"),
    Verknuepfung("work.AgendaPrivateNote", "agenda_item", eindeutig=("author_id",), db_eindeutig=True),
    Verknuepfung("work.AgendaSpeechNote", "agenda_item", eindeutig=("author_id",), db_eindeutig=True),
    Verknuepfung("work.AgendaItemNote", "agenda_item"),
    Verknuepfung("work.AgendaSupplementaryDocument", "agenda_item"),
    Verknuepfung("work.Task", "related_agenda_item"),
    Verknuepfung("work.FactionAgendaItem", "related_agenda_item", organisation="meeting__organization_id"),
)
#: Verknüpfungen mit Vorlagen
VORLAGEN_VERKNUEPFUNGEN = (
    Verknuepfung("work.PaperComment", "paper"),
    Verknuepfung("work.AgendaSupplementaryDocument", "paper"),
    Verknuepfung("work.Motion", "related_paper"),
    Verknuepfung("work.Motion", "parent_paper"),
    Verknuepfung(
        "work.FactionAgendaItem",
        "related_papers",
        organisation="factionagendaitem__meeting__organization_id",
        db_eindeutig=True,
        m2m=True,
    ),
)
VERKNUEPFUNGEN = {RisAnker.ART_TOP: TOP_VERKNUEPFUNGEN, RisAnker.ART_VORLAGE: VORLAGEN_VERKNUEPFUNGEN}
_NACH_NAME = {(art, v.name): v for art, liste in VERKNUEPFUNGEN.items() for v in liste}

_STATUS = {
    ris_neu.BESTAETIGT: RisAnker.AKTUELL,
    ris_neu.ABWEICHEND: RisAnker.NICHT_ZUGEORDNET,
    ris_neu.ENTFALLEN: RisAnker.ENTFALLEN,
    ris_neu.MEHRDEUTIG: RisAnker.MEHRDEUTIG,
}

#: (Organisation, RIS-Objekt)
Schluessel = tuple[Any, uuid.UUID]
#: heutige Beschreibung eines Objekts, wenn es auf der Tagesordnung steht bzw. nicht gelöscht ist
Heute = Callable[[uuid.UUID], Any]


@dataclass
class Bericht:
    """Ergebnis eines Abgleichs (Zahlen für Befehl, Zeitplan und Protokoll)."""

    modus: str
    gesperrt: bool = False
    anker_neu: int = 0
    anker_entfernt: int = 0
    geprueft: int = 0
    umgehaengt: int = 0
    datensaetze: int = 0
    konflikte: int = 0
    juenger: int = 0
    nicht_zugeordnet: int = 0
    entfallen: int = 0
    mehrdeutig: int = 0
    #: Sitzungen bzw. Organisationen, deren Abgleich an einem Datenbankfehler scheiterte (nächster Lauf versucht es)
    fehler: int = 0
    #: Probe: (Art, bisheriges Objekt, Ergebnis, Nachfolger)
    geplant: list[tuple[str, str, str, str | None]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        zahlen = {k: v for k, v in self.__dict__.items() if k != "geplant"}
        zahlen["geplant"] = len(self.geplant)
        return zahlen

    def uebernehmen(self, teil: Bericht) -> None:
        """Zahlen eines erfolgreich abgeschlossenen Teils übernehmen."""
        for feld in fields(self):
            wert = getattr(teil, feld.name)
            if isinstance(wert, int) and not isinstance(wert, bool):
                setattr(self, feld.name, getattr(self, feld.name) + wert)
        self.geplant.extend(teil.geplant)


@dataclass
class _Umhaengung:
    verschoben: Datensaetze = field(default_factory=dict)
    #: Gegenstück am Ziel
    konflikte: Datensaetze = field(default_factory=dict)
    #: nach der letzten Bestätigung angelegt
    juenger: Datensaetze = field(default_factory=dict)

    @property
    def anzahl(self) -> int:
        return sum(len(v) for v in self.verschoben.values())

    @property
    def geblieben(self) -> Datensaetze:
        return _vereinen(self.konflikte, self.juenger)

    @property
    def leer(self) -> bool:
        return not (self.verschoben or self.konflikte or self.juenger)


def _vereinen(*teile: Mapping[str, Iterable[Any]]) -> Datensaetze:
    gesamt: dict[str, set[str]] = defaultdict(set)
    for teil in teile:
        for name, pks in (teil or {}).items():
            gesamt[name].update(str(pk) for pk in pks)
    return {name: sorted(pks) for name, pks in sorted(gesamt.items()) if pks}


def _titel(anker: RisAnker) -> str:
    return str((anker.kennung or {}).get("title") or "")


# =============================================================================
# Anker
# =============================================================================


def verknuepfte_objekte(art: str) -> set[Schluessel]:
    """(Organisation, RIS-Objekt) für alle Objekte dieser Art, an denen Work-Daten hängen."""
    paare: set[Schluessel] = set()
    for verknuepfung in VERKNUEPFUNGEN[art]:
        tabelle, spalte, _ = verknuepfung.tabelle()
        werte = (
            tabelle._base_manager.filter(**{f"{spalte}__isnull": False})
            .values_list(verknuepfung.organisation, spalte)
            .distinct()
        )
        paare.update((organisation, objekt) for organisation, objekt in werte if organisation is not None)
    return paare


def _kennungen(art: str, objekte: Iterable[uuid.UUID]) -> dict[uuid.UUID, TopKennung | VorlagenKennung]:
    ids = list(set(objekte))
    if not ids:
        return {}
    if art == RisAnker.ART_TOP:
        return dict(ris_neu.top_kennungen(ids))
    return {pk: stand.kennung for pk, stand in ris_neu.vorlagen(ids).items()}


def anker_sichern(art: str, objekt: object, organisation: Any) -> None:
    """Legt den Anker eines verknüpften RIS-Objekts an, falls die Organisation keinen hat (heutiger Stand)."""
    pk = objekt if isinstance(objekt, uuid.UUID) else uuid.UUID(str(objekt))
    if RisAnker.objects.filter(organization_id=organisation, art=art, objekt=pk).exists():
        return
    try:
        with transaction.atomic():
            kennung = _kennungen(art, [pk]).get(pk)
            RisAnker.objects.get_or_create(
                organization_id=organisation,
                art=art,
                objekt=pk,
                defaults={
                    "kennung": kennung.as_dict() if kennung else {},
                    "bestaetigt_am": timezone.now() if kennung else None,
                },
            )
    except DatabaseError:
        # Die Verknüpfung selbst ist gespeichert; der Abgleich legt den Anker beim nächsten Lauf an.
        logger.warning("RIS-Anker nicht angelegt (%s %s); der Abgleich holt das nach.", art, pk, exc_info=True)


def _beim_speichern(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:
    if kwargs.get("raw") or settings.WORK_RIS_RELINK == "aus":
        return
    for verknuepfung in TOP_VERKNUEPFUNGEN:
        if verknuepfung.m2m or apps.get_model(verknuepfung.modell) is not sender:
            continue
        _, spalte, _ = verknuepfung.tabelle()
        wert = getattr(instance, spalte, None)
        organisation = verknuepfung.organisation_von(instance) if wert else None
        if wert and organisation is not None:
            anker_sichern(RisAnker.ART_TOP, wert, organisation)


def register() -> None:
    """Signale für Verknüpfungen mit Tagesordnungspunkten (``WorkConfig.ready``)."""
    for modell in {v.modell for v in TOP_VERKNUEPFUNGEN if not v.m2m}:
        post_save.connect(_beim_speichern, sender=apps.get_model(modell), dispatch_uid=f"ris_anker_{modell}")


def _reste_pruefen(art: str, anker: RisAnker) -> None:
    """Zurückgelassenes, das nicht mehr am Objekt hängt (gelöscht, zurückgedreht), aus dem Anker streichen."""
    bleiben: Datensaetze = {}
    for name, pks in (anker.zurueckgelassen or {}).items():
        verknuepfung = _NACH_NAME.get((art, name))
        if verknuepfung is None or not pks:
            continue
        tabelle, spalte, _ = verknuepfung.tabelle()
        da = tabelle._base_manager.filter(pk__in=pks, **{spalte: anker.objekt}).values_list("pk", flat=True)
        if da:
            bleiben[name] = sorted(str(pk) for pk in da)
    if bleiben != anker.zurueckgelassen:
        felder: dict[str, Any] = {"zurueckgelassen": bleiben}
        if not bleiben:
            felder["frueherer_titel"] = ""
        RisAnker.objects.filter(pk=anker.pk).update(**felder)


def _anker_pflegen(art: str, bericht: Bericht, jetzt: datetime) -> None:
    """Fehlende Anker anlegen, leere Kennungen erfassen, Anker ohne Verknüpfung entfernen, Reste prüfen."""
    # Erst die Anker, dann die Verknüpfungen lesen: Ein dazwischen angelegter Anker wird so nicht entfernt.
    vorhanden: dict[Schluessel, RisAnker] = {
        (a.organization_id, a.objekt): a
        for a in RisAnker.objects.filter(art=art).only("pk", "organization_id", "objekt", "kennung", "zurueckgelassen")
    }
    verknuepft = verknuepfte_objekte(art)

    weg = [a.pk for paar, a in vorhanden.items() if paar not in verknuepft]
    if weg:
        RisAnker.objects.filter(pk__in=weg).delete()
        bericht.anker_entfernt += len(weg)

    neu = verknuepft - set(vorhanden)
    leer = [a for paar, a in vorhanden.items() if paar in verknuepft and not a.kennung]
    kennungen = _kennungen(art, [objekt for _, objekt in neu] + [a.objekt for a in leer])
    angelegt = RisAnker.objects.bulk_create(
        [
            RisAnker(
                organization_id=organisation,
                art=art,
                objekt=objekt,
                kennung=kennungen[objekt].as_dict() if objekt in kennungen else {},
                bestaetigt_am=jetzt if objekt in kennungen else None,
            )
            for organisation, objekt in neu
        ],
        ignore_conflicts=True,
    )
    bericht.anker_neu += len(angelegt)
    for anker in leer:
        if anker.objekt in kennungen:
            RisAnker.objects.filter(pk=anker.pk).update(kennung=kennungen[anker.objekt].as_dict(), bestaetigt_am=jetzt)
    for paar, anker in vorhanden.items():
        if paar in verknuepft and anker.zurueckgelassen:
            _reste_pruefen(art, anker)


# =============================================================================
# Umhängen
# =============================================================================


def _umhaengen(
    art: str,
    organisation: Any,
    umzuege: dict[uuid.UUID, uuid.UUID],
    bleiben: Mapping[uuid.UUID, Mapping[str, Iterable[str]]],
    schwelle: Mapping[uuid.UUID, datetime | None] | None,
) -> tuple[dict[uuid.UUID, _Umhaengung], dict[uuid.UUID, Datensaetze]]:
    """
    Hängt die Datensätze der Organisation von ``von`` nach ``nach`` um und gibt je ``von`` zurück, was umzog und
    was blieb, dazu je Objekt die Datensätze vor dem Umhängen.

    Es bleiben: Zurückgelassenes (``bleiben``), an den Objekten in ``schwelle`` (Tagesordnungspunkte, deren Zeile
    noch auf der Tagesordnung steht) Datensätze, die nach der letzten Bestätigung angelegt wurden, und Datensätze,
    deren Gegenstück am Ziel bleibt (Konflikt, nichts wird zusammengeführt). Ketten und Tausch innerhalb einer
    Sitzung gehen in einem Durchgang. Steht die Eindeutigkeit nur in der Logik oder lässt sich eine Zeile parken
    (Position je Organisation, ``parken``), zählt der Endzustand; steht sie ohne Parkplatz in der Datenbank, auch
    jeder Zwischenstand – ein Tausch zweier Punkte mit privaten Notizen derselben Person bleibt dann stehen.
    """
    ergebnis = {von: _Umhaengung() for von in umzuege}
    vorher: dict[uuid.UUID, Datensaetze] = defaultdict(dict)
    objekte = set(umzuege) | set(umzuege.values())
    for verknuepfung in VERKNUEPFUNGEN[art]:
        name = verknuepfung.name
        tabelle, spalte, eindeutig = verknuepfung.tabelle()
        manager = tabelle._base_manager
        angelegt = verknuepfung.angelegt if schwelle is not None else None
        spalten = ["pk", spalte, *eindeutig, *([angelegt] if angelegt else [])]
        auswahl = manager.filter(**{f"{spalte}__in": objekte, verknuepfung.organisation: organisation})
        fest = {objekt: {str(pk) for pk in (reste or {}).get(name, ())} for objekt, reste in bleiben.items()}

        offen: list[tuple[Any, uuid.UUID, tuple[Any, ...]]] = []
        stehend: list[tuple[Any, uuid.UUID, tuple[Any, ...]]] = []
        for werte in auswahl.values_list(*spalten):
            pk, objekt = werte[0], werte[1]
            zeile = (pk, objekt, tuple(werte[2 : 2 + len(eindeutig)]))
            vorher[objekt].setdefault(name, []).append(str(pk))
            if objekt not in umzuege or str(pk) in fest.get(objekt, ()):
                stehend.append(zeile)
                continue
            if angelegt is not None and schwelle is not None and objekt in schwelle:
                grenze = schwelle[objekt]
                if grenze is None or werte[-1] is None or werte[-1] > grenze:
                    ergebnis[objekt].juenger.setdefault(name, []).append(str(pk))
                    stehend.append(zeile)
                    continue
            offen.append(zeile)

        if verknuepfung.parken:
            ziehen, konflikte = _endzustand(umzuege, stehend, offen, pruefen=bool(eindeutig))
            zuege = [(pk, von, umzuege[von]) for pk, von, _ in ziehen]
            umgezogen, zurueck = _geparkt_umhaengen(manager, spalte, verknuepfung.parken, zuege)
            for nummer, (pk, von, _nach) in enumerate(zuege):
                if nummer in umgezogen:
                    ergebnis[von].verschoben.setdefault(name, []).append(str(pk))
                elif nummer in zurueck:
                    konflikte.append((pk, von, ()))
            for pk, von, _ in konflikte:
                ergebnis[von].konflikte.setdefault(name, []).append(str(pk))
            continue

        # Schlüssel, die am jeweiligen Objekt belegt sind: im Endzustand nur die bleibenden, sonst die heutigen
        belegt: dict[tuple[Any, tuple[Any, ...]], Any] = {}
        for pk, objekt, schluessel in stehend + (offen if verknuepfung.db_eindeutig else []):
            if eindeutig and None not in schluessel:
                belegt.setdefault((objekt, schluessel), pk)

        weiter = True
        while offen and weiter:
            weiter = False
            rest = []
            for pk, von, schluessel in offen:
                nach = umzuege[von]
                pruefen = bool(eindeutig) and None not in schluessel
                if pruefen and belegt.get((nach, schluessel), pk) != pk:
                    rest.append((pk, von, schluessel))
                    continue
                weiter = True
                if not manager.filter(pk=pk, **{spalte: von}).update(**{spalte: nach}):
                    continue  # inzwischen geändert oder gelöscht
                if pruefen:
                    if belegt.get((von, schluessel)) == pk:
                        del belegt[(von, schluessel)]
                    belegt[(nach, schluessel)] = pk
                ergebnis[von].verschoben.setdefault(name, []).append(str(pk))
            offen = rest
        for pk, von, _ in offen:
            ergebnis[von].konflikte.setdefault(name, []).append(str(pk))
    return ergebnis, dict(vorher)


Zeile = tuple[Any, uuid.UUID, tuple[Any, ...]]


def _endzustand(
    umzuege: Mapping[uuid.UUID, uuid.UUID], stehend: list[Zeile], offen: list[Zeile], *, pruefen: bool
) -> tuple[list[Zeile], list[Zeile]]:
    """
    Wer zieht und wer bleibt, gemessen am Endzustand: Ein Datensatz bleibt, wenn am Ziel schon einer mit denselben
    Werten steht oder ein früherer dorthin zieht. Wer bleibt, belegt seinen alten Platz; das kann weitere Umzüge
    dorthin aufhalten, deshalb bis zum Stillstand. Liefert (ziehen, bleiben).
    """

    def zaehlt(schluessel: tuple[Any, ...]) -> bool:
        return pruefen and None not in schluessel

    belegt = {(objekt, schluessel) for _pk, objekt, schluessel in stehend if zaehlt(schluessel)}
    ziehen: list[Zeile] = list(offen)
    bleiben: list[Zeile] = []
    while True:
        ziele: set[tuple[Any, tuple[Any, ...]]] = set()
        weiter: list[Zeile] = []
        aufgehalten: list[Zeile] = []
        for zeile in ziehen:
            _pk, von, schluessel = zeile
            ziel = (umzuege[von], schluessel)
            if zaehlt(schluessel) and (ziel in belegt or ziel in ziele):
                aufgehalten.append(zeile)
                continue
            if zaehlt(schluessel):
                ziele.add(ziel)
            weiter.append(zeile)
        if not aufgehalten:
            return weiter, bleiben
        bleiben += aufgehalten
        belegt.update((von, schluessel) for _pk, von, schluessel in aufgehalten if zaehlt(schluessel))
        ziehen = weiter


def _geparkt_umhaengen(
    manager: Any, spalte: str, parken: str, zuege: list[tuple[Any, Any, Any]]
) -> tuple[set[int], set[int]]:
    """
    Hängt Zeilen ``(pk, von, nach)`` um, deren Eindeutigkeit als Bedingung in der Datenbank steht – auch im Tausch,
    im Ring und in Ketten, wo ein Zwischenstand die Bedingung verletzen würde.

    Die Zeilen werden gesperrt und zuerst geparkt: Die Spalte ``parken`` wird ``NULL``, damit die Zeile aus der
    Bedingung fällt (``NULL`` ist nie gleich); der Fremdschlüssel auf das RIS-Objekt bleibt dabei gültig. Dann zieht
    jede Zeile an ihr Ziel und bekommt ihren Wert in derselben Anweisung zurück. Trifft sie dort auf eine stehende
    Zeile, kommt sie mit ihrem Wert an den alten Platz zurück. Geht auch das nicht, bleibt alles wie vorher
    (Savepoint). Die Bedingung bleibt sofort wirksam, auch während des Umhängens.

    Liefert die Nummern in ``zuege`` (umgezogen, zurück am alten Platz). Zeilen, die nicht mehr an ``von`` hängen,
    fehlen in beiden.
    """
    if not zuege:
        return set(), set()
    try:
        with transaction.atomic():
            werte = {
                str(pk): wert
                for pk, wert in manager.select_for_update()
                .filter(pk__in=[pk for pk, _von, _nach in zuege])
                .order_by("pk")
                .values_list("pk", parken)
            }
            geparkt = [
                nummer
                for nummer, (pk, von, _nach) in enumerate(zuege)
                if str(pk) in werte and manager.filter(pk=pk, **{spalte: von}).update(**{parken: None})
            ]
            umgezogen: set[int] = set()
            zurueck: set[int] = set()
            for nummer in geparkt:
                pk, _von, nach = zuege[nummer]
                try:
                    with transaction.atomic():
                        manager.filter(pk=pk).update(**{spalte: nach, parken: werte[str(pk)]})
                    umgezogen.add(nummer)
                except IntegrityError:
                    zurueck.add(nummer)
            for nummer in zurueck:
                pk = zuege[nummer][0]
                manager.filter(pk=pk).update(**{parken: werte[str(pk)]})
            return umgezogen, zurueck
    except IntegrityError:
        logger.warning("RIS-Umhängen über Zwischenplatz nicht möglich, alles bleibt am alten Platz.", exc_info=True)
        return set(), set(range(len(zuege)))


def _status_setzen(felder: dict[str, Any], anker: RisAnker, status: str, jetzt: datetime) -> None:
    if status != anker.status:
        felder.update(status=status, status_seit=jetzt)


def _bewerten(
    art: str, organisation: Any, anker: RisAnker, zuordnung: Zuordnung, jetzt: datetime, bericht: Bericht
) -> None:
    """Ein Anker ohne Umzug: bestätigt, nicht zugeordnet, entfallen oder mehrdeutig."""
    status = _STATUS[zuordnung.ergebnis]
    felder: dict[str, Any] = {"geprueft_am": jetzt}
    if zuordnung.ergebnis == ris_neu.BESTAETIGT:
        felder["bestaetigt_am"] = jetzt
        if zuordnung.kennung is not None:
            felder["kennung"] = zuordnung.kennung.as_dict()
    if status != anker.status:
        RisNeuzuordnung.objects.create(
            organization_id=organisation, art=art, von=anker.objekt, ergebnis=zuordnung.ergebnis
        )
        if status == RisAnker.NICHT_ZUGEORDNET:
            bericht.nicht_zugeordnet += 1
        elif status == RisAnker.ENTFALLEN:
            bericht.entfallen += 1
        elif status == RisAnker.MEHRDEUTIG:
            bericht.mehrdeutig += 1
    _status_setzen(felder, anker, status, jetzt)
    RisAnker.objects.filter(pk=anker.pk).update(**felder)


def _nach_umzug(anker: RisAnker, umhaengung: _Umhaengung, zuzug: Any, heute: Any, jetzt: datetime) -> None:
    """
    Anker eines Objekts, von dem die Daten weggezogen sind. Was blieb, ist zurückgelassen; der Anker beschreibt
    danach, was heute am Objekt steht (zugezogene Daten bzw. der heutige Punkt), oder entfällt ganz.
    """
    reste = _vereinen(anker.zurueckgelassen, umhaengung.geblieben)
    titel = _titel(anker) if umhaengung.geblieben else anker.frueherer_titel
    felder: dict[str, Any] = {"zurueckgelassen": reste, "frueherer_titel": titel if reste else "", "geprueft_am": jetzt}
    if zuzug is None and not reste:
        RisAnker.objects.filter(pk=anker.pk).delete()  # am Objekt hängt nichts mehr
        return
    kennung = zuzug if zuzug is not None else heute
    if kennung is not None:
        felder.update(kennung=kennung.as_dict(), bestaetigt_am=jetzt)
        _status_setzen(felder, anker, RisAnker.AKTUELL, jetzt)
    else:
        # Gelöscht bzw. nicht mehr auf der Tagesordnung: Es hängt nur noch Zurückgelassenes daran.
        _status_setzen(felder, anker, RisAnker.ENTFALLEN, jetzt)
    RisAnker.objects.filter(pk=anker.pk).update(**felder)


def _mit_zuzug(anker: RisAnker, zuordnung: Zuordnung, zuzug: Any, hier: Datensaetze, jetzt: datetime) -> None:
    """
    Anker eines Objekts, an das Daten gezogen sind, ohne dass es selbst umzog. War es selbst ein anderer Punkt
    (nicht zugeordnet, mehrdeutig), gehören seine bisherigen Datensätze zum früheren Stand und bleiben zurückgelassen.
    """
    felder: dict[str, Any] = {"kennung": zuzug.as_dict(), "geprueft_am": jetzt, "bestaetigt_am": jetzt}
    if zuordnung.ergebnis != ris_neu.BESTAETIGT:
        reste = _vereinen(anker.zurueckgelassen, hier)
        felder.update(zurueckgelassen=reste, frueherer_titel=_titel(anker) if reste else anker.frueherer_titel)
    _status_setzen(felder, anker, RisAnker.AKTUELL, jetzt)
    RisAnker.objects.filter(pk=anker.pk).update(**felder)


def _anwenden(
    art: str,
    organisation: Any,
    entscheidungen: list[tuple[RisAnker, Zuordnung]],
    heute: Heute,
    jetzt: datetime,
    bericht: Bericht,
) -> None:
    """Entscheidungen einer Organisation (für eine Sitzung bzw. die Vorlagen) umsetzen."""
    anker_an = {a.objekt: a for a, _ in entscheidungen}
    umzuege = {a.objekt: z.ziel for a, z in entscheidungen if z.ergebnis == ris_neu.NACHFOLGER and z.ziel is not None}
    umhaengungen: dict[uuid.UUID, _Umhaengung] = {}
    vorher: dict[uuid.UUID, Datensaetze] = {}
    if umzuege:
        # Jüngeres kann nur dort schon einen neuen Inhalt meinen, wo die alte Zeile noch auf der Tagesordnung steht;
        # an einem gelöschten bzw. nicht mehr gelisteten Punkt gehört alles zum früheren Stand.
        schwelle = (
            {von: anker_an[von].bestaetigt_am for von in umzuege if heute(von) is not None}
            if art == RisAnker.ART_TOP
            else None
        )
        bleiben = {objekt: a.zurueckgelassen or {} for objekt, a in anker_an.items()}
        umhaengungen, vorher = _umhaengen(art, organisation, umzuege, bleiben, schwelle)

    # Ziele, an die Datensätze gezogen sind, mit ihrer heutigen Beschreibung
    zuzug: dict[uuid.UUID, Any] = {}
    for anker, zuordnung in entscheidungen:
        umhaengung = umhaengungen.get(anker.objekt)
        if umhaengung is None:
            continue
        if umhaengung.verschoben and zuordnung.ziel is not None:
            zuzug[zuordnung.ziel] = zuordnung.kennung
        if umhaengung.leer:
            continue  # nichts Bewegliches mehr am Objekt (alles zurückgelassen): kein Umzug, kein Protokoll
        RisNeuzuordnung.objects.create(
            organization_id=organisation,
            art=art,
            von=anker.objekt,
            nach=zuordnung.ziel,
            ergebnis=ris_neu.NACHFOLGER,
            verschoben=umhaengung.verschoben,
            konflikte=umhaengung.konflikte,
            juenger=umhaengung.juenger,
        )
        bericht.umgehaengt += 1 if umhaengung.anzahl else 0
        bericht.datensaetze += umhaengung.anzahl
        bericht.konflikte += sum(len(v) for v in umhaengung.konflikte.values())
        bericht.juenger += sum(len(v) for v in umhaengung.juenger.values())
        logger.info(
            "RIS-Neuveröffentlichung: %s %s → %s, %s Datensätze umgehängt, %s geblieben",
            art,
            anker.objekt,
            zuordnung.ziel,
            umhaengung.anzahl,
            sum(len(v) for v in umhaengung.geblieben.values()),
        )

    for anker, zuordnung in entscheidungen:
        objekt = anker.objekt
        if objekt in umzuege:
            _nach_umzug(anker, umhaengungen[objekt], zuzug.get(objekt), heute(objekt), jetzt)
        elif objekt in zuzug:
            _mit_zuzug(anker, zuordnung, zuzug[objekt], vorher.get(objekt, {}), jetzt)
        else:
            _bewerten(art, organisation, anker, zuordnung, jetzt, bericht)

    for ziel, kennung in zuzug.items():
        if ziel in anker_an or kennung is None:
            continue
        RisAnker.objects.update_or_create(
            organization_id=organisation,
            art=art,
            objekt=ziel,
            defaults={
                "kennung": kennung.as_dict(),
                "status": RisAnker.AKTUELL,
                "status_seit": jetzt,
                "geprueft_am": jetzt,
                "bestaetigt_am": jetzt,
            },
        )


def _je_organisation(
    entscheidungen: list[tuple[RisAnker, Zuordnung]],
) -> dict[Any, list[tuple[RisAnker, Zuordnung]]]:
    gruppen: dict[Any, list[tuple[RisAnker, Zuordnung]]] = defaultdict(list)
    for anker, zuordnung in entscheidungen:
        gruppen[anker.organization_id].append((anker, zuordnung))
    return gruppen


# =============================================================================
# Abgleich
# =============================================================================


def _melden(art: str, entscheidungen: list[tuple[RisAnker, Zuordnung]], bericht: Bericht) -> None:
    for anker, zuordnung in entscheidungen:
        if zuordnung.ergebnis == ris_neu.BESTAETIGT:
            continue
        ziel = str(zuordnung.ziel) if zuordnung.ziel else None
        bericht.geplant.append((art, str(anker.objekt), zuordnung.ergebnis, ziel))
        logger.info("RIS-Neuveröffentlichung (Probe): %s %s %s %s", art, anker.objekt, zuordnung.ergebnis, ziel)


def _bestaetigen(entscheidungen: list[tuple[RisAnker, Zuordnung]], jetzt: datetime) -> None:
    """Probe: bestätigte Anker fortschreiben (Kennung, Zeitpunkte); sonst ändert die Probe nichts."""
    for anker, zuordnung in entscheidungen:
        if zuordnung.ergebnis != ris_neu.BESTAETIGT:
            continue
        felder: dict[str, Any] = {"geprueft_am": jetzt, "bestaetigt_am": jetzt}
        if zuordnung.kennung is not None:
            felder["kennung"] = zuordnung.kennung.as_dict()
        RisAnker.objects.filter(pk=anker.pk).update(**felder)


def _teil_anwenden(
    art: str,
    bezeichnung: object,
    entscheidungen: list[tuple[RisAnker, Zuordnung]],
    heute: Heute,
    *,
    anwenden: bool,
    jetzt: datetime,
    bericht: Bericht,
) -> None:
    """Eine Sitzung bzw. die Vorlagen: in einer Transaktion, ein Datenbankfehler betrifft nur diesen Teil."""
    teil = Bericht(modus=bericht.modus)
    try:
        with transaction.atomic():
            if anwenden:
                for organisation, eigene in _je_organisation(entscheidungen).items():
                    _anwenden(art, organisation, eigene, heute, jetzt, teil)
            else:
                _bestaetigen(entscheidungen, jetzt)
                _melden(art, entscheidungen, teil)
    except DatabaseError:
        bericht.fehler += 1
        logger.warning(
            "RIS-Abgleich: %s %s übersprungen; der nächste Lauf versucht es erneut.", art, bezeichnung, exc_info=True
        )
        return
    bericht.uebernehmen(teil)


def _heute_tops(stand: list[TopStand]) -> Heute:
    nach_id = {s.id: s for s in stand}

    def heute(objekt: uuid.UUID) -> TopKennung | None:
        eintrag = nach_id.get(objekt)
        if eintrag is None or not eintrag.auf_tagesordnung:
            return None
        return ris_neu.bestaetigte_kennung(eintrag, stand)

    return heute


def _bestaetigung_fortschreiben(
    gruppen: Mapping[str, list[tuple[RisAnker, TopKennung]]], sitzungen: Iterable[uuid.UUID], jetzt: datetime
) -> None:
    """Unveränderte Sitzungen: Was beim letzten Bestätigen galt, gilt bis jetzt (später Angelegtes wandert mit)."""
    ids = [
        anker.pk
        for sitzung in sitzungen
        for anker, _ in gruppen.get(str(sitzung), [])
        if anker.status == RisAnker.AKTUELL and anker.geprueft_am is not None
    ]
    for start in range(0, len(ids), 500):
        RisAnker.objects.filter(pk__in=ids[start : start + 500], status=RisAnker.AKTUELL).update(bestaetigt_am=jetzt)


def _tops_abgleichen(bericht: Bericht, *, anwenden: bool, alle: bool, jetzt: datetime) -> None:
    gruppen: dict[str, list[tuple[RisAnker, TopKennung]]] = defaultdict(list)
    for anker in RisAnker.objects.filter(art=RisAnker.ART_TOP):
        kennung = TopKennung.from_dict(anker.kennung)
        if kennung is not None:
            gruppen[kennung.meeting].append((anker, kennung))
    if not gruppen:
        return

    if alle:
        sitzungen = sorted((uuid.UUID(m) for m in gruppen), key=str)
    else:
        seit: dict[str, datetime | None] = {}
        for m, liste in gruppen.items():
            zeitpunkte = [a.geprueft_am for a, _ in liste]
            seit[m] = None if None in zeitpunkte else min(z for z in zeitpunkte if z is not None)
        lage = ris_neu.sitzungen_pruefen(seit, ruhig_seit=jetzt - RUHEZEIT)
        sitzungen = sorted(lage.geaendert, key=str)
        _bestaetigung_fortschreiben(gruppen, lage.unveraendert, jetzt)

    for start in range(0, len(sitzungen), STAPEL):
        stapel = sitzungen[start : start + STAPEL]
        staende = ris_neu.tagesordnungen(stapel)
        for sitzung in stapel:
            stand = staende.get(sitzung, [])
            entscheidungen = [
                (anker, ris_neu.top_zuordnen(kennung, anker.objekt, stand)) for anker, kennung in gruppen[str(sitzung)]
            ]
            bericht.geprueft += len(entscheidungen)
            _teil_anwenden(
                RisAnker.ART_TOP,
                sitzung,
                entscheidungen,
                _heute_tops(stand),
                anwenden=anwenden,
                jetzt=jetzt,
                bericht=bericht,
            )


def _vorlage_faellig(
    anker: RisAnker, stand: ris_neu.VorlagenStand | None, *, ruhig_seit: datetime, jetzt: datetime
) -> bool:
    """Geänderte Vorlagen nach der Ruhezeit; entfallene und mehrdeutige nur mit Abstand (``NEUPRUEFUNG``)."""
    if stand is not None and stand.geaendert is not None and stand.geaendert > ruhig_seit:
        return False  # in Bewegung
    if anker.geprueft_am is None:
        return True
    unveraendert = stand is not None and (stand.geaendert is None or stand.geaendert <= anker.geprueft_am)
    if anker.status == RisAnker.AKTUELL:
        return not unveraendert
    kuerzlich = anker.geprueft_am > jetzt - NEUPRUEFUNG
    return not ((unveraendert or stand is None) and kuerzlich)


def _vorlagen_abgleichen(bericht: Bericht, *, anwenden: bool, alle: bool, jetzt: datetime) -> None:
    anker_liste = [
        (anker, kennung)
        for anker in RisAnker.objects.filter(art=RisAnker.ART_VORLAGE)
        if (kennung := VorlagenKennung.from_dict(anker.kennung)) is not None
    ]
    staende = ris_neu.vorlagen(anker.objekt for anker, _ in anker_liste)
    ruhig_seit = jetzt - RUHEZEIT
    kandidaten: dict[tuple[str, str], list[ris_neu.VorlagenStand]] = {}
    entscheidungen: list[tuple[RisAnker, Zuordnung]] = []
    for anker, kennung in anker_liste:
        stand = staende.get(anker.objekt)
        if not alle and not _vorlage_faellig(anker, stand, ruhig_seit=ruhig_seit, jetzt=jetzt):
            continue
        gesucht = (kennung.body, ris_neu.reference_key(kennung.reference))
        if (stand is None or stand.geloescht) and gesucht not in kandidaten:
            kandidaten[gesucht] = ris_neu.vorlagen_mit_nummer(kennung)
        entscheidungen.append(
            (anker, ris_neu.vorlage_zuordnen(kennung, anker.objekt, stand, kandidaten.get(gesucht, [])))
        )
    bericht.geprueft += len(entscheidungen)

    def heute(objekt: uuid.UUID) -> VorlagenKennung | None:
        eintrag = staende.get(objekt)
        return eintrag.kennung if eintrag is not None and not eintrag.geloescht else None

    for organisation, eigene in _je_organisation(entscheidungen).items():
        _teil_anwenden(
            RisAnker.ART_VORLAGE, organisation, eigene, heute, anwenden=anwenden, jetzt=jetzt, bericht=bericht
        )


def abgleichen(*, modus: str | None = None, alle: bool = False, jetzt: datetime | None = None) -> Bericht:
    """
    Ein Lauf: Anker pflegen, geänderte Sitzungen und Vorlagen prüfen, Work-Daten umhängen (``modus`` ``aktiv``).

    ``alle`` prüft jede Sitzung und Vorlage, auch unveränderte und gerade erst geänderte.
    """
    modus = modus or settings.WORK_RIS_RELINK
    if modus not in MODI:
        raise ValueError("Unbekannter Modus des Abgleichs.")
    bericht = Bericht(modus=modus)
    if modus == "aus":
        return bericht
    if not cache.add(_SPERRE, "1", timeout=_SPERRE_SEKUNDEN):
        bericht.gesperrt = True
        return bericht
    try:
        jetzt = jetzt or timezone.now()
        for art in (RisAnker.ART_TOP, RisAnker.ART_VORLAGE):
            _anker_pflegen(art, bericht, jetzt)
        anwenden = modus == "aktiv"
        _tops_abgleichen(bericht, anwenden=anwenden, alle=alle, jetzt=jetzt)
        _vorlagen_abgleichen(bericht, anwenden=anwenden, alle=alle, jetzt=jetzt)
    finally:
        cache.delete(_SPERRE)
    return bericht


# =============================================================================
# Rückweg
# =============================================================================


@dataclass
class Rueckbericht:
    """Ergebnis von ``zurueckdrehen``."""

    gesperrt: bool = False
    eintraege: int = 0
    datensaetze: int = 0
    #: (Protokolleintrag, Verknüpfung, Datensatz): hängt nicht mehr am Ziel oder scheitert an einer Eindeutigkeit
    nicht_moeglich: list[tuple[str, str, str]] = field(default_factory=list)


def _festhalten(eintrag: RisNeuzuordnung, datensaetze: Datensaetze) -> None:
    """Zurückgedrehte Datensätze bleiben am früheren Objekt und ziehen nie wieder automatisch um."""
    anker = RisAnker.objects.filter(
        organization_id=eintrag.organization_id, art=eintrag.art, objekt=eintrag.von
    ).first()
    if anker is not None:
        RisAnker.objects.filter(pk=anker.pk).update(
            zurueckgelassen=_vereinen(anker.zurueckgelassen, datensaetze),
            frueherer_titel=anker.frueherer_titel or _titel(anker),
            geprueft_am=None,
        )
        return
    kennung = _kennungen(eintrag.art, [eintrag.von]).get(eintrag.von)
    RisAnker.objects.create(
        organization_id=eintrag.organization_id,
        art=eintrag.art,
        objekt=eintrag.von,
        kennung=kennung.as_dict() if kennung else {},
        bestaetigt_am=timezone.now(),
        zurueckgelassen=_vereinen(datensaetze),
        frueherer_titel=kennung.title if kennung else "",
    )


def _geparkt_zurueckdrehen(
    offen: list[tuple[RisNeuzuordnung, str, str]], zurueck: dict[uuid.UUID, Datensaetze], bericht: Rueckbericht
) -> tuple[list[tuple[RisNeuzuordnung, str, str]], bool]:
    """Rückzüge, die einzeln an der Bedingung scheitern, gemeinsam über den Zwischenplatz (``Verknuepfung.parken``)."""
    gruppen: dict[Verknuepfung, list[tuple[RisNeuzuordnung, str, str]]] = defaultdict(list)
    for eintrag, name, pk in offen:
        verknuepfung = _NACH_NAME.get((eintrag.art, name))
        if verknuepfung is not None and verknuepfung.parken:
            gruppen[verknuepfung].append((eintrag, name, pk))
    erledigt: set[tuple[Any, str, str]] = set()
    for verknuepfung, liste in gruppen.items():
        tabelle, spalte, _ = verknuepfung.tabelle()
        zuege = [(pk, eintrag.nach, eintrag.von) for eintrag, _name, pk in liste]
        umgezogen, _zurueck = _geparkt_umhaengen(tabelle._base_manager, spalte, verknuepfung.parken, zuege)
        for nummer in sorted(umgezogen):
            eintrag, name, pk = liste[nummer]
            zurueck[eintrag.pk].setdefault(name, []).append(pk)
            bericht.datensaetze += 1
            erledigt.add((eintrag.pk, name, pk))
    rest = [(e, name, pk) for e, name, pk in offen if (e.pk, name, pk) not in erledigt]
    return rest, bool(erledigt)


def zurueckdrehen(eintraege: Iterable[RisNeuzuordnung], *, probe: bool = False) -> Rueckbericht:
    """
    Dreht Umzüge aus dem Protokoll zurück (Rückweg im Betrieb, Befehl ``ris_neuzuordnung_zurueckdrehen``).

    Datensätze, die noch am Ziel hängen, kommen an ihr früheres Objekt und bleiben dort (``zurueckgelassen``).
    Jüngste Umzüge zuerst, damit Ketten aufgehen; was an einer Eindeutigkeit scheitert, versucht der nächste
    Durchgang erneut. Inhalte und ``updated_at`` bleiben unberührt.
    """
    liste = sorted(
        (e for e in eintraege if e.ergebnis == ris_neu.NACHFOLGER and e.nach and e.zurueckgedreht_am is None),
        key=lambda e: e.erfolgt_am,
        reverse=True,
    )
    bericht = Rueckbericht(eintraege=len(liste))
    offen = [(e, name, str(pk)) for e in liste for name, pks in (e.verschoben or {}).items() for pk in pks]
    if probe:
        bericht.datensaetze = len(offen)
        return bericht
    if not cache.add(_SPERRE, "1", timeout=_SPERRE_SEKUNDEN):
        return Rueckbericht(gesperrt=True)
    try:
        zurueck: dict[uuid.UUID, Datensaetze] = defaultdict(dict)
        with transaction.atomic():
            weiter = True
            while offen and weiter:
                weiter = False
                rest = []
                for eintrag, name, pk in offen:
                    verknuepfung = _NACH_NAME.get((eintrag.art, name))
                    if verknuepfung is None:
                        bericht.nicht_moeglich.append((str(eintrag.pk), name, pk))
                        continue
                    tabelle, spalte, _ = verknuepfung.tabelle()
                    try:
                        with transaction.atomic():
                            anzahl = tabelle._base_manager.filter(pk=pk, **{spalte: eintrag.nach}).update(
                                **{spalte: eintrag.von}
                            )
                    except IntegrityError:
                        rest.append((eintrag, name, pk))
                        continue
                    weiter = True
                    if anzahl:
                        zurueck[eintrag.pk].setdefault(name, []).append(pk)
                        bericht.datensaetze += 1
                    else:
                        bericht.nicht_moeglich.append((str(eintrag.pk), name, pk))
                offen = rest
                if offen and not weiter:
                    # Festgefahren, etwa ein Tausch zweier Positionen: gemeinsam über den Zwischenplatz, wo möglich
                    offen, weiter = _geparkt_zurueckdrehen(offen, zurueck, bericht)
            bericht.nicht_moeglich.extend((str(e.pk), name, pk) for e, name, pk in offen)
            jetzt = timezone.now()
            for eintrag in liste:
                if zurueck.get(eintrag.pk):
                    _festhalten(eintrag, zurueck[eintrag.pk])
                RisNeuzuordnung.objects.filter(pk=eintrag.pk).update(zurueckgedreht_am=jetzt)
    finally:
        cache.delete(_SPERRE)
    for eintrag_pk, name, pk in bericht.nicht_moeglich:
        logger.warning("RIS-Neuzuordnung %s: %s %s nicht zurückgedreht", eintrag_pk, name, pk)
    return bericht


# =============================================================================
# Anzeige
# =============================================================================


@dataclass(frozen=True)
class Hinweis:
    """Hinweis an einem Tagesordnungspunkt der Vorbereitung."""

    titel: str
    text: str


def hinweise_fuer_tops(organisation: Any, agenda_item_ids: Iterable[object]) -> dict[uuid.UUID, Hinweis]:
    """
    Punkte, an denen Work-Daten der Organisation nicht (mehr) sicher zu diesem Punkt gehören, mit Hinweis für die
    Oberfläche. Nur eigene Anker: Ob eine andere Organisation am Punkt arbeitet, bleibt verborgen.
    """
    ids = [v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in agenda_item_ids]
    if not ids or organisation is None:
        return {}
    hinweise: dict[uuid.UUID, Hinweis] = {}
    zeilen = RisAnker.objects.filter(organization=organisation, art=RisAnker.ART_TOP, objekt__in=ids).values_list(
        "objekt", "status", "kennung", "zurueckgelassen", "frueherer_titel"
    )
    for objekt, status, daten, reste, frueherer_titel in zeilen:
        kennung = TopKennung.from_dict(daten)
        if status in (RisAnker.NICHT_ZUGEORDNET, RisAnker.MEHRDEUTIG):
            frueher = f" („{kennung.title}“)" if kennung and kennung.title else ""
            hinweise[objekt] = Hinweis(
                "Nicht zugeordnet",
                "Das Ratsinformationssystem hat die Tagesordnung neu veröffentlicht. Notizen und Positionen an "
                f"diesem Punkt stammen von einem früheren Stand{frueher}.",
            )
        elif status == RisAnker.ENTFALLEN:
            hinweise[objekt] = Hinweis(
                "Nicht mehr auf der Tagesordnung",
                "Der Punkt steht nicht mehr auf der Tagesordnung. Notizen und Positionen bleiben erhalten.",
            )
        elif reste:
            frueher = f" („{frueherer_titel}“)" if frueherer_titel else ""
            hinweise[objekt] = Hinweis(
                "Nicht zugeordnet",
                "Das Ratsinformationssystem hat die Tagesordnung neu veröffentlicht. Einzelne Notizen oder Positionen "
                f"an diesem Punkt stammen von einem früheren Stand{frueher}.",
            )
    return hinweise
