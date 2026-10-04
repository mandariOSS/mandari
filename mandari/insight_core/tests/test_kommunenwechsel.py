# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommunenwechsel für Tausende Kommunen (Issue #783, Stufe 2): Verzeichnis, Import, Suche, Nähe, Stöbern und die
Schnittstellen des Dialogs. Nie eine lange Liste: höchstens acht Vorschläge, Stöbern in Stufen.
"""

from __future__ import annotations

import io
import re
import statistics
import time
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from insight_core.models import Municipality, MunicipalityTerm, OParlBody, OParlSource
from insight_core.services import kommunenverzeichnis as verzeichnis
from insight_core.services.kommunenverzeichnis_import import aus_koerperschaften, importieren

KOPF = "schluessel;name;art;kreis;breite;laenge;plz;ortsteile\n"
CSV = KOPF + (
    "059990000000;Übungsheim;Kreisfreie Stadt;;51.9625;7.6256;48143|48145|48165;Heidekamp|Lindenhof|Kirchweg\n"
    "064390014014;Übungsheim;Gemeinde;Landkreis Musterkreis;49.9228;8.8656;64839;\n"
    "091890140140;Übungsheim;Gemeinde;Landkreis Talkreis;48.6833;10.9000;86692;\n"
    "059980012012;Nordheim;Stadt;Kreis Nordland;52.0919;7.6083;48268;Feldmark\n"
    "059980028028;Südheim;Stadt;Kreis Nordland;52.1475;7.3442;48565;Oberdorf|Unterdorf\n"
    "033990011011;Heidestadt;Stadt;Landkreis Heideland;52.6256;10.0825;29221|29223;\n"
    "033995401000;Samtgemeinde Moorbach;Samtgemeinde;Landkreis Heideland;52.6194;10.2453;;\n"
    "033995401014;Moorbach;Gemeinde;Landkreis Heideland;52.6194;10.2453;29331;\n"
    "033995401015;Birkholz;Gemeinde;Landkreis Heideland;52.6000;10.3167;29353;\n"
    "069990000000;Brückenstadt am Strom;Kreisfreie Stadt;;50.1109;8.6821;60311;Uferviertel\n"
    "129990000000;Grenzstadt (Oder);Kreisfreie Stadt;;52.3471;14.5506;15230;\n"
    "040990000000;Hafenstadt;Kreisfreie Stadt;;53.0700;8.8000;28195|28199;Altstadt|Speicherhof\n"
)


@pytest.fixture
def source(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")


def _body(source: OParlSource, name: str, **felder: Any) -> OParlBody:
    nummer = OParlBody.objects.count() + 1
    return OParlBody.objects.create(
        external_id=f"https://ris.example.org/body/{nummer}", source=source, name=name, **felder
    )


@pytest.fixture
def verzeichnis_mit_daten(source: OParlSource) -> dict[str, OParlBody]:
    importieren(io.StringIO(CSV))
    return {
        "uebungsheim": _body(source, "Stadt Übungsheim", short_name="Übungsheim", rgs="059990000000", ags="05999000"),
        "heidestadt": _body(source, "Stadt Heidestadt", ags="03399011"),
        # Regionalrat ohne Gemeindeschlüssel: nur über den Namen auffindbar
        "regionalrat": _body(
            source,
            "Regionalrat Beispielbezirk",
            ags="053",
            latitude=50.94,
            longitude=6.96,
            classification="Regionalrat",
        ),
        # Nicht gelistet: bleibt „noch nicht verfügbar“
        "nordheim": _body(source, "Stadt Nordheim", ags="05998012", is_listed=False),
    }


def _namen(treffer: list[verzeichnis.Treffer]) -> list[str]:
    return [f"{t.name} – {t.ort}" for t in treffer]


class TestNormalisieren:
    @pytest.mark.parametrize(
        ("eingabe", "erwartet"),
        [
            ("Übungsheim (Westf.)", "uebungsheim westf"),
            ("Groß-Musterau", "gross musterau"),
            ("  GRENZSTADT  (Oder) ", "grenzstadt oder"),
            ("Kühlbach", "kuehlbach"),
            ("Bad Übungsheim", "bad uebungsheim"),
            ("Sankt Beispiel", "sankt beispiel"),
            ("Sainte-Écluse", "sainte ecluse"),
        ],
    )
    def test_normalisieren(self, eingabe: str, erwartet: str) -> None:
        assert verzeichnis.normalisieren(eingabe) == erwartet

    def test_varianten_fuer_umlaute_ohne_punkte(self) -> None:
        assert verzeichnis.varianten("Übungsheim") == {"uebungsheim", "ubungsheim"}
        assert verzeichnis.varianten("Heidestadt") == {"heidestadt"}


@pytest.mark.django_db
class TestImport:
    def test_eintraege_und_suchbegriffe(self) -> None:
        ergebnis = importieren(io.StringIO(CSV))
        assert (ergebnis.neu, ergebnis.aktualisiert, ergebnis.uebersprungen) == (12, 0, [])
        eintrag = Municipality.objects.get(key="059990000000")
        assert (eintrag.ags, eintrag.district_key, eintrag.state_key) == ("05999000", "05999", "05")
        begriffe = set(eintrag.terms.values_list("kind", "normalized"))
        assert ("name", "uebungsheim") in begriffe and ("name", "ubungsheim") in begriffe
        assert ("ortsteil", "heidekamp") in begriffe and ("plz", "48165") in begriffe

    def test_gemeindeverband_ohne_erfundenen_gemeindeschluessel(self) -> None:
        importieren(io.StringIO(CSV))
        samtgemeinde = Municipality.objects.get(key="033995401000")
        assert samtgemeinde.is_association and samtgemeinde.ags == ""
        assert not Municipality.objects.get(key="033995401014").is_association

    def test_wiederholt_und_ersetzen(self) -> None:
        importieren(io.StringIO(CSV))
        geaendert = KOPF + "059990000000;Übungsheim (Westf.);Kreisfreie Stadt;;51.96;7.62;48143;Heidekamp\n"
        ergebnis = importieren(io.StringIO(geaendert), ersetzen=True)
        assert (ergebnis.neu, ergebnis.aktualisiert, ergebnis.entfernt) == (0, 1, 11)
        eintrag = Municipality.objects.get()
        assert eintrag.name == "Übungsheim (Westf.)"
        assert not eintrag.terms.filter(normalized="lindenhof").exists(), "Begriffe werden neu geschrieben"

    def test_ungueltige_zeilen_werden_uebersprungen(self) -> None:
        daten = (
            KOPF
            + "12345;Kurz;;;;;;\n995150000000;Kein Land;;;;;;\n059990000000;;;;;;;\n05999000;Übungsheim;;;x;y;4814;\n"
        )
        ergebnis = importieren(io.StringIO(daten))
        assert ergebnis.neu == 1 and len(ergebnis.uebersprungen) == 3
        eintrag = Municipality.objects.get(key="05999000")
        assert eintrag.latitude is None and not eintrag.terms.filter(kind="plz").exists()

    def test_fehlende_spalten(self) -> None:
        with pytest.raises(ValueError, match="schluessel"):
            importieren(io.StringIO("name;kreis\nÜbungsheim;\n"))

    def test_aus_koerperschaften(self, source: OParlSource) -> None:
        _body(source, "Stadt Beispielstadt", short_name="Beispielstadt", ags="05999000", latitude=51.5, longitude=7.5)
        _body(source, "Regionalrat Beispielbezirk", ags="053")
        ergebnis = aus_koerperschaften()
        assert ergebnis.neu == 1
        eintrag = Municipality.objects.get(key="05999000")
        assert eintrag.name == "Beispielstadt" and eintrag.latitude == 51.5
        assert aus_koerperschaften().neu == 0, "zweiter Lauf legt nichts doppelt an"

    def test_befehl(self, tmp_path: Any) -> None:
        datei = tmp_path / "kommunen.csv"
        datei.write_text(CSV, encoding="utf-8")
        ausgabe = io.StringIO()
        call_command("kommunenverzeichnis_importieren", "--datei", str(datei), stdout=ausgabe)
        assert "12 neu" in ausgabe.getvalue()


@pytest.mark.django_db
class TestSuche:
    def test_gleichnamige_orte_mit_kreis_und_land_und_verfuegbare_zuerst(
        self, verzeichnis_mit_daten: dict[str, OParlBody]
    ) -> None:
        treffer = verzeichnis.suchen("Übungsheim")
        assert _namen(treffer)[:3] == [
            "Übungsheim – Kreisfreie Stadt, Nordrhein-Westfalen",
            "Übungsheim – Landkreis Musterkreis, Hessen",
            "Übungsheim – Landkreis Talkreis, Bayern",
        ]
        assert treffer[0].url == reverse(
            "insight_core:insight:set_body", args=[verzeichnis_mit_daten["uebungsheim"].id]
        )
        assert not treffer[1].verfuegbar and not treffer[2].verfuegbar

    @pytest.mark.parametrize("eingabe", ["ubungsheim", "uebungsheim", "ÜBUNGSHEIM", "Uebungshiem", "übngsheim"])
    def test_umlaute_und_tippfehler(self, verzeichnis_mit_daten: dict[str, OParlBody], eingabe: str) -> None:
        assert verzeichnis.suchen(eingabe)[0].name == "Übungsheim"

    def test_ortsteil(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        (treffer,) = verzeichnis.suchen("Heidekamp")
        assert (treffer.name, treffer.hinweis, treffer.verfuegbar) == ("Übungsheim", "Ortsteil Heidekamp", True)

    def test_postleitzahl(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        (treffer,) = verzeichnis.suchen("48165")
        assert (treffer.name, treffer.hinweis) == ("Übungsheim", "PLZ 48165")
        assert {t.name for t in verzeichnis.suchen("48")} == {"Übungsheim", "Nordheim", "Südheim"}

    def test_mehrere_woerter_unterscheiden(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        assert _namen(verzeichnis.suchen("Übungsheim Hessen")) == ["Übungsheim – Landkreis Musterkreis, Hessen"]
        assert [t.name for t in verzeichnis.suchen("Grenzstadt Oder")][0] == "Grenzstadt (Oder)"

    def test_wortanfang_im_namen(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        assert [t.name for t in verzeichnis.suchen("strom")] == ["Brückenstadt am Strom"]

    def test_nicht_gelistet_bleibt_nicht_verfuegbar(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        (nordheim,) = verzeichnis.suchen("Nordheim")
        assert not nordheim.verfuegbar and nordheim.url == ""

    def test_koerperschaft_ohne_verzeichniseintrag(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        (treffer,) = verzeichnis.suchen("regionalrat")
        assert treffer.verfuegbar and treffer.ort == "Regionalrat, Nordrhein-Westfalen"

    def test_samtgemeinde_findbar(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        namen = [t.name for t in verzeichnis.suchen("Moorbach")]
        assert namen == ["Moorbach", "Samtgemeinde Moorbach"]

    @pytest.mark.parametrize("eingabe", ["", " ", "m", "-", "xyzzyq"])
    def test_leer_oder_ohne_treffer(self, verzeichnis_mit_daten: dict[str, OParlBody], eingabe: str) -> None:
        assert verzeichnis.suchen(eingabe) == []

    def test_hoechstens_acht(self, source: OParlSource) -> None:
        zeilen = [f"{'09' if i % 2 else '07'}{i:010d};Mustertal;Gemeinde;Kreis {i};;;;\n" for i in range(20)]
        zeilen.append("019990033033;Mustertal am See;Gemeinde;Kreis Seenland;;;23730;\n")
        importieren(io.StringIO(KOPF + "".join(zeilen)))
        assert len(verzeichnis.suchen("Mustertal")) == verzeichnis.MAX_TREFFER
        assert [t.name for t in verzeichnis.suchen("Mustertal am")] == ["Mustertal am See"]
        assert [t.name for t in verzeichnis.suchen("Mustertal Seenland")] == ["Mustertal am See"]

    def test_leeres_verzeichnis_findet_gelistete_kommunen(self, source: OParlSource) -> None:
        _body(source, "Stadt Übungsheim", short_name="Übungsheim", ags="05999000")
        (treffer,) = verzeichnis.suchen("ubungsheim")
        assert treffer.verfuegbar and treffer.name == "Übungsheim"


def _viele_kommunen(anzahl: int) -> None:
    """Testverzeichnis mit ``anzahl`` Kommunen, je drei Ortsteilen und einer Postleitzahl."""
    silben = ["berg", "dorf", "feld", "hausen", "heim", "ingen", "kirchen", "stadt", "tal", "wald", "au", "burg"]
    anfang = ["Alt", "Neu", "Ober", "Unter", "Groß", "Klein", "Bad", "Sankt", "Hohen", "Nieder", "Mühl", "Rot"]
    zeilen = []
    for i in range(anzahl):
        name = f"{anfang[i % 12]}{silben[(i // 12) % 12]}{'' if i < 144 else ' ' + str(i)}"
        land = f"{(i % 16) + 1:02d}"
        ortsteile = "|".join(f"{silben[(i + k) % 12].capitalize()}{anfang[(i + k) % 12].lower()}" for k in range(3))
        zeilen.append(
            f"{land}{i % 10}{i % 100:02d}{i:04d}{i % 1000:03d};{name};Gemeinde;Kreis {i % 40};"
            f"{47.5 + (i % 80) / 10};{6 + (i % 90) / 10};{10000 + i * 7:05d};{ortsteile}\n"
        )
    ergebnis = importieren(io.StringIO(KOPF + "".join(zeilen)))
    assert ergebnis.uebersprungen == []


@pytest.mark.django_db
class TestAntwortzeit:
    """Mit mehreren hundert Kommunen antwortet die Suche unter 150 ms (Vorgabe aus #783)."""

    GRENZE_MS = 150

    def test_suche_mit_600_kommunen(self) -> None:
        _viele_kommunen(600)
        assert Municipality.objects.count() == 600
        eingaben = ["Neuberg", "neub", "Obertal", "groß", "Grossdorf", "Bad Au", "Mühlheim", "muhlheim", "Rotwald",
                    "Sankt Ingen", "Hohenkirch", "Unterfeld 300", "Feldalt", "10007", "1015", "Klainstadt", "Nieder",
                    "Burgrot", "alth", "Talneu"]  # fmt: skip
        dauer = []
        for eingabe in eingaben:
            start = time.perf_counter()
            treffer = verzeichnis.suchen(eingabe)
            dauer.append((time.perf_counter() - start) * 1000)
            assert len(treffer) <= verzeichnis.MAX_TREFFER
        assert statistics.median(dauer) < self.GRENZE_MS, dauer
        assert max(dauer) < self.GRENZE_MS * 2, dauer

    def test_vorschlaege_ueber_die_schnittstelle(self, client: Client) -> None:
        _viele_kommunen(600)
        url = reverse("insight_core:insight:kommunen_vorschlaege")
        client.get(url, {"q": "Neu"})  # erste Anfrage lädt URL-Konfiguration und Vorlagen
        dauer = []
        for eingabe in ["Neuberg", "Grossdorf", "Rotwald", "10007", "Sankt Ingen"]:
            start = time.perf_counter()
            antwort = client.get(url, {"q": eingabe})
            dauer.append((time.perf_counter() - start) * 1000)
            assert antwort.status_code == 200
        assert statistics.median(dauer) < self.GRENZE_MS, dauer


@pytest.mark.django_db
class TestNaehe:
    def test_zelle_rundet_auf_ein_zehntel_grad(self) -> None:
        assert verzeichnis.zelle(51.9612, 7.6287) == (51.95, 7.65)
        assert verzeichnis.zelle(52.0, 7.0) == (52.05, 7.05)

    def test_kandidaten_und_verfuegbarkeit(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        ergebnis = verzeichnis.in_der_naehe(51.96, 7.63)
        namen = [k["name"] for k in ergebnis["kandidaten"]]
        assert namen[:3] == ["Übungsheim", "Nordheim", "Südheim"]
        assert ergebnis["kandidaten"][0]["verfuegbar"] and not ergebnis["kandidaten"][1]["verfuegbar"]
        assert ergebnis["naechste_mit_daten"] is None
        assert all("breite" in k and "laenge" in k for k in ergebnis["kandidaten"])

    def test_naechste_kommune_mit_daten(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        ergebnis = verzeichnis.in_der_naehe(48.68, 10.90)  # Übungsheim (Talkreis), ohne Daten
        assert not any(k["verfuegbar"] for k in ergebnis["kandidaten"])
        assert ergebnis["naechste_mit_daten"]["name"] in {"Regionalrat Beispielbezirk", "Übungsheim", "Heidestadt"}

    def test_entfernung(self) -> None:
        assert 490 < verzeichnis.entfernung_km((51.96, 7.63), (48.14, 11.58)) < 530


@pytest.mark.django_db
class TestStoebern:
    def test_laender(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        stufe = verzeichnis.stoebern()
        laender = {e["name"]: e for e in stufe["eintraege"]}
        assert stufe["stufe"] == "land" and len(laender) == 6
        assert (laender["Nordrhein-Westfalen"]["anzahl"], laender["Nordrhein-Westfalen"]["mit_daten"]) == (3, 1)
        assert laender["Hessen"]["mit_daten"] == 0
        assert {e["art"] for e in stufe["eintraege"]} == {"gruppe"}, "jede Stufe trägt die Art für den Dialog"

    def test_kreise_mit_kreisfreier_stadt_direkt(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        stufe = verzeichnis.stoebern("05")
        assert stufe["titel"] == "Nordrhein-Westfalen"
        art_und_name = [(e["art"], e["name"]) for e in stufe["eintraege"]]
        assert art_und_name == [("gruppe", "Kreis Nordland"), ("kommune", "Übungsheim")]
        assert stufe["eintraege"][1]["verfuegbar"]

    def test_kommunen_eines_kreises(self, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        stufe = verzeichnis.stoebern("03", "03399")
        assert stufe["stufe"] == "kommune" and stufe["zurueck"] == {"titel": "Niedersachsen", "land": "03"}
        assert [e["name"] for e in stufe["eintraege"]] == [
            "Samtgemeinde Moorbach",
            "Birkholz",
            "Heidestadt",
            "Moorbach",
        ]

    def test_grosser_kreis_nach_gemeindeverbaenden(self, source: OParlSource) -> None:
        zeilen = ["033995401000;Samtgemeinde Moorbach;Samtgemeinde;Landkreis Heideland;;;;\n"]
        zeilen += [f"033995401{i:03d};Ort {i};Gemeinde;Landkreis Heideland;;;;\n" for i in range(1, 30)]
        zeilen += [f"03399{i:04d}{i:03d};Stadt {i};Stadt;Landkreis Heideland;;;;\n" for i in range(1, 20)]
        importieren(io.StringIO(KOPF + "".join(zeilen)))
        stufe = verzeichnis.stoebern("03", "03399")
        assert stufe["stufe"] == "verband"
        gruppe = stufe["eintraege"][0]
        assert (gruppe["art"], gruppe["name"], gruppe["anzahl"]) == ("gruppe", "Samtgemeinde Moorbach", 29)
        assert len(stufe["eintraege"]) == 1 + 19
        unten = verzeichnis.stoebern("03", "03399", gruppe["verband"])
        assert len(unten["eintraege"]) == 30 and unten["titel"] == "Samtgemeinde Moorbach"


@pytest.mark.django_db
class TestSchnittstellen:
    def test_vorschlaege(self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        antwort = client.get(reverse("insight_core:insight:kommunen_vorschlaege"), {"q": "Heidekamp"})
        assert antwort.status_code == 200
        assert "public" in antwort["Cache-Control"]
        (treffer,) = antwort.json()["treffer"]
        assert treffer["name"] == "Übungsheim" and treffer["hinweis"] == "Ortsteil Heidekamp" and treffer["verfuegbar"]
        assert "sessionid" not in antwort.cookies, "keine Sitzung für Vorschläge"

    def test_naehe_nur_mit_zelle(self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        url = reverse("insight_core:insight:kommunen_naehe")
        assert client.get(url).status_code == 400
        assert client.get(url, {"zelle": "51.9612345,7.6"}).status_code == 400, "nur grob gerundet"
        assert client.get(url, {"zelle": "abc"}).json() == {"fehler": "Standort fehlt oder ist ungültig."}
        antwort = client.get(url, {"zelle": "51.96,7.63"})
        assert antwort.json()["zelle"] == [51.95, 7.65]
        assert client.get(url, {"zelle": "40.4,-3.7"}).json()["kandidaten"] == []
        assert "sessionid" not in antwort.cookies

    def test_stoebern_prueft_parameter(self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        url = reverse("insight_core:insight:kommunen_stoebern")
        assert client.get(url, {"land": "05"}).json()["stufe"] == "kreis"
        assert client.get(url, {"land": "x5"}).json()["stufe"] == "land"
        assert client.get(url, {"land": "05", "kreis": "03399"}).json()["stufe"] == "kreis", "Kreis passt nicht"

    def test_seite_ohne_javascript(self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        url = reverse("insight_core:insight:kommunen")
        html = client.get(url, {"q": "Übungsheim"}).content.decode()
        assert "Landkreis Musterkreis, Hessen" in html and "noch nicht verfügbar" in html
        assert reverse("insight_core:insight:set_body", args=[verzeichnis_mit_daten["uebungsheim"].id]) in html
        assert 'content="noindex, follow"' in html
        stufe = client.get(url, {"land": "03"}).content.decode()
        assert "Landkreis Heideland" in stufe and "?land=03&amp;kreis=03399" in stufe


def test_suchbegriff_kinds_vollstaendig() -> None:
    assert {k.value for k in MunicipalityTerm.Kind} == {"name", "ortsteil", "plz"}


@pytest.mark.django_db
class TestVerzeichnisImBetrieb:
    """Nach dem Deploy ohne Handgriff vollständig, Namensnennung der Quellen sobald aus einer Datei importiert."""

    def test_zeitplan_uebernimmt_gelistete_kommunen_stuendlich(self, source: OParlSource) -> None:
        from datetime import timedelta

        from apps.events.schedule import Catchup, Every, autodiscover, registry

        autodiscover()
        eintrag = registry.get("insight_core.schedules.kommunenverzeichnis_abgleichen")
        assert eintrag is not None, "Zeitplan im Worker registriert"
        assert eintrag.trigger == Every(timedelta(hours=1)) and eintrag.catchup == Catchup.NACHHOLEN
        assert eintrag.task.queue_name == "default", "der Worker der Compose-Vorlage bedient default"

        _body(source, "Stadt Beispielstadt", short_name="Beispielstadt", ags="05999000")
        _body(source, "Gemeinde Ungelistet", ags="05999001", is_listed=False)
        assert eintrag.task.call() == 1
        assert list(Municipality.objects.values_list("key", "imported")) == [("05999000", False)]
        assert eintrag.task.call() == 0, "idempotent"

    def test_abgleich_fragt_nur_die_schluessel_der_gelisteten_kommunen_ab(self, source: OParlSource) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        importieren(io.StringIO(CSV))
        _body(source, "Stadt Übungsheim", rgs="059990000000", ags="05999000")
        with CaptureQueriesContext(connection) as abfragen:
            assert aus_koerperschaften().neu == 0
        verzeichnis_abfragen = [q["sql"] for q in abfragen.captured_queries if '"insight_municipality"' in q["sql"]]
        assert len(verzeichnis_abfragen) == 2
        assert all(" IN (" in sql for sql in verzeichnis_abfragen), "nicht das ganze Verzeichnis lesen (stündlich)"
        assert len(abfragen.captured_queries) <= 3, "ohne neue Einträge keine Transaktion"

    def test_import_setzt_merkmal_auch_fuer_vorher_uebernommene(self, source: OParlSource) -> None:
        _body(source, "Stadt Übungsheim", short_name="Übungsheim", rgs="059990000000")
        aus_koerperschaften()
        assert not Municipality.objects.get(key="059990000000").imported
        importieren(io.StringIO(CSV))
        assert Municipality.objects.get(key="059990000000").imported

    def test_quellen_erst_nach_dem_import(self, source: OParlSource, django_capture_on_commit_callbacks: Any) -> None:
        _body(source, "Stadt Beispielstadt", ags="05999000")
        aus_koerperschaften()
        assert verzeichnis.quellen() == [], "eigene Daten brauchen keine Namensnennung"
        with django_capture_on_commit_callbacks(execute=True):
            importieren(io.StringIO(CSV))
        namen = [q["name"] for q in verzeichnis.quellen()]
        assert any("Statistischen Bundesamts" in name for name in namen)
        assert any("© OpenStreetMap-Mitwirkende" in name for name in namen)
        lizenzen = {q["lizenz_url"] for q in verzeichnis.quellen()}
        assert "https://www.govdata.de/dl-de/by-2-0" in lizenzen

    def test_schnittstellen_und_seiten_nennen_die_quellen(
        self, client: Client, source: OParlSource, django_capture_on_commit_callbacks: Any
    ) -> None:
        body = _body(source, "Stadt Übungsheim", short_name="Übungsheim", rgs="059990000000", ags="05999000")
        aus_koerperschaften()
        vorschlaege = reverse("insight_core:insight:kommunen_vorschlaege")
        assert "quellen" not in client.get(vorschlaege, {"q": "Übungsheim"}).json()
        seite = client.get(reverse("insight_core:insight:kommunen")).content.decode()
        assert 'data-testid="kommunen-quellen"' not in seite

        with django_capture_on_commit_callbacks(execute=True):
            importieren(io.StringIO(CSV))
        for url, parameter in (
            (vorschlaege, {"q": "Übungsheim"}),
            (reverse("insight_core:insight:kommunen_naehe"), {"zelle": "51.96,7.63"}),
            (reverse("insight_core:insight:kommunen_stoebern"), {}),
        ):
            quellen = client.get(url, parameter).json()["quellen"]
            assert [q["lizenz"] for q in quellen] == [
                "Datenlizenz Deutschland – Namensnennung – Version 2.0",
                "Open Database License (ODbL)",
            ], url
        seite = client.get(reverse("insight_core:insight:kommunen")).content.decode()
        assert 'data-testid="kommunen-quellen"' in seite
        assert 'href="https://www.openstreetmap.org/copyright"' in seite
        client.get(reverse("insight_core:insight:set_body", args=[body.id]))
        dialog = client.get(reverse("insight_core:insight:paper_list")).content.decode()
        assert 'data-testid="kommunen-quellen"' in dialog, "auch im Dialog „Kommune wechseln“"


def _auswahlseite(client: Client) -> str:
    client.get(reverse("insight_core:insight:clear_body"))
    antwort = client.get(reverse("insight_core:insight:portal_home"))
    assert antwort.status_code == 200
    return antwort.content.decode()


def _seite_mit_dialog(client: Client, body: OParlBody) -> str:
    client.get(reverse("insight_core:insight:set_body", args=[body.id]))
    antwort = client.get(reverse("insight_core:insight:paper_list"))
    assert antwort.status_code == 200
    return antwort.content.decode()


def _ohne_templates(html: str) -> str:
    """Markup, wie es vor Alpine im Dokument steht: Inhalte von ``<template>`` (auch verschachtelt) fallen weg."""
    innerstes = re.compile(r"<template\b[^>]*>(?:(?!<template\b).)*?</template>", re.S)
    while (ohne := innerstes.sub("", html)) != html:
        html = ohne
    return html


def _links_ohne_adresse(html: str) -> list[str]:
    return [tag for tag in re.findall(r"<a\b[^>]*>", _ohne_templates(html)) if not re.search(r"\shref=", tag)]


def _ebenen(html: str) -> list[int]:
    return [int(ebene) for ebene in re.findall(r"<h([1-6])\b", html)]


class TestAuswahlseiteOhneSprung:
    """Kommunenauswahl ohne Layout-Verschiebung (CLS), mit Links samt Adresse und lückenloser Überschriftenfolge."""

    def test_erste_stufe_steht_im_markup(self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        html = _auswahlseite(client)
        wahl = html[html.index('id="auswahl-eingabe"') : html.index('id="alle-kommunen-titel"')]
        assert "data-stufe-start" in html, "der Browser lädt die Länder nicht erst nach"
        assert len(re.findall(r'data-land="\d{2}"', wahl)) == len(verzeichnis.stoebern()["eintraege"]) == 6
        nrw = re.search(r'data-land="05".*?</button>', wahl, re.S)
        bremen = re.search(r'data-land="04".*?</button>', wahl, re.S)
        assert nrw and "Nordrhein-Westfalen" in nrw.group(0) and "3 Kommunen, 1 mit Daten" in nrw.group(0)
        assert bremen and "1 Kommune</span>" in bremen.group(0), "Text wie stoebernInfo() im Browser"

    def test_was_erst_mit_alpine_erscheint_verschiebt_nichts(
        self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]
    ) -> None:
        html = _auswahlseite(client)
        wahl = html[html.index('id="auswahl-eingabe"') : html.index('id="alle-kommunen-titel"')]
        stoebern = re.search(r'<section x-show="stoebernSichtbar\(\)"[^>]*>', wahl)
        naehe = re.search(r'<button type="button" @click="inDerNaehe\(\)"[^>]*>', wahl)
        assert stoebern and 'x-cloak="platz"' in stoebern.group(0), "unsichtbar, aber mit Platz bis Alpine läuft"
        assert naehe and 'x-cloak="platz"' in naehe.group(0)
        assert 'id="auswahl-ergebnisse" class="flow-root"' in wahl, "Abstände springen nicht aus dem Behälter"
        assert '[x-cloak="platz"] { visibility: hidden !important; }' in html
        assert '[x-cloak="platz"] { display: none !important; }</style></noscript>' in html, (
            "ohne JavaScript keine Lücke"
        )

    def test_ohne_verzeichnis_laedt_der_browser_die_erste_stufe(self, client: Client, source: OParlSource) -> None:
        _body(source, "Stadt Übungsheim")
        _body(source, "Stadt Heidestadt")
        html = _auswahlseite(client)
        assert "data-stufe-start" not in html and "data-land=" not in html
        assert re.search(r'<section x-show="stoebernSichtbar\(\)"[^>]*x-cloak>', html)

    def test_links_im_markup_haben_eine_adresse(
        self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]
    ) -> None:
        """Lighthouse „crawlable-anchors“: ``<a :href>`` ohne ``href`` nur in ``<template>``, wo Alpine sie einfügt."""
        assert _links_ohne_adresse(_auswahlseite(client)) == []
        assert _links_ohne_adresse(_seite_mit_dialog(client, verzeichnis_mit_daten["uebungsheim"])) == []

    def test_ueberschriften_ohne_sprung(self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]) -> None:
        """Lighthouse „heading-order“: auf der Seite h1 → h2, im Dialog unter dessen h2 dann h3."""
        html = _auswahlseite(client)
        hauptteil = _ebenen(html[html.index('<main id="main-content"') : html.index("</main>")])
        assert hauptteil[0] == 1 and all(b - a <= 1 for a, b in zip(hauptteil, hauptteil[1:], strict=False)), hauptteil
        assert re.search(r'<h2 id="auswahl-stufe"', html)
        seite = _seite_mit_dialog(client, verzeichnis_mit_daten["uebungsheim"])
        dialog = seite[seite.index('aria-labelledby="kommune-dialog-titel"') : seite.index('id="kommune-dialog-stufe"')]
        assert _ebenen(dialog) == [2, 3, 3], "Dialogtitel h2, darunter „Zuletzt besucht“ und Stufe als h3"

    def test_dialog_baut_den_wechsel_erst_beim_oeffnen_auf(
        self, client: Client, verzeichnis_mit_daten: dict[str, OParlBody]
    ) -> None:
        """Weniger Arbeit beim Start jeder Seite (TBT): Der Inhalt des Dialogs steht in ``<template x-if>``."""
        html = _seite_mit_dialog(client, verzeichnis_mit_daten["uebungsheim"])
        start = html.index('<template x-if="kommunenWahlBereit">')
        eingabe = html.index('id="kommune-dialog-eingabe"')
        assert start < html.index('x-data="kommunenWahl"', start) < eingabe
        assert "</template>" not in html[start:eingabe]
        assert 'id="kommune-dialog-eingabe"' not in _ohne_templates(html)


def test_insight_vorlagen_ohne_links_ohne_adresse() -> None:
    """Jedes ``<a :href>`` der Bürgerportal-Vorlagen hat einen ``href``-Rückfall oder steht in einem ``<template>``."""
    vorlagen = Path(settings.BASE_DIR) / "templates"
    dateien = [vorlagen / "base_insight.html"] + [
        datei
        for ordner in ("components", "pages", "partials", "cotton/insight")
        for datei in (vorlagen / ordner).rglob("*.html")
    ]
    fehlend = [
        f"{datei.relative_to(vorlagen)}: {tag[:80]}"
        for datei in dateien
        for tag in re.findall(r"<a\b[^>]*:href=[^>]*>", _ohne_templates(datei.read_text(encoding="utf-8")))
        if not re.search(r"\shref=", tag)
    ]
    assert fehlend == []


def test_caddyfile_erlaubt_den_standort_fuer_die_eigene_seite() -> None:
    """„In meiner Nähe“ scheitert sonst sofort: Permissions-Policy ``geolocation=()`` sperrt die Abfrage im Browser."""
    from pathlib import Path

    from django.conf import settings

    caddyfile = (Path(settings.BASE_DIR).parent / "Caddyfile").read_text(encoding="utf-8")
    (zeile,) = [z.strip() for z in caddyfile.splitlines() if z.strip().startswith("Permissions-Policy")]
    regeln = {regel.strip() for regel in zeile.split('"')[1].split(",")}
    assert "geolocation=(self)" in regeln
    assert {"camera=()", "microphone=()"} <= regeln, "Kamera und Mikrofon bleiben gesperrt"
