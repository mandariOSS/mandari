# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Known German OParl Sources

This module contains a curated list of German municipalities with OParl endpoints.
Sources are categorized by size and priority.

Letzte Prüfung: 2026-03-11 (alle Endpoints live getestet)
Archiv: archive/oparl_sources_2026-03-11/
"""

from dataclasses import dataclass


@dataclass
class OParlSource:
    """An OParl source definition."""
    name: str
    url: str
    priority: int = 2  # 1=high, 2=medium, 3=low
    category: str = "municipality"  # municipality, district, verbandsgemeinde, other


# =============================================================================
# Großstädte (Prio 1) — getestet & erreichbar
# =============================================================================
MAJOR_CITIES = [
    OParlSource("Stadt Köln", "https://buergerinfo.stadt-koeln.de/oparl/system", 1),
    OParlSource("Stadt Bonn", "https://www.bonn.sitzung-online.de/public/oparl/system", 1),  # HTTP 500 bei Prüfung
    OParlSource("Landeshauptstadt Düsseldorf", "https://ris-oparl.itk-rheinland.de/Oparl/system", 1),
    OParlSource("Stadt Dresden", "https://oparl.dresden.de/system", 1),
    OParlSource("Stadt Leipzig", "https://ratsinformation.leipzig.de/allris_leipzig_public/oparl/system", 1),
    OParlSource("Stadt Wuppertal", "https://oparl.wuppertal.de/oparl/system", 1),
    OParlSource("Stadt Münster", "https://oparl.stadt-muenster.de/system", 1),
    OParlSource("Stadt Braunschweig", "https://ratsinfo.braunschweig.de/bi/oparl/1.0/system.asp", 1),
    OParlSource("Stadt Krefeld", "https://ris.krefeld.de/webservice/oparl/v1.1/system", 1),
    OParlSource("Stadt Freiburg", "https://ris.freiburg.de/oparl", 1),
    OParlSource("München Transparent", "https://www.muenchen-transparent.de/oparl/v1.0", 1),
    # Volt-Standorte NRW (Kommunalwahl 2025) — Endpunkte 2026-07 geprüft, OParl aktiv
    OParlSource("Stadt Bochum", "https://bochum.ratsinfomanagement.net/webservice/oparl/v1.1/system", 1),
    OParlSource("Stadt Hamm", "https://hamm.ratsinfomanagement.net/webservice/oparl/v1.1/system", 1),
]

# =============================================================================
# Mittelstädte (Prio 2)
# =============================================================================
MEDIUM_CITIES = [
    OParlSource("Klingenstadt Solingen", "https://sdnetrim.kdvz-frechen.de/rim4957/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Castrop-Rauxel", "https://castroprauxel.gremien.info/oparl"),
    OParlSource("Stadt Herford", "https://herford.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Bergheim", "https://sdnetrim.kdvz-frechen.de/rim4800/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Pulheim", "https://sdnetrim.kdvz-frechen.de/rim4350/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Willich", "https://ris.stadt-willich.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Erftstadt", "https://sdnetrim.kdvz-frechen.de/rim4490/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Rheda-Wiedenbrück", "https://ratsinfo.rheda-wiedenbrueck.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Gronau", "https://gronau.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Brühl", "https://ratsinfo.bruehl.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Kleve", "https://ris.kleve.de/webservice/oparl/v1.1/system"),  # Volt-Standort NRW
    OParlSource("Stadt Moers", "https://ris.moers.de/webservice/oparl/v1.1/system"),
    OParlSource("Bezirksregierung Köln", "https://bezreg-koeln.ratsinfomanagement.net/webservice/oparl/v1.1/system", 2, "other"),
    OParlSource("Stadt Lahr/Schwarzwald", "https://lahr.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Bad Kreuznach", "https://bad-kreuznach-stadt.gremien.info/oparl/system"),
    OParlSource("Stadt Pirmasens", "https://oparl.stadt-pirmasens.de/oparl/system"),
    OParlSource("Stadt Wesseling", "https://ratsinfo.wesseling.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Goch", "https://ris.goch.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Jülich", "https://sdnetrim.kdvz-frechen.de/rim4240/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Emsdetten", "https://emsdetten.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Kolpingstadt Kerpen", "https://ratsinfo.stadt-kerpen.de/webservice/oparl/v1.0/system"),
]

# =============================================================================
# Berliner Bezirke (Prio 2) — Timeouts bei Prüfung 2026-03-11
# Berlin Marzahn-Hellersdorf wurde entfernt (404)
# =============================================================================
BERLIN_DISTRICTS = [
    OParlSource("Berlin Steglitz-Zehlendorf", "https://www.sitzungsdienst-steglitz-zehlendorf.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Treptow-Köpenick", "https://www.sitzungsdienst-treptow-koepenick.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Reinickendorf", "https://www.sitzungsdienst-reinickendorf.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Pankow", "https://www.sitzungsdienst-pankow.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Lichtenberg", "https://www.sitzungsdienst-lichtenberg.de/oi/oparl/1.0/system.asp", 2, "district"),
]

# =============================================================================
# Landkreise & Kreise (Prio 2)
# Entfernt: Landkreis Ludwigslust-Parchim (DNS fail)
# =============================================================================
DISTRICTS = [
    OParlSource("Landkreis Märkisch-Oderland", "https://ratsinfo-online.net/landkreis-mol-bi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Kreis Gütersloh", "https://sdnetrim.kdvz-frechen.de/rim4890/webservice/oparl/v1.1/system", 2, "district"),
    OParlSource("Kreis Viersen", "https://kis.kreis-viersen.de/webservice/oparl/v1.0/system", 2, "district"),
    OParlSource("Kreisverwaltung Euskirchen", "https://sdnetrim.kdvz-frechen.de/rim4520/webservice/oparl/v1.1/system", 2, "district"),
    OParlSource("Regionalverband Ruhr", "https://rvr-online.gremien.info/oparl", 2, "district"),
]

# =============================================================================
# OParl vorhanden, aber vom Betreiber DEAKTIVIERT (Stand 2026-07)
# Antwort des Endpunkts: {"error": "Webservice \"OParl\" ist nicht aktiviert!"}
# -> Kein Scraping noetig, eine Freischaltungsanfrage an die Kommune genuegt.
#    Nach Freischaltung hier auskommentieren und in die Listen oben verschieben.
# =============================================================================
OPARL_DEACTIVATED = [
    # ("Stadt Essen", "https://ris.essen.de/webservice/oparl/v1.1/system"),          # Volt-Standort, 570k EW
    # ("Stadt Minden", "https://minden.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    # ("Stadt Steinfurt", "https://steinfurt.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    # ("Stadt Greven", "https://greven.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    # ("Stadt Tecklenburg", "https://tecklenburg.ratsinfomanagement.net/webservice/oparl/v1.1/system"),  # Volt-Standort
]

# =============================================================================
# Verbandsgemeinden (haben jeweils viele Ortsgemeinden als Bodies)
# =============================================================================
VERBANDSGEMEINDEN = [
    OParlSource("Herxheim", "https://herxheim.gremien.info/oparl/system", 3, "verbandsgemeinde"),
    OParlSource("Emmelshausen", "https://emmelshausen.gremien.info/oparl/system", 3, "verbandsgemeinde"),
    OParlSource("Montabaur", "https://montabaur.gremien.info/oparl/system", 3, "verbandsgemeinde"),
    OParlSource("Westerburg", "https://westerburg.gremien.info/oparl/system", 3, "verbandsgemeinde"),
    OParlSource("Enkenbach-Alsenborn", "https://enkenbach-alsenborn.gremien.info/oparl/system", 3, "verbandsgemeinde"),
    OParlSource("Verbandsgemeinde Hagenbach", "https://www.hagenbach.sitzung-online.de/bi/oparl/1.0/system.asp", 3, "verbandsgemeinde"),
    OParlSource("Verbandsgemeinde Weida-Land", "https://weida-land.gremien.info/oparl/system", 3, "verbandsgemeinde"),
    OParlSource("Amt Itzstedt", "https://www.itzstedt.sitzung-online.de/bi/oparl/1.0/system.asp", 3, "verbandsgemeinde"),
]

# =============================================================================
# Kleine Gemeinden & Städte (Prio 3)
# Entfernt: Stadt Olpe (DNS), Stadt Rees (401)
# =============================================================================
SMALL_MUNICIPALITIES = [
    OParlSource("Stadt Enger", "https://enger.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Spenge", "https://spenge.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Vlotho", "https://vlotho.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Hiddenhausen", "https://hiddenhausen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Kirchlengern", "https://kirchlengern.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Rödinghausen", "https://roedinghausen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Schwalmtal", "https://ris.schwalmtal.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Ladbergen", "https://ladbergen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Rahden", "https://rahden.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Stemwede", "https://stemwede.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Aldenhoven", "https://ratsinfo.aldenhoven.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Nettersheim", "https://sdnetrim.kdvz-frechen.de/rim4580/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Steinhagen", "https://ratsinfo.steinhagen.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Langenberg", "https://ratsinfo.langenberg.de/webservice/oparl/v1.0/system", 3),
    OParlSource("Gemeinde Weilerswist", "https://sdnetrim.kdvz-frechen.de/rim4510/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Bad Münstereifel", "https://ratsinfo.bad-muenstereifel.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Leopoldshohe", "https://leopoldshoehe.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Wachtendonk", "https://ris.wachtendonk.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Bedburg", "https://sdnetrim.kdvz-frechen.de/rim4780/webservice/oparl/v1.1/system", 3),
    OParlSource("Aarbergen", "https://rim.ekom21.de/aarbergen/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Wallenhorst", "https://wallenhorst.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Bad Pyrmont", "https://badpyrmont.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Kronberg im Taunus", "https://kronberg.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Velen", "https://velen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Schiffdorf", "https://schiffdorf.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Willingen", "https://rim.ekom21.de/willingen/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Lohfelden", "https://lohfelden.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Boppard", "https://www.boppard.sitzung-online.de/bi/oparl/1.0/system.asp", 3),
    OParlSource("Gemeinde Cölbe", "https://rim.ekom21.de/coelbe/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Ehringshausen", "https://rim.ekom21.de/ehringshausen/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Fernwald", "https://rim.ekom21.de/fernwald/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Glashütten", "https://rim.ekom21.de/glashuetten/webservice/oparl/v1.1/system", 3),
    OParlSource("Guxhagen", "https://rim.ekom21.de/guxhagen/webservice/oparl/v1.1/system", 3),
    OParlSource("Hessisch Lichtenau", "https://rim.ekom21.de/hessisch-lichtenau/webservice/oparl/v1.1/system", 3),
    OParlSource("Kreisstadt Homberg (Efze)", "https://rim.ekom21.de/homberg-efze/webservice/oparl/v1.1/system", 3),
    OParlSource("Homberg (Ohm)", "https://rim.ekom21.de/homberg-ohm/webservice/oparl/v1.1/system", 3),
    OParlSource("Schmitten", "https://rim.ekom21.de/schmitten/webservice/oparl/v1.1/system", 3),
    OParlSource("Schwarzenborn", "https://rim.ekom21.de/schwarzenborn/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Waldbrunn im Westerwald", "https://rim.ekom21.de/waldbrunn/webservice/oparl/v1.1/system", 3),
    OParlSource("Samtgemeinde Sögel", "https://soegel.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Uplengen", "https://uplengen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Parchim", "https://www.parchim.sitzung-online.de/bi/oparl/1.0/system.asp", 3),
    OParlSource("Stadt Rosbach", "https://www.rosbach.sitzung-online.de/bi/oparl/1.0/system.asp", 3),
    OParlSource("Gemeinde Hürtgenwald", "https://sdnetrim.kdvz-frechen.de/rim4220/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Inden", "https://sdnetrim.kdvz-frechen.de/rim4230/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Kreuzau", "https://sdnetrim.kdvz-frechen.de/rim4250/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Langerwehe", "https://sdnetrim.kdvz-frechen.de/rim4260/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Linnich", "https://sdnetrim.kdvz-frechen.de/rim4270/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Merzenich", "https://sdnetrim.kdvz-frechen.de/rim4280/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Nörvenich", "https://sdnetrim.kdvz-frechen.de/rim4160/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Titz", "https://sdnetrim.kdvz-frechen.de/rim4170/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Vettweiß", "https://sdnetrim.kdvz-frechen.de/rim4180/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Kall", "https://sdnetrim.kdvz-frechen.de/rim4550/webservice/oparl/v1.1/system", 3),
    # Instabil bei Prüfung 2026-03-11
    OParlSource("Eschwege", "https://rim.ekom21.de/eschwege/webservice/oparl/v1.1/system", 3),  # HTTP 400
    OParlSource("Stadtverwaltung Ortenberg", "https://rim.ekom21.de/ortenberg/webservice/oparl/v1.1/system", 3),  # HTTP 400
    OParlSource("Stadt Großalmerode", "https://rim.ekom21.de/grossalmerode/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Bleckede", "https://www.bleckede.sitzung-online.de/bi/oparl/1.0/system.asp", 3),  # HTTP 500
    OParlSource("Gemeinde Harsum", "https://www.harsum.sitzung-online.de/bi/oparl/1.0/system.asp", 3),  # HTTP 500
]

# =============================================================================
# Aggregatoren
# OParl Mirror entfernt (DNS fail)
# =============================================================================
AGGREGATORS = [
    OParlSource("Politik bei Uns", "https://oparl.politik-bei-uns.de/system", 1, "other"),
]


def get_all_sources() -> list[OParlSource]:
    """Get all known OParl sources."""
    return (
        MAJOR_CITIES +
        MEDIUM_CITIES +
        BERLIN_DISTRICTS +
        DISTRICTS +
        VERBANDSGEMEINDEN +
        SMALL_MUNICIPALITIES +
        AGGREGATORS
    )


def get_sources_by_priority(priority: int) -> list[OParlSource]:
    """Get sources by priority level."""
    return [s for s in get_all_sources() if s.priority == priority]


def get_priority_1_sources() -> list[OParlSource]:
    """Get only high-priority sources (major cities)."""
    return get_sources_by_priority(1)


def get_default_sources() -> list[OParlSource]:
    """
    Get recommended default sources for initial setup.

    Returns high-priority sources that are known to be reliable.
    """
    reliable = [
        "Stadt Köln",
        "Landeshauptstadt Düsseldorf",
        "Stadt Münster",
        "Stadt Wuppertal",
        "Stadt Dresden",
        "Stadt Leipzig",
        "München Transparent",
        "Stadt Krefeld",
        "Stadt Freiburg",
        "Stadt Braunschweig",
    ]
    return [s for s in get_all_sources() if s.name in reliable]
