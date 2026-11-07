# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weg eines Vorgangs im Bürgerportal (Issue #1273): Vorlage, übergeordnete Vorlage, Folgevorlagen (Stellungnahmen,
Änderungsanträge) und Beratungen als eine Zeitleiste, Lücken mit Namen, die federführende Stelle und der Stand einer
Anfrage samt „Gefragt und geantwortet“.

Grundlage sind die OParl-Felder der Vorlage, wie der Ingestor sie in ``raw_json`` ablegt: ``superordinatedPaper``,
``subordinatedPaper`` und ``relatedPaper`` (verbundene Vorlagen) sowie ``underDirectionOf`` (Federführung). Abgeleitet
wird wenig und nachvollziehbar:

- **Anfrage** ist eine Vorlage, deren Art „Anfrage“ nennt (nicht „Bauvoranfrage“); bei Sammelarten wie „Anträge und
  Anfragen“ entscheidet der Titel.
- **Antwort** ist eine Folgevorlage der Anfrage, deren Art oder Titel Stellungnahme, Antwort, Beantwortung, Mitteilung
  oder Information nennt, ersatzweise ein Dokument der Anfrage mit „Antwort“, „Beantwortung“ oder „Stellungnahme“ im
  Namen. Eine nur verwandte Vorlage (``relatedPaper``) muss sich zusätzlich als Antwort ausweisen (Art Stellungnahme,
  Antwort oder Beantwortung) oder sich im Titel auf die Anfrage beziehen; eine Mitteilung zum selben Thema genügt nicht.
  Hat ein Gremium die Anfrage als beantwortet oder zur Kenntnis genommen vermerkt, gilt sie ebenfalls als beantwortet,
  nicht aber bei einer verneinten, angekündigten oder vertagten Antwort („nicht beantwortet“, „wird schriftlich
  beantwortet“, „Antwort folgt“, „Beantwortung vertagt“).
- **Antwort steht aus**, wenn nichts davon vorliegt und die Anfrage jünger als ein Jahr ist oder eine Beratung noch
  ansteht. Bei älteren Anfragen ohne jede Spur sagen wir nur, dass uns keine Antwort vorliegt.
- **Federführung** zeigen wir nur, wenn ``underDirectionOf`` auf ein Gremium bzw. eine Organisationseinheit im Bestand
  zeigt, nie eine Person.

Verwandte Vorlagen (``relatedPaper``) kommen nur als Antwort einer Anfrage in die Zeitleiste; sonst stünde dort, was
nur ähnlich ist. Rückverweise (Vorlagen, die nur selbst auf diese zeigen) suchen wir nicht, das wäre je Seitenaufruf
eine Suche über alle Vorlagen der Kommune. Neue KI-Aufrufe gibt es hier keine: Fragen und Antworten stehen im Wortlaut
der Dokumente (``fragen_antworten``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Final

from django.urls import reverse
from django.utils import timezone

from hub.ris import selectors as ris_selectors

from . import fragen_antworten
from .ergebnis import (
    ABGELEHNT,
    ABGESETZT,
    ANTWORT_OFFEN,
    BEANTWORTET,
    KENNTNIS,
    VERTAGT,
    VERWIESEN,
    Ergebnis,
    anfrage_etikett,
    antwort_aussteht,
    ergebnis,
)
from .paper_status import DEFERRED, LATEST, PAST, UPCOMING, PaperStatus, timeline

#: Arten der Stationen; die Zahl ordnet Stationen am selben Tag (erst die Vorlage, dann ihre Folgen, dann Beratungen)
STAMMVORLAGE: Final = "stammvorlage"
VORLAGE: Final = "vorlage"
FOLGEVORLAGE: Final = "folgevorlage"
ANTWORT: Final = "antwort"
BERATUNG: Final = "beratung"
LUECKE: Final = "luecke"
_RANG: Final = {STAMMVORLAGE: 0, VORLAGE: 1, FOLGEVORLAGE: 2, ANTWORT: 2, BERATUNG: 3, LUECKE: 4}

#: Zustand einer Station mit benannter Lücke (gestrichelter Punkt im Zeitstrahl)
OFFEN: Final = "open"

#: So lange nach dem Datum der Anfrage heißt eine fehlende Antwort „steht aus“; danach „liegt uns nicht vor“
ANTWORT_OFFEN_TAGE: Final = 365
#: Höchstens so viele Zeichen aus dem Text je Dokument für „Gefragt und geantwortet“
TEXT_ZEICHEN: Final = 60_000
#: Längster Titel einer verbundenen Vorlage in der Zeitleiste
TITEL_ZEICHEN: Final = 120
#: Ab so vielen Zeichen steht ein Ergebnis ohne Etikett-Begriff zusätzlich ausgeschrieben
ROH_ZEICHEN: Final = 40

_ANFRAGE: Final = re.compile(r"(?<!vor)anfrage|einwohnerfrage|fragestunde")
_ANTRAG: Final = re.compile(r"antr[aä]g")
_ANTWORT_ART: Final = re.compile(r"stellungnahme|antwort|beantwortung|mitteilung|information")
_ANTWORT_TITEL: Final = re.compile(r"(antwort|beantwortung|stellungnahme)\b")
#: Name eines Antwort-Dokuments: beginnt mit Antwort, Beantwortung oder Stellungnahme oder nennt die Antwort der
#: Verwaltung ausdrücklich („Anlage 1 – Antwort der Verwaltung“); „Anfrage zur Stellungnahme des Landes“ zählt nicht
_ANTWORT_DATEI: Final = re.compile(
    r"^\W*(?:anlage\s*\d*\W*)?(?:antwort|beantwortung|stellungnahme)\b"
    r"|\b(?:antwort|stellungnahme) der verwaltung\b|\bbeantwortung de[rs] anfrage\b"
)
#: Ergebnis einer Beratung, nach dem die Anfrage dort beantwortet wurde („beantwortet“, „mündlich beantwortet“,
#: „Beantwortung erfolgte mündlich“, „erledigt durch Beantwortung“); dazu eine Kenntnisnahme. Verneinte und angekündigte
#: Antworten schließt ``ergebnis.antwort_aussteht`` vorher aus.
_IN_SITZUNG_BEANTWORTET: Final = re.compile(
    r"\bbeantwortet\b|\bbeantwortung\s+(?:ist\s+erfolgt|erfolgte)\b|\bmündlich\w*\s+beantwort"
    r"|\berledigt durch\s+(?:\w+\s+){0,2}beantwortung\b"
)
#: Etiketten einer Beratung, nach der die Antwort noch aussteht: vertagt, verwiesen, abgesetzt, abgelehnt
_OHNE_ANTWORT: Final = frozenset({VERTAGT, VERWIESEN, ABGESETZT, ABGELEHNT})
_ZURUECKGEZOGEN: Final = re.compile(r"zurückgezogen|erledigt")
#: Art einer verwandten Vorlage, die sich selbst als Antwort ausweist (eine Mitteilung oder Information genügt nicht)
_ANTWORT_EIGENE_ART: Final = re.compile(r"stellungnahme|antwort|beantwortung")
#: Titel einer verwandten Vorlage, der eine Antwort oder die Anfrage nennt („Mitteilung zur Anfrage der …“)
_BEZUG_AUF_ANFRAGE: Final = re.compile(r"\b(?:antwort|beantwortung|anfrage)\b")


def _klein(text: object) -> str:
    return " ".join(str(text or "").split()).lower()


def ist_anfrage(art: str | None, titel: str | None = None) -> bool:
    """
    Ist die Vorlage eine Anfrage? Nach der Art („Große Anfrage“, „Anfrage nach § 17“, „Einwohnerfrage“), nie eine
    Antwort darauf („Beantwortung einer Anfrage“) und nie eine Bauvoranfrage. Nennt die Art auch Anträge oder fehlt sie,
    entscheidet der Anfang des Titels („SPD-Anfrage: …“, „Anfrage der Fraktion …“).
    """
    art_klein = _klein(art)
    anfrage_art = bool(_ANFRAGE.search(art_klein))
    if anfrage_art and not _ANTWORT_ART.search(art_klein) and not _ANTRAG.search(art_klein):
        return True
    if art_klein and not anfrage_art:
        return False
    # Sammelart („Anträge und Anfragen“, „Mitteilungen/Anfragen“) oder keine Art: der Anfang des Titels
    anfang = _klein(titel)[:80]
    return bool(_ANFRAGE.search(anfang)) and not _ANTWORT_TITEL.match(anfang)


def ist_antwort(art: str | None, titel: str | None = None) -> bool:
    """Ist eine Folgevorlage die Antwort der Verwaltung (Stellungnahme, Antwort, Beantwortung, Mitteilung)?"""
    if _ANTRAG.search(_klein(art)):
        return False
    return bool(_ANTWORT_ART.search(_klein(art)) or _ANTWORT_TITEL.match(_klein(titel)))


def antwort_auf(anfrage: Any, vorlage: Any, *, verwandt: bool = False) -> bool:
    """
    Ist ``vorlage`` die Antwort auf ``anfrage``? Eine Antwort-Vorlage (``ist_antwort``), die nicht vor der Anfrage
    datiert ist. Eine nur verwandte Vorlage (``verwandt``, OParl ``relatedPaper``) muss sich außerdem als Antwort
    ausweisen: Ihre Art nennt Stellungnahme, Antwort oder Beantwortung, oder ihr Titel nennt eine Antwort, die Anfrage,
    deren Nummer oder lautet wie die Anfrage. Eine verwandte Mitteilung zum selben Thema ist noch keine Antwort.
    """
    if not ist_antwort(vorlage.paper_type, vorlage.name):
        return False
    if anfrage.date is not None and vorlage.date is not None and vorlage.date < anfrage.date:
        return False
    if not verwandt:
        return True
    titel = _klein(vorlage.name)
    nummer = _klein(anfrage.reference)
    return bool(
        _ANTWORT_EIGENE_ART.search(_klein(vorlage.paper_type))
        or _BEZUG_AUF_ANFRAGE.search(titel)
        or (len(nummer) >= 3 and nummer in titel)
        or (titel and titel == _klein(anfrage.name))
    )


def ist_antwort_datei(name: str | None) -> bool:
    """Ist ein Dokument der Anfrage selbst die Antwort („Antwort der Verwaltung.pdf“, „Beantwortung“)?"""
    return bool(_ANTWORT_DATEI.search(_klein(name)))


@dataclass(frozen=True)
class Station:
    """
    Eine Station im Weg des Vorgangs: ``art`` (Vorlage, Folgevorlage, Antwort, Beratung, Lücke), Titel und Ziel, Datum
    (oder ``datum_text`` statt eines Datums), Zustand wie im Zeitstrahl (``paper_status``: past, latest, upcoming,
    cancelled, undated, dazu ``open`` für eine benannte Lücke), Detailzeile und Ergebnis-Etikett. ``ergebnis_text`` ist
    der Ergebnistext des Ratsinformationssystems, wenn er mehr sagt als das Etikett.
    """

    art: str
    titel: str
    datum: date | datetime | None = None
    datum_text: str = ""
    zustand: str = PAST
    href: str = ""
    detail: str = ""
    ergebnis: Ergebnis | None = None
    ergebnis_text: str = ""


@dataclass(frozen=True)
class AnfrageStand:
    """
    Stand einer Anfrage: ``schluessel`` ist ``BEANTWORTET``, ``ANTWORT_OFFEN`` oder leer (keine Antwort bekannt, aber
    zu alt für „steht aus“), ``satz`` ergänzt den Stand-Satz im Kopf. Woher die Antwort kommt: ``antwort_vorlage``
    (Folgevorlage), ``antwort_datei`` (Dokument der Anfrage) oder ``in_sitzung`` (Ergebnis einer Beratung).
    """

    schluessel: str
    satz: str
    antwort_vorlage: Any = None
    antwort_datei: Any = None
    in_sitzung: bool = False

    @property
    def etikett(self) -> Ergebnis | None:
        return anfrage_etikett(self.schluessel) if self.schluessel else None

    @property
    def beantwortet(self) -> bool:
        return self.schluessel == BEANTWORTET

    @property
    def luecke(self) -> bool:
        """Fehlt die Antwort (steht aus oder liegt uns nicht vor)? Nicht bei zurückgezogenen Anfragen (ohne Satz)."""
        return not self.beantwortet and bool(self.satz)


@dataclass(frozen=True)
class WegDesVorgangs:
    """Alles für die Vorgangsseite: Stationen, federführende Stelle, Stand der Anfrage, Fragen und Antworten."""

    stationen: list[Station] = field(default_factory=list)
    federfuehrung: str = ""
    anfrage: AnfrageStand | None = None
    fragen: list[fragen_antworten.FrageAntwort] | None = None

    @property
    def zeigen(self) -> bool:
        """Zeitleiste zeigen, wenn sie mehr enthält als die Vorlage selbst."""
        return any(station.art != VORLAGE for station in self.stationen)


def _kuerzen(text: str, grenze: int = TITEL_ZEICHEN) -> str:
    text = " ".join(text.split())
    if len(text) <= grenze:
        return text
    schnitt = text[: grenze - 1]
    if " " in schnitt:
        schnitt = schnitt[: schnitt.rindex(" ")]
    return schnitt.rstrip(" ,;:-–") + "…"


def _tag(wert: date | datetime) -> date:
    if isinstance(wert, datetime):
        return timezone.localtime(wert).date() if timezone.is_aware(wert) else wert.date()
    return wert


def _gleicher_titel(a: str | None, b: str | None) -> bool:
    x, y = _klein(a), _klein(b)
    return bool(x and y) and (x in y or y in x)


def federfuehrung(paper: Any, namen: Mapping[str, str]) -> str:
    """Name der ersten federführenden Stelle aus ``underDirectionOf``, die im Bestand ein Gremium ist; sonst leer."""
    for kennung in ris_selectors.oparl_references(paper.raw_json, "underDirectionOf"):
        if namen.get(kennung):
            return namen[kennung]
    return ""


def _vorlage_detail(vorlage: Any, titel_der_seite: str | None, amt: str, *, mit_art: bool = False) -> str:
    """Detailzeile einer verbundenen Vorlage: Nummer (mit Art), Titel, wenn er etwas anderes sagt, federführend."""
    teile: list[str] = []
    nummer = " ".join(part for part in ((vorlage.paper_type or "") if mit_art else "", vorlage.reference or "") if part)
    if nummer:
        teile.append(nummer)
    if vorlage.name and not _gleicher_titel(vorlage.name, titel_der_seite):
        teile.append(f"„{_kuerzen(vorlage.name)}“")
    if amt:
        teile.append(f"federführend: {amt}")
    return ", ".join(teile)


def _beratung_detail(eintrag: Mapping[str, Any], now: datetime) -> str:
    """Detailzeile einer Beratung (``paper_status``) und die Lücke beim Ergebnis, wenn eines fehlen müsste."""
    teile = [eintrag["detail"]] if eintrag.get("detail") else []
    vergangen = eintrag.get("date") and eintrag["date"] <= now and eintrag.get("state") not in ("cancelled",)
    if vergangen and eintrag.get("public", True) is not False and not _klein(eintrag.get("result")):
        frisch = now - eintrag["date"] <= timedelta(days=60)
        teile.append("Ergebnis noch nicht veröffentlicht" if frisch else "kein Ergebnis angegeben")
    return ", ".join(teile)


def _beratung(eintrag: Mapping[str, Any], zustand: str, now: datetime) -> Station:
    meeting = eintrag.get("meeting")
    href = ""
    if meeting is not None and getattr(meeting, "id", None):
        href = reverse("insight_core:insight:meeting_detail", args=[meeting.id])
    roh = " ".join(str(eintrag.get("result") or "").split())
    etikett = ergebnis(roh) if roh else None
    zusatz = ""
    if etikett is not None:
        zusatz = etikett.roh_abweichend if etikett.bekannt else (roh if len(roh) > ROH_ZEICHEN else "")
    return Station(
        art=BERATUNG,
        titel=eintrag.get("organization_name") or "Sitzung",
        datum=eintrag.get("date"),
        zustand=zustand,
        href=href,
        detail=_beratung_detail(eintrag, now),
        ergebnis=etikett,
        ergebnis_text=zusatz,
    )


def _vorgang_url(vorlage: Any) -> str:
    return reverse("insight_core:insight:paper_detail", kwargs={"pk": vorlage.pk})


def _vorlage_station(art: str, vorlage: Any, titel: str, detail: str) -> Station:
    return Station(
        art=art,
        titel=titel,
        datum=vorlage.date,
        datum_text="" if vorlage.date else "ohne Datum",
        href=_vorgang_url(vorlage),
        detail=detail,
    )


def anfrage_stand(
    paper: Any,
    antworten: Sequence[Any],
    antwort_dateien: Sequence[Any],
    verlauf: Sequence[Mapping[str, Any]],
    status: PaperStatus,
    now: datetime,
) -> AnfrageStand:
    """
    Stand einer Anfrage aus Antwort-Vorlagen (``antworten``, nach Datum), Antwort-Dokumenten und dem Beratungsverlauf.

    Im Verlauf zählt die jüngste Beratung mit Ergebnis: „beantwortet“ oder eine Kenntnisnahme heißt beantwortet. Eine
    verneinte, angekündigte oder vertagte Antwort („nicht beantwortet“, „wird schriftlich beantwortet“, „Kenntnis
    genommen; Antwort folgt schriftlich“, „Beantwortung vertagt“) heißt, dass sie danach noch aussteht; frühere
    Beratungen ändern daran nichts.
    """
    if antworten:
        vorlage = antworten[-1]
        wann = f" am {vorlage.date:%d.%m.%Y}" if vorlage.date else ""
        return AnfrageStand(BEANTWORTET, f"Die Verwaltung hat{wann} geantwortet.", antwort_vorlage=vorlage)
    if antwort_dateien:
        datei = antwort_dateien[0]
        return AnfrageStand(BEANTWORTET, "Die Antwort der Verwaltung liegt als Dokument vor.", antwort_datei=datei)
    beraten = [
        e for e in verlauf if e.get("date") and e["date"] <= now and not getattr(e.get("meeting"), "cancelled", False)
    ]
    for eintrag in reversed(beraten):
        roh = _klein(eintrag.get("result"))
        etikett = ergebnis(roh) if roh else None
        if etikett is None:
            continue
        if antwort_aussteht(roh) or etikett.schluessel in _OHNE_ANTWORT:
            # Antwort verneint, nur angekündigt oder die Beratung vertagt: Sie steht aus (bzw. liegt uns nicht vor)
            break
        if _IN_SITZUNG_BEANTWORTET.search(roh) or etikett.schluessel == KENNTNIS:
            return AnfrageStand(BEANTWORTET, "", in_sitzung=True)
        if _ZURUECKGEZOGEN.search(roh):
            # Zurückgezogen oder erledigt: keine Antwort zu erwarten, also auch keine Lücke
            return AnfrageStand("", "")
    juenger = paper.date is not None and _tag(now) - paper.date <= timedelta(days=ANTWORT_OFFEN_TAGE)
    if juenger or status.upcoming is not None or status.kind == DEFERRED:
        return AnfrageStand(ANTWORT_OFFEN, "Eine Antwort der Verwaltung liegt noch nicht vor.")
    return AnfrageStand("", "Eine Antwort der Verwaltung liegt uns nicht vor.")


def stationen(
    paper: Any,
    verlauf: Sequence[Mapping[str, Any]],
    status: PaperStatus,
    *,
    stammvorlagen: Iterable[Any] = (),
    folgevorlagen: Iterable[Any] = (),
    antworten: Iterable[Any] = (),
    namen: Mapping[str, str] | None = None,
    anfrage: AnfrageStand | None = None,
    urheber: Iterable[Any] = (),
    now: datetime | None = None,
) -> list[Station]:
    """
    Stationen des Vorgangs in zeitlicher Reihenfolge: Vorlage, übergeordnete Vorlagen, Folgevorlagen bzw. Antworten,
    Beratungen und – bei einer Anfrage ohne Antwort – die Lücke „Antwort der Verwaltung“ an der Stelle von heute.
    Beratungen ohne Termin stehen ebenfalls bei heute, Vorlagen ohne Datum zuletzt. „zuletzt“ trägt die Beratung, auf
    der der Stand-Satz beruht, sobald es mehr als eine Station gibt.
    """
    now = now or timezone.now()
    namen = namen or {}
    liste: list[tuple[Station, Any]] = []
    if paper.date:
        gestellt = ", ".join(u.anzeigename for u in urheber if getattr(u, "anzeigename", ""))
        liste.append(
            (
                Station(
                    art=VORLAGE,
                    titel=paper.paper_type or "Vorlage",
                    datum=paper.date,
                    detail=f"Gestellt von {gestellt}" if gestellt else "",
                ),
                None,
            )
        )
    for vorlage in stammvorlagen:
        amt = federfuehrung(vorlage, namen)
        detail = _vorlage_detail(vorlage, paper.name, amt)
        liste.append((_vorlage_station(STAMMVORLAGE, vorlage, vorlage.paper_type or "Vorlage", detail), None))
    for vorlage in antworten:
        detail = _vorlage_detail(vorlage, paper.name, federfuehrung(vorlage, namen), mit_art=True)
        liste.append((_vorlage_station(ANTWORT, vorlage, "Antwort der Verwaltung", detail), None))
    for vorlage in folgevorlagen:
        detail = _vorlage_detail(vorlage, paper.name, federfuehrung(vorlage, namen))
        liste.append((_vorlage_station(FOLGEVORLAGE, vorlage, vorlage.paper_type or "Folgevorlage", detail), None))
    for eintrag, original in zip(timeline(verlauf, status, now), verlauf, strict=True):
        liste.append((_beratung(eintrag, eintrag["state"], now), original))
    if anfrage is not None and anfrage.antwort_datei is not None:
        datei = anfrage.antwort_datei
        titel = datei.name or datei.file_name or "Dokument"
        liste.append(
            (
                Station(
                    art=ANTWORT,
                    titel="Antwort der Verwaltung",
                    datum=datei.file_date,
                    datum_text="" if datei.file_date else "ohne Datum",
                    href="#dokumente-titel",
                    detail=f"Dokument „{_kuerzen(titel)}“",
                ),
                None,
            )
        )
    if anfrage is not None and anfrage.luecke:
        offen = anfrage.schluessel == ANTWORT_OFFEN
        liste.append(
            (
                Station(
                    art=LUECKE,
                    titel="Antwort der Verwaltung",
                    datum_text="offen" if offen else "nicht bekannt",
                    zustand=OFFEN,
                    detail="Steht noch aus" if offen else "Liegt uns nicht vor",
                ),
                None,
            )
        )

    heute = _tag(now)

    def schluessel(paar: tuple[Station, Any]) -> tuple[date, int]:
        station = paar[0]
        if station.datum:
            return _tag(station.datum), _RANG[station.art]
        return (heute if station.art in (BERATUNG, LUECKE) else date.max), _RANG[station.art]

    liste.sort(key=schluessel)
    ergebnis_liste: list[Station] = []
    for station, original in liste:
        if original is not None and original is status.last and len(liste) > 1:
            station = replace(station, zustand=LATEST)
        elif station.art != BERATUNG and station.zustand == PAST and station.datum and _tag(station.datum) > heute:
            station = replace(station, zustand=UPCOMING)
        ergebnis_liste.append(station)
    return ergebnis_liste


def _nach_name(dateien: Iterable[Any]) -> list[Any]:
    """Dokumente nach Name und Dateiname (die Vorgangsseite lädt sie ohne feste Reihenfolge)."""
    return sorted(dateien, key=lambda d: (_klein(d.name), _klein(d.file_name)))


def _hauptdatei(dateien: Sequence[Any], paper: Any, *, ohne: Sequence[Any] = ()) -> Any:
    """
    Hauptdokument einer Vorlage: OParl ``mainFile``, sonst das erste Dokument nach Name; beides ohne die in ``ohne``.
    """
    kennungen = ris_selectors.oparl_references(paper.raw_json, "mainFile")
    kandidaten = [d for d in _nach_name(dateien) if d not in ohne]
    return next((d for d in kandidaten if d.external_id in kennungen), kandidaten[0] if kandidaten else None)


def _haupt_und_antwortdatei(dateien: Sequence[Any], paper: Any) -> tuple[Any, list[Any]]:
    """
    Hauptdokument einer Anfrage und die Antwort-Dokumente unter ihren Dokumenten (nach Name). Das Hauptdokument laut
    OParl (``mainFile``) ist die Anfrage selbst und nie die Antwort.
    """
    haupt_kennungen = ris_selectors.oparl_references(paper.raw_json, "mainFile")
    antworten = [
        d
        for d in _nach_name(dateien)
        if d.external_id not in haupt_kennungen and (ist_antwort_datei(d.name) or ist_antwort_datei(d.file_name))
    ]
    return _hauptdatei(dateien, paper, ohne=antworten), antworten


def _fragen_und_antworten(paper: Any, anfrage: AnfrageStand, dateien: Sequence[Any]) -> Any:
    """
    Paare aus dem Text der Anfrage und der Antwort; ``None``, wenn ein Text fehlt oder nichts sicher passt. Der Text
    kommt nur für die zwei Dokumente aus der Datenbank, die es braucht (Hauptdokument der Anfrage und die Antwort),
    nicht für alle Anlagen.
    """
    if not anfrage.beantwortet or anfrage.in_sitzung:
        return None
    haupt, _ = _haupt_und_antwortdatei(dateien, paper)
    if anfrage.antwort_vorlage is not None:
        fremde = list(ris_selectors.visible_files_of_papers([anfrage.antwort_vorlage.pk]))
        antwort = _hauptdatei(fremde, anfrage.antwort_vorlage)
    else:
        antwort = anfrage.antwort_datei
    if antwort is None:
        return None
    # Nur ein Dokument, und das ist die Antwort: Fragen und Antworten stehen dann meist beide darin
    haupt = haupt or antwort
    texte = ris_selectors.text_starts_of_files([haupt.pk, antwort.pk], max_chars=TEXT_ZEICHEN)
    return fragen_antworten.paare(texte.get(haupt.pk, ""), texte.get(antwort.pk, ""))


def weg_des_vorgangs(
    paper: Any,
    verlauf: Sequence[Mapping[str, Any]],
    status: PaperStatus,
    *,
    dateien: Sequence[Any] = (),
    urheber: Iterable[Any] = (),
    now: datetime | None = None,
) -> WegDesVorgangs:
    """
    Weg des Vorgangs für die Vorgangsseite. ``verlauf`` ist der Beratungsverlauf aus
    ``hub.ris.selectors.consultation_history``, ``status`` der Stand-Satz dazu, ``dateien`` die angezeigten Dokumente.
    Zwei Abfragen (verbundene Vorlagen, Namen der federführenden Stellen), bei einer beantworteten Anfrage eine weitere
    für die zwei Texte und, wenn die Antwort eine eigene Vorlage ist, eine für deren Dokumente.
    """
    now = now or timezone.now()
    verbunden = ris_selectors.related_public_papers(paper)
    stamm = verbunden["superordinatedPaper"]
    folge = verbunden["subordinatedPaper"]
    anfrage_ist = ist_anfrage(paper.paper_type, paper.name)
    antworten: list[Any] = []
    if anfrage_ist:
        antworten = [v for v in folge if antwort_auf(paper, v)]
        antworten += [v for v in verbunden["relatedPaper"] if antwort_auf(paper, v, verwandt=True)]
        antworten = list({v.pk: v for v in antworten}.values())
        antworten.sort(key=lambda v: (v.date is None, v.date or date.min))
        folge = [v for v in folge if v not in antworten]
    kennungen = [
        kennung
        for vorlage in (paper, *stamm, *folge, *antworten)
        for kennung in ris_selectors.oparl_references(vorlage.raw_json, "underDirectionOf")
    ]
    namen = ris_selectors.organization_names_by_external_id(paper.body_id, kennungen)
    anfrage = None
    if anfrage_ist:
        _, antwort_dateien = _haupt_und_antwortdatei(dateien, paper)
        anfrage = anfrage_stand(paper, antworten, antwort_dateien, verlauf, status, now)
    liste = stationen(
        paper,
        verlauf,
        status,
        stammvorlagen=stamm,
        folgevorlagen=folge,
        antworten=antworten,
        namen=namen,
        anfrage=anfrage,
        urheber=urheber,
        now=now,
    )
    fragen = _fragen_und_antworten(paper, anfrage, dateien) if anfrage is not None else None
    return WegDesVorgangs(stationen=liste, federfuehrung=federfuehrung(paper, namen), anfrage=anfrage, fragen=fragen)
