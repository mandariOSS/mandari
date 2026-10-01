# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auswertung eines Locust-Laufs (Issue #228): Bericht mit Kennzahlen und Performance-Budgets.

    python loadtest/auswerten.py parameter --profil klein
    python loadtest/auswerten.py bericht --profil klein --ergebnisse loadtest/results/klein \\
        [--budgets loadtest/budgets.json] [--bericht loadtest/results/klein-bericht.md] \\
        [--nutzer 20 --anlauf 5 --dauer 3m --prozesse 1 --umgebung "…"]

``parameter`` gibt die Laufparameter eines Profils aus ``budgets.json`` als ``schluessel=wert`` aus
(für ``$GITHUB_OUTPUT``): Nutzer, Anlauf je Sekunde, Laufzeit, Zahl der Anwendungsprozesse, die
Startargumente für PostgreSQL (``postgres=-c shared_buffers=… -c …``) und – mit ``--stufen`` – die
Stufen der Kapazitätsmessung. ``bericht`` liest die Ergebnisse eines Laufs, schreibt den Bericht als
Markdown und endet mit Status 1, wenn ein Budget verletzt ist.

Kapazitätsmessung (Stufenlast, ``LOADTEST_STUFEN``): Die Nutzerzahl steigt stufenweise; der Bericht
nennt je Stufe Durchsatz, p95, Fehlerquote und CPU und die höchste Stufe innerhalb der Zielwerte
(``KAPAZITAET_P95_MS``, ``KAPAZITAET_FEHLER_PROZENT``). Die Budgets gelten dabei nicht – die Messung
überschreitet die Grenze absichtlich.

Eingaben, alle mit demselben Präfix (``--ergebnisse``):

- ``<präfix>_stats.csv`` (Locust ``--csv``): je Endpunkt Anfragen, Fehler, Perzentile, Durchsatz
- ``<präfix>_szenarien.json`` (``locustfile.py``): Perzentile je Szenario aus allen Einzelwerten,
  die Antworten bedingter Anfragen (ETag, ``304 Not Modified``) und bei Stufenlast die Stufen
- ``<präfix>_ressourcen.csv`` (``ressourcen.py``, optional): CPU und Speicher je Komponente

Budgets je Profil (``budgets.json``):

- ``fehlerquote_max_prozent`` gilt für den ganzen Lauf und für jedes Szenario einzeln: Ein seltenes
  Szenario (der Sitzungsgeldlauf macht eine Handvoll Anfragen) verschwindet sonst in der Gesamtquote.
- ``szenarien_p95_ms`` und ``endpunkte_p95_ms``: p95 in Millisekunden. Ein Szenario oder Endpunkt mit
  Budget, für das keine Anfrage gemessen wurde, gilt als verletzt – dann ist das Szenario kaputt
  (Anmeldung gescheitert, Adresse geändert) und misst nichts mehr.
- Budgets dürfen nur sinken. Liegt ein p95 unter einem Drittel seines Budgets, nennt der Bericht das
  als Hinweis.

Nur Standardbibliothek: Das Modul läuft auf dem Lastgeber ohne Django und wird in
``mandari/apps/common/tests/test_lasttest_auswertung.py`` geprüft.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUDGETS = Path(__file__).resolve().parent / "budgets.json"
#: Unter so vielen Anfragen ist ein p95 Zufall – das Endpunkt-Budget wird dann nicht bewertet
MINDESTANFRAGEN_ENDPUNKT = 20
#: Liegt ein p95 unter diesem Anteil seines Budgets, schlägt der Bericht vor, das Budget zu senken
RESERVE_HINWEIS = 1 / 3
#: Laufparameter, die ``parameter`` ausgibt (Reihenfolge der Ausgabe)
LAUFPARAMETER = ("nutzer", "anlauf", "dauer", "prozesse")
#: Stufenlast: Sekunden nach jedem Stufenwechsel, die nicht in die Kennzahlen der Stufe eingehen
EINSCHWINGEN_S = 20
#: Zielwerte, bis zu denen eine Stufe als „getragen“ gilt (Kapazität im Bericht)
KAPAZITAET_P95_MS = 1000
KAPAZITAET_FEHLER_PROZENT = 1.0
#: Obergrenzen der Stufenlast (Eingaben des manuellen Starts)
STUFEN_MAX = 12
STUFEN_NUTZER_MAX = 2000


def stufen_lesen(text: str) -> list[int]:
    """``"25,50,100"`` als Stufen der Kapazitätsmessung; leer = keine Stufenlast. Ungültiges bricht ab."""
    teile = [teil.strip() for teil in text.split(",") if teil.strip()]
    if not all(teil.isdigit() for teil in teile):
        raise SystemExit("Stufen als Nutzerzahlen mit Komma angeben, z. B. 25,50,100")
    stufen = [int(teil) for teil in teile]
    if len(stufen) > STUFEN_MAX or any(not 0 < nutzer <= STUFEN_NUTZER_MAX for nutzer in stufen):
        raise SystemExit(f"Höchstens {STUFEN_MAX} Stufen mit je 1 bis {STUFEN_NUTZER_MAX} Nutzern")
    return stufen


# =============================================================================
# Kennzahlen
# =============================================================================


def perzentil(werte: Sequence[float], anteil: float) -> float:
    """Perzentil nach dem Rangverfahren (nearest rank) – ohne Interpolation, wie Locust selbst."""
    if not werte:
        return 0.0
    sortiert = sorted(werte)
    rang = max(1, math.ceil(anteil / 100 * len(sortiert)))
    return float(sortiert[rang - 1])


def szenario_kennzahlen(
    zeiten: Mapping[str, Sequence[float]], fehler: Mapping[str, int], dauer_s: float
) -> dict[str, dict[str, float]]:
    """Kennzahlen je Szenario aus allen Einzelwerten (geschrieben von ``locustfile.py``)."""
    ergebnis: dict[str, dict[str, float]] = {}
    for name in sorted(zeiten):
        werte = zeiten[name]
        anzahl = len(werte)
        ergebnis[name] = {
            "anfragen": anzahl,
            "fehler": int(fehler.get(name, 0)),
            "mittel_ms": round(sum(werte) / anzahl, 1) if anzahl else 0.0,
            "p50_ms": round(perzentil(werte, 50), 1),
            "p90_ms": round(perzentil(werte, 90), 1),
            "p95_ms": round(perzentil(werte, 95), 1),
            "p99_ms": round(perzentil(werte, 99), 1),
            "max_ms": round(max(werte), 1) if anzahl else 0.0,
            "anfragen_pro_s": round(anzahl / dauer_s, 2) if dauer_s > 0 else 0.0,
        }
    return ergebnis


@dataclass(frozen=True)
class Messung:
    """Kennzahlen eines Endpunkts (Locust-CSV) oder eines Szenarios (Szenarien-JSON)."""

    name: str
    anfragen: int
    fehler: int
    median_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    anfragen_pro_s: float

    @property
    def fehlerquote(self) -> float:
        """Fehler in Prozent der Anfragen."""
        return 100 * self.fehler / self.anfragen if self.anfragen else 0.0


@dataclass(frozen=True)
class Ressource:
    komponente: str
    messgroesse: str
    mittel: float
    maximum: float


@dataclass(frozen=True)
class Bewertung:
    verletzungen: list[str]
    hinweise: list[str]

    @property
    def bestanden(self) -> bool:
        return not self.verletzungen


@dataclass(frozen=True)
class Stufe:
    """Eine Stufe der Kapazitätsmessung (ohne Einschwingzeit)."""

    nutzer: int
    anfragen: int
    fehler: int
    median_ms: float
    p95_ms: float
    anfragen_pro_s: float
    cpu_anwendung: float | None = None
    cpu_datenbank: float | None = None

    @property
    def fehlerquote(self) -> float:
        return 100 * self.fehler / self.anfragen if self.anfragen else 0.0

    @property
    def getragen(self) -> bool:
        """Innerhalb der Zielwerte (p95 und Fehlerquote) und überhaupt gemessen."""
        return self.anfragen > 0 and self.p95_ms <= KAPAZITAET_P95_MS and self.fehlerquote <= KAPAZITAET_FEHLER_PROZENT


@dataclass
class Lauf:
    """Rahmen eines Laufs für den Bericht."""

    profil: str
    nutzer: str = ""
    anlauf: str = ""
    dauer: str = ""
    prozesse: str = ""
    umgebung: str = ""
    bedingt: dict[str, dict[str, int]] = field(default_factory=dict)
    stufen: list[Stufe] = field(default_factory=list)


# =============================================================================
# Einlesen
# =============================================================================


def _zahl(wert: str | None) -> float:
    """Zahl aus der Locust-CSV; leere Felder und ``N/A`` (keine Anfrage) zählen als 0."""
    if wert is None or wert.strip() in ("", "N/A"):
        return 0.0
    return float(wert)


def stats_einlesen(zeilen: Iterable[Mapping[str, str]]) -> tuple[list[Messung], Messung | None]:
    """Locust-``_stats.csv``: Endpunkte und die Gesamtzeile ``Aggregated``."""
    endpunkte: list[Messung] = []
    gesamt: Messung | None = None
    for zeile in zeilen:
        messung = Messung(
            name=zeile["Name"],
            anfragen=int(_zahl(zeile.get("Request Count"))),
            fehler=int(_zahl(zeile.get("Failure Count"))),
            median_ms=_zahl(zeile.get("50%") or zeile.get("Median Response Time")),
            p95_ms=_zahl(zeile.get("95%")),
            p99_ms=_zahl(zeile.get("99%")),
            max_ms=_zahl(zeile.get("Max Response Time")),
            anfragen_pro_s=_zahl(zeile.get("Requests/s")),
        )
        if zeile["Name"] == "Aggregated":
            gesamt = messung
        else:
            endpunkte.append(messung)
    return endpunkte, gesamt


def szenarien_einlesen(daten: Mapping[str, Any]) -> list[Messung]:
    """``_szenarien.json`` aus ``locustfile.py``."""
    return [
        Messung(
            name=name,
            anfragen=int(werte["anfragen"]),
            fehler=int(werte["fehler"]),
            median_ms=float(werte["p50_ms"]),
            p95_ms=float(werte["p95_ms"]),
            p99_ms=float(werte["p99_ms"]),
            max_ms=float(werte["max_ms"]),
            anfragen_pro_s=float(werte["anfragen_pro_s"]),
        )
        for name, werte in sorted(daten.get("szenarien", {}).items())
    ]


def ressourcen_zusammenfassen(zeilen: Iterable[Mapping[str, str]]) -> list[Ressource]:
    """Messreihe aus ``ressourcen.py`` zu Mittel und Maximum je Komponente und Messgröße."""
    reihen: dict[tuple[str, str], list[float]] = {}
    for zeile in zeilen:
        try:
            wert = float(zeile["wert"])
        except (KeyError, TypeError, ValueError):
            continue
        reihen.setdefault((zeile["komponente"], zeile["messgroesse"]), []).append(wert)
    return [
        Ressource(komponente, messgroesse, sum(werte) / len(werte), max(werte))
        for (komponente, messgroesse), werte in sorted(reihen.items())
    ]


def stufen_einlesen(daten: Mapping[str, Any], ressourcen: Iterable[Mapping[str, str]] = ()) -> list[Stufe]:
    """
    Stufen aus ``_szenarien.json`` (Schlüssel ``stufen``), dazu die mittlere CPU je Stufe aus der
    Messreihe von ``ressourcen.py`` (Zeitachse ab Beginn der Messung, ohne Einschwingzeit).
    """
    stufen: Mapping[str, Any] = daten.get("stufen") or {}
    liste: list[Mapping[str, Any]] = stufen.get("liste", [])
    if not liste:
        return []
    dauer = float(stufen.get("dauer_s", 0)) or 1.0
    einschwingen = float(stufen.get("einschwingen_s", 0))
    cpu: dict[tuple[int, str], list[float]] = {}
    for zeile in ressourcen:
        if zeile.get("messgroesse") != "cpu_prozent":
            continue
        try:
            zeit, wert = float(zeile["zeit_s"]), float(zeile["wert"])
        except (KeyError, TypeError, ValueError):
            continue
        index = int(zeit // dauer)
        if zeit - index * dauer >= einschwingen:
            cpu.setdefault((index, zeile.get("komponente", "")), []).append(wert)

    def mittel(index: int, komponente: str) -> float | None:
        werte = cpu.get((index, komponente))
        return sum(werte) / len(werte) if werte else None

    return [
        Stufe(
            nutzer=int(eintrag["nutzer"]),
            anfragen=int(eintrag.get("anfragen", 0)),
            fehler=int(eintrag.get("fehler", 0)),
            median_ms=float(eintrag.get("p50_ms", 0.0)),
            p95_ms=float(eintrag.get("p95_ms", 0.0)),
            anfragen_pro_s=float(eintrag.get("anfragen_pro_s", 0.0)),
            cpu_anwendung=mittel(index, "anwendung"),
            cpu_datenbank=mittel(index, "postgres"),
        )
        for index, eintrag in enumerate(liste)
    ]


def kapazitaet(stufen: Sequence[Stufe]) -> Stufe | None:
    """Höchste Stufe, bis zu der alle Stufen innerhalb der Zielwerte bleiben (None: schon die erste nicht)."""
    getragen: Stufe | None = None
    for stufe in stufen:
        if not stufe.getragen:
            break
        getragen = stufe
    return getragen


def _csv_lesen(pfad: Path) -> list[dict[str, str]]:
    with pfad.open(encoding="utf-8", newline="") as datei:
        return list(csv.DictReader(datei))


# =============================================================================
# Bewertung
# =============================================================================


def _schlechteste(messungen: Iterable[Messung], name: str) -> Messung | None:
    """Gleicher Name mit mehreren Methoden (selten): die Zeile mit dem höchsten p95 zählt."""
    passend = [m for m in messungen if m.name == name]
    return max(passend, key=lambda m: m.p95_ms) if passend else None


def bewerten(
    gesamt: Messung | None, endpunkte: list[Messung], szenarien: list[Messung], budget: Mapping[str, Any]
) -> Bewertung:
    """Messwerte gegen die Budgets eines Profils stellen."""
    verletzungen: list[str] = []
    hinweise: list[str] = []
    fehler_max = float(budget.get("fehlerquote_max_prozent", 1.0))

    if gesamt is None or gesamt.anfragen == 0:
        verletzungen.append("Keine Anfragen gemessen – der Lauf hat nichts geprüft")
    elif gesamt.fehlerquote > fehler_max:
        verletzungen.append(
            f"Fehlerquote gesamt {gesamt.fehlerquote:.2f} % ({gesamt.fehler} von {gesamt.anfragen}), "
            f"Budget {fehler_max:g} %"
        )

    for szenario in szenarien:
        if szenario.anfragen and szenario.fehlerquote > fehler_max:
            verletzungen.append(
                f"Szenario {szenario.name}: Fehlerquote {szenario.fehlerquote:.2f} % "
                f"({szenario.fehler} von {szenario.anfragen}), Budget {fehler_max:g} %"
            )

    for name, grenze in budget.get("szenarien_p95_ms", {}).items():
        szenario = _schlechteste(szenarien, name)
        if szenario is None or szenario.anfragen == 0:
            verletzungen.append(f"Szenario {name}: keine Anfragen gemessen")
        elif szenario.p95_ms > grenze:
            verletzungen.append(f"Szenario {name}: p95 {szenario.p95_ms:.0f} ms, Budget {grenze} ms")
        elif szenario.p95_ms < grenze * RESERVE_HINWEIS:
            hinweise.append(f"Szenario {name}: p95 {szenario.p95_ms:.0f} ms, Budget {grenze} ms – senken?")

    for name, grenze in budget.get("endpunkte_p95_ms", {}).items():
        endpunkt = _schlechteste(endpunkte, name)
        if endpunkt is None or endpunkt.anfragen == 0:
            verletzungen.append(f"Endpunkt {name}: keine Anfragen gemessen")
        elif endpunkt.anfragen < MINDESTANFRAGEN_ENDPUNKT:
            hinweise.append(f"Endpunkt {name}: nur {endpunkt.anfragen} Anfragen, p95 nicht bewertet")
        elif endpunkt.p95_ms > grenze:
            verletzungen.append(f"Endpunkt {name}: p95 {endpunkt.p95_ms:.0f} ms, Budget {grenze} ms")
        elif endpunkt.p95_ms < grenze * RESERVE_HINWEIS:
            hinweise.append(f"Endpunkt {name}: p95 {endpunkt.p95_ms:.0f} ms, Budget {grenze} ms – senken?")

    return Bewertung(verletzungen, hinweise)


def bewerten_kapazitaet(gesamt: Messung | None, stufen: Sequence[Stufe]) -> Bewertung:
    """
    Kapazitätsmessung: Sie sucht die Grenze und überschreitet sie absichtlich – die Budgets gelten
    hier nicht. Verletzt ist nur ein Lauf, der nichts gemessen hat.
    """
    if gesamt is None or gesamt.anfragen == 0 or not any(stufe.anfragen for stufe in stufen):
        return Bewertung(["Keine Anfragen gemessen – der Lauf hat nichts geprüft"], [])
    return Bewertung([], ["Kapazitätsmessung (Stufenlast): Budgets nicht bewertet"])


# =============================================================================
# Bericht
# =============================================================================


def _de(wert: float, stellen: int = 0) -> str:
    """Zahl in deutscher Schreibweise (Tausenderpunkt, Dezimalkomma)."""
    text = f"{wert:,.{stellen}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _ms(wert: float) -> str:
    return _de(wert)


def bericht(
    lauf: Lauf,
    gesamt: Messung | None,
    endpunkte: list[Messung],
    szenarien: list[Messung],
    ressourcen: list[Ressource],
    bewertung: Bewertung,
    budget: Mapping[str, Any],
) -> str:
    """Bericht als Markdown (Schrittzusammenfassung der CI und Artefakt)."""
    szenario_budget: Mapping[str, int] = budget.get("szenarien_p95_ms", {})
    endpunkt_budget: Mapping[str, int] = budget.get("endpunkte_p95_ms", {})
    fehler_max = float(budget.get("fehlerquote_max_prozent", 1.0))
    if lauf.stufen:
        ergebnis = "Kapazitätsmessung" if bewertung.bestanden else "KEINE MESSUNG"
    else:
        ergebnis = "Budgets eingehalten" if bewertung.bestanden else "BUDGET VERLETZT"
    zeilen = [
        f"## Lasttest „{lauf.profil}“: {ergebnis}",
        "",
        "| Kennzahl | Wert |",
        "|---|---|",
        f"| Profil (Mengengerüst) | {lauf.profil} |",
    ]
    if lauf.stufen:
        zeilen.append(f"| Stufen (gleichzeitige Nutzer) | {', '.join(str(s.nutzer) for s in lauf.stufen)} |")
    elif lauf.nutzer:
        zeilen.append(
            f"| Gleichzeitige Nutzer, Anlauf je Sekunde, Laufzeit | {lauf.nutzer}, {lauf.anlauf}, {lauf.dauer} |"
        )
    if lauf.prozesse:
        zeilen.append(f"| Anwendungsprozesse (Daphne) | {lauf.prozesse} |")
    if lauf.umgebung:
        zeilen.append(f"| Umgebung | {lauf.umgebung} |")
    if gesamt is not None:
        zeilen += [
            f"| Anfragen gesamt | {_de(gesamt.anfragen)} |",
            f"| Durchsatz | {_de(gesamt.anfragen_pro_s, 1)} Anfragen/s |",
            f"| Fehlerquote | {_de(gesamt.fehlerquote, 2)} % (Budget {_de(fehler_max, 1)} %) |",
            f"| Median / p95 / p99 gesamt | {_ms(gesamt.median_ms)} / {_ms(gesamt.p95_ms)} / {_ms(gesamt.p99_ms)} ms |",
        ]

    if lauf.stufen:
        grenze = kapazitaet(lauf.stufen)
        zeilen += [
            "",
            "### Stufenlast (Kapazität)",
            "",
            f"Je Stufe ohne die ersten {EINSCHWINGEN_S} s nach dem Wechsel. Getragen heißt: p95 bis "
            f"{_de(KAPAZITAET_P95_MS)} ms und Fehlerquote bis {_de(KAPAZITAET_FEHLER_PROZENT, 1)} %. "
            "CPU in Prozent eines Kerns.",
            "",
            "| Nutzer | Anfragen/s | Fehlerquote | Median | p95 | CPU Anwendung | CPU Datenbank | getragen |",
            "|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for s in lauf.stufen:
            cpu_a = _de(s.cpu_anwendung) + " %" if s.cpu_anwendung is not None else "–"
            cpu_d = _de(s.cpu_datenbank) + " %" if s.cpu_datenbank is not None else "–"
            zeilen.append(
                f"| {s.nutzer} | {_de(s.anfragen_pro_s, 1)} | {_de(s.fehlerquote, 2)} % | {_ms(s.median_ms)} "
                f"| {_ms(s.p95_ms)} | {cpu_a} | {cpu_d} | {'ja' if s.getragen else 'nein'} |"
            )
        zeilen += [
            "",
            (
                f"Kapazität: **{grenze.nutzer} gleichzeitige Nutzer** bei {_de(grenze.anfragen_pro_s, 1)} Anfragen/s "
                f"(p95 {_ms(grenze.p95_ms)} ms)."
                if grenze
                else "Kapazität: Schon die erste Stufe liegt außerhalb der Zielwerte."
            ),
        ]

    zeilen += [
        "",
        "### Szenarien",
        "",
        "p95 aus allen Einzelwerten des Szenarios; Zeiten in Millisekunden.",
        "",
        "| Szenario | Anfragen | Fehler | Anfragen/s | Median | p95 | p99 | Max | Budget p95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in szenarien:
        grenze = szenario_budget.get(s.name)
        zeilen.append(
            f"| {s.name} | {_de(s.anfragen)} | {_de(s.fehler)} | {_de(s.anfragen_pro_s, 2)} | {_ms(s.median_ms)} "
            f"| {_ms(s.p95_ms)} | {_ms(s.p99_ms)} | {_ms(s.max_ms)} | {_de(grenze) if grenze else '–'} |"
        )

    zeilen += [
        "",
        "### Endpunkte",
        "",
        "| Endpunkt | Anfragen | Fehler | Anfragen/s | Median | p95 | p99 | Max | Budget p95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for e in sorted(endpunkte, key=lambda m: m.name):
        grenze = endpunkt_budget.get(e.name)
        zeilen.append(
            f"| `{e.name}` | {_de(e.anfragen)} | {_de(e.fehler)} | {_de(e.anfragen_pro_s, 2)} | {_ms(e.median_ms)} "
            f"| {_ms(e.p95_ms)} | {_ms(e.p99_ms)} | {_ms(e.max_ms)} | {_de(grenze) if grenze else '–'} |"
        )

    if lauf.bedingt:
        zeilen += [
            "",
            "### Bedingte Anfragen (ETag)",
            "",
            "Wiederholte Abrufe mit `If-None-Match`; unveränderte Listen beantwortet die Schnittstelle mit "
            "`304 Not Modified` ohne Inhalt.",
            "",
            "| Endpunkt | Anfragen | davon 304 | Anteil |",
            "|---|---:|---:|---:|",
        ]
        for name, werte in sorted(lauf.bedingt.items()):
            anfragen = int(werte.get("anfragen", 0))
            nicht_geaendert = int(werte.get("nicht_geaendert", 0))
            anteil = 100 * nicht_geaendert / anfragen if anfragen else 0.0
            zeilen.append(f"| `{name}` | {_de(anfragen)} | {_de(nicht_geaendert)} | {_de(anteil, 1)} % |")

    if ressourcen:
        zeilen += [
            "",
            "### Ressourcen",
            "",
            "CPU in Prozent eines Kerns (100 % = ein voller Kern), Speicher in MB.",
            "",
            "| Komponente | Messgröße | Mittel | Maximum |",
            "|---|---|---:|---:|",
        ]
        for r in ressourcen:
            zeilen.append(f"| {r.komponente} | {r.messgroesse} | {_de(r.mittel, 1)} | {_de(r.maximum, 1)} |")

    zeilen += ["", "### Ergebnis", ""]
    if bewertung.verletzungen:
        zeilen.append("Budget verletzt:")
        zeilen += [f"- {v}" for v in bewertung.verletzungen]
    elif lauf.stufen:
        zeilen.append("Kapazitätsmessung ohne Budgetprüfung.")
    else:
        zeilen.append("Alle Budgets eingehalten.")
    if bewertung.hinweise:
        zeilen += ["", "Hinweise:"]
        zeilen += [f"- {h}" for h in bewertung.hinweise]
    return "\n".join(zeilen) + "\n"


# =============================================================================
# Budgets und Laufparameter
# =============================================================================


def budgets_lesen(pfad: Path = BUDGETS) -> dict[str, Any]:
    daten: dict[str, Any] = json.loads(pfad.read_text(encoding="utf-8"))
    return daten


def profil_budget(budgets: Mapping[str, Any], profil: str) -> dict[str, Any]:
    profile: Mapping[str, Any] = budgets.get("profile", {})
    if profil not in profile:
        raise SystemExit(f"Profil „{profil}“ fehlt in budgets.json (vorhanden: {', '.join(sorted(profile))})")
    budget: dict[str, Any] = profile[profil]
    return budget


def laufparameter(budget: Mapping[str, Any], ueberschreibungen: Mapping[str, str | None]) -> dict[str, str]:
    """Laufparameter des Profils; gesetzte Überschreibungen (Eingaben des manuellen Starts) gehen vor."""
    lauf: Mapping[str, Any] = budget.get("lauf", {})
    ergebnis: dict[str, str] = {}
    for schluessel in LAUFPARAMETER:
        wert = ueberschreibungen.get(schluessel) or lauf.get(schluessel)
        if wert is None or str(wert).strip() == "":
            raise SystemExit(f"Laufparameter „{schluessel}“ fehlt")
        ergebnis[schluessel] = str(wert).strip()
    if not ergebnis["nutzer"].isdigit() or not ergebnis["prozesse"].isdigit() or not ergebnis["anlauf"].isdigit():
        raise SystemExit("nutzer, anlauf und prozesse müssen ganze Zahlen sein")
    if not ergebnis["dauer"][:-1].isdigit() or ergebnis["dauer"][-1] not in "smh":
        raise SystemExit("dauer im Format von Locust angeben, z. B. 180s, 3m oder 1h")
    # Einstellungen der Datenbank wie in der Größenempfehlung (docs/LASTTESTS.md), als Startargumente
    postgres: Mapping[str, Any] = lauf.get("postgres", {})
    for name, wert in postgres.items():
        if not all(z.isalnum() or z in "_." for z in f"{name}{wert}"):
            raise SystemExit(f"Ungültige PostgreSQL-Einstellung: {name}={wert}")
    ergebnis["postgres"] = " ".join(f"-c {name}={wert}" for name, wert in postgres.items())
    # Stufenlast (Kapazitätsmessung): Nutzerzahl je Stufe statt fester Zahl, Laufzeit aus den Stufen
    stufen = stufen_lesen(str(ueberschreibungen.get("stufen") or ""))
    stufendauer = str(ueberschreibungen.get("stufendauer") or "120").strip()
    if not stufendauer.isdigit() or not 2 * EINSCHWINGEN_S <= int(stufendauer) <= 1800:
        raise SystemExit(f"stufendauer in Sekunden angeben, {2 * EINSCHWINGEN_S} bis 1800")
    ergebnis["stufen"] = ",".join(str(nutzer) for nutzer in stufen)
    ergebnis["stufendauer"] = stufendauer
    if stufen:
        ergebnis["nutzer"] = str(max(stufen))
        # Puffer: Locust beendet über die Stufen, die Laufzeit ist nur die harte Obergrenze
        ergebnis["dauer"] = f"{len(stufen) * int(stufendauer) + 30}s"
    return ergebnis


# =============================================================================
# Kommandozeile
# =============================================================================


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    befehle = parser.add_subparsers(dest="befehl", required=True)

    p_param = befehle.add_parser("parameter", help="Laufparameter eines Profils ausgeben (schluessel=wert)")
    p_param.add_argument("--profil", required=True)
    p_param.add_argument("--budgets", type=Path, default=BUDGETS)
    for schluessel in LAUFPARAMETER:
        p_param.add_argument(f"--{schluessel}", default="")
    p_param.add_argument("--stufen", default="", help="Stufenlast, z. B. 25,50,100 (leer: feste Nutzerzahl)")
    p_param.add_argument("--stufendauer", default="", help="Sekunden je Stufe (Vorgabe 120)")

    p_bericht = befehle.add_parser("bericht", help="Bericht schreiben und Budgets prüfen")
    p_bericht.add_argument("--profil", required=True)
    p_bericht.add_argument("--ergebnisse", required=True, help="Präfix der Ergebnisdateien (Locust --csv)")
    p_bericht.add_argument("--budgets", type=Path, default=BUDGETS)
    p_bericht.add_argument("--bericht", type=Path, help="Bericht zusätzlich in diese Datei schreiben")
    p_bericht.add_argument("--umgebung", default="")
    for schluessel in LAUFPARAMETER:
        p_bericht.add_argument(f"--{schluessel}", default="")

    args = parser.parse_args(argv)
    budget = profil_budget(budgets_lesen(args.budgets), args.profil)

    if args.befehl == "parameter":
        werte = laufparameter(budget, {s: getattr(args, s) for s in (*LAUFPARAMETER, "stufen", "stufendauer")})
        for schluessel, wert in werte.items():
            print(f"{schluessel}={wert}")
        return 0

    praefix = args.ergebnisse
    stats = Path(f"{praefix}_stats.csv")
    if not stats.exists():
        print(f"Keine Locust-Ergebnisse unter {stats} – lief Locust mit --csv {praefix}?", file=sys.stderr)
        return 1
    endpunkte, gesamt = stats_einlesen(_csv_lesen(stats))
    szenario_datei = Path(f"{praefix}_szenarien.json")
    szenario_daten: dict[str, Any] = (
        json.loads(szenario_datei.read_text(encoding="utf-8")) if szenario_datei.exists() else {}
    )
    szenarien = szenarien_einlesen(szenario_daten)
    ressourcen_datei = Path(f"{praefix}_ressourcen.csv")
    messreihe = _csv_lesen(ressourcen_datei) if ressourcen_datei.exists() else []
    ressourcen = ressourcen_zusammenfassen(messreihe)
    stufen = stufen_einlesen(szenario_daten, messreihe)

    lauf = Lauf(
        profil=args.profil,
        nutzer=args.nutzer,
        anlauf=args.anlauf,
        dauer=args.dauer,
        prozesse=args.prozesse,
        umgebung=args.umgebung,
        bedingt=szenario_daten.get("bedingt", {}),
        stufen=stufen,
    )
    bewertung = bewerten_kapazitaet(gesamt, stufen) if stufen else bewerten(gesamt, endpunkte, szenarien, budget)
    text = bericht(lauf, gesamt, endpunkte, szenarien, ressourcen, bewertung, budget)
    print(text)
    if args.bericht:
        args.bericht.write_text(text, encoding="utf-8")
    return 0 if bewertung.bestanden else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
