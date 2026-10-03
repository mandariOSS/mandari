# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunenwechsel im Bürgerportal (Issue #783, Stufe 2).

In Deutschland gibt es rund 11.000 Gemeinden. Der Wechsel zeigt deshalb nie eine Liste aller Kommunen, sondern

- Vorschläge zur Eingabe von Name, Ortsteil oder Postleitzahl: höchstens acht, unscharf und umlauttolerant,
  mit Kreis und Land zur Unterscheidung gleichnamiger Orte (``suchen``);
- Kommunen in der Nähe eines grob gerundeten Standorts (``in_der_naehe``); die genaue Entfernung rechnet
  der Browser, der Server erfährt nur eine Zelle von etwa zehn Kilometern;
- ein Stöbern Land → Kreis → (Gemeindeverband →) Kommune (``stoebern``).

Grundlage ist das Kommunenverzeichnis (``Municipality``/``MunicipalityTerm``). Wählbar ist ein Eintrag, wenn eine
gelistete Kommune (``OParlBody.objects.listed()``) denselben Regionalschlüssel oder AGS trägt; alle anderen
erscheinen mit dem Hinweis „noch nicht verfügbar“. Gelistete Kommunen ohne Verzeichniseintrag (etwa ein
Regionalrat ohne Gemeindeschlüssel) werden über ihren Namen gefunden, damit der Wechsel auch mit leerem
Verzeichnis funktioniert.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from django.db import connection
from django.db.models import Count, Q
from django.urls import reverse

from ..models import Municipality, MunicipalityTerm, OParlBody

#: Höchstzahl der Vorschläge je Eingabe
MAX_TREFFER = 8
#: Mindestlänge der Eingabe (normalisiert); Ziffern ab zwei Stellen
MIN_LAENGE = 2
#: Unscharfe Treffer erst ab dieser Länge, darunter nur Wortanfänge
MIN_LAENGE_UNSCHARF = 4
#: Mindestähnlichkeit (0–1) unscharfer Treffer in Python
MIN_AEHNLICHKEIT = 0.72
#: Kandidaten aus der Datenbank vor der Rangfolge
MAX_KANDIDATEN = 300
#: Größe der Standortzelle in Grad (eine Nachkommastelle, etwa 11 km Nord-Süd)
ZELLE_GRAD = 0.1
#: Kandidaten in der Nähe, aus denen der Browser die nächsten auswählt
MAX_NAEHE = 60
#: Ab so vielen Einträgen in einem Kreis gliedert das Stöbern nach Gemeindeverbänden
STOEBERN_GRUPPIEREN_AB = 40

#: Bundesland aus den ersten beiden Stellen des Gemeinde- bzw. Regionalschlüssels
LAENDER: dict[str, str] = {
    "01": "Schleswig-Holstein",
    "02": "Hamburg",
    "03": "Niedersachsen",
    "04": "Bremen",
    "05": "Nordrhein-Westfalen",
    "06": "Hessen",
    "07": "Rheinland-Pfalz",
    "08": "Baden-Württemberg",
    "09": "Bayern",
    "10": "Saarland",
    "11": "Berlin",
    "12": "Brandenburg",
    "13": "Mecklenburg-Vorpommern",
    "14": "Sachsen",
    "15": "Sachsen-Anhalt",
    "16": "Thüringen",
}

#: Ableitung des Körperschafts-Typs aus dem Namen, wenn die OParl-Quelle keine ``classification`` liefert
#: (Reihenfolge = Priorität)
_KIND_PATTERNS = (
    (re.compile(r"^(bezirksregierung|regionalrat|regionalverband|landschaftsverband)", re.I), "Regionalrat"),
    (re.compile(r"^(landkreis|kreis)(\s|$)", re.I), "Landkreis"),
    (re.compile(r"^(bundesstadt|landeshauptstadt|freie und hansestadt|hansestadt)", re.I), "Kreisfreie Stadt"),
    (re.compile(r"kreisfrei", re.I), "Kreisfreie Stadt"),
    (re.compile(r"^(samtgemeinde|verbandsgemeinde|amt)(\s|$)", re.I), "Gemeindeverband"),
    (re.compile(r"^(gemeinde|markt|flecken)(\s|$)", re.I), "Gemeinde"),
    (re.compile(r"^stadt(\s|$)", re.I), "Stadt"),
)

_UMLAUTE_LANG = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_UMLAUTE_KURZ = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})
_NICHT_ALNUM = re.compile(r"[^a-z0-9]+")


def get_kind_label_for_body(body: OParlBody) -> str:
    """Anzeige-Typ einer Körperschaft: OParl-``classification`` oder Ableitung aus dem Namen, sonst „Kommune“."""
    if body.classification:
        return str(body.classification)
    name = body.name or ""
    for pattern, label in _KIND_PATTERNS:
        if pattern.search(name):
            return label
    return "Kommune"


def get_bundesland_for_body(body: OParlBody) -> str | None:
    """Bundesland aus dem Gemeinde- oder Regionalschlüssel der Kommune (oder None)."""
    for schluessel in (body.ags, body.rgs):
        if schluessel and len(schluessel) >= 2 and schluessel[:2] in LAENDER:
            return LAENDER[schluessel[:2]]
    return None


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


def _ohne_akzente(text: str) -> str:
    zerlegt = unicodedata.normalize("NFKD", text)
    return "".join(zeichen for zeichen in zerlegt if not unicodedata.combining(zeichen))


def normalisieren(text: str, *, kurz: bool = False) -> str:
    """Kleinschreibung, Umlaute als ``ae``/``oe``/``ue`` (``kurz``: ``a``/``o``/``u``), ``ß`` als ``ss``, ohne Akzente
    und Satzzeichen, einfache Leerzeichen. „Grenzstadt (Oder)“ → ``grenzstadt oder``."""
    klein = text.casefold().translate(_UMLAUTE_KURZ if kurz else _UMLAUTE_LANG)
    return _NICHT_ALNUM.sub(" ", _ohne_akzente(klein)).strip()


def varianten(text: str) -> set[str]:
    """Gespeicherte Schreibweisen eines Suchbegriffs: mit ``ae`` und mit ``a`` für Umlaute ohne Punkte."""
    return {wert for wert in (normalisieren(text), normalisieren(text, kurz=True)) if wert}


# ---------------------------------------------------------------------------
# Treffer
# ---------------------------------------------------------------------------


@dataclass
class Treffer:
    """Ein Vorschlag im Kommunenwechsel; ``url`` nur, wenn die Kommune Daten hat."""

    name: str
    ort: str
    hinweis: str = ""
    url: str = ""
    breite: float | None = None
    laenge: float | None = None
    schluessel: str = ""
    rang: int = field(default=0, repr=False)
    aehnlichkeit: float = field(default=0.0, repr=False)

    @property
    def verfuegbar(self) -> bool:
        return bool(self.url)

    def als_dict(self) -> dict[str, Any]:
        daten: dict[str, Any] = {"name": self.name, "ort": self.ort, "verfuegbar": self.verfuegbar}
        if self.hinweis:
            daten["hinweis"] = self.hinweis
        if self.url:
            daten["url"] = self.url
        if self.breite is not None and self.laenge is not None:
            daten["breite"] = round(self.breite, 4)
            daten["laenge"] = round(self.laenge, 4)
        return daten


def _ort(eintrag: Municipality) -> str:
    """Zweite Zeile zur Unterscheidung gleichnamiger Orte: „Kreis Nordland, Nordrhein-Westfalen“."""
    land = LAENDER.get(eintrag.state_key, "")
    teile: list[str] = []
    if eintrag.district and normalisieren(eintrag.district) != normalisieren(eintrag.name):
        teile.append(eintrag.district)
    elif eintrag.kind:
        teile.append(eintrag.kind)
    if land and normalisieren(land) != normalisieren(eintrag.name):
        teile.append(land)
    return ", ".join(teile)


def ort_der_koerperschaft(body: OParlBody) -> str:
    """Zweite Zeile einer gelisteten Kommune: Art und Land."""
    land = get_bundesland_for_body(body)
    art = get_kind_label_for_body(body)
    return ", ".join(teil for teil in (art, land) if teil)


def _gelistete_koerperschaften() -> list[OParlBody]:
    return list(
        OParlBody.objects.listed().only(
            "id", "name", "short_name", "display_name", "ags", "rgs", "classification", "latitude", "longitude"
        )
    )


def _waehlen_url(body: OParlBody) -> str:
    return reverse("insight_core:insight:set_body", args=[body.id])


class _Zuordnung:
    """Gelistete Kommunen nach Regionalschlüssel und AGS, um Verzeichniseinträge wählbar zu machen."""

    def __init__(self, koerperschaften: Iterable[OParlBody]) -> None:
        self.koerperschaften = list(koerperschaften)
        self.nach_rs: dict[str, OParlBody] = {}
        self.nach_ags: dict[str, OParlBody] = {}
        for body in self.koerperschaften:
            if body.rgs:
                self.nach_rs.setdefault(body.rgs, body)
            if body.ags:
                self.nach_ags.setdefault(body.ags, body)

    def fuer(self, eintrag: Municipality) -> OParlBody | None:
        body = self.nach_rs.get(eintrag.key)
        if body is None and eintrag.ags and not eintrag.is_association:
            body = self.nach_ags.get(eintrag.ags)
        return body

    def ohne_eintrag(self, eintraege: Iterable[Municipality]) -> list[OParlBody]:
        """Gelistete Kommunen, die keinem der Einträge entsprechen (z. B. ein Regionalrat)."""
        vergeben = {body.pk for eintrag in eintraege if (body := self.fuer(eintrag)) is not None}
        return [body for body in self.koerperschaften if body.pk not in vergeben]


def _treffer(eintrag: Municipality, zuordnung: _Zuordnung, hinweis: str = "") -> Treffer:
    body = zuordnung.fuer(eintrag)
    return Treffer(
        name=eintrag.name,
        ort=_ort(eintrag),
        hinweis=hinweis,
        url=_waehlen_url(body) if body is not None else "",
        breite=eintrag.latitude,
        laenge=eintrag.longitude,
        schluessel=eintrag.key,
    )


def _treffer_koerperschaft(body: OParlBody) -> Treffer:
    return Treffer(
        name=body.get_display_name(),
        ort=ort_der_koerperschaft(body),
        url=_waehlen_url(body),
        breite=float(body.latitude) if body.latitude is not None else None,
        laenge=float(body.longitude) if body.longitude is not None else None,
    )


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def _bewerten(eingabe: str, begriff: str) -> tuple[int, float]:
    """Rang (3 gleich, 2 Anfang, 1 Wortanfang, 0 unscharf, -1 kein Treffer) und Ähnlichkeit 0–1."""
    if begriff == eingabe:
        return 3, 1.0
    if begriff.startswith(eingabe):
        return 2, len(eingabe) / len(begriff)
    woerter = begriff.split()
    if any(wort.startswith(eingabe) for wort in woerter[1:]):
        return 1, len(eingabe) / len(begriff)
    if len(eingabe) < MIN_LAENGE_UNSCHARF or eingabe.isdigit():
        return -1, 0.0
    # Unscharf: Tippfehler im Namen oder in einem seiner Wörter („Münser“, „Frankfrt“)
    vergleiche = [begriff, *woerter, *(wort[: len(eingabe) + 1] for wort in woerter)]
    aehnlichkeit = max(SequenceMatcher(None, eingabe, kandidat).ratio() for kandidat in vergleiche)
    return (0, aehnlichkeit) if aehnlichkeit >= MIN_AEHNLICHKEIT else (-1, 0.0)


#: Kandidaten in PostgreSQL: Wortanfang oder Wortähnlichkeit (``<%``, pg_trgm); beides nutzt den GIN-Index
_SQL_KANDIDATEN = (
    "SELECT id FROM insight_municipality_term "
    "WHERE normalized LIKE %s OR normalized LIKE %s OR %s <%% normalized "
    "ORDER BY word_similarity(%s, normalized) DESC LIMIT %s"
)


def _kandidaten(eingabe: str) -> list[MunicipalityTerm]:
    """Suchbegriffe, die als Treffer in Frage kommen. PostgreSQL nutzt den Trigramm-Index, SQLite Wortanfänge."""
    basis = MunicipalityTerm.objects.select_related("municipality")
    if eingabe.isdigit():
        return list(basis.filter(kind=MunicipalityTerm.Kind.POSTCODE, normalized__startswith=eingabe)[:MAX_KANDIDATEN])
    if connection.vendor == "postgresql" and len(eingabe) >= MIN_LAENGE_UNSCHARF:
        with connection.cursor() as cursor:
            cursor.execute(_SQL_KANDIDATEN, [eingabe + "%", "% " + eingabe + "%", eingabe, eingabe, MAX_KANDIDATEN])
            ids = [zeile[0] for zeile in cursor.fetchall()]
        return list(basis.filter(id__in=ids))
    praefix = Q(normalized__startswith=eingabe) | Q(normalized__contains=" " + eingabe)
    kandidaten = list(basis.filter(praefix)[:MAX_KANDIDATEN])
    if len(eingabe) >= MIN_LAENGE_UNSCHARF and len(kandidaten) < MAX_TREFFER:
        # SQLite (Tests, Entwicklung): unscharfe Kandidaten über die ersten beiden Buchstaben eines Wortes
        anfang = eingabe[:2]
        weitere = basis.filter(Q(normalized__startswith=anfang) | Q(normalized__contains=" " + anfang))
        kandidaten.extend(weitere.exclude(id__in=[k.id for k in kandidaten])[: MAX_KANDIDATEN * 4])
    return kandidaten


def _passt_zu_weiteren_woertern(eintrag: Municipality, begriffe: list[str], woerter: list[str]) -> bool:
    """Weitere Wörter der Eingabe („Mustertal Seenland“) müssen in Name, Kreis, Land oder Begriffen vorkommen."""
    land = LAENDER.get(eintrag.state_key, "")
    text = " ".join([normalisieren(eintrag.name), normalisieren(eintrag.district), normalisieren(land), *begriffe])
    teile = text.split()
    return all(any(teil.startswith(wort) for teil in teile) for wort in woerter)


def _begriffe_je_eintrag(ids: Iterable[int]) -> dict[int, list[str]]:
    begriffe: dict[int, list[str]] = {}
    zeilen = MunicipalityTerm.objects.filter(municipality_id__in=set(ids)).values_list("municipality_id", "normalized")
    for eintrag_id, normalized in zeilen:
        begriffe.setdefault(eintrag_id, []).append(normalized)
    return begriffe


def _durchlauf(suchwort: str, weitere: list[str]) -> dict[int, tuple[int, float, MunicipalityTerm]]:
    """Bester Suchbegriff je Verzeichniseintrag: (Rang, Ähnlichkeit, Begriff); ``weitere`` Wörter filtern."""
    bester: dict[int, tuple[int, float, MunicipalityTerm]] = {}
    kandidaten = _kandidaten(suchwort)
    begriffe = _begriffe_je_eintrag(k.municipality_id for k in kandidaten) if weitere else {}
    for begriff in kandidaten:
        rang, aehnlichkeit = _bewerten(suchwort, begriff.normalized)
        if rang < 0:
            continue
        if weitere and not _passt_zu_weiteren_woertern(
            begriff.municipality, begriffe.get(begriff.municipality_id, []), weitere
        ):
            continue
        vorher = bester.get(begriff.municipality_id)
        if vorher is None or (rang, aehnlichkeit) > (vorher[0], vorher[1]):
            bester[begriff.municipality_id] = (rang, aehnlichkeit, begriff)
    return bester


def suchen(text: str, limit: int = MAX_TREFFER) -> list[Treffer]:
    """Bis zu ``limit`` Vorschläge zur Eingabe. Leere oder zu kurze Eingaben ergeben keine Vorschläge."""
    woerter = normalisieren(text).split()
    if not woerter:
        return []
    eingabe = " ".join(woerter)
    erstes = woerter[0]
    if len(eingabe) < MIN_LAENGE:
        return []

    # 1. Verzeichnis. Bei mehreren Wörtern gilt: ganze Eingabe genau oder als Anfang („Mustertal am“), sonst erstes
    #    Wort mit den übrigen als Filter auf Kreis, Land und Begriffe („Beispielstadt Hessen“), sonst unscharf.
    bester = _durchlauf(eingabe, [])
    if len(woerter) > 1:
        scharf = {schluessel: wert for schluessel, wert in bester.items() if wert[0] > 0}
        bester = scharf or _durchlauf(erstes, woerter[1:]) or bester

    zuordnung = _Zuordnung(_gelistete_koerperschaften())
    ergebnisse: list[Treffer] = []
    for rang, aehnlichkeit, begriff in bester.values():
        hinweis = ""
        if begriff.kind == MunicipalityTerm.Kind.DISTRICT_PART:
            hinweis = f"Ortsteil {begriff.label}"
        elif begriff.kind == MunicipalityTerm.Kind.POSTCODE:
            hinweis = f"PLZ {begriff.label}"
        treffer = _treffer(begriff.municipality, zuordnung, hinweis)
        treffer.rang, treffer.aehnlichkeit = rang, aehnlichkeit
        ergebnisse.append(treffer)

    # 2. Gelistete Kommunen ohne Verzeichniseintrag über ihren Namen
    if not eingabe.isdigit():
        eintraege = Municipality.objects.filter(
            Q(key__in=[b.rgs for b in zuordnung.koerperschaften if b.rgs])
            | Q(ags__in=[b.ags for b in zuordnung.koerperschaften if b.ags], is_association=False)
        )
        for body in zuordnung.ohne_eintrag(eintraege):
            namen = {normalisieren(n) for n in (body.get_display_name(), body.name, body.short_name or "") if n}
            bewertungen = [_bewerten(eingabe, name) for name in namen]
            rang, aehnlichkeit = max(bewertungen, default=(-1, 0.0))
            if rang >= 0:
                treffer = _treffer_koerperschaft(body)
                treffer.rang, treffer.aehnlichkeit = rang, aehnlichkeit
                ergebnisse.append(treffer)

    ergebnisse.sort(key=lambda t: (-t.rang, -t.aehnlichkeit, not t.verfuegbar, t.name, t.ort))
    return ergebnisse[:limit]


# ---------------------------------------------------------------------------
# In der Nähe
# ---------------------------------------------------------------------------


def zelle(breite: float, laenge: float) -> tuple[float, float]:
    """Standort auf eine Zelle von 0,1 Grad abrunden (Mitte der Zelle); genauer erfährt der Server ihn nicht."""
    return (
        round(math.floor(breite / ZELLE_GRAD) * ZELLE_GRAD + ZELLE_GRAD / 2, 2),
        round(math.floor(laenge / ZELLE_GRAD) * ZELLE_GRAD + ZELLE_GRAD / 2, 2),
    )


def entfernung_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Entfernung zweier Punkte (Breite, Länge) in Kilometern (Haversine)."""
    breite_a, laenge_a, breite_b, laenge_b = (math.radians(wert) for wert in (*a, *b))
    h = (
        math.sin((breite_b - breite_a) / 2) ** 2
        + math.cos(breite_a) * math.cos(breite_b) * math.sin((laenge_b - laenge_a) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _punkt(body: OParlBody) -> tuple[float, float] | None:
    if body.latitude is None or body.longitude is None:
        return None
    return (float(body.latitude), float(body.longitude))


def in_der_naehe(breite: float, laenge: float) -> dict[str, Any]:
    """Kandidaten rund um die Zelle des Standorts, dazu die nächste Kommune mit Daten.

    Der Server rundet selbst auf die Zelle; der Browser sortiert die Kandidaten nach der genauen Entfernung.
    """
    mitte = zelle(breite, laenge)
    zuordnung = _Zuordnung(_gelistete_koerperschaften())
    eintraege: list[Municipality] = []
    halb = 0.15
    while halb <= 1.6:
        spanne_laenge = halb / max(math.cos(math.radians(mitte[0])), 0.2)
        eintraege = list(
            Municipality.objects.filter(
                latitude__gte=mitte[0] - halb,
                latitude__lte=mitte[0] + halb,
                longitude__gte=mitte[1] - spanne_laenge,
                longitude__lte=mitte[1] + spanne_laenge,
            )
        )
        if len(eintraege) >= MAX_TREFFER:
            break
        halb *= 2
    eintraege.sort(key=lambda e: entfernung_km(mitte, (e.latitude or 0.0, e.longitude or 0.0)))
    kandidaten = [_treffer(eintrag, zuordnung) for eintrag in eintraege[:MAX_NAEHE]]
    # Gelistete Kommunen ohne Verzeichniseintrag, aber mit Koordinaten
    for body in zuordnung.ohne_eintrag(eintraege):
        punkt = _punkt(body)
        if punkt is not None and entfernung_km(mitte, punkt) <= 60:
            kandidaten.append(_treffer_koerperschaft(body))

    naechste: Treffer | None = None
    if not any(t.verfuegbar for t in kandidaten):
        mit_ort = [(punkt, b) for b in zuordnung.koerperschaften if (punkt := _punkt(b)) is not None]
        if mit_ort:
            naechste = _treffer_koerperschaft(min(mit_ort, key=lambda paar: entfernung_km(mitte, paar[0]))[1])
    return {
        "zelle": list(mitte),
        "kandidaten": [t.als_dict() for t in kandidaten],
        "naechste_mit_daten": naechste.als_dict() if naechste is not None else None,
    }


# ---------------------------------------------------------------------------
# Stöbern
# ---------------------------------------------------------------------------


def ist_leer() -> bool:
    return not Municipality.objects.exists()


def _verfuegbar_nach_praefix(zuordnung: _Zuordnung, laenge: int) -> dict[str, int]:
    """Zahl der Kommunen mit Daten je Schlüsselanfang (2 Stellen Land, 5 Stellen Kreis)."""
    zaehler: dict[str, int] = {}
    for body in zuordnung.koerperschaften:
        schluessel = body.rgs or body.ags
        # Nur Gemeinden und Gemeindeverbände; ein Regionalrat steht nicht im Verzeichnis
        if schluessel and len(schluessel) >= 8:
            zaehler[schluessel[:laenge]] = zaehler.get(schluessel[:laenge], 0) + 1
    return zaehler


def stoebern(land: str = "", kreis: str = "", verband: str = "") -> dict[str, Any]:
    """Eine Stufe des Stöberns: Länder, Kreise eines Landes, Gemeindeverbände oder Kommunen eines Kreises.

    Antwort: ``{"stufe", "titel", "zurueck", "eintraege"}``; Gruppen tragen ``land``/``kreis``/``verband`` für die
    nächste Stufe, Kommunen ``url`` (mit Daten) oder nur Name und Ort.
    """
    zuordnung = _Zuordnung(_gelistete_koerperschaften())
    if not land:
        mit_daten = _verfuegbar_nach_praefix(zuordnung, 2)
        zahlen = dict(
            Municipality.objects.values_list("state_key").annotate(n=Count("id")).values_list("state_key", "n")
        )
        return {
            "stufe": "land",
            "titel": "Bundesland",
            "zurueck": None,
            "eintraege": [
                {
                    "art": "gruppe",
                    "name": name,
                    "land": schluessel,
                    "anzahl": zahlen[schluessel],
                    "mit_daten": mit_daten.get(schluessel, 0),
                }
                for schluessel, name in sorted(LAENDER.items(), key=lambda paar: paar[1])
                if zahlen.get(schluessel)
            ],
        }

    land_name = LAENDER.get(land, "")
    if not kreis:
        mit_daten = _verfuegbar_nach_praefix(zuordnung, 5)
        kreise = list(
            Municipality.objects.filter(state_key=land)
            .values("district_key", "district")
            .annotate(n=Count("id"))
            .order_by("district")
        )
        # Kreisfreie Städte (und Stadtstaaten) sind direkt wählbar statt einer Stufe mit einem Eintrag
        einzeln = {
            e.district_key: e
            for e in Municipality.objects.filter(
                state_key=land, district_key__in=[z["district_key"] for z in kreise if z["n"] == 1]
            )
        }
        eintraege: list[dict[str, Any]] = []
        for zeile in kreise:
            if zeile["district_key"] in einzeln:
                eintraege.append({"art": "kommune", **_treffer(einzeln[zeile["district_key"]], zuordnung).als_dict()})
            else:
                eintraege.append(
                    {
                        "art": "gruppe",
                        "name": zeile["district"] or zeile["district_key"],
                        "land": land,
                        "kreis": zeile["district_key"],
                        "anzahl": zeile["n"],
                        "mit_daten": mit_daten.get(zeile["district_key"], 0),
                    }
                )
        eintraege.sort(key=lambda e: normalisieren(str(e["name"])))
        return {"stufe": "kreis", "titel": land_name, "zurueck": {"titel": "Alle Länder"}, "eintraege": eintraege}

    im_kreis = list(Municipality.objects.filter(district_key=kreis).order_by("-is_association", "name"))
    kreis_name = next((e.district for e in im_kreis if e.district), kreis)
    zurueck: dict[str, str] = {"titel": land_name, "land": land}
    verbaende = [e for e in im_kreis if e.is_association]
    if not verband and len(im_kreis) > STOEBERN_GRUPPIEREN_AB and verbaende:
        # Großer Kreis: erst die Gemeindeverbände (Samtgemeinde, Verbandsgemeinde, Amt), dann die übrigen Gemeinden
        praefixe = {v.key[:9] for v in verbaende}
        gruppen: list[dict[str, Any]] = [
            {
                "art": "gruppe",
                "name": v.name,
                "ort": v.kind,
                "land": land,
                "kreis": kreis,
                "verband": v.key[:9],
                "anzahl": sum(1 for e in im_kreis if e.key[:9] == v.key[:9] and not e.is_association),
            }
            for v in verbaende
        ]
        gruppen += [
            {"art": "kommune", **_treffer(e, zuordnung).als_dict()}
            for e in im_kreis
            if not e.is_association and e.key[:9] not in praefixe
        ]
        return {"stufe": "verband", "titel": kreis_name, "zurueck": zurueck, "eintraege": gruppen}

    auswahl = [e for e in im_kreis if e.key.startswith(verband)] if verband else im_kreis
    titel = kreis_name
    if verband:
        zurueck = {"titel": kreis_name, "land": land, "kreis": kreis}
        titel = next((v.name for v in verbaende if v.key.startswith(verband)), kreis_name)
    return {
        "stufe": "kommune",
        "titel": titel,
        "zurueck": zurueck,
        "eintraege": [{"art": "kommune", **_treffer(e, zuordnung).als_dict()} for e in auswahl],
    }
