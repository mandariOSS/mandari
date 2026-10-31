# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antragstellende Fraktionen der Vorgänge ermitteln und speichern (Issue #1138).

Die Erkennung selbst rechnet ``urheber_erkennung`` (ohne Datenbank, ohne KI). Dieses Modul lädt dafür je Körperschaft
die Fraktionen und Ratsgruppen samt Mitgliedschaften (``kommune_laden``) und je Vorgang Titel, Art, Datum, die
Verweise der Quelle und das Ende des Hauptdokuments (``vorlagen_laden``), und speichert das Ergebnis in
``VorgangUrheber`` mit dem Stand in ``VorgangUrheberStand``.

**Fraktionen der Kommune:** Gremien des RIS-Bestands, die Fraktion oder Ratsgruppe sind (``ist_fraktion``), nicht die
Parteien der Wahl. Kennt OParl in der Kommune keine einzige Fraktionsmitgliedschaft, gilt die gepflegte Liste
``FraktionsZugehoerigkeit`` (Münster): Ihre Fraktionen kommen dazu, soweit es sie nicht schon als Gremium gibt, und
ihre Personen zählen für Antragsteller und Unterschriften.

**Wann es läuft:** nach dem Abruf über das Abonnement ``insight.urheber`` (neue oder geänderte Vorlage, neuer Text
einer Anlage), einmalig über den Bestand mit ``manage.py vorgang_urheber_ermitteln`` (in Stapeln, drosselbar,
setzt nach Abbruch fort) und erneut für alles, was eine ältere ``VERSION`` der Erkennung bearbeitet hat.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any, Final, cast

from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Right
from django.utils import timezone

from ..models import (
    FraktionsBezeichnung,
    FraktionsZugehoerigkeit,
    OParlFile,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    VorgangUrheber,
    VorgangUrheberStand,
)
from . import urheber_erkennung as erkennung
from .urheber_erkennung import Fraktion, Kommune, Vorlage, Zugehoerigkeit

if TYPE_CHECKING:
    from apps.events.models import Event
    from apps.events.registry import Delivery

logger = logging.getLogger(__name__)

NAME: Final = "insight.urheber"
TYPES: Final = ("ris.paper.released", "ris.paper.created", "ris.paper.changed", "ris.file.text_extracted")
BATCH: Final = 200
QUEUE: Final = "default"


def ist_fraktion(org: OParlOrganization) -> bool:
    """Fraktion oder Ratsgruppe (keine Partei, kein Ausschuss, keine Einzelmandate), siehe ``urheber_erkennung``."""
    return erkennung.ist_fraktion(
        org.name or org.short_name or "", org.organization_type or "", org.classification or ""
    )


def _fraktion(org: OParlOrganization) -> Fraktion:
    name = org.name or org.short_name or ""
    return Fraktion.aus_name(
        name,
        organisation_id=org.id,
        art=erkennung.GRUPPE if erkennung.ist_gruppe(name, org.classification or "") else erkennung.FRAKTION,
        beginn=org.start_date,
        ende=org.end_date,
        kurzname=org.short_name or "",
        kurzform=org.short_name if org.short_name and len(org.short_name) < len(name) else "",
    )


def _ohne_titel_teilen(name: str) -> tuple[str, str]:
    """„Dr. Max Mustermann“ → („Max“, „Mustermann“), wenn die Quelle Vor- und Nachname nicht getrennt liefert."""
    teile = [t for t in re.sub(r"\b(?:Prof|Dr|Dipl|Ing)\.\s*", " ", name or "").split() if t]
    if len(teile) < 2:
        return "", ""
    return " ".join(teile[:-1]), teile[-1]


def _schluessel(vorname: str, nachname: str) -> tuple[str, str]:
    return (erkennung._ohne_titel(vorname).casefold(), erkennung._ohne_titel(nachname).casefold())


def kommune_laden(body_id: Any) -> Kommune:
    """Fraktionen und Ratsgruppen der Körperschaft mit den Personen, die ihnen angehören."""
    orgs = [o for o in OParlOrganization.objects.filter(body_id=body_id, deleted=False) if ist_fraktion(o)]
    fraktionen = {o.id: _fraktion(o) for o in orgs if (o.name or o.short_name)}
    zugehoerigkeiten: list[Zugehoerigkeit] = []
    mitgliedschaften = OParlMembership.objects.filter(organization_id__in=list(fraktionen), deleted=False).values_list(
        "person_id",
        "organization_id",
        "start_date",
        "end_date",
        "person__given_name",
        "person__family_name",
        "person__name",
    )
    for person_id, org_id, beginn, ende, vorname, nachname, name in mitgliedschaften:
        if not (vorname and nachname):
            vorname, nachname = _ohne_titel_teilen(name or "")
        zugehoerigkeiten.append(
            Zugehoerigkeit(
                fraktion=fraktionen[org_id],
                person_id=person_id,
                vorname=vorname or "",
                nachname=nachname or "",
                beginn=beginn,
                ende=ende,
            )
        )
    liste = list(fraktionen.values())
    if not zugehoerigkeiten:
        gepflegt = _gepflegt_laden(body_id, liste)
        zugehoerigkeiten = gepflegt
        for z in gepflegt:
            if all(z.fraktion.bezeichnung.casefold() != f.bezeichnung.casefold() for f in liste):
                liste.append(z.fraktion)
    return Kommune(fraktionen=liste, zugehoerigkeiten=zugehoerigkeiten)


def _gepflegt_laden(body_id: Any, oparl: list[Fraktion]) -> list[Zugehoerigkeit]:
    """
    Gepflegte Zugehörigkeiten der Körperschaft (``FraktionsZugehoerigkeit``) mit den Namen der Fraktionen zum Datum.

    Je Fraktion und Zeitraum ihres Namens (``FraktionsBezeichnung``; ohne Eintrag gilt der Name der Zugehörigkeit)
    entsteht eine ``Fraktion``; die Zugehörigkeit einer Person wird auf die Zeiträume der Namen aufgeteilt. Eine
    Fraktion, die es als Gremium im RIS gibt, zeigt auf das Gremium. Was mit der Liste beginnt, gilt auch davor: Die
    Liste startet mit einer Wahlperiode, Titel älterer Anträge nennen die Fraktion unter demselben Namen. Später
    Gegründetes (etwa eine Fraktion ab 01.10.2026) gilt erst ab dann.
    """
    eintraege = list(FraktionsZugehoerigkeit.objects.filter(body_id=body_id).exclude(fraktion=""))
    if not eintraege:
        return []
    personen: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for pid, vorname, nachname, name in OParlPerson.objects.filter(body_id=body_id, deleted=False).values_list(
        "id", "given_name", "family_name", "name"
    ):
        if not (vorname and nachname):
            vorname, nachname = _ohne_titel_teilen(name or "")
        if vorname and nachname:
            personen[_schluessel(vorname, nachname)].append(pid)
    namen: dict[str, list[FraktionsBezeichnung]] = defaultdict(list)
    for bezeichnung in FraktionsBezeichnung.objects.filter(body_id=body_id).order_by("gueltig_ab"):
        namen[bezeichnung.fraktion.casefold()].append(bezeichnung)

    anfaenge_alle = [e.gueltig_ab for e in eintraege if e.gueltig_ab]
    listenbeginn = min(anfaenge_alle) if anfaenge_alle else None

    def offen_davor(beginn: date | None) -> date | None:
        return None if beginn is not None and beginn == listenbeginn else beginn

    je_fraktion: dict[tuple[str, str], list[FraktionsZugehoerigkeit]] = defaultdict(list)
    for e in eintraege:
        je_fraktion[(e.fraktion.casefold(), e.art)].append(e)

    ergebnis: list[Zugehoerigkeit] = []
    for (schluessel, art), mitglieder in je_fraktion.items():
        anfaenge = [e.gueltig_ab for e in mitglieder]
        enden = [e.gueltig_bis for e in mitglieder]
        beginn = None if None in anfaenge else min(a for a in anfaenge if a)
        ende = None if None in enden else max(b for b in enden if b)
        kurzform = next((e.kurzform for e in mitglieder if e.kurzform), "")
        zeitraeume = [(n.bezeichnung, n.kurzform or kurzform, n.gueltig_ab, n.gueltig_bis) for n in namen[schluessel]]
        if not zeitraeume:
            zeitraeume = [(mitglieder[0].fraktion, kurzform, beginn, ende)]
        for name, kurz, ab, bis in zeitraeume:
            fraktion = Fraktion.aus_name(name, art=art, beginn=offen_davor(ab), ende=bis, kurzform=kurz)
            gleich = [
                f
                for f in oparl
                if (f.schluessel and f.schluessel == fraktion.schluessel)
                or (f.eigenname and f.eigenname == fraktion.eigenname)
            ]
            if len(gleich) == 1 and art != FraktionsZugehoerigkeit.ART_EINZELMANDAT:
                fraktion = dataclasses.replace(gleich[0], kurzform=gleich[0].kurzform or fraktion.kurzform)
            for e in mitglieder:
                von = max((d for d in (e.gueltig_ab, ab) if d), default=None)
                bis_person = min((d for d in (e.gueltig_bis, bis) if d), default=None)
                if von and bis_person and bis_person < von:
                    continue  # Person gehörte der Fraktion unter diesem Namen nicht an
                treffer = personen.get(_schluessel(e.vorname, e.nachname), [])
                ergebnis.append(
                    Zugehoerigkeit(
                        fraktion=fraktion,
                        person_id=treffer[0] if len(treffer) == 1 else None,
                        vorname=e.vorname,
                        nachname=e.nachname,
                        beginn=von,
                        ende=bis_person,
                    )
                )
    return ergebnis


def _referenzen(wert: Any) -> list[str]:
    """Kennungen aus einem Verweis der Rohdaten (Adresse, eingebettetes Objekt oder Liste davon)."""
    werte = wert if isinstance(wert, list) else [wert]
    kennungen = []
    for w in werte:
        if isinstance(w, dict):
            w = w.get("id")
        if isinstance(w, str) and w:
            kennungen.append(w)
    return kennungen


def vorlagen_laden(paper_ids: Iterable[Any]) -> dict[Any, tuple[Any, Vorlage]]:
    """Vorgang → (Körperschaft, Daten für die Erkennung); gelöschte Vorgänge entfallen."""
    zeilen = list(
        OParlPaper.objects.filter(pk__in=list(paper_ids), deleted=False).values(
            "id",
            "body_id",
            "name",
            "paper_type",
            "date",
            "raw_json__mainFile",
            "raw_json__originatorPerson",
            "raw_json__originatorOrganization",
        )
    )
    if not zeilen:
        return {}
    org_ext = {ext for z in zeilen for ext in _referenzen(z["raw_json__originatorOrganization"])}
    person_ext = {ext for z in zeilen for ext in _referenzen(z["raw_json__originatorPerson"])}
    orgs = {
        o.external_id: o
        for o in OParlOrganization.objects.filter(external_id__in=org_ext, deleted=False).only(
            "id", "external_id", "body_id", "name", "short_name", "classification", "start_date", "end_date"
        )
    }
    personen = dict(
        OParlPerson.objects.filter(external_id__in=person_ext, deleted=False).values_list("external_id", "id")
    )
    texte = _haupttexte(zeilen)

    ergebnis: dict[Any, tuple[Any, Vorlage]] = {}
    for z in zeilen:
        quelle = [
            _fraktion(o)
            for ext in _referenzen(z["raw_json__originatorOrganization"])
            if (o := orgs.get(ext)) is not None and o.body_id == z["body_id"] and (o.name or o.short_name)
        ]
        ergebnis[z["id"]] = (
            z["body_id"],
            Vorlage(
                name=z["name"] or "",
                paper_type=z["paper_type"] or "",
                datum=z["date"],
                quelle=quelle,
                personen=[personen[ext] for ext in _referenzen(z["raw_json__originatorPerson"]) if ext in personen],
                text=texte.get(z["id"], ""),
            ),
        )
    return ergebnis


def _haupttexte(zeilen: Sequence[Mapping[str, Any]]) -> dict[Any, str]:
    """
    Ende des Texts des Hauptdokuments je Vorgang (``TEXTENDE`` Zeichen, gelesen in der Datenbank).

    Hauptdokument ist ``mainFile`` der Quelle; ohne Angabe die einzige Anlage. Bei mehreren Anlagen ohne Angabe
    lesen wir keine: Eine Stellungnahme in der Anlage nennt andere Namen als der Antrag.
    """
    haupt_ext = {z["id"]: next(iter(_referenzen(z["raw_json__mainFile"])), None) for z in zeilen}
    dateien = (
        OParlFile.objects.filter(paper_id__in=[z["id"] for z in zeilen], deleted=False)
        .exclude(text_content__isnull=True)
        .exclude(text_content="")
        .annotate(ende=Right("text_content", erkennung.TEXTENDE))
        .values_list("paper_id", "external_id", "ende")
    )
    je_vorgang: dict[Any, list[tuple[str, str]]] = defaultdict(list)
    for paper_id, ext, ende in dateien:
        je_vorgang[paper_id].append((ext, ende or ""))
    anzahl = dict(
        OParlFile.objects.filter(paper_id__in=list(je_vorgang), deleted=False)
        .values("paper_id")
        .annotate(n=Count("id"))
        .values_list("paper_id", "n")
    )
    texte = {}
    for paper_id, liste in je_vorgang.items():
        haupt = haupt_ext.get(paper_id)
        if haupt:
            texte[paper_id] = next((text for ext, text in liste if ext == haupt), "")
        elif anzahl.get(paper_id) == 1:
            texte[paper_id] = liste[0][1]
    return texte


def ermitteln(
    paper_ids: Iterable[Any], *, speichern: bool = True, kommunen: dict[Any, Kommune] | None = None
) -> dict[Any, erkennung.Ergebnis]:
    """
    Antragstellende Fraktionen der Vorgänge ermitteln und (mit ``speichern``) ablegen.

    Liefert Vorgang → ``Ergebnis``. ``kommunen`` dient als Zwischenspeicher über mehrere Aufrufe (Bestandslauf).
    """
    kommunen = {} if kommunen is None else kommunen
    daten = vorlagen_laden(paper_ids)
    ergebnisse: dict[Any, erkennung.Ergebnis] = {}
    koerperschaft: dict[Any, Any] = {}
    for paper_id, (body_id, vorlage) in daten.items():
        if body_id not in kommunen:
            kommunen[body_id] = kommune_laden(body_id)
        ergebnisse[paper_id] = erkennung.ermitteln(vorlage, kommunen[body_id])
        koerperschaft[paper_id] = body_id
    if speichern and ergebnisse:
        _speichern(ergebnisse, koerperschaft)
    return ergebnisse


def _speichern(ergebnisse: dict[Any, erkennung.Ergebnis], koerperschaft: dict[Any, Any]) -> None:
    jetzt = timezone.now()
    zeilen = [
        VorgangUrheber(
            paper_id=paper_id,
            body_id=koerperschaft[paper_id],
            organisation_id=cast(uuid.UUID | None, f.organisation_id),
            bezeichnung=f.bezeichnung[:500],
            art=f.art,
            kurzform=f.kurzform[:100],
            herkunft=ergebnis.herkunft or "",
        )
        for paper_id, ergebnis in ergebnisse.items()
        for f in ergebnis.fraktionen
    ]
    staende = [
        VorgangUrheberStand(
            paper_id=paper_id,
            body_id=koerperschaft[paper_id],
            version=erkennung.VERSION,
            herkunft=ergebnis.herkunft or "",
            ermittelt=jetzt,
        )
        for paper_id, ergebnis in ergebnisse.items()
    ]
    with transaction.atomic():
        VorgangUrheber.objects.filter(paper_id__in=list(ergebnisse)).delete()
        VorgangUrheber.objects.bulk_create(zeilen)
        VorgangUrheberStand.objects.bulk_create(
            staende,
            update_conflicts=True,
            unique_fields=["paper_id"],
            update_fields=["body_id", "version", "herkunft", "ermittelt"],
        )


# =============================================================================
# Lesen (Insight, Schnittstelle)
# =============================================================================


def urheber_je_vorgang(paper_ids: Iterable[Any]) -> dict[Any, list[VorgangUrheber]]:
    """Gespeicherte antragstellende Fraktionen je Vorgang, in einer Abfrage."""
    je_vorgang: dict[Any, list[VorgangUrheber]] = defaultdict(list)
    for zeile in VorgangUrheber.objects.filter(paper_id__in=list(paper_ids)).order_by("bezeichnung"):
        je_vorgang[zeile.paper_id].append(zeile)
    return je_vorgang


def fraktionen_der_kommune(body_id: Any) -> list[tuple[str, str]]:
    """(Bezeichnung, Name für Leser) der Fraktionen, die in der Kommune Vorgänge gestellt haben (Filter)."""
    paare = dict(VorgangUrheber.objects.filter(body_id=body_id).values_list("bezeichnung", "kurzform").distinct())
    return sorted(((b, k or b) for b, k in paare.items()), key=lambda paar: paar[1].casefold())


# =============================================================================
# Abonnement nach dem Abruf
# =============================================================================


def _vorgaenge(events: list[Event]) -> set[uuid.UUID]:
    """Betroffene Vorgänge: die Vorlage selbst oder der Vorgang der Datei mit neuem Text."""
    vorgaenge: set[uuid.UUID] = set()
    dateien: set[uuid.UUID] = set()
    for event in events:
        roh = event.aggregate_id
        try:
            kennung = uuid.UUID(str(roh))
        except (TypeError, ValueError):
            continue
        if event.aggregate_type == "Paper":
            vorgaenge.add(kennung)
        elif event.aggregate_type == "File":
            dateien.add(kennung)
    if dateien:
        vorgaenge.update(
            OParlFile.objects.filter(pk__in=dateien, paper_id__isnull=False).values_list("paper_id", flat=True)
        )
    return vorgaenge


def urheber_abonnement(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements ``insight.urheber``: betroffene Vorgänge neu ermitteln (idempotent)."""
    vorgaenge = _vorgaenge(events)
    if not vorgaenge:
        return
    if delivery.shadow:
        logger.info("Abonnement %s (Schatten): %d Vorgänge würden ermittelt", NAME, len(vorgaenge))
        return
    ergebnisse = ermitteln(vorgaenge)
    gefunden = sum(1 for e in ergebnisse.values() if e.herkunft)
    logger.info("Abonnement %s: %d Vorgänge ermittelt, %d mit Fraktion", NAME, len(ergebnisse), gefunden)


# =============================================================================
# Bestandslauf
# =============================================================================


def koerperschaft_finden(wert: str) -> Any:
    """Körperschaft per Kürzel (``slug``), Kennung oder genauem Namen; ``None``, wenn es keine eindeutige gibt."""
    from django.db.models import Q

    from ..models import OParlBody

    abfrage = Q(slug=wert) | Q(name=wert)
    with contextlib.suppress(ValueError):
        abfrage |= Q(pk=uuid.UUID(wert))
    treffer = list(OParlBody.objects.filter(abfrage)[:2])
    return treffer[0] if len(treffer) == 1 else None


def ausstehend(body_id: Any = None, *, alle: bool = False) -> Any:
    """Vorgänge ohne Stand der aktuellen Version (mit ``alle``: jeder Vorgang), nach Kennung sortiert."""
    vorgaenge = OParlPaper.objects.filter(deleted=False)
    if body_id is not None:
        vorgaenge = vorgaenge.filter(body_id=body_id)
    if not alle:
        aktuell = VorgangUrheberStand.objects.filter(version__gte=erkennung.VERSION)
        if body_id is not None:
            aktuell = aktuell.filter(body_id=body_id)
        vorgaenge = vorgaenge.exclude(pk__in=aktuell.values("paper_id"))
    return vorgaenge.order_by("pk").values_list("pk", flat=True)
