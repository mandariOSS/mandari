# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verortung ohne Briefkopf und ohne Stadtteilnamen.

Ein Antrag einer Fraktion nannte als Orte die Geschäftsstelle der Partei, die Wohnanschrift des
Fraktionsvorsitzenden und die Bezirksverwaltung (alle mit Postleitzahl im Briefkopf bzw. Empfängerfeld) sowie die
Straße „Kinderhaus“, weil der Antrag das „Schulzentrum Kinderhaus“ als Vergleich nennt. Gemeint war nur die Schule
im Titel. Anschriften mit Postleitzahl zählen deshalb nicht, und ein Straßenname ohne Straßen-Grundwort zählt nicht,
wenn er nur als Teil eines Einrichtungs- oder Stadtteilnamens vorkommt.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from insight_core.models import OParlBody, OParlFile, OParlPaper, Street
from insight_core.services.gazetteer import StreetGazetteer
from insight_core.services.georeferencing import extract_with_gazetteer, ohne_anschriften, process_paper_georef

pytestmark = pytest.mark.django_db

#: Aufbau wie im gemeldeten Antrag (Namen und Anschriften erfunden): zweispaltiger Briefkopf, Empfängerfeld,
#: Vergleich mit anderen Schulen; der Briefkopf wiederholt sich auf Seite 2
ANTRAG = """Fraktion in der Bezirksvertretung Beispielstadt-Süd
__________________________________________________________________________________
Kreisverband Beispielstadt e.V. Erika Musterfrau
Hafenweg 10, 48153 Beispielstadt Fraktionsvorsitzende BV Süd
Tel. 0251 / 418 42-0, Fax 0251 / 418 42-44 Am Lindenhof 124, 48167 Beispielstadt
post@example.org Tel. 0251 / 614 201
Stadt Beispielstadt
Bezirksverwaltung Süd
Wolbecker Straße 7
48167 Beispielstadt
Beispielstadt, den 16.11.2026
Installation einer Videoüberwachungsanlage an der Lindenschule
Die Bezirksvertretung möge beschließen: Die Verwaltung prüft eine Videoüberwachung an der Lindenschule.
__________________________________________________________________________________
Kreisverband Beispielstadt e.V. Erika Musterfrau
Hafenweg 10, 48153 Beispielstadt Fraktionsvorsitzende BV Süd
Begründung: An anderen Schulen hat sich das bewährt.
# Schulzentrum Kinderhaus: Die Zahl der Straftaten sank von 15 auf zwei.
Zudem wurde eine Ausweitung auf die Grundschule Kinderhaus-West vorgesehen.
"""


@pytest.fixture
def kommune(geo_body: OParlBody, make_street: Callable[..., Street]) -> OParlBody:
    make_street(geo_body, "Hafenweg", 51.9540, 7.6260)
    make_street(geo_body, "Am Lindenhof", 51.9166, 7.6946)
    make_street(geo_body, "Wolbecker Straße", 51.9174, 7.7281)
    make_street(geo_body, "Kinderhaus", 51.9970, 7.6150)
    make_street(geo_body, "Rothenburg", 51.9600, 7.6270)
    return geo_body


def _datei(paper: OParlPaper, text: str) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"{paper.external_id}/file/1",
        body=paper.body,
        paper=paper,
        name="Antrag",
        text_content=text,
        text_extraction_status="completed",
    )


def _namen(text: str, kommune: OParlBody, titel: str = "") -> list[str]:
    return [ort["name"] for ort in extract_with_gazetteer(text, StreetGazetteer(kommune), set(), titel=titel)]


def test_antrag_mit_briefkopf_hat_keine_orte(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(
        kommune, name="Installation einer Videoüberwachungsanlage an der Lindenschule", paper_type="Antrag"
    )
    _datei(paper, ANTRAG)
    ergebnis = process_paper_georef(paper, mode="regex")
    assert ergebnis["locations"] == []
    assert ergebnis["status"] == "ai_needed"


class TestOhneAnschriften:
    @pytest.mark.parametrize(
        ("text", "bleibt", "faellt_weg"),
        [
            ("Hafenweg 10, 48153 Beispielstadt Fraktionsvorsitzende", "Fraktionsvorsitzende", "Hafenweg"),
            ("Fax 0251 / 418 42-44 Am Lindenhof 124, 48167 Beispielstadt", "", "Lindenhof"),
            (
                "Bezirksverwaltung Süd\nWolbecker Straße 7 \n48167 Beispielstadt\nDatum",
                "Bezirksverwaltung",
                "Wolbecker",
            ),
            ("Bezirksverwaltung, Wolbecker Str. 7a, D-48167 Beispielstadt", "Bezirksverwaltung", "Wolbecker"),
            ("Rathaus\r\nWolbecker Straße 7\r\n48167 Beispielstadt", "Rathaus", "Wolbecker"),
            ("Rathaus\rWolbecker Straße 7\r48167 Beispielstadt\rDatum", "Rathaus\n", "Wolbecker"),
            ("Fraktionsbüro · Hafenweg 10 · 48153 Beispielstadt", "Fraktionsbüro", "Hafenweg"),
            ("Erika Musterfrau Am Lindenhof 124, 48167 Beispielstadt", "", "Lindenhof"),
            ("Kreisverband  Hafenweg 10  48153 Beispielstadt", "Kreisverband", "Hafenweg"),
        ],
    )
    def test_anschrift_faellt_weg(self, text: str, bleibt: str, faellt_weg: str) -> None:
        ergebnis = ohne_anschriften(text)
        assert faellt_weg not in ergebnis
        assert bleibt in ergebnis
        assert "4816" not in ergebnis and "4815" not in ergebnis

    @pytest.mark.parametrize(
        "text",
        [
            "Fraktionsbüro Hafenweg 10 · Tel. 0251 41842-0",
            "Hafenweg 10, Telefax 0251 41842-44",
            "Hafenweg 10      Fon 0251 8995820",
            "Hafenweg 10 info@example.org",
            "Hafenweg 10, www.example.org",
            "Postfach 12 34, Hafenweg 10",
        ],
    )
    def test_kontaktzeile_faellt_ganz_weg(self, text: str, kommune: OParlBody) -> None:
        assert _namen(ohne_anschriften(f"Der Antrag:\n{text}\nEnde"), kommune) == []

    @pytest.mark.parametrize(
        "text",
        [
            "Der Hafenweg wird saniert.",
            "Die Straftaten sanken von 26 im Jahr 2017 auf drei.",
            "Im Jahr 2025 wurden 15000 Euro bereitgestellt.",
            "Die Ampel an der Wolbecker Straße 7 wird erneuert.",
            "Die Mobilität am Hafenweg 3 verbessern; Telematik prüfen.",
            # Tabelle mit laufender Nummer und fünfstelliger Spielplatznummer: keine Postleitzahl
            "Rang Nr. Spielplatz\n1 25405 Hafenweg 2. BA\n2 25117 Rothenburg Nord",
            "25117\nSP ROTHENBURG-NORD",
            "Mitte 1 25405 Hafenweg 2. BA",
            "Spielplatz 3 25117 Rothenburg Nord",
            "Wolbecker Straße 2019 14500 Kfz/24h",
            # Wörter wie „mobil“ oder „E-Mail“ im Fließtext machen keine Kontaktzeile
            "Für mobil eingeschränkte Menschen wird die Haltestelle Hafenweg umgebaut.",
            "Zebrastreifen am Hafenweg. Rückfragen per E-Mail an die Fraktion.",
            # Ein Absatz in einer einzigen Zeile ist keine Kontaktzeile, auch mit Adresse darin
            "Der Hafenweg wird saniert. " * 10 + "Kontakt: info@example.org, Tel. 0251 123",
        ],
    )
    def test_ohne_postleitzahl_bleibt_alles(self, text: str) -> None:
        assert ohne_anschriften(text) == text

    def test_satz_vor_der_anschrift_bleibt(self, kommune: OParlBody) -> None:
        text = ohne_anschriften(
            "Die Stadt mietet für die OGS am Hafenweg Räume in der Wolbecker Straße 7, 48167 Beispielstadt an."
        )
        assert "Wolbecker" not in text
        assert _namen(text, kommune) == ["Hafenweg"]

    def test_dieselbe_strasse_ohne_postleitzahl_bleibt_ein_ort(self, kommune: OParlBody) -> None:
        text = ohne_anschriften("Stadt Beispielstadt\nHafenweg 10\n48153 Beispielstadt\n\nDer Hafenweg wird saniert.")
        assert _namen(text, kommune) == ["Hafenweg"]

    def test_titel_behaelt_die_anschrift(self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        paper = make_paper(kommune, name="Neubau Kita Hafenweg 10, 48153 Beispielstadt", paper_type="Vorlage")
        _datei(paper, "Die Kita wird größer.")
        orte = process_paper_georef(paper, mode="regex")["locations"]
        assert [ort["name"] for ort in orte] == ["Hafenweg 10"]

    def test_lange_zeile_ohne_umbruch_bleibt_schnell(self) -> None:
        text = ("Wort " * 20_000) + "Hafenweg 10, 48153 Beispielstadt " + ("1  48153 Beispielstadt " * 5_000)
        beginn = time.perf_counter()
        ergebnis = ohne_anschriften(text)
        # Großzügige Grenze für langsame CI-Rechner; quadratischer Aufwand bräuchte hier Minuten
        assert time.perf_counter() - beginn < 5
        assert "Hafenweg" not in ergebnis and ergebnis.startswith("Wort Wort")


class TestEinrichtungsname:
    @pytest.mark.parametrize(
        "text",
        [
            "Im Schulzentrum Kinderhaus sank die Zahl der Straftaten.",
            "Ausweitung auf die Grundschule Kinderhaus-West",
            "Neue Wohnungen in Kinderhaus",
            "Der Stadtteil Kinderhaus wächst.",
        ],
    )
    def test_nur_als_einrichtung_kein_ort(self, text: str, kommune: OParlBody) -> None:
        assert _namen(text, kommune) == []

    @pytest.mark.parametrize(
        ("text", "ort"),
        [
            ("Das Museum liegt an der Straße Kinderhaus.", "Kinderhaus"),
            ("Grundschule Kinderhaus, Zufahrt über die Straße Kinderhaus", "Kinderhaus"),
            ("Das Museum, Kinderhaus 15, öffnet wieder.", "Kinderhaus 15"),
            ("Die Rothenburg wird Fußgängerzone.", "Rothenburg"),
            ("Grundschule Wolbecker Straße", "Wolbecker Straße"),
            # Spielplätze heißen nach ihrer Straße, auch mit Himmelsrichtung
            ("Sanierung SP Rothenburg-Nord", "Rothenburg"),
            ("Spielplatz Rothenburg Mitte", "Rothenburg"),
        ],
    )
    def test_strasse_bleibt_ein_ort(self, text: str, ort: str, kommune: OParlBody) -> None:
        assert _namen(text, kommune) == [ort]
