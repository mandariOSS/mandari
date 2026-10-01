# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lizenzen und Raumbezug: Angaben der Kommunen in die Vokabulare von DCAT-AP.de übersetzen."""

from __future__ import annotations

import pytest

from hub.adapters.dcat import vokabular

DL_BY = "http://dcat-ap.de/def/licenses/dl-by-de/2.0"


@pytest.mark.parametrize(
    ("angabe", "erwartet"),
    [
        # Die Auswahl der Session-Mandanten (apps/session/services/oparl_access.py)
        ("https://www.govdata.de/dl-de/zero-2-0", "http://dcat-ap.de/def/licenses/dl-zero-de/2.0"),
        ("https://www.govdata.de/dl-de/by-2-0", DL_BY),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "http://dcat-ap.de/def/licenses/cc-zero"),
        ("https://creativecommons.org/licenses/by/4.0/", "http://dcat-ap.de/def/licenses/cc-by/4.0"),
        # Gängige Schreibweisen derselben Adresse
        ("http://www.govdata.de/dl-de/by-2-0/", DL_BY),
        ("HTTPS://GOVDATA.DE/dl-de/by-2-0", DL_BY),
        ("  https://www.govdata.de/dl-de/by-2-0  ", DL_BY),
        ("https://creativecommons.org/licenses/by/4.0/deed.de", "http://dcat-ap.de/def/licenses/cc-by/4.0"),
        ("https://creativecommons.org/licenses/by/3.0/de/", "http://dcat-ap.de/def/licenses/cc-by-de/3.0"),
        ("https://opendatacommons.org/licenses/odbl/1-0/", "http://dcat-ap.de/def/licenses/odbl"),
        ("https://opendatacommons.org/licenses/odbl/1.0/", "http://dcat-ap.de/def/licenses/odbl"),
        # Eine Adresse der Liste selbst
        (DL_BY, DL_BY),
        ("https://dcat-ap.de/def/licenses/dl-by-de/2.0", DL_BY),
    ],
)
def test_lizenz_der_liste(angabe: str, erwartet: str | None) -> None:
    lizenz = vokabular.lizenz(angabe)
    assert (lizenz.uri if lizenz else None) == erwartet


@pytest.mark.parametrize(
    "angabe",
    [
        None,
        "",
        "   ",
        # Nicht offen: nicht kommerziell, keine Bearbeitung
        "https://creativecommons.org/licenses/by-nc/4.0/",
        "https://creativecommons.org/licenses/by-nd/4.0/",
        "https://www.govdata.de/dl-de/by-nc-1-0",
        "http://dcat-ap.de/def/licenses/other-closed",
        # Unbekannt oder Freitext
        "https://example.org/lizenz",
        "Datenlizenz Deutschland",
    ],
)
def test_ohne_offene_lizenz_keine(angabe: str | None) -> None:
    assert vokabular.lizenz(angabe) is None


def test_namensnennung_nur_bei_by_lizenzen() -> None:
    assert vokabular.lizenz("https://www.govdata.de/dl-de/by-2-0").namensnennung  # type: ignore[union-attr]
    assert not vokabular.lizenz("https://www.govdata.de/dl-de/zero-2-0").namensnennung  # type: ignore[union-attr]


def test_jede_lizenz_aus_der_liste_von_dcat_ap_de() -> None:
    assert all(lizenz.uri.startswith("http://dcat-ap.de/def/licenses/") for lizenz in vokabular.LIZENZEN)


@pytest.mark.parametrize(
    ("ags", "rgs", "uri", "ebene"),
    [
        ("05515000", None, "municipalityKey/05515000", "municipality"),
        ("05515", None, "districtKey/05515", "administrativeDistrict"),
        ("055", None, "governmentDistrictKey/055", "administrativeDistrict"),
        ("05", None, "stateKey/05", "state"),
        (None, "071435001", "municipalAssociationKey/071435001", "municipality"),
        ("", "055150000000", "regionalKey/055150000000", "municipality"),
        # Der Gemeindeschlüssel geht vor
        ("05515000", "055150000000", "municipalityKey/05515000", "municipality"),
    ],
)
def test_raumbezug(ags: str | None, rgs: str | None, uri: str, ebene: str) -> None:
    raum = vokabular.raumbezug(ags, rgs)
    assert raum == vokabular.Raumbezug(
        f"http://dcat-ap.de/def/politicalGeocoding/{uri}", f"http://dcat-ap.de/def/politicalGeocoding/Level/{ebene}"
    )


@pytest.mark.parametrize(("ags", "rgs"), [(None, None), ("", ""), ("0551500", None), ("abc", "12345"), (" ", "1")])
def test_ohne_gueltigen_schluessel_kein_raumbezug(ags: str | None, rgs: str | None) -> None:
    assert vokabular.raumbezug(ags, rgs) is None
