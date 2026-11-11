# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine in ``dev`` gemergte Korrektur in die unterstützten Pflegelinien übernehmen (Rückportierung).

    python scripts/rueckportieren.py <PR-Nummer> --grund sicherheit-hoch [--linie 0.12 ...] [--trocken]

Regeln: docs/RELEASE_POLITIK.md (Abschnitt 5) und docs/WEITERENTWICKLUNG.md („Rückportierung“).

Ablauf je Pflegelinie ``release/v<Linie>``:

1. Fehlt der Zweig der Linie, wird er auf dem Commit des Tags ``v<Linie>.0`` angelegt.
2. In einem eigenen, temporären Worktree entsteht der Zweig ``rueckport/<PR>-v<Linie>``; darauf wird der
   Squash-Commit des Pull Requests mit ``git cherry-pick -x`` übernommen.
3. Zweig pushen, Pull Request gegen ``release/v<Linie>`` mit Label ``rueckportierung`` öffnen.

Welche Linien unterstützt werden, steht in der Tabelle „Unterstützte Versionen“ in SECURITY.md (Zeilen
``| 0.12 (…) | … | TT.MM.JJJJ |``); abgelaufene Linien überspringt das Skript. ``--linie`` schränkt auf
einzelne Linien ein.

Das Skript bricht ab, wenn der Pull Request nicht in ``dev`` gemergt ist oder Datenbankmigrationen enthält
(Rückportierungen enthalten keine Migration, Entscheidung der Projektleitung). Bei einem Konflikt bleibt der
Worktree stehen; das Skript nennt den Pfad und die Befehle zum Abschließen. Es mergt nie selbst.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECURITY_MD = REPO / "SECURITY.md"
GRUENDE = ("sicherheit-kritisch", "sicherheit-hoch", "sicherheit-mittel", "kritischer-fehler")
LABEL = "rueckportierung"

# | 0.12 (Pflegeversion 4. Quartal 2026) | Sicherheitskorrekturen … | 17.05.2027 |
ZEILE = re.compile(r"^\|\s*(?P<linie>\d+\.\d+)\b[^|]*\|[^|]*\|\s*(?P<bis>\d{2}\.\d{2}\.\d{4})\s*\|\s*$")


class RueckportError(Exception):
    """Fehler, der den Lauf beendet; die Meldung geht an die Person, die das Skript aufruft."""


def pflegelinien(text: str, heute: dt.date) -> list[tuple[str, dt.date]]:
    """Unterstützte Pflegelinien aus der Tabelle in SECURITY.md, ohne abgelaufene, älteste zuerst."""
    linien = []
    for zeile in text.splitlines():
        treffer = ZEILE.match(zeile.strip())
        if not treffer:
            continue
        bis = dt.datetime.strptime(treffer["bis"], "%d.%m.%Y").date()
        if bis >= heute:
            linien.append((treffer["linie"], bis))
    return sorted(linien, key=lambda eintrag: tuple(int(teil) for teil in eintrag[0].split(".")))


def enthaelt_migration(dateien: list[str]) -> bool:
    return any("/migrations/" in datei and datei.endswith(".py") for datei in dateien)


def _pull_request_zweige(workflow: str) -> list[str]:
    """Die Muster unter ``on.pull_request.branches`` in ihrer Reihenfolge, Block- oder Flussliste.

    Kommentare und andere Ereignisse (``push``) zählen nicht. Zeilenweise statt mit PyYAML, damit das Skript
    ohne zusätzliche Pakete läuft.
    """
    muster: list[str] = []
    ereignis = None  # Name des Ereignisses unter "on:", in dem die Zeile steht
    ereignis_einzug = -1
    in_on = in_zweigen = False
    for roh in workflow.splitlines():
        zeile = roh.split("#", 1)[0].rstrip()
        if not zeile.strip():
            continue
        einzug = len(zeile) - len(zeile.lstrip())
        inhalt = zeile.strip()
        if einzug == 0:
            in_on = inhalt in ("on:", "'on':", '"on":')
            ereignis, in_zweigen = None, False
            continue
        if not in_on:
            continue
        if ereignis is None or einzug <= ereignis_einzug:
            ereignis, ereignis_einzug, in_zweigen = inhalt.rstrip(":"), einzug, False
            continue
        if inhalt == "branches:":
            in_zweigen = True
        elif inhalt.startswith("branches:") and "[" in inhalt:
            # Flussliste: branches: [dev, 'release/v*']
            if ereignis == "pull_request":
                if inhalt.count("[") > 1 or inhalt.count("]") > 1:
                    muster.append("[")  # Klammern im Muster: Liste nicht sicher lesbar, siehe ci_ausloeser_vorhanden
                else:
                    eintraege = inhalt.split("[", 1)[1].split("]", 1)[0].split(",")
                    muster += [e.strip().strip("'\"") for e in eintraege if e.strip()]
            in_zweigen = False
        elif in_zweigen and inhalt.startswith("- "):
            if ereignis == "pull_request":
                muster.append(inhalt[2:].strip().strip("'\""))
        else:
            in_zweigen = False
    return muster


def ci_ausloeser_vorhanden(workflow: str, zweig: str = "release/v0.0") -> bool:
    """Löst der Workflow pr-check.yml die CI für Pull Requests gegen ``zweig`` sicher erkennbar aus?

    Ausgewertet werden nur einfache Muster mit ``*``. Enthält ein Muster einen Ausschluss (``!``), eine
    Zeichenklasse oder ``+``/``?`` (GitHub wertet sie anders als ``fnmatch``), gilt der Auslöser als nicht sicher
    erkennbar: lieber abbrechen und von Hand prüfen als einen PR ohne CI öffnen.
    """
    muster = _pull_request_zweige(workflow)
    if any(set(eintrag) & set("![]+?") for eintrag in muster):
        return False
    return any(fnmatch.fnmatchcase(zweig, eintrag) for eintrag in muster)


def _lauf(*befehl: str, cwd: Path = REPO, pruefen: bool = True) -> subprocess.CompletedProcess[str]:
    ergebnis = subprocess.run(befehl, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if pruefen and ergebnis.returncode != 0:
        raise RueckportError(f"{' '.join(befehl)} scheiterte:\n{ergebnis.stderr.strip() or ergebnis.stdout.strip()}")
    return ergebnis


def _pull_request(nummer: int) -> dict:
    daten = _lauf("gh", "pr", "view", str(nummer), "--json", "state,baseRefName,mergeCommit,title,url").stdout
    pr = json.loads(daten)
    if pr["state"] != "MERGED" or pr["baseRefName"] != "dev" or not pr.get("mergeCommit"):
        raise RueckportError(f"PR #{nummer} ist nicht in dev gemergt (Stand {pr['state']}, Basis {pr['baseRefName']}).")
    return pr


def _zweig_vorhanden(zweig: str) -> bool:
    return bool(_lauf("git", "ls-remote", "--heads", "origin", zweig).stdout.strip())


def _linie_anlegen(linie: str, trocken: bool) -> str:
    """Stellt sicher, dass die Pflegelinie existiert; liefert die Referenz mit ihrem Stand."""
    zweig = f"release/v{linie}"
    holen = f"+refs/heads/{zweig}:refs/remotes/origin/{zweig}"
    if _zweig_vorhanden(zweig):
        _lauf("git", "fetch", "--quiet", "origin", holen)
        return f"refs/remotes/origin/{zweig}"
    tag = f"v{linie}.0"
    print(f"  Pflegelinie {zweig} fehlt, lege sie auf {tag} an.")
    if trocken:
        return f"refs/tags/{tag}"
    _lauf("git", "push", "origin", f"refs/tags/{tag}^{{commit}}:refs/heads/{zweig}")
    _lauf("git", "fetch", "--quiet", "origin", holen)
    return f"refs/remotes/origin/{zweig}"


def _ci_pruefen(linie: str, referenz: str) -> None:
    workflow = _lauf("git", "show", f"{referenz}:.github/workflows/pr-check.yml", pruefen=False).stdout
    if not ci_ausloeser_vorhanden(workflow, f"release/v{linie}"):
        raise RueckportError(
            f"Die Pflegelinie release/v{linie} löst für Pull Requests gegen sich nicht erkennbar die CI aus "
            "(fehlender Zweig oder komplexe Muster mit !, [], +, ?). Zuerst einen PR "
            "gegen die Linie öffnen, der in .github/workflows/pr-check.yml unter pull_request den Zweig "
            "'release/v*' ergänzt (docs/WEITERENTWICKLUNG.md, Abschnitt 9), dann erneut aufrufen."
        )


def rueckportieren(nummer: int, grund: str, linien: list[str], trocken: bool) -> int:
    pr = _pull_request(nummer)
    sha = pr["mergeCommit"]["oid"]
    _lauf("git", "fetch", "--quiet", "--tags", "origin")
    # -z und quotepath=off: Pfade mit Umlauten unverändert, sonst entginge der Prüfung eine Migration
    ausgabe = _lauf("git", "-c", "core.quotepath=off", "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
    dateien = [datei for datei in ausgabe.stdout.split("\0") if datei]
    if enthaelt_migration(dateien):
        raise RueckportError(
            f"PR #{nummer} enthält eine Datenbankmigration. Rückportierungen enthalten keine Migration; "
            "die Projektleitung entscheidet im Einzelfall (docs/RELEASE_POLITIK.md, Abschnitt 5)."
        )

    fehler = 0
    for linie in linien:
        basis = f"release/v{linie}"
        zweig = f"rueckport/{nummer}-v{linie}"
        referenz = _linie_anlegen(linie, trocken)
        _ci_pruefen(linie, referenz)
        print(f"{basis}: übernehme {sha[:10]} aus #{nummer}")
        if trocken:
            continue
        ordner = Path(tempfile.mkdtemp(prefix=f"rueckport-{nummer}-v{linie}-"))
        _lauf("git", "worktree", "add", "-b", zweig, str(ordner), referenz)
        auswahl = _lauf("git", "cherry-pick", "-x", sha, cwd=ordner, pruefen=False)
        if auswahl.returncode != 0:
            fehler += 1
            print(
                f"  Konflikt. Worktree bleibt stehen: {ordner}\n"
                "  Konflikte lösen (Verhalten der Korrektur übernehmen, nichts anderes), dann:\n"
                f"    git -C {ordner} cherry-pick --continue\n"
                f"    python scripts/rueckportieren.py {nummer} --grund {grund} --linie {linie} --nur-pr\n"
            )
            continue
        _pr_oeffnen(nummer, pr, linie, ordner)
        _lauf("git", "worktree", "remove", str(ordner))
    return fehler


def _pr_oeffnen(nummer: int, pr: dict, linie: str, ordner: Path) -> None:
    # Der Text nennt bewusst keinen Grund und keinen Schweregrad: Der PR ist öffentlich, die Patch-Version
    # der Linie noch nicht erschienen (docs/WEITERENTWICKLUNG.md, Abschnitt 9).
    zweig = f"rueckport/{nummer}-v{linie}"
    _lauf("git", "push", "-u", "origin", zweig, cwd=ordner)
    text = (
        f"Rückportierung von #{nummer} in die Pflegelinie `release/v{linie}` "
        "(docs/RELEASE_POLITIK.md, Abschnitt 5).\n\n"
        f"- Original: {pr['url']}, Commit {pr['mergeCommit']['oid']}\n"
        "- Inhalt: unverändert übernommen (`cherry-pick -x`); geprüft ist die Korrektur im Original-PR. "
        "Hier zählt nur, dass der Diff dem Original entspricht und die CI grün ist.\n"
    )
    _lauf(
        "gh",
        "pr",
        "create",
        "--base",
        f"release/v{linie}",
        "--head",
        zweig,
        "--title",
        f"{pr['title']} (Rückportierung nach {linie})",
        "--body",
        text,
        "--label",
        LABEL,
        cwd=ordner,
    )
    print(f"  PR gegen release/v{linie} geöffnet.")


def nur_pr(nummer: int, linien: list[str]) -> None:
    """Nach gelöstem Konflikt: PR für den schon vorhandenen Worktree-Zweig öffnen."""
    pr = _pull_request(nummer)
    for linie in linien:
        _pr_oeffnen(nummer, pr, linie, REPO)
    print("Den stehen gebliebenen Worktree danach mit `git worktree remove <pfad>` entfernen.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=int, help="Nummer des in dev gemergten Pull Requests")
    parser.add_argument(
        "--grund", required=True, choices=GRUENDE, help="warum zurückportiert wird (nur Protokoll, nicht im PR)"
    )
    parser.add_argument("--linie", action="append", help="nur diese Pflegelinie(n), z. B. 0.12")
    parser.add_argument("--trocken", action="store_true", help="nur anzeigen, nichts anlegen")
    parser.add_argument("--nur-pr", action="store_true", help="nach gelöstem Konflikt nur den PR öffnen")
    for strom in (sys.stdout, sys.stderr):
        if isinstance(strom, io.TextIOWrapper):
            strom.reconfigure(encoding="utf-8")  # Umlaute auch in der Windows-Konsole
    args = parser.parse_args(argv)
    if args.nur_pr and args.trocken:
        parser.error("--nur-pr öffnet Pull Requests und lässt sich nicht mit --trocken verbinden")

    unterstuetzt = pflegelinien(SECURITY_MD.read_text(encoding="utf-8"), dt.date.today())
    linien = [linie for linie, _ in unterstuetzt]
    if args.linie:
        unbekannt = sorted(set(args.linie) - set(linien))
        if unbekannt:
            print(f"Nicht unterstützt (SECURITY.md): {', '.join(unbekannt)}", file=sys.stderr)
            return 2
        linien = [linie for linie in linien if linie in args.linie]
    if not linien:
        print("Keine unterstützte Pflegelinie; nichts zu tun.")
        return 0
    print("Pflegelinien: " + ", ".join(f"{linie} (bis {bis:%d.%m.%Y})" for linie, bis in unterstuetzt))
    try:
        if args.nur_pr:
            nur_pr(args.pr, linien)
            return 0
        return 1 if rueckportieren(args.pr, args.grund, linien, args.trocken) else 0
    except RueckportError as fehler:
        print(fehler, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
