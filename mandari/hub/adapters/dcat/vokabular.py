# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kontrollierte Vokabulare, die der Katalog verwendet (DCAT-AP.de 3.0, Abschnitt „Kontrollierte Vokabulare“).

Jeder Wert hier stammt aus einem der Vokabulare, die DCAT-AP.de vorschreibt:

- Lizenzen: Lizenzliste von DCAT-AP.de (``http://dcat-ap.de/def/licenses``, Stand 20210721)
- Dateiformate, Themen, Aktualisierungsfrequenz, Sprache, Verfügbarkeit, Zugänglichkeit: Vokabulare des
  Amts für Veröffentlichungen der EU (``http://publications.europa.eu/resource/authority/…``)
- Medientypen: IANA-Register der Medientypen
- Raumbezug: Ebenen und Schlüssel der geopolitischen Verwaltungscodierung von DCAT-AP.de

Die SHACL-Prüfung der CI lädt die Vokabulare selbst und meldet Werte, die es dort nicht gibt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

EU_AUTHORITY: Final = "http://publications.europa.eu/resource/authority"
DCATDE_DEF: Final = "http://dcat-ap.de/def"

#: Sprache aller Angaben im Katalog
SPRACHE_DEUTSCH: Final = f"{EU_AUTHORITY}/language/DEU"
#: Thema „Regierung und öffentlicher Sektor“
THEMA_REGIERUNG: Final = f"{EU_AUTHORITY}/data-theme/GOVE"
#: Themenvokabular (``dcat:themeTaxonomy`` des Katalogs)
THEMEN: Final = f"{EU_AUTHORITY}/data-theme"
#: Laufend aktualisiert (der Bestand wird fortlaufend abgeglichen bzw. sofort geschrieben)
FREQUENZ_LAUFEND: Final = f"{EU_AUTHORITY}/frequency/UPDATE_CONT"
#: Keine Aktualisierung mehr (als Archiv behaltene Kommune)
FREQUENZ_KEINE: Final = f"{EU_AUTHORITY}/frequency/NEVER"
#: Öffentlich zugänglich, ohne Anmeldung
ZUGANG_OEFFENTLICH: Final = f"{EU_AUTHORITY}/access-right/PUBLIC"
#: Verfügbarkeit: dauerhaft unter derselben Adresse
VERFUEGBARKEIT_STABIL: Final = f"{EU_AUTHORITY}/planned-availability/STABLE"

#: Dateiformate (EU-Vokabular „File type“)
FORMAT_JSON: Final = f"{EU_AUTHORITY}/file-type/JSON"
FORMAT_ICS: Final = f"{EU_AUTHORITY}/file-type/ICS"

_IANA: Final = "https://www.iana.org/assignments/media-types"
#: Medientypen (IANA)
MEDIENTYP_JSON: Final = f"{_IANA}/application/json"
MEDIENTYP_KALENDER: Final = f"{_IANA}/text/calendar"

#: Spezifikation, der die Listen der Schnittstelle folgen
OPARL_STANDARD: Final = "https://schema.oparl.org/1.1/"
#: Beschreibung der Schnittstelle für Menschen
OPARL_SPEZIFIKATION: Final = "https://oparl.org/spezifikation/"


# =============================================================================
# Lizenzen
# =============================================================================

_LIZENZEN: Final = f"{DCATDE_DEF}/licenses"


@dataclass(frozen=True)
class Lizenz:
    """Eine Lizenz der Lizenzliste von DCAT-AP.de."""

    uri: str
    name: str
    #: Die Lizenz verlangt die Nennung des Datenbereitstellers (``dcatde:licenseAttributionByText``)
    namensnennung: bool


#: Offene Lizenzen der DCAT-AP.de-Liste, die für Ratsinformationen in Frage kommen. Lizenzen mit
#: Einschränkungen (nicht kommerziell, keine Bearbeitung) fehlen bewusst: Daten unter ihnen sind keine
#: offenen Daten, und der Katalog bietet sie nicht als solche an.
LIZENZEN: Final[tuple[Lizenz, ...]] = (
    Lizenz(f"{_LIZENZEN}/dl-zero-de/2.0", "Datenlizenz Deutschland – Zero – Version 2.0", False),
    Lizenz(f"{_LIZENZEN}/dl-by-de/2.0", "Datenlizenz Deutschland – Namensnennung – Version 2.0", True),
    Lizenz(f"{_LIZENZEN}/dl-by-de/1.0", "Datenlizenz Deutschland – Namensnennung – Version 1.0", True),
    Lizenz(f"{_LIZENZEN}/cc-zero", "Creative Commons Zero 1.0 (CC0)", False),
    Lizenz(f"{_LIZENZEN}/ccpdm/1.0", "Public Domain Mark 1.0", False),
    Lizenz(f"{_LIZENZEN}/cc-by/4.0", "Creative Commons Namensnennung 4.0 International (CC BY 4.0)", True),
    Lizenz(f"{_LIZENZEN}/cc-by-de/3.0", "Creative Commons Namensnennung 3.0 Deutschland (CC BY 3.0 DE)", True),
    Lizenz(
        f"{_LIZENZEN}/cc-by-sa/4.0",
        "Creative Commons Namensnennung – Weitergabe unter gleichen Bedingungen 4.0 International",
        True,
    ),
    Lizenz(
        f"{_LIZENZEN}/cc-by-sa-de/3.0",
        "Creative Commons Namensnennung – Weitergabe unter gleichen Bedingungen 3.0 Deutschland",
        True,
    ),
    Lizenz(f"{_LIZENZEN}/odbl", "Open Data Commons Open Database License (ODbL)", True),
    Lizenz(f"{_LIZENZEN}/odby", "Open Data Commons Attribution License (ODC-BY 1.0)", True),
    Lizenz(f"{_LIZENZEN}/odcpddl", "Open Data Commons Public Domain Dedication and Licence (PDDL)", False),
    Lizenz(f"{_LIZENZEN}/officialWork", "Amtliches Werk, lizenzfrei nach § 5 Abs. 1 UrhG", False),
)

_NACH_URI: Final[dict[str, Lizenz]] = {lizenz.uri: lizenz for lizenz in LIZENZEN}

#: Adressen der Lizenztexte (ohne Schema, ``www.`` und Schrägstrich am Ende) -> Lizenz der Liste
_TEXTE: Final[dict[str, str]] = {
    "govdata.de/dl-de/zero-2-0": f"{_LIZENZEN}/dl-zero-de/2.0",
    "govdata.de/dl-de/by-2-0": f"{_LIZENZEN}/dl-by-de/2.0",
    "govdata.de/dl-de/by-1-0": f"{_LIZENZEN}/dl-by-de/1.0",
    "creativecommons.org/publicdomain/zero/1.0": f"{_LIZENZEN}/cc-zero",
    "creativecommons.org/publicdomain/mark/1.0": f"{_LIZENZEN}/ccpdm/1.0",
    "creativecommons.org/licenses/by/4.0": f"{_LIZENZEN}/cc-by/4.0",
    "creativecommons.org/licenses/by/3.0/de": f"{_LIZENZEN}/cc-by-de/3.0",
    "creativecommons.org/licenses/by-sa/4.0": f"{_LIZENZEN}/cc-by-sa/4.0",
    "creativecommons.org/licenses/by-sa/3.0/de": f"{_LIZENZEN}/cc-by-sa-de/3.0",
    "opendatacommons.org/licenses/odbl": f"{_LIZENZEN}/odbl",
    "opendatacommons.org/licenses/odbl/1.0": f"{_LIZENZEN}/odbl",
    "opendatacommons.org/licenses/odbl/1-0": f"{_LIZENZEN}/odbl",
    "opendatacommons.org/licenses/by": f"{_LIZENZEN}/odby",
    "opendatacommons.org/licenses/by/1.0": f"{_LIZENZEN}/odby",
    "opendatacommons.org/licenses/by/1-0": f"{_LIZENZEN}/odby",
    "opendatacommons.org/licenses/pddl": f"{_LIZENZEN}/odcpddl",
    "opendatacommons.org/licenses/pddl/1.0": f"{_LIZENZEN}/odcpddl",
    "opendatacommons.org/licenses/pddl/1-0": f"{_LIZENZEN}/odcpddl",
    "opendefinition.org/licenses/cc-zero": f"{_LIZENZEN}/cc-zero",
    "opendefinition.org/licenses/odc-odbl": f"{_LIZENZEN}/odbl",
    "opendefinition.org/licenses/odc-by": f"{_LIZENZEN}/odby",
    "opendefinition.org/licenses/odc-pddl": f"{_LIZENZEN}/odcpddl",
}

_SCHEMA: Final = re.compile(r"^https?://", re.IGNORECASE)
#: Anhänge an Lizenzadressen, die dieselbe Lizenz meinen (Sprachfassung, Rechtstext)
_ANHAENGE: Final = ("/deed.de", "/deed.en", "/legalcode.de", "/legalcode")


def _schluessel(url: str) -> str:
    text = _SCHEMA.sub("", url.strip()).lower()
    text = text.removeprefix("www.").rstrip("/")
    for anhang in _ANHAENGE:
        if text.endswith(anhang):
            text = text.removesuffix(anhang).rstrip("/")
    return text


def lizenz(angabe: str | None) -> Lizenz | None:
    """
    Lizenz der DCAT-AP.de-Liste zu einer Lizenzangabe (OParl ``license``: Adresse des Lizenztexts) –
    ``None`` ohne Angabe oder wenn die Angabe keiner offenen Lizenz der Liste entspricht.

    Gängige Schreibweisen derselben Adresse (``http``/``https``, ``www.``, Schrägstrich am Ende, Sprachfassung)
    gelten als dieselbe Lizenz; eine Adresse der Liste selbst gilt unverändert.
    """
    text = (angabe or "").strip()
    if not text:
        return None
    if text in _NACH_URI:
        return _NACH_URI[text]
    schluessel = _schluessel(text)
    return _NACH_URI.get(f"http://{schluessel}") or _NACH_URI.get(_TEXTE.get(schluessel, ""))


# =============================================================================
# Raumbezug (geopolitische Verwaltungscodierung)
# =============================================================================

_GEOCODING: Final = f"{DCATDE_DEF}/politicalGeocoding"

#: Stellen des Amtlichen Gemeindeschlüssels -> (Schlüsselliste, Ebene)
_AGS_ARTEN: Final[dict[int, tuple[str, str]]] = {
    8: ("municipalityKey", "municipality"),
    5: ("districtKey", "administrativeDistrict"),
    3: ("governmentDistrictKey", "administrativeDistrict"),
    2: ("stateKey", "state"),
}
#: Stellen des Regionalschlüssels -> (Schlüsselliste, Ebene)
_RGS_ARTEN: Final[dict[int, tuple[str, str]]] = {
    12: ("regionalKey", "municipality"),
    9: ("municipalAssociationKey", "municipality"),
}


@dataclass(frozen=True)
class Raumbezug:
    """Gebiet eines Datensatzes: Schlüssel (``dct:spatial``) und Ebene (``dcatde:politicalGeocodingLevelURI``)."""

    uri: str
    ebene: str


def _codierung(schluessel: str | None, arten: dict[int, tuple[str, str]]) -> Raumbezug | None:
    text = (schluessel or "").strip()
    art = arten.get(len(text)) if text.isdigit() else None
    if art is None:
        return None
    liste, ebene = art
    return Raumbezug(f"{_GEOCODING}/{liste}/{text}", f"{_GEOCODING}/Level/{ebene}")


def raumbezug(ags: str | None, rgs: str | None = None) -> Raumbezug | None:
    """
    Raumbezug aus dem Amtlichen Gemeindeschlüssel (8 Stellen Gemeinde, 5 Kreis, 3 Regierungsbezirk,
    2 Land) oder ersatzweise dem Regionalschlüssel (12 Stellen Gemeinde, 9 Gemeindeverband).
    ``None``, wenn keiner der beiden eine gültige Form hat.
    """
    return _codierung(ags, _AGS_ARTEN) or _codierung(rgs, _RGS_ARTEN)
