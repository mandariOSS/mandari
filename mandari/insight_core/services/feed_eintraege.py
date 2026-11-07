# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse für die Feeds des Bürgerportals (Issue #1274), ein Eintrag je Ereignis.

=================================  ========================================  ======================================
Ereignis                           Kennung (nach ``feeds.KENNUNG_PRAEFIX``)    Datum (``updated``)
=================================  ========================================  ======================================
neue Vorlage                       ``vorgang/<vorgang>``                      Datum der Vorlage
Beratung mit Ergebnis              ``vorgang/<vorgang>/beratung/<sitzung>``   Beginn der Sitzung
Beratung angesetzt (nur Vorgang)   ``vorgang/<vorgang>/termin/<sitzung>``     seit wann wir die Beratung kennen
neues Dokument (nur Vorgang)       ``vorgang/<vorgang>/dokument/<datei>``     Datum des Dokuments
Beschluss (Gremium, Beschlüsse)    ``beschluss/<tagesordnungspunkt>``          Beginn der Sitzung
Sitzung angesetzt (Gremium)        ``sitzung/<sitzung>``                      seit wann wir die Sitzung kennen
Tagesordnung veröffentlicht        ``sitzung/<sitzung>/tagesordnung``          erster öffentlicher Punkt bekannt
=================================  ========================================  ======================================

Die Kennungen bestehen aus den kanonischen Kennungen der Objekte (UUID aus der OParl-Kennung), nicht aus Datum,
Name oder Position: Verschiebt die Kommune eine Sitzung oder benennt sie einen Punkt um, bleibt der Eintrag derselbe.
Ein Zeitpunkt „seit wann bekannt“ liegt nie in der Zukunft. Ergebnisse stehen als Begriff des festen Vokabulars
(``services.ergebnis``) im Titel, der Text des Ratsinformationssystems in der Zusammenfassung.

KI-Zusammenfassungen stehen bewusst nicht in den Feeds: Ein Eintrag bleibt, wenn der Reader ihn einmal geladen hat,
eine veraltete Kurzfassung ließe sich dort nicht mehr korrigieren.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from hub.ris import selectors as ris_selectors

from ..models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    VorgangUrheber,
)
from .ergebnis import ergebnis
from .feeds import Eintrag, kennung, tagesbeginn, text
from .search_presentation import normalize_paper_type

#: Satz zu einer Sitzung ohne veröffentlichte Tagesordnung (wie auf der Sitzungsseite, Etikett „angekündigt“)
ANGEKUENDIGT_SATZ: Final = "Der Termin steht fest, die Tagesordnung ist noch nicht veröffentlicht."
_WOCHENTAGE: Final = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")
#: Längster Betreff im Titel eines Eintrags (Zeichen)
_BETREFF_MAX: Final = 160
#: Längste Art eines Vorgangs im Titel; längere Kategorien heißen „Vorgang“
_ART_MAX: Final = 30


def _ortszeit(zeitpunkt: datetime) -> datetime:
    return timezone.localtime(zeitpunkt) if timezone.is_aware(zeitpunkt) else zeitpunkt


def datum(zeitpunkt: datetime | None) -> str:
    """„24.11.2026“ in Ortszeit; leer ohne Angabe."""
    return _ortszeit(zeitpunkt).strftime("%d.%m.%Y") if zeitpunkt else ""


def termin(zeitpunkt: datetime | None) -> str:
    """„Di., 24.11.2026, 17:00 Uhr“ in Ortszeit; leer ohne Angabe."""
    if not zeitpunkt:
        return ""
    ort = _ortszeit(zeitpunkt)
    return f"{_WOCHENTAGE[ort.weekday()]} {ort.strftime('%d.%m.%Y, %H:%M')} Uhr"


def _bis_jetzt(zeitpunkt: datetime | None, jetzt: datetime) -> datetime:
    """Zeitpunkt „seit wann bekannt“: nie in der Zukunft, ohne Angabe jetzt."""
    return min(zeitpunkt, jetzt) if zeitpunkt else jetzt


class Adressen:
    """Absolute Adressen der Seiten im Bürgerportal (``basis`` ohne Schrägstrich am Ende)."""

    def __init__(self, basis: str) -> None:
        self.basis = basis.rstrip("/")

    def _url(self, name: str, **kwargs: Any) -> str:
        return self.basis + reverse(f"insight_core:insight:{name}", kwargs=kwargs or None)

    def vorgang(self, pk: object) -> str:
        return self._url("paper_detail", pk=pk)

    def sitzung(self, pk: object, punkt: object = None) -> str:
        adresse = self._url("meeting_detail", pk=pk)
        return f"{adresse}#top-{punkt}" if punkt else adresse

    def gremium(self, pk: object) -> str:
        return self._url("organization_detail", pk=pk)

    def seite(self, name: str, abfrage: str = "", **kwargs: Any) -> str:
        """Adresse einer Seite des Portals, optional mit Abfrage (``abfrage`` ohne „?“)."""
        adresse = self._url(name, **kwargs)
        return f"{adresse}?{abfrage}" if abfrage else adresse


# ---------------------------------------------------------------------------
# Vorgänge
# ---------------------------------------------------------------------------


def betreff(paper: OParlPaper) -> str:
    """Betreff ohne vorangestellte Drucksachennummer."""
    name = " ".join(str(paper.name or "").split())
    nummer = (paper.reference or "").strip()
    if nummer and name.lower().startswith(nummer.lower()):
        name = name[len(nummer) :].lstrip(" :–-,")
    return name


def kennzeichen(paper: OParlPaper, body_slug: str = "") -> str:
    """Art und Nummer: „Antrag 2026/0815“; ohne Nummer nur die Art."""
    art = normalize_paper_type(paper.paper_type, body_slug)
    if art.lower() == "anträge":
        art = "Antrag"
    if not art or len(art) > _ART_MAX:
        art = "Vorgang"
    nummer = " ".join(str(paper.reference or "").split())
    if nummer.lower().startswith(art.lower()):
        return nummer
    return f"{art} {nummer}".strip()


def _ueber(paper: OParlPaper, body_slug: str) -> str:
    """Wovon ein Eintrag handelt: Betreff, sonst Art und Nummer."""
    return text(betreff(paper), _BETREFF_MAX) or kennzeichen(paper, body_slug)


def vorlage_eintrag(
    paper: OParlPaper, adressen: Adressen, *, body_slug: str = "", urheber: Sequence[VorgangUrheber] = ()
) -> Eintrag:
    """Die Vorlage selbst: neu im Ratsinformationssystem."""
    zeichen = kennzeichen(paper, body_slug)
    inhalt = betreff(paper)
    zeitpunkt = tagesbeginn(paper.date) or paper.oparl_created or paper.created_at
    teile = [f"{zeichen} vom {datum(zeitpunkt)}." if paper.date else f"{zeichen}."]
    if urheber:
        teile.append("Gestellt von " + ", ".join(dict.fromkeys(u.anzeigename for u in urheber)) + ".")
    return Eintrag(
        kennung=kennung("vorgang", paper.pk),
        titel=f"{zeichen}: {text(inhalt, _BETREFF_MAX)}" if inhalt else zeichen,
        zeitpunkt=zeitpunkt,
        link=adressen.vorgang(paper.pk),
        zusammenfassung=" ".join(teile),
    )


@dataclass(frozen=True)
class Beratung:
    """Eine Beratung eines Vorgangs, so weit sie für die Feeds zählt (aus einem der beiden Verläufe)."""

    sitzung_id: Any
    beginn: datetime | None
    abgesagt: bool
    gremium: str
    top_nummer: str
    oeffentlich: bool
    ergebnis_roh: str
    #: Seit wann wir die Beratung kennen (nur im Verlauf eines einzelnen Vorgangs)
    bekannt_seit: datetime | None = None
    punkt_id: Any = None


def beratungen_aus_liste(schritte: Iterable[ris_selectors.ConsultationStep]) -> list[Beratung]:
    """Aus ``public_consultation_history`` (Listen: eine Abfrage für alle Vorgänge)."""
    return [
        Beratung(
            sitzung_id=schritt.meeting_id,
            beginn=schritt.date,
            abgesagt=schritt.cancelled,
            gremium=schritt.organization_name or "",
            top_nummer=schritt.agenda_number or "",
            oeffentlich=schritt.public,
            ergebnis_roh=schritt.result or "",
        )
        for schritt in schritte
    ]


def beratungen_aus_verlauf(verlauf: Iterable[dict[str, Any]]) -> list[Beratung]:
    """Aus ``consultation_history`` eines Vorgangs (mit Tagesordnungspunkt und Zeitpunkt der Beratung)."""
    beratungen = []
    for schritt in verlauf:
        sitzung: OParlMeeting | None = schritt.get("meeting")
        if sitzung is None or sitzung.deleted:
            continue
        punkt: OParlAgendaItem | None = schritt.get("agenda_item")
        oeffentlich = bool(schritt.get("public", True)) and not (punkt is not None and punkt.deleted)
        beratungen.append(
            Beratung(
                sitzung_id=sitzung.pk,
                beginn=sitzung.start,
                abgesagt=sitzung.cancelled,
                gremium=str(schritt.get("organization_name") or ""),
                top_nummer=str(schritt.get("agenda_number") or ""),
                oeffentlich=oeffentlich,
                ergebnis_roh=str(schritt.get("result") or "") if oeffentlich else "",
                bekannt_seit=getattr(schritt.get("consultation"), "created_at", None),
                punkt_id=punkt.pk if punkt is not None and oeffentlich else None,
            )
        )
    return beratungen


def beratung_eintraege(
    paper: OParlPaper,
    beratungen: Iterable[Beratung],
    adressen: Adressen,
    jetzt: datetime,
    *,
    body_slug: str = "",
    mit_terminen: bool = False,
) -> list[Eintrag]:
    """Beratungen mit Ergebnis; mit ``mit_terminen`` auch angesetzte Beratungen kommender Sitzungen."""
    eintraege: list[Eintrag] = []
    ueber = _ueber(paper, body_slug)
    zeichen = kennzeichen(paper, body_slug)
    for beratung in beratungen:
        if beratung.sitzung_id is None or beratung.beginn is None:
            continue
        gremium = beratung.gremium or "Sitzung"
        top = f", TOP {beratung.top_nummer}" if beratung.top_nummer else ""
        if beratung.beginn <= jetzt:
            etikett = ergebnis(beratung.ergebnis_roh) if beratung.oeffentlich else None
            if etikett is None:
                continue
            eintraege.append(
                Eintrag(
                    kennung=kennung("vorgang", paper.pk, "beratung", beratung.sitzung_id),
                    titel=f"{gremium}: {etikett.text} – {ueber}",
                    zeitpunkt=beratung.beginn,
                    link=adressen.vorgang(paper.pk),
                    zusammenfassung=(
                        f"{zeichen}, beraten am {datum(beratung.beginn)} ({gremium}{top}). "
                        f"Ergebnis laut Ratsinformationssystem: „{text(beratung.ergebnis_roh, 300)}“."
                    ),
                )
            )
        elif mit_terminen and not beratung.abgesagt:
            eintraege.append(
                Eintrag(
                    kennung=kennung("vorgang", paper.pk, "termin", beratung.sitzung_id),
                    titel=f"{gremium}: Beratung am {datum(beratung.beginn)} – {ueber}",
                    zeitpunkt=_bis_jetzt(beratung.bekannt_seit, jetzt),
                    link=adressen.sitzung(beratung.sitzung_id, beratung.punkt_id),
                    zusammenfassung=(
                        f"{zeichen} steht auf der Tagesordnung der Sitzung am {termin(beratung.beginn)}"
                        f" ({gremium}{top})."
                    ),
                )
            )
    return eintraege


def dokument_eintraege(paper: OParlPaper, dateien: Iterable[OParlFile], adressen: Adressen) -> list[Eintrag]:
    """Dokumente eines Vorgangs (Vorlage, Anlagen, Stellungnahmen), je Datei ein Eintrag."""
    ueber = _ueber(paper, "")
    eintraege = []
    for datei in dateien:
        name = text(datei.name or datei.file_name or "Dokument", 200)
        zeitpunkt = datei.file_date or datei.oparl_created or datei.created_at
        eintraege.append(
            Eintrag(
                kennung=kennung("vorgang", paper.pk, "dokument", datei.pk),
                titel=f"Dokument: {name} – {ueber}",
                zeitpunkt=zeitpunkt,
                link=adressen.vorgang(paper.pk),
                zusammenfassung=f"Neues Dokument zum Vorgang: {name}"
                + (f", vom {datum(datei.file_date)}." if datei.file_date else "."),
            )
        )
    return eintraege


def vorgaenge_eintraege(papers: Sequence[OParlPaper], adressen: Adressen, *, body_slug: str = "") -> list[Eintrag]:
    """
    Listen und Suche: je Vorgang die Vorlage und die Beratungen mit Ergebnis.

    Zwei Abfragen für alle Vorgänge (Verlauf und antragstellende Fraktionen).
    """
    from .vorgang_urheber import urheber_je_vorgang

    kennungen = [paper.pk for paper in papers]
    verlauf = ris_selectors.public_consultation_history(kennungen)
    urheber = urheber_je_vorgang(kennungen)
    jetzt = timezone.now()
    eintraege: list[Eintrag] = []
    for paper in papers:
        eintraege.append(vorlage_eintrag(paper, adressen, body_slug=body_slug, urheber=urheber.get(paper.pk, [])))
        eintraege.extend(
            beratung_eintraege(
                paper, beratungen_aus_liste(verlauf.get(paper.pk, [])), adressen, jetzt, body_slug=body_slug
            )
        )
    return eintraege


# ---------------------------------------------------------------------------
# Sitzungen und Beschlüsse
# ---------------------------------------------------------------------------


def beschluss_punkte(
    body: OParlBody, jetzt: datetime, *, seit: timedelta, gremium: OParlOrganization | None = None
) -> QuerySet[OParlAgendaItem]:
    """
    Öffentliche Tagesordnungspunkte mit Ergebnis in Sitzungen der Kommune zwischen ``jetzt - seit`` und ``jetzt``,
    neueste Sitzung zuerst; ohne gelöschte (und damit ohne von Session zurückgenommene) Punkte und Sitzungen. Grundlage des Beschluss-Feeds und des Gremien-Feeds;
    weitere Filter der Liste der Entscheidungen (Issue #1270) gehören hierher.
    """
    punkte = OParlAgendaItem.objects.filter(
        meeting__body=body,
        meeting__deleted=False,
        meeting__start__lte=jetzt,
        meeting__start__gte=jetzt - seit,
        deleted=False,
        public=True,
        result__gt="",
    )
    if gremium is not None:
        punkte = punkte.filter(meeting__organizations=gremium)
    return (
        punkte.select_related("meeting")
        .prefetch_related("meeting__organizations")
        .defer("raw_json", "roll_call")
        .order_by("-meeting__start", "order", "number")
    )


def beschluss_eintraege(punkte: Iterable[OParlAgendaItem], adressen: Adressen) -> list[Eintrag]:
    """
    Tagesordnungspunkte mit Ergebnis (Gremium, Beschlüsse der Kommune). ``punkte`` mit ``select_related("meeting")``
    und ``prefetch_related("meeting__organizations")``; Punkte ohne Ergebnis oder Sitzungsbeginn fallen weg.
    """
    eintraege = []
    for punkt in punkte:
        sitzung = punkt.meeting
        etikett = ergebnis(punkt.result)
        if etikett is None or sitzung.start is None:
            continue
        name = text(punkt.name or "Tagesordnungspunkt", _BETREFF_MAX)
        gremium = sitzung.get_display_name()
        top = f", TOP {punkt.number}" if punkt.number else ""
        titel = (
            f"{etikett.text[:1].upper()}{etikett.text[1:]}: {name}" if etikett.bekannt else f"{name} ({etikett.text})"
        )
        teile = [
            f"{gremium}, Sitzung am {datum(sitzung.start)}{top}.",
            f"Ergebnis laut Ratsinformationssystem: „{text(punkt.result, 300)}“.",
        ]
        if punkt.resolution_text:
            teile.append(f"Beschluss: {text(punkt.resolution_text, 400)}")
        eintraege.append(
            Eintrag(
                kennung=kennung("beschluss", punkt.pk),
                titel=titel,
                zeitpunkt=sitzung.start,
                link=adressen.sitzung(sitzung.pk, punkt.pk),
                zusammenfassung=" ".join(teile),
            )
        )
    return eintraege


def sitzung_eintraege(sitzungen: Iterable[OParlMeeting], adressen: Adressen, jetzt: datetime) -> list[Eintrag]:
    """
    Kommende Sitzungen eines Gremiums: angesetzt (Etikett „angekündigt“ ohne Tagesordnung) und, sobald es sie gibt,
    die veröffentlichte Tagesordnung. ``sitzungen`` mit den Annotationen ``anzahl_tops`` und ``tops_seit``.
    """
    eintraege = []
    for sitzung in sitzungen:
        if sitzung.start is None:
            continue
        gremium = sitzung.get_display_name()
        anzahl = int(getattr(sitzung, "anzahl_tops", 0) or 0)
        ort = f" Ort: {text(sitzung.location_name, 200)}." if sitzung.location_name else ""
        if sitzung.cancelled:
            satz = "Die Sitzung ist abgesagt."
        elif anzahl:
            satz = f"Die Tagesordnung hat {anzahl} öffentliche Punkt{'e' if anzahl != 1 else ''}."
        else:
            satz = f"angekündigt: {ANGEKUENDIGT_SATZ}"
        bekannt = sitzung.oparl_created or sitzung.created_at
        if sitzung.cancelled:
            bekannt = sitzung.oparl_modified or bekannt
        eintraege.append(
            Eintrag(
                kennung=kennung("sitzung", sitzung.pk),
                titel=f"{gremium}: Sitzung am {termin(sitzung.start)}" + (" – abgesagt" if sitzung.cancelled else ""),
                zeitpunkt=_bis_jetzt(bekannt, jetzt),
                link=adressen.sitzung(sitzung.pk),
                zusammenfassung=satz + ort,
            )
        )
        tops_seit = getattr(sitzung, "tops_seit", None)
        if anzahl and not sitzung.cancelled:
            eintraege.append(
                Eintrag(
                    kennung=kennung("sitzung", sitzung.pk, "tagesordnung"),
                    titel=f"{gremium}: Tagesordnung für den {datum(sitzung.start)} veröffentlicht",
                    zeitpunkt=_bis_jetzt(tops_seit, jetzt),
                    link=adressen.sitzung(sitzung.pk),
                    zusammenfassung=f"Sitzung am {termin(sitzung.start)}. {satz}{ort}",
                )
            )
    return eintraege
