# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auswertung der Lasttests (loadtest/auswerten.py, loadtest/ressourcen.py, Issue #228).

Geprüft wird, was der Workflow „Lasttest“ daraus macht: Perzentile je Szenario, das Einlesen der
Locust-CSV, die Bewertung gegen die Budgets (Fehlerquote gesamt und je Szenario, p95, kaputte
Szenarien), der Bericht, die Laufparameter aus ``loadtest/budgets.json`` samt Prüfung der Eingaben
und dass die Budgetdatei nur Szenarien und Endpunkte nennt, die ``loadtest/locustfile.py`` misst.
Dazu die Verdrahtung des Workflows, die in keinem Lauf auffiele: Der Zeitplan prüft den
Integrationszweig ``dev``, und der Pfadfilter nennt nur vorhandene Dateien.

Die Module brauchen kein Django und keine Datenbank; der Workflow führt diese Datei vor dem Lauf aus.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
LOADTEST = REPO / "loadtest"
WORKFLOW = REPO / ".github" / "workflows" / "lasttest.yml"


def _laden(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"lasttest_{name}", LOADTEST / f"{name}.py")
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    # dataclasses löst die Annotationen über sys.modules auf — ohne Eintrag scheitert der Import
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture(scope="module")
def auswerten() -> ModuleType:
    return _laden("auswerten")


@pytest.fixture(scope="module")
def ressourcen() -> ModuleType:
    return _laden("ressourcen")


def _messung(modul: ModuleType, name: str, *, anfragen: int = 100, fehler: int = 0, p95: float = 100) -> object:
    return modul.Messung(
        name=name,
        anfragen=anfragen,
        fehler=fehler,
        median_ms=p95 / 2,
        p95_ms=p95,
        p99_ms=p95 * 1.2,
        max_ms=p95 * 2,
        anfragen_pro_s=1.0,
    )


BUDGET = {
    "fehlerquote_max_prozent": 1.0,
    "szenarien_p95_ms": {"Portal": 1000, "Sitzungsgeldlauf": 10000},
    "endpunkte_p95_ms": {"/insight/": 800},
}


# =============================================================================
# Kennzahlen
# =============================================================================


def test_perzentil_nach_rangverfahren(auswerten: ModuleType) -> None:
    werte = list(range(1, 101))
    assert auswerten.perzentil(werte, 95) == 95
    assert auswerten.perzentil(werte, 50) == 50
    assert auswerten.perzentil(werte, 100) == 100
    assert auswerten.perzentil([7.5], 95) == 7.5
    assert auswerten.perzentil([], 95) == 0.0
    # Reihenfolge der Eingabe spielt keine Rolle
    assert auswerten.perzentil([300, 100, 200], 50) == 200


def test_kennzahlen_je_szenario(auswerten: ModuleType) -> None:
    zeiten = {"Portal": [float(z) for z in range(1, 201)], "OParl": [50.0, 70.0]}
    ergebnis = auswerten.szenario_kennzahlen(zeiten, {"OParl": 1}, dauer_s=100)

    assert ergebnis["Portal"]["anfragen"] == 200
    assert ergebnis["Portal"]["p95_ms"] == 190
    assert ergebnis["Portal"]["anfragen_pro_s"] == 2.0
    assert ergebnis["Portal"]["fehler"] == 0
    assert ergebnis["OParl"]["fehler"] == 1
    assert ergebnis["OParl"]["max_ms"] == 70


def test_locust_csv_mit_gesamtzeile_und_leeren_werten(auswerten: ModuleType, tmp_path: Path) -> None:
    pfad = tmp_path / "klein_stats.csv"
    felder = ["Type", "Name", "Request Count", "Failure Count", "Max Response Time", "Requests/s", "50%", "95%", "99%"]
    with pfad.open("w", encoding="utf-8", newline="") as datei:
        schreiber = csv.writer(datei)
        schreiber.writerow(felder)
        schreiber.writerow(["GET", "/insight/", "120", "2", "900", "0.66", "90", "240", "400"])
        schreiber.writerow(["POST", "/session/allowances/generate/ [POST]", "0", "0", "0", "0", "N/A", "N/A", "N/A"])
        schreiber.writerow(["", "Aggregated", "120", "2", "900", "0.66", "90", "240", "400"])

    endpunkte, gesamt = auswerten.stats_einlesen(auswerten._csv_lesen(pfad))

    assert [e.name for e in endpunkte] == ["/insight/", "/session/allowances/generate/ [POST]"]
    assert endpunkte[1].p95_ms == 0.0
    assert gesamt is not None
    assert gesamt.anfragen == 120
    assert gesamt.fehlerquote == pytest.approx(1.667, abs=0.001)


# =============================================================================
# Bewertung
# =============================================================================


def test_alles_im_budget(auswerten: ModuleType) -> None:
    gesamt = _messung(auswerten, "Aggregated", anfragen=1000, fehler=0, p95=400)
    szenarien = [_messung(auswerten, "Portal", p95=500), _messung(auswerten, "Sitzungsgeldlauf", anfragen=6, p95=4000)]
    endpunkte = [_messung(auswerten, "/insight/", p95=500)]

    bewertung = auswerten.bewerten(gesamt, endpunkte, szenarien, BUDGET)

    assert bewertung.bestanden, bewertung.verletzungen
    assert bewertung.hinweise == []


def test_ueberschrittenes_p95_und_fehlerquote_sind_verletzungen(auswerten: ModuleType) -> None:
    gesamt = _messung(auswerten, "Aggregated", anfragen=1000, fehler=20)
    szenarien = [_messung(auswerten, "Portal", p95=1500), _messung(auswerten, "Sitzungsgeldlauf", p95=4000)]
    endpunkte = [_messung(auswerten, "/insight/", p95=900)]

    bewertung = auswerten.bewerten(gesamt, endpunkte, szenarien, BUDGET)

    assert not bewertung.bestanden
    text = "\n".join(bewertung.verletzungen)
    assert "Fehlerquote gesamt 2.00 %" in text
    assert "Szenario Portal: p95 1500 ms, Budget 1000 ms" in text
    assert "Endpunkt /insight/: p95 900 ms, Budget 800 ms" in text


def test_fehler_eines_seltenen_szenarios_fallen_auf(auswerten: ModuleType) -> None:
    """Sechs Anfragen des Abrechnungslaufs verschwinden sonst in der Gesamtquote."""
    gesamt = _messung(auswerten, "Aggregated", anfragen=1000, fehler=1)
    szenarien = [
        _messung(auswerten, "Portal", p95=500),
        _messung(auswerten, "Sitzungsgeldlauf", anfragen=6, fehler=1, p95=4000),
    ]
    bewertung = auswerten.bewerten(gesamt, [_messung(auswerten, "/insight/")], szenarien, BUDGET)

    assert bewertung.verletzungen == ["Szenario Sitzungsgeldlauf: Fehlerquote 16.67 % (1 von 6), Budget 1 %"]


def test_kaputtes_szenario_misst_nichts_und_faellt_durch(auswerten: ModuleType) -> None:
    gesamt = _messung(auswerten, "Aggregated", anfragen=1000)
    bewertung = auswerten.bewerten(gesamt, [], [_messung(auswerten, "Portal")], BUDGET)

    assert "Szenario Sitzungsgeldlauf: keine Anfragen gemessen" in bewertung.verletzungen
    assert "Endpunkt /insight/: keine Anfragen gemessen" in bewertung.verletzungen


def test_ohne_anfragen_kein_bestandener_lauf(auswerten: ModuleType) -> None:
    bewertung = auswerten.bewerten(None, [], [], {"fehlerquote_max_prozent": 1.0})
    assert not bewertung.bestanden


def test_wenige_anfragen_und_grosse_reserve_sind_nur_hinweise(auswerten: ModuleType) -> None:
    gesamt = _messung(auswerten, "Aggregated", anfragen=1000)
    szenarien = [_messung(auswerten, "Portal", p95=200), _messung(auswerten, "Sitzungsgeldlauf", p95=4000)]
    endpunkte = [_messung(auswerten, "/insight/", anfragen=5, p95=5000)]

    bewertung = auswerten.bewerten(gesamt, endpunkte, szenarien, BUDGET)

    assert bewertung.bestanden
    assert "Endpunkt /insight/: nur 5 Anfragen, p95 nicht bewertet" in bewertung.hinweise
    assert "Szenario Portal: p95 200 ms, Budget 1000 ms – senken?" in bewertung.hinweise


# =============================================================================
# Bericht und Kommandozeile
# =============================================================================


def _ergebnisse_schreiben(praefix: Path, *, p95_portal: int) -> None:
    with Path(f"{praefix}_stats.csv").open("w", encoding="utf-8", newline="") as datei:
        schreiber = csv.writer(datei)
        schreiber.writerow(["Type", "Name", "Request Count", "Failure Count", "Max Response Time", "Requests/s", "95%"])
        schreiber.writerow(["GET", "/insight/", "1234", "0", "900", "6.86", "240"])
        schreiber.writerow(["", "Aggregated", "1234", "0", "900", "6.86", "240"])
    szenarien = {
        "szenarien": {
            "Portal": {
                "anfragen": 1234,
                "fehler": 0,
                "p50_ms": 90,
                "p95_ms": p95_portal,
                "p99_ms": 400,
                "max_ms": 900,
                "anfragen_pro_s": 6.86,
            }
        },
        "bedingt": {"/oparl/v1/body/<id>/papers [bedingt]": {"anfragen": 40, "nicht_geaendert": 38}},
    }
    Path(f"{praefix}_szenarien.json").write_text(json.dumps(szenarien), encoding="utf-8")
    with Path(f"{praefix}_ressourcen.csv").open("w", encoding="utf-8", newline="") as datei:
        schreiber = csv.writer(datei)
        schreiber.writerow(["zeit_s", "komponente", "messgroesse", "wert"])
        schreiber.writerows([[5, "anwendung", "cpu_prozent", 40], [10, "anwendung", "cpu_prozent", 80]])


def test_bericht_und_status(auswerten: ModuleType, tmp_path: Path) -> None:
    budgets = tmp_path / "budgets.json"
    budgets.write_text(
        json.dumps(
            {
                "profile": {
                    "klein": {
                        "fehlerquote_max_prozent": 1.0,
                        "szenarien_p95_ms": {"Portal": 1000},
                        "endpunkte_p95_ms": {"/insight/": 800},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    praefix = tmp_path / "klein"
    bericht = tmp_path / "bericht.md"
    argumente = ["bericht", "--profil", "klein", "--ergebnisse", str(praefix), "--budgets", str(budgets)]

    _ergebnisse_schreiben(praefix, p95_portal=480)
    assert auswerten.main([*argumente, "--bericht", str(bericht), "--nutzer", "20"]) == 0
    text = bericht.read_text(encoding="utf-8")
    assert "Budgets eingehalten" in text
    assert "| Anfragen gesamt | 1.234 |" in text
    assert "| Durchsatz | 6,9 Anfragen/s |" in text
    assert "| Portal | 1.234 | 0 | 6,86 | 90 | 480 | 400 | 900 | 1.000 |" in text
    assert "| `/oparl/v1/body/<id>/papers [bedingt]` | 40 | 38 | 95,0 % |" in text
    assert "| anwendung | cpu_prozent | 60,0 | 80,0 |" in text

    _ergebnisse_schreiben(praefix, p95_portal=1500)
    assert auswerten.main([*argumente, "--bericht", str(bericht)]) == 1
    assert "BUDGET VERLETZT" in bericht.read_text(encoding="utf-8")


def test_ohne_locust_ergebnisse_scheitert_der_bericht(auswerten: ModuleType, tmp_path: Path) -> None:
    assert auswerten.main(["bericht", "--profil", "klein", "--ergebnisse", str(tmp_path / "fehlt")]) == 1


# =============================================================================
# Laufparameter und Budgetdatei
# =============================================================================


def test_laufparameter_mit_vorgaben_und_ueberschreibungen(auswerten: ModuleType) -> None:
    budget = {"lauf": {"nutzer": 20, "anlauf": 5, "dauer": "3m", "prozesse": 1, "postgres": {"work_mem": "8MB"}}}

    assert auswerten.laufparameter(budget, {}) == {
        "nutzer": "20",
        "anlauf": "5",
        "dauer": "3m",
        "prozesse": "1",
        "postgres": "-c work_mem=8MB",
        "stufen": "",
        "stufendauer": "120",
    }
    assert auswerten.laufparameter(budget, {"nutzer": "50", "dauer": "15m"})["nutzer"] == "50"
    assert auswerten.laufparameter(budget, {"nutzer": "", "dauer": None})["dauer"] == "3m"


@pytest.mark.parametrize(
    "ueberschreibung",
    [
        {"nutzer": "20; rm -rf /"},
        {"dauer": "3 Minuten"},
        {"dauer": "$(id)"},
        {"nutzer": "-1"},
        {"stufen": "25,50;id"},
        {"stufen": "0,50"},
        {"stufen": ",".join(["10"] * 13)},
        {"stufen": "25", "stufendauer": "10"},
        {"stufen": "25", "stufendauer": "2m"},
        # Tippfehler, die den Läufer sonst bis zum Timeout belegen
        {"nutzer": "20000"},
        {"nutzer": "0"},
        {"prozesse": "100"},
        {"prozesse": "0"},
        {"dauer": "61m"},
        {"dauer": "2h"},
        {"dauer": "0s"},
        {"stufen": "25,50,100", "stufendauer": "1800"},
        # Ziffern außerhalb von 0–9 (str.isdigit ließe sie zu)
        {"nutzer": "²"},
        {"dauer": "³m"},
        {"stufen": "25,⁵0"},
    ],
)
def test_ungueltige_eingaben_des_manuellen_starts(auswerten: ModuleType, ueberschreibung: dict[str, str]) -> None:
    budget = {"lauf": {"nutzer": 20, "anlauf": 5, "dauer": "3m", "prozesse": 1}}
    with pytest.raises(SystemExit):
        auswerten.laufparameter(budget, ueberschreibung)


def test_obergrenzen_des_manuellen_starts_sind_erlaubt(auswerten: ModuleType) -> None:
    budget = {"lauf": {"nutzer": 20, "anlauf": 5, "dauer": "3m", "prozesse": 1}}
    werte = auswerten.laufparameter(
        budget, {"nutzer": str(auswerten.NUTZER_MAX), "prozesse": str(auswerten.PROZESSE_MAX), "dauer": "1h"}
    )

    assert (werte["nutzer"], werte["prozesse"], werte["dauer"]) == ("2000", "16", "1h")
    assert auswerten.dauer_sekunden("60m") == auswerten.DAUER_MAX_S == 3600
    assert auswerten.laufparameter(budget, {"stufen": "10,20,30", "stufendauer": "1200"})["stufen"] == "10,20,30"


def test_ungueltige_postgres_einstellung(auswerten: ModuleType) -> None:
    budget = {"lauf": {"nutzer": 1, "anlauf": 1, "dauer": "1m", "prozesse": 1, "postgres": {"work_mem": "8MB -c x"}}}
    with pytest.raises(SystemExit):
        auswerten.laufparameter(budget, {})


def test_parameter_ausgabe_fuer_den_workflow(auswerten: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert auswerten.main(["parameter", "--profil", "gross"]) == 0
    zeilen = dict(zeile.split("=", 1) for zeile in capsys.readouterr().out.splitlines())
    assert zeilen["nutzer"] == "400"
    assert zeilen["prozesse"] == "4"
    assert "-c shared_buffers=2GB" in zeilen["postgres"]


def test_budgetdatei_passt_zu_den_szenarien(auswerten: ModuleType) -> None:
    """Jedes Budget gehört zu einem Szenario bzw. Endpunkt, den das Locust-Skript misst."""
    locustfile = (LOADTEST / "locustfile.py").read_text(encoding="utf-8")
    szenarien = set(re.findall(r'^\s+szenario = "([^"]+)"', locustfile, flags=re.MULTILINE))
    assert {"Portal", "Sitzungsdienst", "Fraktion", "OParl", "Live-Abstimmung", "Sitzungsgeldlauf"} <= szenarien

    budgets = auswerten.budgets_lesen()
    assert set(budgets["profile"]) == {"klein", "mittel", "gross"}
    for profil, budget in budgets["profile"].items():
        auswerten.laufparameter(budget, {})  # vollständig und gültig
        assert set(budget["szenarien_p95_ms"]) <= szenarien, profil
        for endpunkt in budget["endpunkte_p95_ms"]:
            assert f'"{endpunkt}"' in locustfile, f"{profil}: Endpunkt {endpunkt} kommt im Locust-Skript nicht vor"
    # Das Wochen-Gate braucht Budgets für alle Szenarien
    assert set(budgets["profile"]["klein"]["szenarien_p95_ms"]) == szenarien - {"Sonstige"}


# =============================================================================
# Ressourcen
# =============================================================================


@pytest.mark.parametrize(
    ("angabe", "mb"),
    [
        ("123.4MiB / 15.61GiB", 129.4),
        ("1.5GiB / 15.61GiB", 1610.6),
        ("512kB / 1GB", 0.512),
        ("2GB", 2000.0),
        ("--", 0.0),
    ],
)
def test_speicherangaben_von_docker_stats(ressourcen: ModuleType, angabe: str, mb: float) -> None:
    assert ressourcen.speicher_mb(angabe) == pytest.approx(mb, abs=0.1)


def test_cpu_angaben_von_docker_stats(ressourcen: ModuleType) -> None:
    assert ressourcen.cpu_prozent("152.31%") == 152.31
    assert ressourcen.cpu_prozent("--") == 0.0


def test_ressourcen_zusammenfassen(auswerten: ModuleType) -> None:
    zeilen = [
        {"komponente": "postgres", "messgroesse": "verbindungen", "wert": "4"},
        {"komponente": "postgres", "messgroesse": "verbindungen", "wert": "12"},
        {"komponente": "postgres", "messgroesse": "verbindungen", "wert": "kaputt"},
        {"komponente": "anwendung", "messgroesse": "speicher_mb", "wert": "300"},
    ]
    ergebnis = auswerten.ressourcen_zusammenfassen(zeilen)
    assert [(r.komponente, r.messgroesse, r.mittel, r.maximum) for r in ergebnis] == [
        ("anwendung", "speicher_mb", 300.0, 300.0),
        ("postgres", "verbindungen", 8.0, 12.0),
    ]


# =============================================================================
# Stufenlast (Kapazitätsmessung)
# =============================================================================


def test_stufenlast_bestimmt_nutzer_und_laufzeit(auswerten: ModuleType) -> None:
    budget = {"lauf": {"nutzer": 400, "anlauf": 2, "dauer": "10m", "prozesse": 4}}
    werte = auswerten.laufparameter(budget, {"stufen": " 25, 50,100 ", "stufendauer": "90"})

    assert werte["stufen"] == "25,50,100"
    assert werte["nutzer"] == "100"
    assert werte["dauer"] == "300s"
    assert auswerten.stufen_lesen("") == []


def _stufe(modul: ModuleType, nutzer: int, *, p95: float, fehler: int = 0) -> object:
    return modul.Stufe(nutzer=nutzer, anfragen=1000, fehler=fehler, median_ms=p95 / 3, p95_ms=p95, anfragen_pro_s=10.0)


def test_kapazitaet_ist_die_letzte_getragene_stufe_vor_der_ersten_ueberlast(auswerten: ModuleType) -> None:
    stufen = [
        _stufe(auswerten, 25, p95=300),
        _stufe(auswerten, 50, p95=700),
        _stufe(auswerten, 100, p95=1800),
        # Eine spätere Stufe innerhalb der Zielwerte (Zufall, Cache) hebt die Grenze nicht an
        _stufe(auswerten, 150, p95=900),
    ]
    assert auswerten.kapazitaet(stufen).nutzer == 50
    assert auswerten.kapazitaet([_stufe(auswerten, 25, p95=300, fehler=20)]) is None
    assert auswerten.kapazitaet([]) is None


def test_stufen_mit_cpu_je_stufe_ohne_einschwingzeit(auswerten: ModuleType) -> None:
    daten = {
        "stufen": {
            "dauer_s": 100,
            "einschwingen_s": 20,
            "liste": [
                {"nutzer": 25, "anfragen": 900, "fehler": 0, "p50_ms": 80, "p95_ms": 300, "anfragen_pro_s": 11.2},
                {"nutzer": 50, "anfragen": 0},
            ],
        }
    }
    messreihe = [
        {"zeit_s": "10", "komponente": "anwendung", "messgroesse": "cpu_prozent", "wert": "400"},  # Einschwingen
        {"zeit_s": "50", "komponente": "anwendung", "messgroesse": "cpu_prozent", "wert": "100"},
        {"zeit_s": "90", "komponente": "anwendung", "messgroesse": "cpu_prozent", "wert": "140"},
        {"zeit_s": "90", "komponente": "postgres", "messgroesse": "cpu_prozent", "wert": "30"},
        {"zeit_s": "90", "komponente": "anwendung", "messgroesse": "speicher_mb", "wert": "900"},
    ]

    stufen = auswerten.stufen_einlesen(daten, messreihe)

    assert [s.nutzer for s in stufen] == [25, 50]
    assert stufen[0].cpu_anwendung == 120
    assert stufen[0].cpu_datenbank == 30
    assert stufen[1].cpu_anwendung is None
    assert not stufen[1].getragen  # nichts gemessen
    assert auswerten.stufen_einlesen({}) == []


def test_bericht_der_kapazitaetsmessung(auswerten: ModuleType, tmp_path: Path) -> None:
    praefix = tmp_path / "gross"
    _ergebnisse_schreiben(praefix, p95_portal=5000)  # weit über jedem Budget: gilt hier nicht
    daten = json.loads(Path(f"{praefix}_szenarien.json").read_text(encoding="utf-8"))
    daten["stufen"] = {
        "dauer_s": 120,
        "einschwingen_s": 20,
        "liste": [
            {"nutzer": 50, "anfragen": 2000, "fehler": 0, "p50_ms": 90, "p95_ms": 400, "anfragen_pro_s": 20.0},
            {"nutzer": 100, "anfragen": 3000, "fehler": 90, "p50_ms": 900, "p95_ms": 2400, "anfragen_pro_s": 30.0},
        ],
    }
    Path(f"{praefix}_szenarien.json").write_text(json.dumps(daten), encoding="utf-8")
    bericht = tmp_path / "bericht.md"

    assert (
        auswerten.main(["bericht", "--profil", "gross", "--ergebnisse", str(praefix), "--bericht", str(bericht)]) == 0
    )
    text = bericht.read_text(encoding="utf-8")
    assert "Lasttest „gross“: Kapazitätsmessung" in text
    assert "| Stufen (gleichzeitige Nutzer) | 50, 100 |" in text
    assert "| 100 | 30,0 | 3,00 % | 900 | 2.400 |" in text
    assert "Kapazität: **50 gleichzeitige Nutzer** bei 20,0 Anfragen/s (p95 400 ms)." in text
    assert "Kapazitätsmessung ohne Budgetprüfung." in text


# =============================================================================
# Workflow „Lasttest“
# =============================================================================


def _workflow() -> dict[Any, Any]:
    daten: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return daten


def _ausloeser() -> dict[str, Any]:
    daten = _workflow()
    # YAML 1.1 liest den Schlüssel „on“ als True
    ausloeser: dict[str, Any] = daten["on"] if "on" in daten else daten[True]
    return ausloeser


def test_zeitplan_prueft_den_integrationszweig() -> None:
    """Der Zeitplan startet auf dem Standardzweig main; Pull Requests gehen aber nach dev.

    Ohne ``ref`` fiele eine Zeit-Regression erst auf, wenn sie schon auf main liegt.
    """
    assert _ausloeser()["schedule"]
    schritte = _workflow()["jobs"]["lasttest"]["steps"]
    checkout = [schritt for schritt in schritte if str(schritt.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkout) == 1
    ref = str(checkout[0].get("with", {}).get("ref", ""))
    assert "github.event_name == 'schedule' && 'dev'" in ref, ref
    # Alle übrigen Auslöser prüfen ihren eigenen Stand (Pull Request, Branch des manuellen Starts)
    assert ref.replace(" ", "").endswith("||''}}"), ref


def test_pfadfilter_nennt_vorhandene_dateien() -> None:
    """Eine umbenannte Datei fiele sonst still aus dem Filter, und der Lasttest liefe nicht mehr."""
    pfade: list[str] = _ausloeser()["pull_request"]["paths"]
    einschluesse = [muster for muster in pfade if not muster.startswith("!")]
    # GitHub wertet der Reihe nach aus: Ein Ausschluss wirkt nur nach den Einschlüssen
    assert pfade[: len(einschluesse)] == einschluesse
    for muster in einschluesse:
        fester_teil = muster.split("*", 1)[0]
        assert (REPO / fester_teil).exists(), f"{muster}: {fester_teil} gibt es nicht"
    # Code der Szenarien, deren Zeit kein anderes Gate prüft (docs/LASTTESTS.md 4.2)
    assert {
        "loadtest/**",
        "mandari/hub/api/**",
        "mandari/apps/session/api/oparl.py",
        "mandari/apps/session/services/voting_service.py",
        "mandari/apps/session/services/allowance_service.py",
    } <= set(einschluesse)
