# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teile des Jobs "Test" und den Job "Migrationstests" zusammen prüfen (Issue #935).

    python scripts/ci_testteile.py VERZEICHNIS --teile 3 [--migrationen-noetig true|false]
                                   [--dauern-ausgabe DATEI] [--zusammenfassung DATEI]

VERZEICHNIS enthält je Job ein Unterverzeichnis mit ``auswahl.json`` und ``ergebnis.json`` (geschrieben von
``mandari/apps/common/tests/testlauf.py`` mit ``--teil-protokoll``). Geprüft wird:

- Es gibt genau die Teile 1 bis N, alle mit derselben Sammlung (``alle``) und denselben Kandidaten.
- Die Teile sind überschneidungsfrei und ergeben zusammen genau die Kandidaten; Kandidaten und Migrationstests
  ergeben zusammen genau die Sammlung. So kann kein Test zwischen den Teilen verloren gehen.
- Jeder ausgewählte Test hat ein Ergebnis, keiner ist fehlgeschlagen.
- Lief der Job "Migrationstests", hat er genau die Migrationstests ausgeführt. War er nötig, muss er gelaufen sein.

Die Tabelle (Tests je Teil) geht nach stdout und, falls angegeben, an die Zusammenfassung des Laufs.
``--dauern-ausgabe`` schreibt die gemessenen Sekunden je Datei im Format von ``mandari/testdauern.json``;
die CI legt sie als Artefakt ab, damit die Aufteilung bei Bedarf erneuert werden kann.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HAUPTTEILE = "not migrationen"
MIGRATIONSTESTS = "migrationen"


def _lesen(pfad: Path) -> dict[str, Any]:
    daten: dict[str, Any] = json.loads(pfad.read_text(encoding="utf-8"))
    return daten


def _protokolle(verzeichnis: Path) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    gefunden = []
    for auswahl in sorted(verzeichnis.rglob("auswahl.json")):
        ergebnis = auswahl.with_name("ergebnis.json")
        gefunden.append(
            (auswahl.parent.name, _lesen(auswahl), _lesen(ergebnis) if ergebnis.is_file() else {"ergebnisse": {}})
        )
    return gefunden


def pruefen(
    verzeichnis: Path, teile: int, migrationen_noetig: bool
) -> tuple[list[str], list[str], dict[str, float], float]:
    """Fehler, Tabellenzeilen, Sekunden je Datei (Hauptteile) und mittlere Dauer je Test."""
    fehler: list[str] = []
    zeilen = [
        "| Job | Tests | bestanden | übersprungen | fehlgeschlagen | Testzeit (s) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    haupt = []
    migration = []
    for name, auswahl, ergebnis in _protokolle(verzeichnis):
        if auswahl.get("auswahl") == HAUPTTEILE:
            haupt.append((name, auswahl, ergebnis))
        elif auswahl.get("auswahl") == MIGRATIONSTESTS:
            migration.append((name, auswahl, ergebnis))
        else:
            fehler.append(f"{name}: unerwartete Auswahl -m {auswahl.get('auswahl')!r}")

    nummern = sorted(a["teil"] for _, a, _ in haupt)
    if nummern != list(range(1, teile + 1)) or any(a["teile"] != teile for _, a, _ in haupt):
        fehler.append(f"Teile {nummern} statt 1 bis {teile}")
    if not haupt:
        return [*fehler, "Keine Protokolle der Testteile gefunden"], zeilen, {}, 0.0

    alle = haupt[0][1]["alle"]
    kandidaten = haupt[0][1]["kandidaten"]
    migrationstests = haupt[0][1]["migrationstests"]
    for name, auswahl, _ in haupt[1:] + migration:
        if auswahl["alle"] != alle:
            fehler.append(f"{name}: andere Sammlung als {haupt[0][0]} ({len(auswahl['alle'])} statt {len(alle)})")
    for name, auswahl, _ in haupt[1:]:
        if auswahl["kandidaten"] != kandidaten:
            fehler.append(f"{name}: andere Kandidaten als {haupt[0][0]}")

    zaehler = Counter(kennung for _, auswahl, _ in haupt for kennung in auswahl["ausgewaehlt"])
    doppelt = sorted(k for k, n in zaehler.items() if n > 1)
    fehlend = sorted(set(kandidaten) - set(zaehler))
    fremd = sorted(set(zaehler) - set(kandidaten))
    for text, liste in (("in mehreren Teilen", doppelt), ("in keinem Teil", fehlend), ("kein Kandidat", fremd)):
        if liste:
            fehler.append(f"{len(liste)} Tests {text}, z. B. {liste[:3]}")
    if sorted([*kandidaten, *migrationstests]) != sorted(alle):
        fehler.append("Kandidaten der Teile und Migrationstests ergeben zusammen nicht die gesammelten Tests")

    dauern: dict[str, float] = {}
    gesamt = Counter[str]()
    jobs = [(f"Test, Teil {a['teil']}/{a['teile']}", n, a, e) for n, a, e in sorted(haupt, key=lambda t: t[1]["teil"])]
    jobs += [("Migrationstests", n, a, e) for n, a, e in migration]
    for bezeichnung, name, auswahl, ergebnis in jobs:
        ergebnisse: dict[str, str] = ergebnis["ergebnisse"]
        ohne = sorted(set(auswahl["ausgewaehlt"]) - set(ergebnisse))
        if ohne:
            fehler.append(f"{name}: {len(ohne)} ausgewählte Tests ohne Ergebnis, z. B. {ohne[:3]}")
        stand = Counter(ergebnisse.values())
        if stand["fehlgeschlagen"] or stand["fehler"]:
            fehler.append(f"{name}: {stand['fehlgeschlagen'] + stand['fehler']} Tests fehlgeschlagen")
        sekunden = sum(ergebnis.get("dauern", {}).values())
        zeilen.append(
            f"| {bezeichnung} | {len(auswahl['ausgewaehlt'])} | {stand['bestanden']} | {stand['uebersprungen']} "
            f"| {stand['fehlgeschlagen'] + stand['fehler']} | {sekunden:.0f} |"
        )
        gesamt.update(stand)
        gesamt["tests"] += len(auswahl["ausgewaehlt"])
        if auswahl.get("auswahl") == HAUPTTEILE:
            dauern.update(ergebnis.get("dauern", {}))

    if migration:
        ausgefuehrt = sorted(k for _, a, _ in migration for k in a["ausgewaehlt"])
        if ausgefuehrt != sorted(migrationstests):
            fehler.append(
                f"Job Migrationstests: {len(ausgefuehrt)} Tests statt der {len(migrationstests)} gekennzeichneten"
            )
    elif migrationen_noetig:
        fehler.append("Der Job Migrationstests war nötig, hat aber kein Protokoll geliefert")
    else:
        zeilen.append(f"| Migrationstests (nicht nötig, nicht ausgeführt) | {len(migrationstests)} | – | – | – | – |")

    zeilen.append(
        f"| **Summe** | **{gesamt['tests']}** von {len(alle)} gesammelten | {gesamt['bestanden']} "
        f"| {gesamt['uebersprungen']} | {gesamt['fehlgeschlagen'] + gesamt['fehler']} | |"
    )
    tests_haupt = sum(len(a["ausgewaehlt"]) for _, a, _ in haupt)
    je_test = sum(dauern.values()) / tests_haupt if tests_haupt else 1.0
    return fehler, zeilen, dauern, je_test


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Teile des Jobs Test und den Job Migrationstests prüfen")
    parser.add_argument("verzeichnis", type=Path)
    parser.add_argument("--teile", type=int, required=True)
    parser.add_argument("--migrationen-noetig", choices=["true", "false"], default="false")
    parser.add_argument("--dauern-ausgabe", type=Path)
    parser.add_argument("--zusammenfassung", type=Path)
    args = parser.parse_args(argv)

    fehler, zeilen, dauern, je_test = pruefen(args.verzeichnis, args.teile, args.migrationen_noetig == "true")
    text = "\n".join(["### Tests je Teil (Issue #935)", "", *zeilen, ""])
    print(text)
    if args.zusammenfassung:
        with args.zusammenfassung.open("a", encoding="utf-8") as datei:
            datei.write(text + "\n")
    if args.dauern_ausgabe and dauern:
        args.dauern_ausgabe.write_text(
            json.dumps(
                {
                    "_hinweis": "Sekunden je Testdatei aus einem CI-Lauf (scripts/ci_testteile.py); Grundlage von --teil",
                    "je_test": round(je_test, 4),
                    "dateien": {datei: round(sekunden, 1) for datei, sekunden in sorted(dauern.items())},
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    for meldung in fehler:
        print(f"::error::{meldung}")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
