# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work-Daten folgen ihrem Tagesordnungspunkt bzw. ihrer Vorlage über Neuveröffentlichungen (Issue #547).

Ablauf (Zeitplan ``ris_verknuepfungen_abgleichen`` im Worker, Befehl ``ris_verknuepfungen_abgleichen``):

1. **Anker pflegen:** Für jedes RIS-Objekt, an dem Work-Daten einer Organisation hängen (``VERKNUEPFUNGEN``), hat
   die Organisation einen ``RisAnker`` mit der fachlichen Kennung (``hub.ris.neuveroeffentlichung``). Neue
   Verknüpfungen mit einem Tagesordnungspunkt erfasst ein Signal sofort, damit die Kennung den Stand beim
   Verknüpfen trägt; der Abgleich holt fehlende Anker nach und räumt Anker ohne Verknüpfung weg.
2. **Prüfen:** Sitzungen, an denen sich seit der letzten Prüfung etwas geändert hat und die seit ``RUHEZEIT``
   ruhen, und Vorlagen, die sich geändert haben oder entfallen sind.
3. **Umhängen:** Hat ein Punkt bzw. eine Vorlage genau einen Nachfolger, wandern die Datensätze der Organisation
   dorthin (nur der Fremdschlüssel; verschlüsselte Inhalte bleiben unberührt, ``updated_at`` auch). Hat das Ziel
   schon einen Datensatz derselben Person bzw. Organisation (private Notiz, Redebeitrag, Position), bleibt der
   alte, wo er ist – es wird nichts zusammengeführt. Jeder Umzug steht mit den Kennungen der Datensätze in
   ``RisNeuzuordnung``.
4. **Nicht zuordnen statt falsch zuordnen:** Steht unter der alten Kennung inzwischen ein anderer Punkt und gibt
   es keinen eindeutigen Nachfolger, bleibt alles, wo es ist; die Vorbereitung zeigt „Nicht zugeordnet“ mit dem
   früheren Titel.

Mandantentrennung: Anker, Entscheidung, Umhängen, Protokoll und Hinweis gelten je Organisation; die Regeln lesen
nur den RIS-Bestand. Organisation und Autor eines Datensatzes bleiben, ein Nachfolger liegt immer in derselben
Sitzung bzw. Kommune.

``WORK_RIS_RELINK``: ``aktiv`` (Standard) hängt um, ``probe`` pflegt nur Anker und meldet, was geschähe,
``aus`` tut nichts.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, models, transaction
from django.db.models.signals import post_save
from django.utils import timezone

from hub.ris import neuveroeffentlichung as ris_neu
from hub.ris.neuveroeffentlichung import TopKennung, VorlagenKennung, Zuordnung

from .models import RisAnker, RisNeuzuordnung

logger = logging.getLogger(__name__)

#: Eine Sitzung wird erst geprüft, wenn sie so lange unverändert ist (kein halber Stand mitten im Abruf).
RUHEZEIT = timedelta(minutes=10)
#: Sitzungen je Lesevorgang
STAPEL = 50
_SPERRE = "work:ris_verknuepfungen_abgleichen"
_SPERRE_SEKUNDEN = 30 * 60

MODI = ("aus", "probe", "aktiv")


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
    m2m: bool = False

    @property
    def name(self) -> str:
        return f"{self.modell}.{self.feld}"

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
    Verknuepfung("work.AgendaItemPosition", "agenda_item", eindeutig=("organization_id",)),
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

_STATUS = {
    ris_neu.BESTAETIGT: RisAnker.AKTUELL,
    ris_neu.ABWEICHEND: RisAnker.NICHT_ZUGEORDNET,
    ris_neu.ENTFALLEN: RisAnker.ENTFALLEN,
    ris_neu.MEHRDEUTIG: RisAnker.MEHRDEUTIG,
}

#: (Organisation, RIS-Objekt)
Schluessel = tuple[Any, uuid.UUID]


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
    nicht_zugeordnet: int = 0
    entfallen: int = 0
    mehrdeutig: int = 0
    #: Probe: (Art, bisheriges Objekt, Ergebnis, Nachfolger)
    geplant: list[tuple[str, str, str, str | None]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        zahlen = {k: v for k, v in self.__dict__.items() if k != "geplant"}
        zahlen["geplant"] = len(self.geplant)
        return zahlen


@dataclass
class _Umhaengung:
    verschoben: dict[str, list[str]] = field(default_factory=dict)
    konflikte: dict[str, list[str]] = field(default_factory=dict)

    @property
    def anzahl(self) -> int:
        return sum(len(v) for v in self.verschoben.values())

    @property
    def anzahl_konflikte(self) -> int:
        return sum(len(v) for v in self.konflikte.values())


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
                defaults={"kennung": kennung.as_dict() if kennung else {}},
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


def _anker_pflegen(art: str, bericht: Bericht) -> None:
    """Fehlende Anker anlegen, leere Kennungen erfassen, Anker ohne Verknüpfung entfernen."""
    # Erst die Anker, dann die Verknüpfungen lesen: Ein dazwischen angelegter Anker wird so nicht entfernt.
    vorhanden: dict[Schluessel, RisAnker] = {
        (a.organization_id, a.objekt): a
        for a in RisAnker.objects.filter(art=art).only("pk", "organization_id", "objekt", "kennung")
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
            )
            for organisation, objekt in neu
        ],
        ignore_conflicts=True,
    )
    bericht.anker_neu += len(angelegt)
    for anker in leer:
        if anker.objekt in kennungen:
            RisAnker.objects.filter(pk=anker.pk).update(kennung=kennungen[anker.objekt].as_dict())


# =============================================================================
# Umhängen
# =============================================================================


def _umhaengen(art: str, organisation: Any, umzuege: dict[uuid.UUID, uuid.UUID]) -> dict[uuid.UUID, _Umhaengung]:
    """
    Hängt die Datensätze der Organisation von ``von`` nach ``nach`` um; ein Datensatz, dessen Gegenstück am Ziel
    bleibt, bleibt selbst stehen (Konflikt, nichts wird zusammengeführt).

    Ketten und Tausch innerhalb einer Sitzung gehen in einem Durchgang. Steht die Eindeutigkeit nur in der Logik
    (Position je Organisation), zählt der Endzustand; steht sie in der Datenbank, auch jeder Zwischenstand – ein
    Tausch zweier Punkte mit Notizen derselben Person bleibt dann stehen.
    """
    ergebnis = {von: _Umhaengung() for von in umzuege}
    objekte = set(umzuege) | set(umzuege.values())
    for verknuepfung in VERKNUEPFUNGEN[art]:
        tabelle, spalte, eindeutig = verknuepfung.tabelle()
        manager = tabelle._base_manager
        auswahl = manager.filter(**{f"{spalte}__in": objekte, verknuepfung.organisation: organisation})
        zeilen = [(pk, objekt, tuple(werte)) for pk, objekt, *werte in auswahl.values_list("pk", spalte, *eindeutig)]
        offen = [(pk, objekt, schluessel) for pk, objekt, schluessel in zeilen if objekt in umzuege]
        # Schlüssel, die am jeweiligen Objekt belegt sind: im Endzustand nur die bleibenden, sonst die heutigen
        belegt: dict[tuple[Any, tuple[Any, ...]], Any] = {}
        for pk, objekt, schluessel in zeilen:
            if eindeutig and None not in schluessel and (verknuepfung.db_eindeutig or objekt not in umzuege):
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
                ergebnis[von].verschoben.setdefault(verknuepfung.name, []).append(str(pk))
            offen = rest
        for pk, von, _ in offen:
            ergebnis[von].konflikte.setdefault(verknuepfung.name, []).append(str(pk))
    return ergebnis


def _anwenden(
    art: str,
    organisation: Any,
    entscheidungen: list[tuple[RisAnker, Zuordnung]],
    stehend: set[uuid.UUID] | None,
    jetzt: datetime,
    bericht: Bericht,
) -> None:
    """Entscheidungen einer Organisation (für eine Sitzung bzw. die Vorlagen) umsetzen."""
    umzuege = {a.objekt: z.ziel for a, z in entscheidungen if z.ergebnis == ris_neu.NACHFOLGER and z.ziel is not None}
    umhaengungen = _umhaengen(art, organisation, umzuege) if umzuege else {}

    for anker, zuordnung in entscheidungen:
        if anker.objekt in umzuege:
            continue
        status = _STATUS[zuordnung.ergebnis]
        felder: dict[str, Any] = {"geprueft_am": jetzt}
        if zuordnung.ergebnis == ris_neu.BESTAETIGT and zuordnung.kennung is not None:
            felder["kennung"] = zuordnung.kennung.as_dict()
        if status != anker.status:
            felder.update(status=status, status_seit=jetzt)
            RisNeuzuordnung.objects.create(
                organization_id=organisation, art=art, von=anker.objekt, ergebnis=zuordnung.ergebnis
            )
            if status == RisAnker.NICHT_ZUGEORDNET:
                bericht.nicht_zugeordnet += 1
            elif status == RisAnker.ENTFALLEN:
                bericht.entfallen += 1
            elif status == RisAnker.MEHRDEUTIG:
                bericht.mehrdeutig += 1
        RisAnker.objects.filter(pk=anker.pk).update(**felder)

    mit_rest: set[uuid.UUID] = set()
    for anker, zuordnung in entscheidungen:
        if anker.objekt not in umzuege:
            continue
        umhaengung = umhaengungen[anker.objekt]
        RisNeuzuordnung.objects.create(
            organization_id=organisation,
            art=art,
            von=anker.objekt,
            nach=zuordnung.ziel,
            ergebnis=ris_neu.NACHFOLGER,
            verschoben=umhaengung.verschoben,
            konflikte=umhaengung.konflikte,
        )
        bericht.umgehaengt += 1
        bericht.datensaetze += umhaengung.anzahl
        bericht.konflikte += umhaengung.anzahl_konflikte
        logger.info(
            "RIS-Neuveröffentlichung: %s %s → %s, %s Datensätze umgehängt, %s geblieben",
            art,
            anker.objekt,
            zuordnung.ziel,
            umhaengung.anzahl,
            umhaengung.anzahl_konflikte,
        )
        if umhaengung.konflikte:
            mit_rest.add(anker.objekt)
            steht = stehend is not None and anker.objekt in stehend
            RisAnker.objects.filter(pk=anker.pk).update(
                status=RisAnker.NICHT_ZUGEORDNET if steht else RisAnker.ENTFALLEN, status_seit=jetzt, geprueft_am=jetzt
            )
        else:
            RisAnker.objects.filter(pk=anker.pk).delete()

    for anker, zuordnung in entscheidungen:
        ziel = umzuege.get(anker.objekt)
        if ziel is None or ziel in mit_rest or zuordnung.kennung is None:
            continue
        RisAnker.objects.update_or_create(
            organization_id=organisation,
            art=art,
            objekt=ziel,
            defaults={
                "kennung": zuordnung.kennung.as_dict(),
                "status": RisAnker.AKTUELL,
                "status_seit": jetzt,
                "geprueft_am": jetzt,
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


def _tops_abgleichen(bericht: Bericht, *, anwenden: bool, alle: bool, jetzt: datetime) -> None:
    gruppen: dict[str, list[tuple[RisAnker, TopKennung]]] = defaultdict(list)
    for anker in RisAnker.objects.filter(art=RisAnker.ART_TOP):
        kennung = TopKennung.from_dict(anker.kennung)
        if kennung is not None:
            gruppen[kennung.meeting].append((anker, kennung))
    if not gruppen:
        return

    if alle:
        sitzungen = [uuid.UUID(m) for m in gruppen]
    else:
        seit: dict[str, datetime | None] = {}
        for m, liste in gruppen.items():
            zeitpunkte = [a.geprueft_am for a, _ in liste]
            seit[m] = None if None in zeitpunkte else min(z for z in zeitpunkte if z is not None)
        sitzungen = sorted(ris_neu.sitzungen_geaendert(seit, ruhig_seit=jetzt - RUHEZEIT))

    for start in range(0, len(sitzungen), STAPEL):
        stapel = sitzungen[start : start + STAPEL]
        staende = ris_neu.tagesordnungen(stapel)
        for sitzung in stapel:
            stand = staende.get(sitzung, [])
            entscheidungen = [
                (anker, ris_neu.top_zuordnen(kennung, anker.objekt, stand)) for anker, kennung in gruppen[str(sitzung)]
            ]
            bericht.geprueft += len(entscheidungen)
            if not anwenden:
                _melden(RisAnker.ART_TOP, entscheidungen, bericht)
                continue
            stehend = {s.id for s in stand if s.auf_tagesordnung}
            with transaction.atomic():
                for organisation, eigene in _je_organisation(entscheidungen).items():
                    _anwenden(RisAnker.ART_TOP, organisation, eigene, stehend, jetzt, bericht)


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
        if not alle:
            unveraendert = (
                stand is not None
                and anker.geprueft_am is not None
                and anker.status == RisAnker.AKTUELL
                and (stand.geaendert is None or stand.geaendert <= anker.geprueft_am)
            )
            in_bewegung = stand is not None and stand.geaendert is not None and stand.geaendert > ruhig_seit
            if unveraendert or in_bewegung:
                continue
        gesucht = (kennung.body, ris_neu.reference_key(kennung.reference))
        if (stand is None or stand.geloescht) and gesucht not in kandidaten:
            kandidaten[gesucht] = ris_neu.vorlagen_mit_nummer(kennung)
        entscheidungen.append(
            (anker, ris_neu.vorlage_zuordnen(kennung, anker.objekt, stand, kandidaten.get(gesucht, [])))
        )
    bericht.geprueft += len(entscheidungen)
    if not entscheidungen:
        return
    if not anwenden:
        _melden(RisAnker.ART_VORLAGE, entscheidungen, bericht)
        return
    with transaction.atomic():
        for organisation, eigene in _je_organisation(entscheidungen).items():
            _anwenden(RisAnker.ART_VORLAGE, organisation, eigene, None, jetzt, bericht)


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
            _anker_pflegen(art, bericht)
        anwenden = modus == "aktiv"
        _tops_abgleichen(bericht, anwenden=anwenden, alle=alle, jetzt=jetzt)
        _vorlagen_abgleichen(bericht, anwenden=anwenden, alle=alle, jetzt=jetzt)
    finally:
        cache.delete(_SPERRE)
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
    zeilen = (
        RisAnker.objects.filter(organization=organisation, art=RisAnker.ART_TOP, objekt__in=ids)
        .exclude(status=RisAnker.AKTUELL)
        .values_list("objekt", "status", "kennung")
    )
    for objekt, status, daten in zeilen:
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
    return hinweise
