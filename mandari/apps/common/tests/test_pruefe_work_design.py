# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Regeln des Prüfskripts für das neue Work-Design (``scripts/pruefe_work_design.py``, Issue #884) ohne Browser.

Der Lauf im Browser steht in ``tests_e2e/test_work_design_pruefung.py``. Hier: welche Links als Seite gelten,
wie Messungen bewertet werden, woher die Zugänge kommen und dass das Skript gegen entfernte Instanzen nie von
sich aus umschaltet.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from apps.common.demo_daten import DEMO_ORG_SLUG, DEMO_USERS

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "pruefe_work_design.py"
BASIS = "https://demo.example"
ORG = "musterfraktion-demo"
KENNUNG = "0f8b6a0e-5d2c-4c4e-9a37-3f1b2c4d5e6f"


def _skript() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pruefe_work_design", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules["pruefe_work_design"] = modul
    spec.loader.exec_module(modul)
    return modul


skript = _skript()


def _messung(**werte: Any) -> dict[str, Any]:
    return {"breite": 1440, "scroll": 1440, "rechts": 1400, "rahmen": "neu", "titel": "Work", "text": "", **werte}


class TestSeiten:
    @pytest.mark.parametrize(
        ("href", "erwartet"),
        [
            (f"/work/{ORG}/faction/", f"/work/{ORG}/faction/"),
            (f"/work/{ORG}/faction", f"/work/{ORG}/faction/"),
            (f"{BASIS}/work/{ORG}/ris/papers/?page=2#liste", f"/work/{ORG}/ris/papers/"),
            ("../tasks/", f"/work/{ORG}/tasks/"),
            (f"/work/{ORG}/faction/{KENNUNG}/", f"/work/{ORG}/faction/{KENNUNG}/"),
            (f"/work/{ORG}/organization/api/", f"/work/{ORG}/organization/api/"),
            (f"/work/{ORG}/profile/data/", f"/work/{ORG}/profile/data/"),
        ],
    )
    def test_seiten_der_organisation(self, href: str, erwartet: str) -> None:
        assert skript.seitenpfad(BASIS, ORG, href, f"{BASIS}/work/{ORG}/meetings/") == erwartet

    @pytest.mark.parametrize(
        "href",
        [
            "https://anderswo.example/work/musterfraktion-demo/",
            "/work/andere-fraktion/",
            "/insight/",
            "/accounts/logout/",
            "mailto:info@example.org",
            "javascript:void(0)",
            "#inhalt",
            f"/work/{ORG}/documents/{KENNUNG}/export/",
            f"/work/{ORG}/documents/{KENNUNG}/files/{KENNUNG}/download/",
            f"/work/{ORG}/documents/{KENNUNG}/permanent-delete/",
            f"/work/{ORG}/notifications/mark-read/",
            f"/work/{ORG}/notifications/{KENNUNG}/mark-read/",
            f"/work/{ORG}/faction/{KENNUNG}/niederschrift/lang.pdf",
            f"/work/{ORG}/faction/nachweise/export/",
            f"/work/{ORG}/meetings/calendar/events/",
            f"/work/{ORG}/ris/map/data/",
            f"/work/{ORG}/tasks/api/",
            f"/work/{ORG}/faction/{KENNUNG}/item/{KENNUNG}/panel/",
            f"/work/{ORG}/meetings/ladungen/{KENNUNG}/tagesordnung.pdf",
            f"/work/{ORG}/organization/roles/restore-defaults/",
        ],
    )
    def test_keine_seiten(self, href: str) -> None:
        assert skript.seitenpfad(BASIS, ORG, href, f"{BASIS}/work/{ORG}/") is None

    def test_muster_ersetzt_kennungen(self) -> None:
        assert skript.muster(f"/work/{ORG}/faction/{KENNUNG}/") == f"/work/{ORG}/faction/{{id}}/"


class TestErlaubnisliste:
    """Gegen entfernte Instanzen ruft das Skript nur Seiten der Erlaubnisliste auf (die Sperrliste kennt nur Bekanntes)."""

    @pytest.mark.parametrize(
        "pfad",
        [
            f"/work/{ORG}/",
            f"/work/{ORG}",
            f"/work/{ORG}/faction/",
            f"/work/{ORG}/faction/{KENNUNG}/",
            f"/work/{ORG}/meetings/{KENNUNG}/prepare/",
            f"/work/{ORG}/ris/papers/{KENNUNG}/",
            f"/work/{ORG}/organization/faction-settings/",
            f"/work/{ORG}/support/kb/erste-schritte/anmelden/",
        ],
    )
    def test_seiten_der_liste(self, pfad: str) -> None:
        assert skript.erlaubt_entfernt(pfad, ORG) is True

    @pytest.mark.parametrize(
        "pfad",
        [
            # Eine künftige Adresse, deren Aufruf etwas ändert, und die die Sperrliste nicht kennt
            f"/work/{ORG}/notifications/alle-gelesen/",
            f"/work/{ORG}/faction/{KENNUNG}/abschliessen/",
            # Schnittstellen, denen das Skript lokal folgen würde
            f"/work/{ORG}/meetings/{KENNUNG}/tasks/{KENNUNG}/",
            f"/work/{ORG}/paper/{KENNUNG}/comments/",
            f"/work/{ORG}/tasks/labels/{KENNUNG}/",
            # Kennung, wo eine erwartet wird, fehlt; andere Organisation
            f"/work/{ORG}/faction/x/",
            "/work/andere-fraktion/faction/",
        ],
    )
    def test_nicht_auf_der_liste(self, pfad: str) -> None:
        assert skript.erlaubt_entfernt(pfad, ORG) is False

    def test_lokal_gilt_nur_die_sperrliste(self) -> None:
        """Lokal folgt das Skript auch unbekannten Seiten (damit neue Seiten geprüft werden), entfernt nicht."""
        neu = f"/work/{ORG}/notifications/alle-gelesen/"
        assert skript.seitenpfad(BASIS, ORG, neu, f"{BASIS}/work/{ORG}/") == neu
        assert skript.erlaubt_entfernt(neu, ORG) is False

    @pytest.mark.parametrize("eintrag", skript.ERLAUBT_ENTFERNT)
    def test_jeder_eintrag_ist_eine_seite_von_work(self, eintrag: str) -> None:
        """Kein Eintrag zeigt auf eine Aktion, Schnittstelle oder eine Adresse, die es nicht gibt."""
        from django.urls import resolve

        pfad = f"/work/{ORG}/" + eintrag.replace("{id}", KENNUNG).replace("{name}", "beispiel")
        treffer = resolve(pfad)
        assert treffer.app_name == "work", pfad
        assert not (treffer.url_name or "").endswith(("_api", "_delete", "_partial", "_download", "_export")), pfad
        assert skript.seitenpfad(BASIS, ORG, pfad, f"{BASIS}/work/{ORG}/") == pfad, "steht auf der Sperrliste"
        klasse = getattr(treffer.func, "view_class", None)
        if klasse is not None:
            methoden = getattr(treffer.func, "view_initkwargs", {}).get("http_method_names", klasse.http_method_names)
            assert "get" in methoden and hasattr(klasse, "get"), f"{pfad}: keine Seite (kein GET)"

    def test_eintraege_ohne_doppelte(self) -> None:
        assert len(set(skript.ERLAUBT_ENTFERNT)) == len(skript.ERLAUBT_ENTFERNT)


class TestBewertung:
    def bewerten(self, messung: dict[str, Any], schalter: str = "an", breite: int = 1440) -> list[tuple[str, str]]:
        befunde = skript.bewerten(messung, rolle="vorsitz", schalter=schalter, pfad="/work/x/", breite=breite)
        return [(b.art, b.schwere) for b in befunde]

    def test_ohne_befund(self) -> None:
        assert self.bewerten(_messung()) == []

    def test_ueberlauf(self) -> None:
        assert self.bewerten(_messung(breite=390, scroll=520, rechts=380), breite=390) == [("Überlauf", "Fehler")]

    def test_leerflaeche_fehler_nur_mit_neuem_design(self) -> None:
        leer = _messung(breite=1920, scroll=1920, rechts=1200)
        assert self.bewerten(leer, "an", 1920) == [("Leerfläche", "Fehler")]
        leer_alt = _messung(breite=1920, scroll=1920, rechts=1200, rahmen=None)
        assert self.bewerten(leer_alt, "aus", 1920) == [("Leerfläche", "Hinweis")]
        assert self.bewerten(leer_alt, "aktuell", 1920) == [("Leerfläche", "Hinweis")]

    def test_leerflaeche_ohne_umschalten_nach_dem_rahmen_der_seite(self) -> None:
        """Gegen die Demo läuft das Skript ohne Umschalten: Dort entscheidet der Rahmen der Seite."""
        leer = _messung(breite=1920, scroll=1920, rechts=1200, rahmen="neu")
        assert self.bewerten(leer, "aktuell", 1920) == [("Leerfläche", "Fehler")]

    def test_leerflaeche_ab_1280(self) -> None:
        """Für Work gilt die Regel von 1.280 bis 2.560 px, darunter (Tablet, Handy) nicht."""
        assert self.bewerten(_messung(breite=1024, scroll=1024, rechts=500), "an", 1024) == []
        assert self.bewerten(_messung(breite=1280, scroll=1280, rechts=900), "an", 1280) == [("Leerfläche", "Fehler")]
        # genau ein Viertel frei ist erlaubt
        assert self.bewerten(_messung(breite=1280, scroll=1280, rechts=960), "an", 1280) == []
        assert self.bewerten(_messung(breite=2560, scroll=2560, rechts=1920), "an", 2560) == []

    def test_fehlerseite(self) -> None:
        assert ("Serverfehler", "Fehler") in self.bewerten(_messung(titel="Server Error (500)"))

    def test_rahmen_passt_nicht_zum_schalter(self) -> None:
        assert self.bewerten(_messung(rahmen="neu"), "aus") == [("Design passt nicht zum Schalter", "Fehler")]
        assert self.bewerten(_messung(rahmen="alt"), "an") == [("Design passt nicht zum Schalter", "Fehler")]
        assert self.bewerten(_messung(rahmen="neu"), "an") == []
        assert self.bewerten(_messung(rahmen=None), "aus") == []
        assert self.bewerten(_messung(rahmen="neu"), "aktuell") == []
        assert self.bewerten(_messung(rahmen=None), "aktuell") == []

    def test_seite_ohne_rahmen_bei_eingeschaltetem_schalter_ist_hinweis(self) -> None:
        assert self.bewerten(_messung(rahmen=None), "an") == [("Ohne neuen Rahmen", "Hinweis")]

    def test_zusammenfassung_zaehlt_nur_fehler(self) -> None:
        lauf = skript.Lauf("vorsitz", "aus", erreichbar=["/work/x/"])
        lauf.befunde = [
            skript.Befund("vorsitz", "aus", "/work/x/", "Leerfläche", "rechts 40% frei", 1920, "Hinweis"),
            skript.Befund("vorsitz", "aus", "/work/x/", "Konsole", "Alpine Expression Error", 1280),
        ]
        text, fehler = skript.zusammenfassen([lauf])
        assert fehler == 1
        assert "[Fehler] Konsole" in text and "[Hinweis] Leerfläche" in text


class TestGestaltung:
    """Seiten ohne neue Gestaltung zählen (Issue #884, Ziel aus #951: 0)."""

    @pytest.mark.parametrize(
        ("rahmen", "gestaltung", "erwartet"),
        [
            ("neu", "neu", "neu"),
            ("neu", "bisher", "bisher"),
            ("neu", None, "bisher"),
            (None, None, None),
            (None, "neu", None),
        ],
    )
    def test_einordnung(self, rahmen: str | None, gestaltung: str | None, erwartet: str | None) -> None:
        assert skript.gestaltung(_messung(rahmen=rahmen, gestaltung=gestaltung)) == erwartet

    def _laeufe(self) -> list[Any]:
        vorsitz = skript.Lauf("vorsitz", "an", erreichbar=["/work/x/", "/work/x/team/", f"/work/x/faction/{KENNUNG}/"])
        vorsitz.neu_gestaltet = ["/work/x/"]
        vorsitz.ohne_neue_gestaltung = ["/work/x/team/", f"/work/x/faction/{KENNUNG}/"]
        mitglied = skript.Lauf("mitglied", "an", erreichbar=["/work/x/", "/work/x/team/"])
        mitglied.neu_gestaltet = ["/work/x/"]
        mitglied.ohne_neue_gestaltung = ["/work/x/team/", "/work/x/faction/0f8b6a0e-0000-4c4e-9a37-3f1b2c4d5e6f/"]
        aus = skript.Lauf("vorsitz", "aus", erreichbar=["/work/x/"])
        return [vorsitz, mitglied, aus]

    def test_zaehlt_adressmuster_ueber_alle_rollen(self) -> None:
        assert skript.ohne_neue_gestaltung(self._laeufe()) == ["/work/x/faction/{id}/", "/work/x/team/"]

    def test_zusammenfassung_nennt_die_zahl_als_hinweis(self) -> None:
        text, fehler = skript.zusammenfassen(self._laeufe())
        assert fehler == 0
        assert "Seiten ohne neue Gestaltung (Adressmuster, alle Rollen): 2 – neu gestaltet: 1" in text
        assert "[Hinweis] /work/x/team/" in text
        assert "2 von 3 im neuen Rahmen ohne neue Gestaltung" in text

    def test_mit_pflicht_zaehlt_jede_seite_als_fehler(self) -> None:
        text, fehler = skript.zusammenfassen(self._laeufe(), gestaltung_pflicht=True)
        assert fehler == 2
        assert "[Fehler] /work/x/faction/{id}/" in text

    def test_ohne_neuen_rahmen_keine_zaehlung(self) -> None:
        text, fehler = skript.zusammenfassen(
            [skript.Lauf("vorsitz", "aus", erreichbar=["/work/x/"])], gestaltung_pflicht=True
        )
        assert fehler == 0 and "ohne neue Gestaltung" not in text


class TestZugaenge:
    def test_gemeinsames_passwort_und_ueberschreiben_je_rolle(self) -> None:
        umgebung = {
            "MANDARI_PRUEF_PASSWORT": "gemeinsam",
            "MANDARI_PRUEF_GAST_EMAIL": "gast@staging.example",
            "MANDARI_PRUEF_GAST_PASSWORT": "eigenes",
        }
        gefunden, fehlend = skript.zugaenge(["vorsitz", "gast"], umgebung)
        assert fehlend == []
        assert [(z.rolle, z.email, z.passwort) for z in gefunden] == [
            ("vorsitz", DEMO_USERS["vorsitz"]["email"], "gemeinsam"),
            ("gast", "gast@staging.example", "eigenes"),
        ]

    def test_ohne_passwort_wird_uebersprungen(self) -> None:
        gefunden, fehlend = skript.zugaenge(["vorsitz", "mitglied"], {"MANDARI_PRUEF_MITGLIED_PASSWORT": "x"})
        assert [z.rolle for z in gefunden] == ["mitglied"]
        assert fehlend == ["vorsitz"]

    def test_konten_wie_in_der_demo(self) -> None:
        """Das Skript läuft ohne Django; seine Demo-Konten müssen zu setup_demo_environment passen."""
        assert {rolle: DEMO_USERS[rolle]["email"] for rolle in skript.ROLLEN} == skript.DEMO_EMAILS
        assert skript.DEMO_ORG == DEMO_ORG_SLUG


class TestSchalter:
    def test_entfernte_instanz_wird_nicht_ohne_befehl_umgeschaltet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MANDARI_PRUEF_PASSWORT", "x")
        monkeypatch.setattr(skript, "pruefen", lambda *a, **k: pytest.fail("darf nicht starten"))
        with pytest.raises(SystemExit) as abbruch:
            skript.main(["--basis", "https://demo.mandari.de", "--schalter", "beide"])
        assert abbruch.value.code == 2

    def test_lokal_und_mit_befehl_erlaubt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MANDARI_PRUEF_PASSWORT", "x")
        aufrufe: list[dict[str, Any]] = []

        def pruefen(*_: Any, **optionen: Any) -> list[Any]:
            aufrufe.append(optionen)
            return []

        monkeypatch.setattr(skript, "pruefen", pruefen)
        assert skript.main(["--basis", "http://localhost:8000", "--schalter", "beide", "--rollen", "vorsitz"]) == 0
        assert (
            skript.main(["--basis", "https://staging.example", "--schalter", "an", "--schalt-befehl", "x {aktion}"])
            == 0
        )
        assert [k["schalter"] for k in aufrufe] == ["beide", "an"]
        assert all(callable(k["schalten"]) for k in aufrufe)

    def test_stellt_den_schalter_danach_wieder_her(self) -> None:
        befehle: list[str] = []

        def schalten(aktion: str, org: str) -> str:
            befehle.append(aktion)
            return json.dumps({org: True}) if aktion == "status" else ""

        skript.pruefen(BASIS, ORG, [], schalter="beide", schalten=schalten, browser=object(), protokoll=lambda _: None)
        assert befehle == ["status", "an", "aus", "an"]

    def test_stellt_auch_nach_einem_abbruch_wieder_her(self) -> None:
        befehle: list[str] = []

        def schalten(aktion: str, org: str) -> str:
            befehle.append(aktion)
            if aktion == "aus":
                raise RuntimeError("Abbruch")
            return json.dumps({org: False}) if aktion == "status" else ""

        with pytest.raises(RuntimeError):
            skript.pruefen(BASIS, ORG, [], schalter="beide", schalten=schalten, browser=object(), protokoll=print)
        assert befehle == ["status", "an", "aus", "aus"]


class _Antwort:
    def __init__(self, status: int) -> None:
        self.status = status


class _Feld:
    def __init__(self, anzahl: int) -> None:
        self.anzahl = anzahl

    def count(self) -> int:
        return self.anzahl


class _Seite:
    """Ersatz für eine Playwright-Seite: liefert eine feste Anmeldeseite und merkt sich Eingaben."""

    def __init__(self, status: int, felder: int, ziel: str) -> None:
        self.status, self.felder, self.ziel = status, felder, ziel
        self.url = ""
        self.eingaben: list[str] = []

    def goto(self, url: str, **_: Any) -> _Antwort:
        self.url = url
        return _Antwort(self.status)

    def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    def locator(self, _: str) -> _Feld:
        return _Feld(self.felder)

    def fill(self, auswahl: str, _: str) -> None:
        if not self.felder:
            raise TimeoutError(f"{auswahl} fehlt")
        self.eingaben.append(auswahl)

    def click(self, _: str) -> None:
        self.url = self.ziel


class TestAnmeldung:
    ZUGANG = skript.Zugang("vorsitz", DEMO_USERS["vorsitz"]["email"], "x")

    @pytest.mark.parametrize(("status", "felder"), [(400, 0), (200, 0), (503, 1)])
    def test_unbrauchbare_anmeldeseite_ist_ein_befund_statt_eines_abbruchs(self, status: int, felder: int) -> None:
        """Etwa ein nicht zugelassener Host (HTTP 400) oder eine Sperre davor: Befund, der Lauf geht weiter."""
        pruefer = skript.Pruefer(object(), BASIS, ORG, protokoll=lambda _: None)
        seite = _Seite(status, felder, f"{BASIS}/work/{ORG}/")
        lauf = skript.Lauf("vorsitz", "an")

        assert pruefer._anmelden(seite, self.ZUGANG, lauf) is False
        assert seite.eingaben == []
        assert [(b.art, b.pfad, b.schwere) for b in lauf.befunde] == [("Anmeldung", "/accounts/login/", "Fehler")]
        assert f"HTTP {status}" in lauf.befunde[0].detail

    def test_anmeldung_mit_formular(self) -> None:
        pruefer = skript.Pruefer(object(), BASIS, ORG, protokoll=lambda _: None)
        seite = _Seite(200, 1, f"{BASIS}/work/{ORG}/")
        lauf = skript.Lauf("vorsitz", "an")

        assert pruefer._anmelden(seite, self.ZUGANG, lauf) is True
        assert seite.eingaben == ["input[name=email]", "input[name=password]"]
        assert lauf.befunde == []


class _Netz:
    """Ersatz für eine Playwright-Seite beim Erkunden: feste Links je Seite, merkt sich jeden Aufruf."""

    def __init__(self, links: dict[str, list[str]]) -> None:
        self.links = links
        self.url = ""
        self.aufrufe: list[str] = []

    def set_viewport_size(self, _: dict[str, int]) -> None:
        return None

    def goto(self, url: str, **_: Any) -> _Antwort:
        self.url = url
        self.aufrufe.append(url.removeprefix(BASIS))
        return _Antwort(200)

    def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    def eval_on_selector_all(self, *_: Any) -> list[str]:
        return self.links.get(self.url.removeprefix(BASIS), [])


class TestErkunden:
    START = f"/work/{ORG}/"
    AKTION = f"/work/{ORG}/notifications/alle-gelesen/"
    LINKS = {START: [f"/work/{ORG}/faction/", AKTION, f"/work/{ORG}/tasks/labels/{KENNUNG}/"]}

    def erkunden(self, *, nur_erlaubte: bool) -> tuple[_Netz, Any]:
        pruefer = skript.Pruefer(object(), BASIS, ORG, nur_erlaubte=nur_erlaubte, protokoll=lambda _: None)
        pruefer._messen = lambda *_, **__: None
        netz = _Netz(self.LINKS)
        lauf = skript.Lauf("vorsitz", "aktuell")
        pruefer._erkunden(netz, lauf, [])
        return netz, lauf

    def test_entfernt_nur_seiten_der_erlaubnisliste(self) -> None:
        netz, lauf = self.erkunden(nur_erlaubte=True)
        assert netz.aufrufe == [self.START, f"/work/{ORG}/faction/"]
        assert lauf.nicht_geprueft == [
            f"/work/{ORG}/notifications/alle-gelesen/",
            f"/work/{ORG}/tasks/labels/{{id}}/",
        ]
        text, fehler = skript.zusammenfassen([lauf])
        assert fehler == 0
        assert "Nicht geprüft (nicht auf der Erlaubnisliste für entfernte Instanzen): 2" in text
        assert f"  [Hinweis] /work/{ORG}/notifications/alle-gelesen/" in text

    def test_lokal_alle_seiten_ausser_der_sperrliste(self) -> None:
        netz, lauf = self.erkunden(nur_erlaubte=False)
        assert self.AKTION in netz.aufrufe
        assert lauf.nicht_geprueft == []

    def test_entfernte_instanz_immer_mit_erlaubnisliste(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MANDARI_PRUEF_PASSWORT", "x")
        aufrufe: list[dict[str, Any]] = []

        def pruefen(*_: Any, **optionen: Any) -> list[Any]:
            aufrufe.append(optionen)
            return []

        monkeypatch.setattr(skript, "pruefen", pruefen)
        assert skript.main(["--basis", "https://demo.mandari.de"]) == 0
        assert skript.main(["--basis", "http://localhost:8000"]) == 0
        assert skript.main(["--basis", "http://localhost:8000", "--nur-erlaubte"]) == 0
        assert [k["nur_erlaubte"] for k in aufrufe] == [True, False, True]

    def test_entfernt_auch_nur_mit_seiten_der_liste(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MANDARI_PRUEF_PASSWORT", "x")
        monkeypatch.setattr(skript, "pruefen", lambda *a, **k: pytest.fail("darf nicht starten"))
        with pytest.raises(SystemExit) as abbruch:
            skript.main(["--basis", "https://demo.mandari.de", "--nur", "notifications/alle-gelesen/"])
        assert abbruch.value.code == 2


class TestKonsole:
    @pytest.mark.parametrize(
        ("text", "quelle", "erwartet"),
        [
            # Fremde Kartenkacheln ohne Namensauflösung: Hinweis, kein Fehler der Seite
            (
                "Failed to load resource: net::ERR_NAME_NOT_RESOLVED",
                "https://kacheln.example/14/8500/5500.png",
                ("Fremde Ressource", "kacheln.example: Failed to load resource: net::ERR_NAME_NOT_RESOLVED"),
            ),
            # Eigene Ressource bleibt ein Fehler
            (
                "Failed to load resource: the server responded with a status of 404 (Not Found)",
                f"{BASIS}/static/dist/assets/main.js",
                ("Konsole", "Failed to load resource: the server responded with a status of 404 (Not Found)"),
            ),
            # Ohne Adresse lässt sich nichts zuordnen: Fehler
            ("Failed to load resource: net::ERR_FAILED", "", ("Konsole", "Failed to load resource: net::ERR_FAILED")),
            # CSP-Verstöße und Alpine-Fehler bleiben Fehler, auch wenn sie eine fremde Adresse nennen
            (
                "Refused to load the script 'https://cdn.example/x.js' because it violates the CSP",
                "https://cdn.example/x.js",
                ("Konsole", "Refused to load the script 'https://cdn.example/x.js' because it violates the CSP"),
            ),
            (
                "Alpine Expression Error: x is not defined\nmehr",
                f"{BASIS}/work/",
                ("Konsole", "Alpine Expression Error: x is not defined"),
            ),
            # Zusammenarbeit im Editor (WebSocket) zählt nicht
            ("WebSocket connection to 'wss://demo.example/ws/' failed", "", None),
        ],
    )
    def test_einordnung(self, text: str, quelle: str, erwartet: tuple[str, str] | None) -> None:
        assert skript.konsole_einordnen(text, quelle, BASIS) == erwartet

    def test_fremde_ressource_zaehlt_nicht_als_fehler(self) -> None:
        lauf = skript.Lauf("vorsitz", "an")
        lauf.befunde.append(
            skript.Befund(
                "vorsitz", "an", "/work/x/ris/map/", "Fremde Ressource", "kacheln.example: …", 1280, "Hinweis"
            )
        )
        _, fehler = skript.zusammenfassen([lauf])
        assert fehler == 0
        assert "Fremde Ressource" in skript.HINWEIS_ARTEN
