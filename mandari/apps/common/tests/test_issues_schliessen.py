# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issues aus PR-Texten schließen (scripts/issues_schliessen.py, .github/workflows/issues-schliessen.yml).

Seit der Merge-Queue (Issue #737) trägt der Squash-Commit nur den PR-Titel; „Closes #N“ im PR-Text schließt
über GitHub nichts mehr. „Teil von #N“ darf nie schließen.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
SKRIPT = REPO / "scripts" / "issues_schliessen.py"
WORKFLOW = REPO / ".github" / "workflows" / "issues-schliessen.yml"
NAME = "mandariOSS/mandari"


def _skript() -> ModuleType:
    spec = importlib.util.spec_from_file_location("issues_schliessen", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("Closes #12", {12}),
        ("closes #12\nFixes: #13\nRESOLVED #14", {12, 13, 14}),
        ("Fix #1, fixed #2, resolve #3, close #4, closed #5", {1, 2, 3, 4, 5}),
        ("Closes mandariOSS/mandari#20", {20}),
        # Teilschritte und bloße Verweise schließen nichts
        ("Teil von #939", set()),
        ("Teil von #939, siehe #12 und PR #13", set()),
        # Verweise in andere Repositorys bleiben offen
        ("Closes mandariOSS/intern#5", set()),
        # PR-Vorlage, HTML-Kommentare und Code zählen nicht
        ("## Bezug zu Issue\n\nCloses #(Nummer)", set()),
        ("<!-- Closes #7 -->\nTeil von #8", set()),
        ("Beispiel: `Closes #9`", set()),
        ("```\nCloses #10\n```\nCloses #11", {11}),
        # Nur ganze Wörter: „prefixes #3“, „unfixed #4“ sind keine Schlüsselwörter
        ("prefixes #3, unfixed #4, closes-#5", set()),
        ("Closes #12abc", set()),
        ("", set()),
        (None, set()),
    ],
)
def test_schliessende_verweise(text: str | None, erwartet: set[int]) -> None:
    assert _skript().schliessende_verweise(text, NAME) == erwartet


def test_pr_nummer_aus_titel() -> None:
    skript = _skript()
    assert skript.pr_nummer_aus_titel("fix(ci): etwas (Teil von #939) (#959)\n\nCo-authored-by: x") == 959
    assert skript.pr_nummer_aus_titel("ohne Nummer") is None


class _GitHub:
    """Nachbau der benötigten API-Aufrufe für main()."""

    def __init__(self) -> None:
        self.issues = {
            12: {"state": "open"},
            13: {"state": "closed"},
            14: {"state": "open", "pull_request": {}},
            15: {"state": "open"},
            939: {"state": "open"},
        }
        self.aufrufe: list[tuple[str, ...]] = []

    def __call__(self, *argumente: str) -> Any:
        self.aufrufe.append(argumente)
        pfad = argumente[0].removeprefix(f"repos/{NAME}/")
        if pfad.startswith("compare/"):
            return {
                "commits": [
                    {"sha": "a" * 40, "commit": {"message": "feat: x (Teil von #939) (#100)"}},
                    {"sha": "b" * 40, "commit": {"message": "fix: y (Closes #15) (#101)"}},
                ]
            }
        if pfad == f"commits/{'a' * 40}/pulls":
            return [
                {"number": 100, "merged_at": "2026-10-10", "body": "Closes #12\nCloses #13\nFixes #14\nTeil von #939"}
            ]
        if pfad == f"commits/{'b' * 40}/pulls":
            return []
        if pfad == "pulls/101":
            return {"number": 101, "merged_at": "2026-10-10", "body": "Closes #15"}
        if pfad.startswith("issues/") and pfad.count("/") == 1:
            if len(argumente) == 1:
                return self.issues[int(pfad.split("/")[1])]
            return {}
        if pfad.endswith("/comments"):
            return {}
        raise AssertionError(argumente)

    def geschlossen(self) -> list[int]:
        return [int(a[0].rsplit("/", 1)[1]) for a in self.aufrufe if "PATCH" in a]


def test_main_schliesst_nur_offene_issues_aus_pr_texten(monkeypatch: pytest.MonkeyPatch) -> None:
    skript = _skript()
    github = _GitHub()
    monkeypatch.setattr(skript, "_gh", github)
    monkeypatch.setenv("GITHUB_REPOSITORY", NAME)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert skript.main(["--vorher", "c" * 40, "--nachher", "b" * 40]) == 0

    # 12 offen -> geschlossen; 13 schon zu; 14 ist ein PR; 939 nur „Teil von“;
    # 15 steht im Commit-Titel (schließt GitHub selbst), auch wenn der PR-Text es nennt
    assert github.geschlossen() == [12]
    kommentare = [a for a in github.aufrufe if a[0].endswith("/issues/12/comments")]
    assert len(kommentare) == 1 and "PR #100" in kommentare[0][2]


def test_trockenlauf_schliesst_nichts(monkeypatch: pytest.MonkeyPatch) -> None:
    skript = _skript()
    github = _GitHub()
    monkeypatch.setattr(skript, "_gh", github)
    monkeypatch.setenv("GITHUB_REPOSITORY", NAME)
    assert skript.main(["--vorher", "c" * 40, "--nachher", "b" * 40, "--trockenlauf"]) == 0
    assert github.geschlossen() == []
    assert not [a for a in github.aufrufe if a[0].endswith("/comments")]


def test_workflow_rechte_und_ausloeser() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML liest den Schlüssel „on“ als True
    ausloeser = workflow[True]
    assert ausloeser["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["schliessen"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "write"}
    assert "scripts/issues_schliessen.py" in job["steps"][-1]["run"]


class _Fehler(_GitHub):
    """Issue 12 gelöscht (410), 16 nach intern verschoben, 17 offen, bei 18 scheitert nur der Hinweis."""

    def __init__(self) -> None:
        super().__init__()
        self.issues[16] = {
            "state": "open",
            "number": 3,
            "repository_url": "https://api.github.com/repos/mandariOSS/intern",
        }
        self.issues[17] = {"state": "open", "number": 17, "repository_url": f"https://api.github.com/repos/{NAME}"}
        self.issues[18] = {"state": "open"}

    def __call__(self, *argumente: str) -> Any:
        pfad = argumente[0].removeprefix(f"repos/{NAME}/")
        if pfad == f"commits/{'a' * 40}/pulls":
            self.aufrufe.append(argumente)
            return [
                {"number": 100, "merged_at": "2026-10-10", "body": "Closes #12, closes #16, closes #17, closes #18"}
            ]
        if pfad == "issues/12" and len(argumente) == 1:
            self.aufrufe.append(argumente)
            raise RuntimeError("gh api repos/mandariOSS/mandari/issues/12: HTTP 410: This issue was deleted")
        if pfad == "issues/18/comments":
            self.aufrufe.append(argumente)
            raise RuntimeError("HTTP 502")
        return super().__call__(*argumente)


def test_fehler_je_issue_brechen_den_lauf_nicht_ab(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    skript = _skript()
    github = _Fehler()
    monkeypatch.setattr(skript, "_gh", github)
    monkeypatch.setenv("GITHUB_REPOSITORY", NAME)
    zusammenfassung = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(zusammenfassung))

    assert skript.main(["--vorher", "c" * 40, "--nachher", "b" * 40]) == 0

    # 12 gelöscht, 16 verschoben: Warnung, kein Schließen; 17 und 18 geschlossen
    assert github.geschlossen() == [17, 18]
    ausgabe = capsys.readouterr().out
    assert "::warning::#12" in ausgabe
    assert "::warning::#16" in ausgabe
    assert "::warning::#18 geschlossen, Hinweis nicht geschrieben" in ausgabe
    assert "Warnungen" in zusammenfassung.read_text(encoding="utf-8")


def test_erst_schliessen_dann_kommentieren(monkeypatch: pytest.MonkeyPatch) -> None:
    skript = _skript()
    github = _GitHub()
    monkeypatch.setattr(skript, "_gh", github)
    monkeypatch.setenv("GITHUB_REPOSITORY", NAME)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    skript.main(["--vorher", "c" * 40, "--nachher", "b" * 40])

    zu_12 = [i for i, a in enumerate(github.aufrufe) if a[0].endswith("/issues/12") and "PATCH" in a]
    hinweis_12 = [i for i, a in enumerate(github.aufrufe) if a[0].endswith("/issues/12/comments")]
    assert zu_12 and hinweis_12 and zu_12[0] < hinweis_12[0]

    # Zweiter Lauf: Issue ist zu, also weder erneut schließen noch ein zweiter Hinweis
    github.issues[12] = {"state": "closed"}
    github.aufrufe.clear()
    skript.main(["--vorher", "c" * 40, "--nachher", "b" * 40])
    assert github.geschlossen() == []
    assert not [a for a in github.aufrufe if a[0].endswith("/comments")]


def _vergleich(gesamt: int, seiten: dict[int, int]) -> tuple[Any, list[str]]:
    """Nachbau von compare mit ``seiten`` = {Seite: Anzahl Commits}."""
    aufrufe: list[str] = []

    def gh(*argumente: str) -> Any:
        aufrufe.append(argumente[0])
        seite = int(argumente[0].rsplit("page=", 1)[1])
        anzahl = seiten.get(seite, 0)
        return {
            "total_commits": gesamt,
            "commits": [{"sha": f"{seite}-{i}", "commit": {"message": "x"}} for i in range(anzahl)],
        }

    return gh, aufrufe


def test_vergleich_liest_alle_seiten(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    skript = _skript()
    gh, aufrufe = _vergleich(150, {1: 100, 2: 50})
    monkeypatch.setattr(skript, "_gh", gh)
    commits = skript._commits(NAME, "c" * 40, "b" * 40)
    assert len(commits) == 150
    assert len(aufrufe) == 2 and all("per_page=100" in a for a in aufrufe)
    assert "::warning::" not in capsys.readouterr().out


def test_vergleich_warnt_bei_fehlenden_commits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skript = _skript()
    # Die API meldet 300 Commits, liefert aber nur 250 (Obergrenze von compare)
    gh, _ = _vergleich(300, {1: 100, 2: 100, 3: 50})
    monkeypatch.setattr(skript, "_gh", gh)
    assert len(skript._commits(NAME, "c" * 40, "b" * 40)) == 250
    assert "meldet 300 Commits, geliefert wurden 250" in capsys.readouterr().out
