# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Skripte parallel ausführen (CI).

Jedes Smoke-Skript legt seine SQLite-Datenbank in einem eigenen temporären Verzeichnis an
(bzw. kopiert die Schema-Vorlage aus ``MANDARI_SMOKE_DB_TEMPLATE`` dorthin) und ist damit
unabhängig von den anderen. Nacheinander brauchten die rund 60 Skripte gut fünf Minuten; auf
vier Kernen parallel etwa ein Viertel davon.

Die Ausgabe jedes Skripts erscheint gesammelt, fehlgeschlagene zuletzt und vollständig. Der
Rückgabewert ist 1, sobald ein Skript fehlschlägt.

    python scripts/run_smokes.py [-j 4] scripts/smoke_a.py scripts/smoke_b.py …
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass
class Ergebnis:
    skript: str
    code: int
    sekunden: float
    ausgabe: str


def ausfuehren(skript: str, zeitlimit: int) -> Ergebnis:
    beginn = time.monotonic()
    try:
        lauf = subprocess.run(
            [sys.executable, skript],
            capture_output=True,
            text=True,
            timeout=zeitlimit,
            env=os.environ.copy(),
        )
        code, ausgabe = lauf.returncode, lauf.stdout + lauf.stderr
    except subprocess.TimeoutExpired as abbruch:
        teil = (abbruch.stdout or b"") + (abbruch.stderr or b"")
        text = teil.decode(errors="replace") if isinstance(teil, bytes) else str(teil)
        code, ausgabe = 124, f"{text}\nZeitlimit von {zeitlimit} s überschritten."
    return Ergebnis(skript, code, time.monotonic() - beginn, ausgabe)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skripte", nargs="+")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--zeitlimit", type=int, default=900, help="Sekunden je Skript")
    args = parser.parse_args()

    beginn = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        ergebnisse = list(pool.map(lambda s: ausfuehren(s, args.zeitlimit), args.skripte))

    fehler = [e for e in ergebnisse if e.code != 0]
    for e in sorted(ergebnisse, key=lambda e: e.code != 0):
        status = "OK " if e.code == 0 else f"FEHLER (Code {e.code})"
        print(f"::group::{status} {e.skript} ({e.sekunden:.0f} s)")
        print(e.ausgabe.rstrip())
        print("::endgroup::")
    for e in fehler:
        print(f"\n===== FEHLGESCHLAGEN: {e.skript} =====\n{e.ausgabe.rstrip()}")

    print(
        f"\n{len(ergebnisse) - len(fehler)} von {len(ergebnisse)} Smoke-Skripten bestanden "
        f"in {time.monotonic() - beginn:.0f} s ({args.jobs} parallel)."
    )
    langsam = sorted(ergebnisse, key=lambda e: e.sekunden, reverse=True)[:5]
    print("Langsamste: " + ", ".join(f"{e.skript.rsplit('/', 1)[-1]} {e.sekunden:.0f} s" for e in langsam))
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
