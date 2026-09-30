# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Workflow mit Pfadfilter und Sammel-Ergebnis (Issue #621).

Die Jobs in ``.github/workflows/pr-check.yml`` laufen nur, wenn der Job ``changes`` ihren Bereich
als betroffen meldet; ``ci-ergebnis`` fasst alles zusammen und soll später der einzige Pflicht-Check
sein. Fehler in dieser Verdrahtung fallen in keinem Lauf auf – ein Tippfehler im Bereichsnamen lässt
einen Job nie mehr starten, ein vergessener Eintrag unter ``needs`` lässt einen roten Job am
Pflicht-Check vorbei. Diese Tests halten die Verdrahtung fest.

Dazu prüfen sie die Zuordnung selbst: Die Muster werden wie bei ``dorny/paths-filter`` (picomatch mit
``dot: true``, Quantor ``some-with-excludes``) ausgewertet. Für alle Dateien des Repositorys ist das
gegen picomatch 2.3.1 abgeglichen; die Nachbildung deckt nur die hier genutzten Formen ab (``**``,
``*``, ``?``, einfache ``{a,b}``, führendes ``!``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
WORKFLOW = REPO / ".github" / "workflows" / "pr-check.yml"

pytestmark = pytest.mark.skipif(not WORKFLOW.exists(), reason="Workflow nur im Repository, nicht im Image")


def _workflow() -> dict[Any, Any]:
    daten: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return daten


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = _workflow()["jobs"]
    return jobs


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _schritt(job: str, schritt_id: str) -> dict[str, Any]:
    for schritt in _jobs()[job]["steps"]:
        if schritt.get("id") == schritt_id:
            return dict(schritt)
    raise AssertionError(f"Schritt {schritt_id} fehlt im Job {job}")


def _filter() -> dict[str, list[str]]:
    filter_: dict[str, list[str]] = yaml.safe_load(_schritt("changes", "filter")["with"]["filters"])
    return filter_


def _bereiche_im_ergebnis_schritt() -> list[str]:
    skript = _schritt("changes", "ergebnis")["run"]
    treffer = re.search(r"for bereich in ([a-z0-9_ ]+); do", skript)
    assert treffer, "Schleife über die Bereiche nicht gefunden"
    return treffer.group(1).split()


def _klammern_aufloesen(muster: str) -> list[str]:
    """``a/{b,c}.py`` → ``a/b.py``, ``a/c.py`` (picomatch kennt nur diese einfache Form)."""
    treffer = re.search(r"\{([^{}]*)\}", muster)
    if not treffer:
        return [muster]
    return [
        aufgeloest
        for teil in treffer.group(1).split(",")
        for aufgeloest in _klammern_aufloesen(muster[: treffer.start()] + teil + muster[treffer.end() :])
    ]


def _als_regex(muster: str) -> re.Pattern[str]:
    """Glob wie bei picomatch mit ``dot: true``: ``**`` über Verzeichnisse, ``*``/``?`` innerhalb."""
    teile: list[str] = []
    segmente = muster.split("/")
    for nummer, segment in enumerate(segmente):
        letztes = nummer == len(segmente) - 1
        if segment == "**":
            teile.append(".*" if letztes else "(?:[^/]+/)*")
            continue
        regex = "".join("[^/]*" if z == "*" else "[^/]" if z == "?" else re.escape(z) for z in segment)
        teile.append(regex if letztes else regex + "/")
    return re.compile("".join(teile))


def _passt(pfad: str, muster: str) -> bool:
    # Klassen und Extglobs bildet _als_regex nicht nach; dann lieber scheitern als falsch zuordnen
    assert not re.search(r"[\[\]()]", muster), f"Glob-Form nicht nachgebildet, Test erweitern: {muster}"
    return any(_als_regex(einzeln).fullmatch(pfad) for einzeln in _klammern_aufloesen(muster))


def _bereiche(pfad: str) -> set[str]:
    """Filter, denen die Datei zugeordnet wird (Quantor some-with-excludes von dorny/paths-filter)."""
    bereiche = set()
    for name, muster in _filter().items():
        positiv = [m for m in muster if not m.startswith("!")]
        negativ = [m[1:] for m in muster if m.startswith("!")]
        if any(_passt(pfad, m) for m in positiv) and not any(_passt(pfad, m) for m in negativ):
            bereiche.add(name)
    return bereiche


def test_ci_ergebnis_laeuft_immer_und_wartet_auf_alle_jobs() -> None:
    jobs = _jobs()
    ergebnis = jobs["ci-ergebnis"]
    assert ergebnis["if"] == "always()"
    assert _needs(ergebnis) == set(jobs) - {"ci-ergebnis"}, "Neuer Job fehlt unter ci-ergebnis.needs"


def test_jeder_job_haengt_am_pfadfilter() -> None:
    for name, job in _jobs().items():
        if name == "changes":
            continue
        assert "changes" in _needs(job), f"{name} braucht needs: changes"


def test_jobs_fragen_nur_vorhandene_bereiche_ab() -> None:
    ausgaben = set(_jobs()["changes"]["outputs"])
    abgefragt = set(re.findall(r"needs\.changes\.outputs\.([a-z0-9_]+)", WORKFLOW.read_text(encoding="utf-8")))
    assert abgefragt, "Keine Bereichsabfragen gefunden"
    assert abgefragt <= ausgaben, f"Unbekannte Bereiche: {sorted(abgefragt - ausgaben)}"


def test_jeder_bereich_hat_filter_und_ausgabe() -> None:
    ausgaben = set(_jobs()["changes"]["outputs"]) - {"codeql"}
    schleife = set(_bereiche_im_ergebnis_schritt())
    filter_ = set(_filter())
    assert schleife == ausgaben, "Ausgaben von changes und Schleife im Ergebnis-Schritt laufen auseinander"
    assert schleife <= filter_, f"Bereiche ohne Filter: {sorted(schleife - filter_)}"
    # Übrige Filter: das Sicherheitsnetz und die CodeQL-Sprachen, die zur Sprachliste werden
    assert filter_ - schleife == {"sicherheitsnetz", "codeql_python", "codeql_js"}


def test_pull_requests_ohne_workflow_weiten_pfadfilter() -> None:
    # PyYAML liest den Schlüssel "on" als True (YAML 1.1)
    workflow = _workflow()
    ausloeser = workflow.get("on") or workflow[True]
    pull_request = ausloeser["pull_request"] or {}
    assert "paths" not in pull_request and "paths-ignore" not in pull_request, (
        "Ein paths-Filter am pull_request lässt ci-ergebnis ausbleiben; Pfade gehören in den Job changes"
    )


def test_filter_nennen_nur_vorhandene_dateien() -> None:
    # Eine umbenannte Datei fiele still aus dem Filter (Sicherheitsnetz, Views der E2E-Seiten …)
    assert ".github/**" in _filter()["sicherheitsnetz"]
    for bereich, muster_liste in _filter().items():
        for muster in muster_liste:
            for einzeln in _klammern_aufloesen(muster.removeprefix("!")):
                if "*" in einzeln or "?" in einzeln:
                    continue
                assert (REPO / einzeln).is_file(), f"{bereich}: {einzeln} existiert nicht – umbenannt?"


def test_pfadfilter_action_ist_auf_einen_commit_gepinnt() -> None:
    # Die Action entscheidet, welche Prüfungen überhaupt starten; ein verschobener Tag wirkt sofort
    uses = _schritt("changes", "filter")["uses"]
    assert re.fullmatch(r"dorny/paths-filter@[0-9a-f]{40}", uses), uses


@pytest.mark.parametrize(
    ("pfad", "erwartet"),
    [
        ("README.md", set()),
        ("mandari/apps/session/services/insight_service.py", {"qualitaet", "python", "test", "smoke", "codeql_python"}),
        ("mandari/apps/session/tests/test_meetings.py", {"qualitaet", "python", "test", "codeql_python"}),
        ("ingestor/src/sync/orchestrator.py", {"qualitaet", "vertrag", "ingestor", "codeql_python"}),
        ("ingestor/tests/test_loeschmarkierung.py", {"qualitaet", "ingestor", "codeql_python"}),
        ("scripts/smoke_tombstones.py", {"qualitaet", "test", "smoke", "codeql_python"}),
        ("mandari/Dockerfile", {"qualitaet", "docker"}),
        (".github/workflows/pr-check.yml", {"sicherheitsnetz", "qualitaet", "test"}),
        (
            "mandari/templates/work/base_work.html",
            {"qualitaet", "templates", "test", "smoke", "e2e", "codeql_js"},
        ),
        (
            "mandari/frontend/js/main.ts",
            {"qualitaet", "frontend", "test", "smoke", "e2e", "docker", "codeql_js"},
        ),
        # Views der Seiten, die E2E aufruft
        (
            "mandari/apps/work/meetings/views/prepare.py",
            {"qualitaet", "python", "test", "smoke", "e2e", "codeql_python"},
        ),
        (
            "mandari/apps/session/views/resolutions.py",
            {"qualitaet", "python", "test", "smoke", "e2e", "codeql_python"},
        ),
        (
            "mandari/insight_core/views/decisions.py",
            {"qualitaet", "python", "test", "smoke", "e2e", "codeql_python"},
        ),
        # Andere Views nicht: Die deckt E2E erst auf dev ab
        ("mandari/apps/session/views/voting.py", {"qualitaet", "python", "test", "smoke", "codeql_python"}),
    ],
)
def test_zuordnung_ausgewaehlter_dateien(pfad: str, erwartet: set[str]) -> None:
    assert _bereiche(pfad) == erwartet


def _pfade_im_skript(quelltext: str) -> set[str]:
    """Repo-Pfade der Form ``ROOT / "a" / "b"`` (ROOT = ``Path(__file__).resolve().parent.parent``)."""
    zeichenketten = r"((?:\s*/\s*\"[^\"]+\")+)"

    def teile(kette: str) -> list[str]:
        return re.findall(r'"([^"]+)"', kette)

    basis: dict[str, list[str]] = {
        name: [] for name in re.findall(r"^(\w+) = Path\(__file__\)\.resolve\(\)\.parent\.parent$", quelltext, re.M)
    }
    for name, von, kette in re.findall(rf"^(\w+) = (\w+){zeichenketten}$", quelltext, re.M):
        if von in basis:
            basis[name] = basis[von] + teile(kette)
    pfade = set()
    for von, kette in re.findall(rf"\b(\w+){zeichenketten}", quelltext):
        if von in basis:
            pfade.add("/".join(basis[von] + teile(kette)))
    return pfade


def test_smoke_skripte_lesen_nur_dateien_aus_dem_smoke_bereich() -> None:
    # Ein Smoke-Skript, das etwa Ingestor-Quelltext liest, liefe bei einer reinen Ingestor-Änderung
    # nicht mit; die Regression fiele erst nach dem Merge auf. Solche Prüfungen gehören in die Tests
    # des jeweiligen Bereichs (so wie ingestor/tests/test_loeschmarkierung.py).
    skripte = [p for p in sorted((REPO / "scripts").glob("*.py")) if "smoke" in _bereiche(f"scripts/{p.name}")]
    assert len(skripte) > 50, "Smoke-Skripte nicht gefunden"
    ausserhalb = {
        f"{skript.name}: {pfad}"
        for skript in skripte
        for pfad in _pfade_im_skript(skript.read_text(encoding="utf-8"))
        if (REPO / pfad).is_file() and "smoke" not in _bereiche(pfad)
    }
    assert not ausserhalb, (
        f"Smoke-Skripte lesen Dateien, deren Änderung keinen Smoke-Lauf auslöst: {sorted(ausserhalb)}"
    )
