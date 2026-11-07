# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenschutz bei Orten (Issue #1269): keine Adresspunkte und keine Hausnummern für Privatanschriften.

Eingaben aus der Einwohnerschaft (Bürgerantrag, Anregungen und Beschwerden, Einwohnerfrage und -antrag, Bürgerbrief,
Eingaben, Petitionen) und Bauanträge bzw. Vorgänge der Bauordnung nennen oft eine Wohnanschrift oder ein privates
Grundstück. Für diese Vorgänge gilt im Bürgerportal:

* Die Verortung setzt nie einen Adresspunkt, höchstens den Punkt der Straße (``georeferencing``, ``oparl_locations``,
  ``verortung_pruefung``). Eingaben aus der Einwohnerschaft werden nur aus dem Titel verortet, nicht aus dem Volltext:
  Dort steht meist die Anschrift der Einreichenden, nicht der Ort der Sache.
* Öffentliche Titel, Ortslisten, Suchtreffer, Benachrichtigungen und die Werkzeuge des KI-Assistenten nennen keine
  Hausnummer (``oeffentlicher_titel``, ``ort_ohne_hausnummer``). Intern (Datenbank, Work, Verortung) bleibt der volle
  Titel; das Original ist über den Link ins Ratsinformationssystem erreichbar.

Zwei Prüfungen: ``grund_nach_art`` braucht nur Art und Titel und taugt für Listen ohne weitere Abfrage. ``grund``
schaut zusätzlich auf das federführende Amt (OParl ``underDirectionOf``: Bauordnung, Bauaufsicht, Untere
Naturschutzbehörde) und auf über- und untergeordnete Vorgänge (``superordinatedPaper``/``subordinatedPaper``), denn
Stellungnahmen zu einem Bürgerantrag nennen oft dieselbe Anschrift; sie läuft bei der Verortung, auf der Vorgangsseite
und im Befehl ``adresspunkte_zurueckstufen``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from insight_core.models import OParlBody, OParlPaper

EINWOHNERSCHAFT: Final = "einwohnerschaft"
BAUORDNUNG: Final = "bauordnung"

#: Ersatz für eine ausgelassene Hausnummer in Titeln und Texten
OHNE_NR: Final = "[Hausnummer ausgelassen]"

# --- Vorgangsarten ---------------------------------------------------------------------------------------------------

#: Vorgangsart (OParl ``paperType``) einer Eingabe aus der Einwohnerschaft; „Bürgerbegehren“ ist ein förmliches
#: Verfahren ohne Privatanschrift und zählt nicht
_EINWOHNER_ART: Final = re.compile(
    r"bürger\s*-?\s*(?:antr[aä]g|brief|eingabe|anregung|frage)|einwohner\s*-?\s*(?:antr[aä]g|frage|anfrage|eingabe|"
    r"anregung)|anregung|beschwerde|\beingabe|petition",
    re.IGNORECASE,
)
#: Titel einer Eingabe aus der Einwohnerschaft (strenger als die Art: „Anregung“ allein kommt in vielen Titeln vor)
_EINWOHNER_TITEL: Final = re.compile(
    r"bürger\s*-?\s*(?:antr[aä]g|brief|eingabe|anregung)|einwohner\s*-?\s*(?:antr[aä]g|frage|anfrage|eingabe)|"
    r"anregungen?\s+(?:und|u\.|&|bzw\.?)\s+beschwerden?|(?:anregung|beschwerde|eingabe)\s+(?:gem\.?|gemäß|nach)\s*§|"
    r"§\s*24\s*go\b|petition",
    re.IGNORECASE,
)
#: Vorgangsart der Bauordnung (Bauantrag, Voranfrage, Vorbescheid, Fällgenehmigung)
_BAU_ART: Final = re.compile(
    r"bau\s*-?\s*(?:antr[aä]g|voranfrage|vorbescheid|genehmigung|ordnung|aufsicht)|fäll\s*-?\s*(?:genehmigung|antrag)",
    re.IGNORECASE,
)
#: Titel eines Vorgangs zu einem privaten Bauvorhaben
_BAU_TITEL: Final = re.compile(
    r"bau\s*-?\s*(?:antr[aä]g|voranfrage|vorbescheid|genehmigung)|nutzungsänderung|"
    r"§\s*3[1456]\s*(?:abs\.?\s*\d\s*)?baugb|einvernehmen\s+(?:der\s+(?:gemeinde|stadt)\s+)?(?:gem\.?|gemäß|nach)\s*§\s*36|"
    r"befreiung\s+von\s+(?:den\s+)?festsetzungen|fäll\s*-?\s*(?:genehmigung|antrag)",
    re.IGNORECASE,
)
#: Federführendes Amt mit privaten Grundstücken (Bauordnung, Bauaufsicht, Untere Naturschutzbehörde)
_AMT_PRIVAT: Final = re.compile(r"bauordnung|bauaufsicht|naturschutzbehörde", re.IGNORECASE)


def grund_nach_art(paper_type: str | None, name: str | None) -> str:
    """
    ``EINWOHNERSCHAFT`` oder ``BAUORDNUNG``, wenn Art oder Titel auf eine Privatanschrift schließen lassen, sonst leer.
    Ohne Datenbankzugriff, für Listen.
    """
    art = paper_type or ""
    titel = name or ""
    if _EINWOHNER_ART.search(art) or _EINWOHNER_TITEL.search(titel):
        return EINWOHNERSCHAFT
    if _BAU_ART.search(art) or _BAU_TITEL.search(titel):
        return BAUORDNUNG
    return ""


def _kennungen(werte: Any) -> list[str]:
    """OParl-Verweise (Adresse oder Objekt mit ``id``) aus den Rohdaten."""
    if isinstance(werte, (str, dict)):
        werte = [werte]
    if not isinstance(werte, list):
        return []
    kennungen: list[str] = []
    for wert in werte:
        kennung = wert.get("id") if isinstance(wert, dict) else wert
        if isinstance(kennung, str) and kennung:
            kennungen.append(kennung)
    return kennungen


def grund(paper: OParlPaper) -> str:
    """
    Wie ``grund_nach_art``, dazu das federführende Amt und die über- und untergeordneten Vorgänge (eine Stufe). Zwei
    kleine Abfragen höchstens; für die Verortung, die Vorgangsseite und den Befehl ``adresspunkte_zurueckstufen``.
    """
    from insight_core.models import OParlOrganization, OParlPaper

    eigener = grund_nach_art(paper.paper_type, paper.name)
    if eigener:
        return eigener
    roh = paper.raw_json if isinstance(paper.raw_json, dict) else {}
    aemter = _kennungen(roh.get("underDirectionOf"))
    if aemter:
        namen = OParlOrganization.objects.filter(external_id__in=aemter).values_list("name", flat=True)
        if any(_AMT_PRIVAT.search(name or "") for name in namen):
            return BAUORDNUNG
    verbunden = _kennungen(roh.get("superordinatedPaper")) + _kennungen(roh.get("subordinatedPaper"))
    if verbunden:
        for art, name in OParlPaper.objects.filter(external_id__in=verbunden[:50]).values_list("paper_type", "name"):
            anderer = grund_nach_art(art, name)
            if anderer:
                return anderer
    return ""


# --- Hausnummern auslassen -------------------------------------------------------------------------------------------

#: Buchstabe ohne Ziffer und Unterstrich
_BUCHSTABE: Final = r"[^\W\d_]"
#: Eine Hausnummer: höchstens drei Ziffern (vierstellige Zahlen sind meist Jahre), ein Buchstabe dahinter nur als
#: eigenes Zeichen („12a“, „12 a“, nicht „12 Wohnungen“ und nicht das „u.“ in „7 u. 9“)
_EINE_NR: Final = rf"\d{{1,3}}(?:\s?(?!u\.)[a-zA-Z](?!{_BUCHSTABE}))?"
#: Hausnummer samt Bereich oder Aufzählung („12–14“, „3/5“, „7 und 9“, „2, 4“); keine Ziffer direkt dahinter
_NR: Final = rf"{_EINE_NR}(?:\s*(?:-|–|bis|und|u\.|/|,|&)\s*{_EINE_NR})*(?!\d)"
#: Grundwörter, auf die ein Straßenname endet (auch „Am Hof“, „Markt“)
_GRUNDWORT: Final = (
    r"(?:straße|strasse|str\.|weg|platz|gasse|allee|ring|ufer|damm|pfad|chaussee|hof|markt|graben|berg|feld|wall|"
    r"steig|stieg|kamp|bogen|deich|garten|brücke|anger|winkel|wiese|weide|mühle|siedlung)"
)
#: Straßenname (letztes Wort mit Grundwort) und Hausnummer, auch mit „Nr.“ dazwischen
_STRASSE_NR: Final = re.compile(rf"(?<![\w-])([\w.-]*{_GRUNDWORT})(\s+)(?:nr\.?\s*)?{_NR}", re.IGNORECASE)
#: Straßen mit Vorwort („Am Wehr 4“, „An der Kirche 5“, „Zur Linde 2“): ein bis drei großgeschriebene Wörter
_VORWORT_NR: Final = re.compile(
    r"(?<!\w)((?:Am|An der|An den|Auf dem|Auf der|Auf den|Im|In der|In den|Zum|Zur|Hinter dem|Hinter der|Unter den|"
    rf"Vor dem|Bei der|Beim)(?:\s+[A-ZÄÖÜ][\w.-]*){{1,3}})(\s+)(?:Nr\.?\s*)?{_NR}"
)
#: „Hausnummer 82“, „Haus-Nr. 5“, „Hausnummern 36 bis 40“; eine schließende Klammer dazwischen bleibt stehen
_WORT_NR: Final = re.compile(rf"\b(?:hausnummern?|hausnr\.?|haus-nr\.?|haus\s+nr\.?)\s*(\))?\s*{_NR}", re.IGNORECASE)


def hausnummern_auslassen(text: str | None, ersatz: str = OHNE_NR) -> str:
    """
    Hausnummern hinter Straßennamen und hinter „Hausnummer“ durch ``ersatz`` ersetzen.

    „Bürgerantrag: Parkverbot vor Musterstraße 12a“ → „… vor Musterstraße [Hausnummer ausgelassen]“. Erkennt eine
    Straße am Grundwort (-straße, -weg, -platz, Allee …) oder am Vorwort („Am Wehr 4“); im Zweifel lieber eine Zahl zu
    viel ausgelassen als eine Anschrift gezeigt. Ohne Ziffer im Text kehrt die Funktion sofort zurück.
    """
    if not text or not any(zeichen.isdigit() for zeichen in text):
        return text or ""

    def strasse(treffer: re.Match[str]) -> str:
        return f"{treffer.group(1)} {ersatz}" if ersatz else treffer.group(1)

    ergebnis = _STRASSE_NR.sub(strasse, text)
    ergebnis = _VORWORT_NR.sub(strasse, ergebnis)
    ergebnis = _WORT_NR.sub(lambda treffer: ersatz + (treffer.group(1) or ""), ergebnis)
    if not ersatz:
        ergebnis = re.sub(r"\s+([,;:)])", r"\1", re.sub(r"\s{2,}", " ", ergebnis)).strip(" ,;")
    return ergebnis


def hat_hausnummer(text: str | None) -> bool:
    """Steht im Text eine Hausnummer hinter einem Straßennamen oder hinter „Hausnummer“?"""
    return bool(text) and hausnummern_auslassen(text, "\x00") != text


def ort_ohne_hausnummer(name: str | None) -> str:
    """Ortsbezeichnung ohne Hausnummer: „Musterstraße 12, 53111 Bonn“ → „Musterstraße, 53111 Bonn“."""
    from insight_core.services.gazetteer import strip_house_number

    ohne = hausnummern_auslassen(name, "")
    basis, nummer = strip_house_number(ohne)
    return basis if nummer else ohne


def oeffentlicher_titel(name: str | None, paper_type: str | None = None, grund_bekannt: str | None = None) -> str:
    """
    Titel für öffentliche Seiten: bei Eingaben aus der Einwohnerschaft und Bauanträgen ohne Hausnummer.

    ``grund_bekannt`` übergibt ein Ergebnis von ``grund`` (Vorgangsseite); sonst entscheidet ``grund_nach_art``.
    """
    if not name:
        return name or ""
    schutz = grund_bekannt if grund_bekannt is not None else grund_nach_art(paper_type, name)
    return hausnummern_auslassen(name) if schutz else name


# --- Straßenpunkt statt Adresspunkt ----------------------------------------------------------------------------------


def strassenpunkt(body: OParlBody | None, name: str | None) -> dict[str, Any] | None:
    """
    Punkt der Straße aus dem Straßenverzeichnis der Kommune (Mittel der Abschnitte, wie ``StreetGazetteer``) für eine
    Ortsbezeichnung mit oder ohne Hausnummer: ``{"lat", "lon", "name"}`` oder ``None``, wenn die Straße unbekannt ist.
    """
    from insight_core.models import Street
    from insight_core.services.gazetteer import normalize_street_name

    if body is None or not name:
        return None
    strasse = ort_ohne_hausnummer(name).split(",")[0].strip()
    normalisiert = normalize_street_name(strasse)
    if len(normalisiert) < 3:
        return None
    zeilen = list(
        Street.objects.filter(body=body, normalized_name=normalisiert).values_list("name", "latitude", "longitude")
    )
    if not zeilen:
        return None
    return {
        "lat": round(sum(float(z[1]) for z in zeilen) / len(zeilen), 7),
        "lon": round(sum(float(z[2]) for z in zeilen) / len(zeilen), 7),
        "name": zeilen[0][0],
    }
