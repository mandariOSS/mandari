# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Orte eines Vorgangs für die Randspalte der Vorgangsseite (Issue #1269).

Jeder Ort trägt eine Sicherheit in drei Stufen und seine Fundstelle, statt eines Pauschalsatzes für alle:

* **gesichert:** Ort der Kommune (OParl ``location``), amtlicher Umring eines Bebauungsplans, von Hand eingetragen
  oder im Admin bestätigt;
* **erschlossen:** Straßenname im Titel oder im Hauptdokument, abgeglichen mit dem Straßenverzeichnis der Kommune;
* **unsicher:** von einer KI im Text erkannt oder ergänzt, oder die KI-Prüfung war sich nicht sicher.

Die Randspalte bleibt kurz: höchstens ``ORTE_SICHTBAR`` Orte, weitere aufklappbar in einer Zeile. Betrifft ein Vorgang
die ganze Stadt, steht „Stadtweit“ mit Begründung statt einer Straßenliste: Beraten ihn mindestens
``MIN_BEZIRKE_STADTWEIT`` Bezirksvertretungen (bzw. Ortsräte, Bezirksausschüsse …), oder liegen viele Orte über einen
großen Teil des Stadtgebiets verteilt (``MIN_ORTE_VERTEILT`` Orte über mindestens die Hälfte der Ausdehnung der
Kommune). Bei Eingaben aus der Einwohnerschaft und Bauanträgen stehen die Orte ohne Hausnummer da (``ortsschutz``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from insight_core.services import ortsschutz

if TYPE_CHECKING:
    from insight_core.models import OParlBody, OParlPaper

GESICHERT: Final = "gesichert"
ERSCHLOSSEN: Final = "erschlossen"
UNSICHER: Final = "unsicher"

#: Erklärung je Stufe (Tooltip und Legende)
ERKLAERUNG: Final = {
    GESICHERT: "Angabe der Kommune, amtlicher Plan oder geprüft.",
    ERSCHLOSSEN: "Name im Titel oder Dokument; ein Straßenpunkt steht für die ganze Straße.",
    UNSICHER: "Von einer KI erkannt, nicht geprüft.",
}
_RANG: Final = {GESICHERT: 0, ERSCHLOSSEN: 1, UNSICHER: 2}

#: So viele Orte nennt die Randspalte, weitere stehen aufklappbar in einer Zeile
ORTE_SICHTBAR: Final = 5
#: Ab so vielen beratenden Bezirksgremien gilt ein Vorgang als stadtweit
MIN_BEZIRKE_STADTWEIT: Final = 3
#: Ab so vielen Orten, die mindestens ``ANTEIL_VERTEILT`` der Ausdehnung der Kommune überspannen, ebenso
MIN_ORTE_VERTEILT: Final = 6
ANTEIL_VERTEILT: Final = 0.5

#: Gremium eines Stadtbezirks bzw. Ortsteils und der Name des Bezirks dahinter
_BEZIRKSGREMIUM: Final = re.compile(
    r"^(?:Stadtbezirksrat|Stadtbezirksbeirat|Bezirksvertretung|Bezirksausschuss|Bezirksbeirat|Bezirksrat|"
    r"Ortsbezirksrat|Ortsbeirat|Ortschaftsrat|Ortsrat|Beirat für den Stadtbezirk)\s+(?P<bezirk>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Ort:
    """Ein Ort der Randspalte: Bezeichnung, Sicherheit und Fundstelle als Satzteil („aus dem Titel“)."""

    name: str
    sicherheit: str
    fundstelle: str

    @property
    def erklaerung(self) -> str:
        return ERKLAERUNG[self.sicherheit]


@dataclass
class Ortsangaben:
    """Orte für die Randspalte: sichtbare, weitere (aufklappbar), Begründung für „stadtweit“, Datenschutzhinweis."""

    orte: list[Ort] = field(default_factory=list)
    weitere: list[Ort] = field(default_factory=list)
    stadtweit: str = ""
    ohne_hausnummer: bool = False

    @property
    def anzahl(self) -> int:
        return len(self.orte) + len(self.weitere)

    @property
    def stufen(self) -> list[tuple[str, str]]:
        """Vorkommende Sicherheitsstufen mit Erklärung, in fester Reihenfolge (für die Legende)."""
        da = {ort.sicherheit for ort in [*self.orte, *self.weitere]}
        return [(stufe, ERKLAERUNG[stufe]) for stufe in (GESICHERT, ERSCHLOSSEN, UNSICHER) if stufe in da]


def sicherheit(eintrag: dict[str, Any], status: str = "", ki_urteil: str = "") -> tuple[str, str]:
    """Sicherheit und Fundstelle eines Eintrags aus ``paper.locations`` (dazu Status und KI-Urteil der Tabelle)."""
    from insight_core.models import PaperLocation

    herkunft = str(eintrag.get("source") or "")
    fundstelle = str(eintrag.get("fundstelle") or "")
    if herkunft == "oparl":
        return GESICHERT, "Angabe der Kommune"
    if herkunft == "plan_boundary":
        return GESICHERT, "amtlicher Umring des Bebauungsplans"
    if herkunft == "manual":
        return GESICHERT, "von Hand eingetragen"
    if status == PaperLocation.STATUS_CONFIRMED:
        return GESICHERT, "von Hand geprüft"
    if herkunft in ("ai", "ai_check") or fundstelle == "ki":
        return UNSICHER, "von einer KI erkannt" if herkunft != "ai_check" else "von einer KI ergänzt"
    if ki_urteil == PaperLocation.KI_UNSICHER:
        return UNSICHER, "im Dokument genannt, Zuordnung unsicher"
    if fundstelle == "titel":
        return ERSCHLOSSEN, "aus dem Titel"
    if fundstelle == "dokument":
        return ERSCHLOSSEN, "aus dem Dokument"
    return ERSCHLOSSEN, "aus den Dokumenten"


def bezirke(beratungen: list[dict[str, Any]]) -> list[str]:
    """Namen der Stadtbezirke, deren Gremien den Vorgang beraten (aus dem Beratungsverlauf), ohne Dopplungen."""
    namen: list[str] = []
    for beratung in beratungen:
        sitzung = beratung.get("meeting")
        gremien = [o.name for o in sitzung.organizations.all()] if sitzung is not None else []
        if not gremien and beratung.get("organization_name"):
            gremien = [str(beratung["organization_name"])]
        for gremium in gremien:
            treffer = _BEZIRKSGREMIUM.match((gremium or "").strip())
            if treffer:
                bezirk = treffer.group("bezirk").strip()
                if bezirk and bezirk not in namen:
                    namen.append(bezirk)
    return namen


def _aufzaehlung(namen: list[str]) -> str:
    """„A, B und C“."""
    if len(namen) <= 1:
        return "".join(namen)
    return ", ".join(namen[:-1]) + " und " + namen[-1]


def _verteilt(punkte: list[tuple[float, float]], body: OParlBody | None) -> bool:
    """Überspannen die Punkte mindestens ``ANTEIL_VERTEILT`` der Nord-Süd- oder Ost-West-Ausdehnung der Kommune?"""
    if body is None or len(punkte) < MIN_ORTE_VERTEILT:
        return False
    grenzen = (body.bbox_south, body.bbox_north, body.bbox_west, body.bbox_east)
    if not all(grenzen):
        return False
    sued, nord, west, ost = (float(g or 0) for g in grenzen)
    if nord <= sued or ost <= west:
        return False
    breiten = [p[0] for p in punkte]
    laengen = [p[1] for p in punkte]
    return (max(breiten) - min(breiten)) / (nord - sued) >= ANTEIL_VERTEILT or (max(laengen) - min(laengen)) / (
        ost - west
    ) >= ANTEIL_VERTEILT


def ortsangaben(
    paper: OParlPaper, eintraege: list[dict[str, Any]], beratungen: list[dict[str, Any]], schutz: str = ""
) -> Ortsangaben:
    """
    Orte der Randspalte aus ``paper.locations`` (bereits gefiltert auf Einträge mit Punkt), dem Beratungsverlauf
    (``hub.ris.selectors.consultation_history``) und dem Ergebnis von ``ortsschutz.grund``. Eine Abfrage für Status und
    KI-Urteil der Orte.
    """
    from insight_core.models import PaperLocation
    from insight_core.services.paper_locations import point_key

    pruefung = {
        point_key(lat, lon): (status, urteil)
        for lat, lon, status, urteil in PaperLocation.objects.filter(paper=paper).values_list(
            "latitude", "longitude", "status", "ki_urteil"
        )
    }
    orte: list[Ort] = []
    gesehen: set[str] = set()
    punkte: list[tuple[float, float]] = []
    for eintrag in eintraege:
        try:
            schluessel = point_key(eintrag["lat"], eintrag["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        status, urteil = pruefung.get(schluessel, ("", ""))
        name = str(eintrag.get("name") or "").strip() or "Ort ohne Bezeichnung"
        if schutz:
            name = ortsschutz.ort_ohne_hausnummer(name) or "Ort ohne Bezeichnung"
        stufe, fundstelle = sicherheit(eintrag, status, urteil)
        punkte.append(schluessel)
        if name.casefold() in gesehen:
            continue
        gesehen.add(name.casefold())
        orte.append(Ort(name=name, sicherheit=stufe, fundstelle=fundstelle))
    orte.sort(key=lambda ort: _RANG[ort.sicherheit])

    stadtweit = ""
    namen = bezirke(beratungen)
    if len(namen) >= MIN_BEZIRKE_STADTWEIT:
        stadtweit = f"Beraten in {len(namen)} Stadtbezirken: {_aufzaehlung(namen)}."
    elif _verteilt(punkte, paper.body):
        stadtweit = f"Die {len(orte)} genannten Orte liegen über das ganze Stadtgebiet verteilt."
    sichtbar = 0 if stadtweit else ORTE_SICHTBAR
    return Ortsangaben(
        orte=orte[:sichtbar],
        weitere=orte[sichtbar:],
        stadtweit=stadtweit,
        ohne_hausnummer=bool(schutz),
    )


def kartenorte(eintraege: list[dict[str, Any]], schutz: str = "") -> list[dict[str, Any]]:
    """Einträge für die Karte der Vorgangsseite; bei Privatanschriften mit Namen ohne Hausnummer."""
    if not schutz:
        return eintraege
    return [
        {**eintrag, "name": ortsschutz.ort_ohne_hausnummer(str(eintrag.get("name") or ""))} for eintrag in eintraege
    ]
