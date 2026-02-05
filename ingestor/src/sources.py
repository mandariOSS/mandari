# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Known German OParl Sources

This module contains a curated list of German municipalities with OParl endpoints.
Sources are categorized by size and priority.
"""

from dataclasses import dataclass


@dataclass
class OParlSource:
    """An OParl source definition."""
    name: str
    url: str
    priority: int = 2  # 1=high, 2=medium, 3=low
    category: str = "municipality"  # municipality, district, state, other


# Major German Cities (Priority 1)
MAJOR_CITIES = [
    OParlSource("Stadt Köln", "https://buergerinfo.stadt-koeln.de/oparl/system", 1),
    OParlSource("Stadt Bonn", "https://www.bonn.sitzung-online.de/public/oparl/system", 1),
    OParlSource("Landeshauptstadt Düsseldorf", "https://ris-oparl.itk-rheinland.de/Oparl/system", 1),
    OParlSource("Stadt Dresden", "https://oparl.dresden.de/system", 1),
    OParlSource("Stadt Leipzig", "https://ratsinformation.leipzig.de/allris_leipzig_public/oparl/system", 1),
    OParlSource("Stadt Wuppertal", "https://oparl.wuppertal.de/oparl/system", 1),
    OParlSource("Stadt Münster", "https://oparl.stadt-muenster.de/system", 1),
    OParlSource("Stadt Aachen", "https://ratsinfo.aachen.de/bi/oparl/1.0/system.asp", 1),
    OParlSource("Stadt Braunschweig", "https://ratsinfo.braunschweig.de/bi/oparl/1.0/system.asp", 1),
    OParlSource("Stadt Krefeld", "https://ris.krefeld.de/webservice/oparl/v1.1/system", 1),
    OParlSource("Stadt Freiburg", "https://ris.freiburg.de/oparl", 1),
    OParlSource("Stadt Ulm", "https://buergerinfo.ulm.de/oparl/system", 1),
    OParlSource("München Transparent", "https://www.muenchen-transparent.de/oparl/v1.0", 1),
]

# Medium-sized Cities (Priority 2)
MEDIUM_CITIES = [
    OParlSource("Stadt Hagen", "https://www.hagen.de/buergerinfo/oparl/1.0/system.asp"),
    OParlSource("Klingenstadt Solingen", "https://sdnetrim.kdvz-frechen.de/rim4957/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Castrop-Rauxel", "https://castroprauxel.gremien.info/oparl"),
    OParlSource("Stadt Herford", "https://herford.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Bergheim", "https://sdnetrim.kdvz-frechen.de/rim4800/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Pulheim", "https://sdnetrim.kdvz-frechen.de/rim4350/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Willich", "https://ris.stadt-willich.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Erftstadt", "https://sdnetrim.kdvz-frechen.de/rim4490/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Rheda-Wiedenbrück", "https://ratsinfo.rheda-wiedenbrueck.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Gronau", "https://gronau.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Erkelenz", "https://ratsinfo.erkelenz.de/bi/oparl/1.0/system.asp"),
    OParlSource("Stadt Brühl", "https://ratsinfo.bruehl.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Lahr/Schwarzwald", "https://lahr.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Bad Kreuznach", "https://bad-kreuznach-stadt.gremien.info/oparl/system"),
    OParlSource("Stadt Pirmasens", "https://oparl.stadt-pirmasens.de/oparl/system"),
    OParlSource("Stadt Wesseling", "https://ratsinfo.wesseling.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Goch", "https://ris.goch.de/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Jülich", "https://sdnetrim.kdvz-frechen.de/rim4240/webservice/oparl/v1.1/system"),
    OParlSource("Stadt Emsdetten", "https://emsdetten.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    OParlSource("Kolpingstadt Kerpen", "https://ratsinfo.stadt-kerpen.de/webservice/oparl/v1.0/system"),
]

# Berlin Districts (Priority 2)
BERLIN_DISTRICTS = [
    OParlSource("Berlin Marzahn-Hellersdorf", "https://www.sitzungsdienst-marzahn-hellersdorf.de/oi/oparl/1.1/system.asp", 2, "district"),
    OParlSource("Berlin Steglitz-Zehlendorf", "https://www.sitzungsdienst-steglitz-zehlendorf.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Treptow-Köpenick", "https://www.sitzungsdienst-treptow-koepenick.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Reinickendorf", "https://www.sitzungsdienst-reinickendorf.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Pankow", "https://www.sitzungsdienst-pankow.de/oi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Berlin Lichtenberg", "https://www.sitzungsdienst-lichtenberg.de/oi/oparl/1.0/system.asp", 2, "district"),
]

# Districts/Counties (Priority 2)
DISTRICTS = [
    OParlSource("Landkreis Ludwigslust-Parchim", "https://www.lwl-pch.sitzung-online.de/bi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Landkreis Märkisch-Oderland", "https://ratsinfo-online.net/landkreis-mol-bi/oparl/1.0/system.asp", 2, "district"),
    OParlSource("Kreis Gütersloh", "https://sdnetrim.kdvz-frechen.de/rim4890/webservice/oparl/v1.1/system", 2, "district"),
    OParlSource("Kreis Viersen", "https://kis.kreis-viersen.de/webservice/oparl/v1.0/system", 2, "district"),
    OParlSource("Kreisverwaltung Euskirchen", "https://sdnetrim.kdvz-frechen.de/rim4520/webservice/oparl/v1.1/system", 2, "district"),
    OParlSource("Regionalverband Ruhr", "https://rvr-online.gremien.info/oparl", 2, "district"),
]

# Smaller Municipalities (Priority 3)
SMALL_MUNICIPALITIES = [
    OParlSource("Eschwege", "https://rim.ekom21.de/eschwege/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Enger", "https://enger.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Spenge", "https://spenge.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Vlotho", "https://vlotho.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Hiddenhausen", "https://hiddenhausen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Kirchlengern", "https://kirchlengern.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Rödinghausen", "https://roedinghausen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Schwalmtal", "https://ris.schwalmtal.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Ladbergen", "https://ladbergen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Stemwede", "https://stemwede.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Aldenhoven", "https://ratsinfo.aldenhoven.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Nettersheim", "https://sdnetrim.kdvz-frechen.de/rim4580/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Olpe", "https://sitzungsdienst.kdz-ws.net/gkz330/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Steinhagen", "https://ratsinfo.steinhagen.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Langenberg", "https://ratsinfo.langenberg.de/webservice/oparl/v1.0/system", 3),
    OParlSource("Gemeinde Weilerswist", "https://sdnetrim.kdvz-frechen.de/rim4510/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Bad Münstereifel", "https://ratsinfo.bad-muenstereifel.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Leopoldshohe", "https://leopoldshoehe.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Gemeinde Wachtendonk", "https://ris.wachtendonk.de/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Rees", "https://sessionnet-oparl.krz.de/oparl/bodies/5205", 3),
    OParlSource("Stadt Bedburg", "https://sdnetrim.kdvz-frechen.de/rim4780/webservice/oparl/v1.1/system", 3),
    OParlSource("Aarbergen", "https://rim.ekom21.de/aarbergen/webservice/oparl/v1.1/system", 3),
    OParlSource("Westerburg", "https://westerburg.gremien.info/oparl/system", 3),
    OParlSource("Gemeinde Wallenhorst", "https://wallenhorst.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Stadt Bad Pyrmont", "https://badpyrmont.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Kronberg im Taunus", "https://kronberg.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
    OParlSource("Velen", "https://velen.ratsinfomanagement.net/webservice/oparl/v1.1/system", 3),
]

# Aggregators (Priority 1)
AGGREGATORS = [
    OParlSource("Politik bei Uns", "https://oparl.politik-bei-uns.de/system", 1, "other"),
    OParlSource("OParl Mirror", "https://mirror.oparl.org/system", 2, "other"),
]


def get_all_sources() -> list[OParlSource]:
    """Get all known OParl sources."""
    return (
        MAJOR_CITIES +
        MEDIUM_CITIES +
        BERLIN_DISTRICTS +
        DISTRICTS +
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
    # Start with major cities that have reliable OParl APIs
    reliable = [
        "Stadt Köln",
        "Stadt Bonn",
        "Landeshauptstadt Düsseldorf",
        "Stadt Münster",
        "Stadt Aachen",
        "Stadt Wuppertal",
        "Stadt Dresden",
        "Stadt Leipzig",
        "München Transparent",
    ]
    return [s for s in get_all_sources() if s.name in reliable]
