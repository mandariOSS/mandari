# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Issues schließen, die ein gemergter Pull Request in seinem Text schließen soll.

    python scripts/issues_schliessen.py --vorher SHA --nachher SHA [--trockenlauf]

Seit der Merge-Queue (Issue #737) übernimmt der Squash-Commit nur den Titel des Pull Requests. GitHub
schließt Issues aber nur über Schlüsselwörter im Commit, das auf ``main`` landet; ein „Closes #N“ im
PR-Text bewirkt deshalb nichts mehr. Dieses Skript läuft bei jedem push auf ``main``
(``.github/workflows/issues-schliessen.yml``), liest für jedes neue Commit den zugehörigen Pull Request und
schließt die Issues, die dessen Text mit einem Schlüsselwort nennt.

Regeln:

- Schlüsselwörter wie bei GitHub: close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved
  (Groß-/Kleinschreibung egal, optional mit Doppelpunkt), je Issue ein Schlüsselwort: ``Closes #12``.
- Nur Issues dieses Repositorys (``#12`` oder ``owner/repo#12``); Verweise in andere Repositorys bleiben offen.
- „Teil von #12“ und jeder andere Verweis ohne Schlüsselwort schließt nichts.
- Text in HTML-Kommentaren und Code (```…``` und `…`) zählt nicht; so schließt weder die PR-Vorlage
  noch ein Beispiel in einer Beschreibung etwas.
- Was schon im Commit selbst steht (etwa „(Closes #935)“ im Titel), schließt GitHub selbst und bleibt hier außen vor.
- Bereits geschlossene Issues und Pull Requests werden übersprungen.
- Erst schließen, dann kommentieren: Ein wiederholter Lauf findet das Issue geschlossen und schreibt keinen
  zweiten Hinweis.
- Fehler bei einem Issue (gelöscht, verschoben, keine Rechte) werden als Warnung gemeldet; der Lauf geht weiter.

Benötigt ``gh`` mit ``GH_TOKEN`` (Rechte: issues write, pull-requests read, contents read) und
``GITHUB_REPOSITORY`` (``owner/repo``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

NULL_SHA = "0" * 40
SEITENGROESSE = 100

_SCHLUESSELWORT = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_KOMMENTAR = re.compile(r"<!--.*?-->", re.S)
_CODEBLOCK = re.compile(r"(`{3,}|~{3,}).*?\1", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_PR_IM_TITEL = re.compile(r"\(#(\d+)\)\s*$")


def _ohne_kommentare_und_code(text: str) -> str:
    text = _KOMMENTAR.sub(" ", text)
    text = _CODEBLOCK.sub(" ", text)
    return _INLINE_CODE.sub(" ", text)


def schliessende_verweise(text: str | None, repo: str) -> set[int]:
    """Nummern der Issues dieses Repositorys, die ``text`` mit einem Schlüsselwort schließt."""
    if not text:
        return set()
    muster = re.compile(
        rf"(?<![\w/-]){_SCHLUESSELWORT}(?:\s*:\s*|\s+)(?:{re.escape(repo)})?#(\d+)\b",
        re.IGNORECASE,
    )
    return {int(nummer) for nummer in muster.findall(_ohne_kommentare_und_code(text))}


def pr_nummer_aus_titel(nachricht: str) -> int | None:
    """``(#123)`` am Ende der ersten Zeile, wie es der Squash-Merge anhängt."""
    erste_zeile = nachricht.splitlines()[0] if nachricht else ""
    treffer = _PR_IM_TITEL.search(erste_zeile)
    return int(treffer.group(1)) if treffer else None


def _gh(*argumente: str) -> Any:
    lauf = subprocess.run(["gh", "api", *argumente], capture_output=True, text=True, encoding="utf-8", check=False)  # noqa: S603, S607
    if lauf.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(argumente)}: {lauf.stderr.strip()}")
    return json.loads(lauf.stdout) if lauf.stdout.strip() else None


def _commits(repo: str, vorher: str, nachher: str) -> list[dict[str, Any]]:
    """Die neuen Commits des push (älteste zuerst); ohne Vorgänger nur das letzte."""
    if vorher and vorher != NULL_SHA:
        try:
            return _verglichene_commits(repo, vorher, nachher)
        except RuntimeError as fehler:
            # Etwa nach einem force-push, wenn ``vorher`` nicht mehr existiert
            print(f"::warning::Vergleich {vorher[:7]}...{nachher[:7]} nicht möglich, nur letztes Commit: {fehler}")
    commit = _gh(f"repos/{repo}/commits/{nachher}")
    return [{"sha": commit["sha"], "nachricht": commit["commit"]["message"]}]


def _verglichene_commits(repo: str, vorher: str, nachher: str) -> list[dict[str, Any]]:
    """Alle Commits zwischen ``vorher`` und ``nachher``, seitenweise (die API liefert je Seite höchstens 100)."""
    commits: list[dict[str, Any]] = []
    gesamt: int | None = None
    seite = 1
    while True:
        vergleich = _gh(f"repos/{repo}/compare/{vorher}...{nachher}?per_page={SEITENGROESSE}&page={seite}")
        if gesamt is None:
            gesamt = vergleich.get("total_commits")
        neu = vergleich.get("commits") or []
        commits.extend({"sha": c["sha"], "nachricht": c["commit"]["message"]} for c in neu)
        if gesamt is None or len(neu) < SEITENGROESSE or len(commits) >= gesamt:
            break
        seite += 1
    if gesamt is not None and len(commits) < gesamt:
        print(
            f"::warning::Vergleich {vorher[:7]}...{nachher[:7]} meldet {gesamt} Commits, geliefert wurden "
            f"{len(commits)}; ältere Commits bitte per workflow_dispatch nachholen."
        )
    return commits


def _issue_im_repo(issue: dict[str, Any], repo: str, nummer: int) -> bool:
    """Ein verschobenes Issue leitet ``gh api`` in ein anderes Repository weiter; das gehört nicht uns."""
    repository_url = issue.get("repository_url")
    if repository_url and not repository_url.lower().endswith(f"/repos/{repo}".lower()):
        return False
    return issue.get("number", nummer) == nummer


def _pull_requests(repo: str, commit: dict[str, Any]) -> list[dict[str, Any]]:
    """Gemergte Pull Requests zum Commit; ersatzweise die Nummer aus ``(#N)`` im Titel."""
    try:
        prs = [pr for pr in _gh(f"repos/{repo}/commits/{commit['sha']}/pulls") if pr.get("merged_at")]
    except RuntimeError:
        prs = []
    if prs:
        return prs
    nummer = pr_nummer_aus_titel(commit["nachricht"])
    if nummer is None:
        return []
    try:
        pr = _gh(f"repos/{repo}/pulls/{nummer}")
    except RuntimeError:
        return []
    return [pr] if pr.get("merged_at") else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vorher", default="", help="SHA vor dem push (github.event.before)")
    parser.add_argument("--nachher", required=True, help="SHA nach dem push (github.event.after)")
    parser.add_argument("--trockenlauf", action="store_true", help="nur anzeigen, nichts schließen")
    args = parser.parse_args(argv)

    repo = os.environ["GITHUB_REPOSITORY"]
    geschlossen: list[str] = []
    warnungen: list[str] = []
    commits = _commits(repo, args.vorher, args.nachher)
    print(f"{len(commits)} Commit(s) geprüft")
    for commit in commits:
        im_commit = schliessende_verweise(commit["nachricht"], repo)
        for pr in _pull_requests(repo, commit):
            for nummer in sorted(schliessende_verweise(pr.get("body"), repo) - im_commit):
                kurz = commit["sha"][:7]
                try:
                    issue = _gh(f"repos/{repo}/issues/{nummer}")
                except RuntimeError as fehler:
                    # Gelöscht (410), unbekannt (404) oder nicht lesbar: melden und weitermachen
                    warnungen.append(f"#{nummer} (PR #{pr['number']}): nicht lesbar")
                    print(f"::warning::#{nummer} (aus PR #{pr['number']}) nicht lesbar, übersprungen: {fehler}")
                    continue
                if not isinstance(issue, dict) or not _issue_im_repo(issue, repo, nummer):
                    warnungen.append(f"#{nummer} (PR #{pr['number']}): verschoben")
                    print(f"::warning::#{nummer} (aus PR #{pr['number']}) liegt nicht mehr in {repo}, übersprungen")
                    continue
                if "pull_request" in issue:
                    print(f"#{nummer} (aus PR #{pr['number']}) ist ein Pull Request, übersprungen")
                    continue
                if issue["state"] != "open":
                    print(f"#{nummer} (aus PR #{pr['number']}) ist schon geschlossen")
                    continue
                if args.trockenlauf:
                    print(f"Trockenlauf: würde #{nummer} schließen (PR #{pr['number']}, {kurz})")
                    continue
                # Erst schließen: Scheitert danach der Hinweis, schreibt ein neuer Lauf keinen doppelten
                try:
                    _gh(
                        f"repos/{repo}/issues/{nummer}",
                        "-X",
                        "PATCH",
                        "-f",
                        "state=closed",
                        "-f",
                        "state_reason=completed",
                    )
                except RuntimeError as fehler:
                    warnungen.append(f"#{nummer} (PR #{pr['number']}): Schließen fehlgeschlagen")
                    print(f"::warning::#{nummer} (aus PR #{pr['number']}) ließ sich nicht schließen: {fehler}")
                    continue
                geschlossen.append(f"#{nummer} (PR #{pr['number']}, {kurz})")
                print(f"#{nummer} geschlossen (PR #{pr['number']}, {kurz})")
                try:
                    _gh(
                        f"repos/{repo}/issues/{nummer}/comments",
                        "-f",
                        f"body=Erledigt mit PR #{pr['number']} ({commit['sha']}), jetzt auf `main`.",
                    )
                except RuntimeError as fehler:
                    warnungen.append(f"#{nummer} (PR #{pr['number']}): Hinweis fehlt")
                    print(f"::warning::#{nummer} geschlossen, Hinweis nicht geschrieben: {fehler}")

    zusammenfassung = os.environ.get("GITHUB_STEP_SUMMARY")
    if zusammenfassung:
        with open(zusammenfassung, "a", encoding="utf-8") as datei:
            datei.write("### Issues aus PR-Texten\n\n")
            datei.write("\n".join(f"- {eintrag}" for eintrag in geschlossen) or "Keine zu schließen.")
            datei.write("\n")
            if warnungen:
                datei.write("\n**Warnungen**\n\n" + "\n".join(f"- {eintrag}" for eintrag in warnungen) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
