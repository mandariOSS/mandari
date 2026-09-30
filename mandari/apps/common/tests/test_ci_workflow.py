# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Workflow mit Pfadfilter und Sammel-Ergebnis (Issue #621), Link-Prüfung und Badges (Issue #690).

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


def _link_pruefungen() -> list[dict[str, Any]]:
    return [dict(s) for s in _jobs()["verweise"]["steps"] if "lychee" in s.get("uses", "")]


def test_link_pruefung_ist_auf_commit_und_version_gepinnt() -> None:
    # Issue #690: Drittanbieter-Action wie dorny/paths-filter auf den Commit, lychee auf eine feste Version
    schritte = _link_pruefungen()
    assert len(schritte) == 2, "Erwartet: interne Verweise (offline) und externe Verweise"
    for schritt in schritte:
        assert re.fullmatch(r"lycheeverse/lychee-action@[0-9a-f]{40}", schritt["uses"]), schritt["uses"]
        assert re.fullmatch(r"v\d+\.\d+\.\d+", schritt["with"]["lycheeVersion"]), schritt["with"]


def test_link_pruefung_intern_offline_extern_nicht_bei_push() -> None:
    intern, extern = _link_pruefungen()
    assert "--offline" in intern["with"]["args"] and "--include-fragments" in intern["with"]["args"]
    assert "--offline" not in extern["with"]["args"]
    # Externe Verweise nicht bei jedem push auf dev/main abrufen (sparsam, keine Flake-Quelle)
    assert "github.event_name != 'push'" in extern["if"]


def test_externe_link_pruefung_nennt_vorhandene_dateien_des_auftritts() -> None:
    dateien = _link_pruefungen()[1]["with"]["args"].split()
    for datei in dateien:
        assert (REPO / datei).is_file(), f"{datei} existiert nicht – umbenannt?"
    pflicht = {
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/config.yml",
    }
    vorlagen = {f".github/ISSUE_TEMPLATE/{p.name}" for p in (REPO / ".github" / "ISSUE_TEMPLATE").iterdir()}
    assert pflicht | vorlagen <= set(dateien), sorted((pflicht | vorlagen) - set(dateien))


def test_readme_badges_zeigen_einen_pruefbaren_stand() -> None:
    # Issue #690: Ein statischer Badge „REUSE konform“ verlinkte ins Leere und stimmte nicht.
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    workflows = set(re.findall(r"actions/workflows?/(?:status/mandariOSS/mandari/)?([\w.-]+\.yml)", readme))
    assert "pr-check.yml" in workflows
    # REUSE: Status des eigenen Workflows oder, nach der Anmeldung beim Dienst der FSFE, deren Live-Badge
    live_badge = "https://api.reuse.software/badge/github.com/mandariOSS/mandari"
    assert "reuse.yml" in workflows or live_badge in readme
    for name in workflows:
        assert (REPO / ".github" / "workflows" / name).is_file(), f"Badge zeigt auf fehlenden Workflow {name}"
    # Statische Badges dürfen keinen Zustand behaupten (nur die Lizenz ist eine feste Angabe)
    statisch = re.findall(r"img\.shields\.io/badge/([^\"?]+)", readme)
    assert all(badge.startswith("License-") for badge in statisch), statisch


def test_reuse_workflow_prueft_blockierend() -> None:
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "reuse.yml").read_text(encoding="utf-8"))
    schritte = workflow["jobs"]["reuse"]["steps"]
    lint = [s for s in schritte if "reuse lint" in s.get("run", "")]
    assert len(lint) == 1
    # Der Badge zeigt den Status dieses Workflows; mit continue-on-error wäre er immer grün
    assert not lint[0].get("continue-on-error") and not workflow["jobs"]["reuse"].get("continue-on-error")
    assert re.search(r"pip install reuse==\d", lint[0]["run"]), "reuse auf eine feste Version pinnen"


@pytest.mark.parametrize(
    ("pfad", "erwartet"),
    [
        ("README.md", {"verweise"}),
        ("docs/RELEASE_POLITIK.md", {"verweise"}),
        ("lychee.toml", {"qualitaet", "verweise", "test"}),
        (
            "mandari/apps/session/services/insight_service.py",
            {"qualitaet", "python", "test", "smoke", "oparl", "codeql_python"},
        ),
        # OParl-Ausgaben: externer Validator gegen die Testinstanz
        ("mandari/hub/api/serialization.py", {"qualitaet", "python", "test", "smoke", "oparl", "codeql_python"}),
        (
            "mandari/apps/session/api/oparl.py",
            {"qualitaet", "python", "test", "smoke", "oparl", "e2e", "codeql_python"},
        ),
        ("scripts/oparl_validator.py", {"qualitaet", "test", "oparl", "codeql_python"}),
        ("docs/OPARL_API.md", {"verweise"}),
        ("mandari/apps/session/tests/test_meetings.py", {"qualitaet", "python", "test", "codeql_python"}),
        ("ingestor/src/sync/orchestrator.py", {"qualitaet", "vertrag", "ingestor", "codeql_python"}),
        # Journal der Ereignistechnik auf der Seite des Ingestors: zusätzlich die Django-Tests (PostgreSQL)
        ("ingestor/src/storage/models.py", {"qualitaet", "vertrag", "ingestor", "journal", "codeql_python"}),
        ("ingestor/src/storage/events.py", {"qualitaet", "vertrag", "ingestor", "journal", "codeql_python"}),
        ("ingestor/src/storage/database.py", {"qualitaet", "vertrag", "ingestor", "codeql_python"}),
        ("ingestor/tests/test_loeschmarkierung.py", {"qualitaet", "ingestor", "codeql_python"}),
        ("scripts/smoke_tombstones.py", {"qualitaet", "test", "smoke", "codeql_python"}),
        ("mandari/Dockerfile", {"qualitaet", "docker"}),
        (".github/workflows/pr-check.yml", {"sicherheitsnetz", "qualitaet", "test"}),
        # Lock-Datei der Django-Anwendung und ihr Export: Daraus installieren alle Jobs (Sicherheitsnetz)
        ("mandari/uv.lock", {"sicherheitsnetz", "qualitaet", "test", "smoke"}),
        ("scripts/export_requirements.sh", {"sicherheitsnetz", "qualitaet", "test", "audit"}),
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


def test_journal_des_ingestors_loest_die_django_tests_aus() -> None:
    """
    Der Ingestor schreibt ins Journal der Ereignistechnik. Ob seine INSERT-Anweisung zur Tabelle aus
    den Django-Migrationen passt (Trigger, Prüfbedingungen), prüft ein Test, der PostgreSQL braucht
    und deshalb nur im Job ``test`` läuft. Reine Ingestor-Änderungen lösen diesen Job sonst nicht aus.
    """
    bedingung = _jobs()["test"]["if"]
    assert "needs.changes.outputs.test == 'true'" in bedingung
    assert "needs.changes.outputs.journal == 'true'" in bedingung
    assert "||" in bedingung and "&&" not in bedingung
    # Der Job hat die Datenbank, und der Test steht dort, wo pytest ihn findet.
    assert "postgres" in _jobs()["test"]["services"]
    vertragstest = REPO / "mandari" / "insight_core" / "tests" / "test_schema_contract.py"
    assert "def test_ingestor_insert_gegen_das_journal_aus_den_migrationen" in vertragstest.read_text(encoding="utf-8")
    # Das Journal beschreibt der Ingestor in genau diesen Dateien.
    assert "class JournalEvent" in (REPO / "ingestor/src/storage/models.py").read_text(encoding="utf-8")
    assert "JournalEvent" in (REPO / "ingestor/src/storage/events.py").read_text(encoding="utf-8")


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
