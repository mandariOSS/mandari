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

import json
import os
import re
import shutil
import subprocess
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
    pull_request = _ausloeser()["pull_request"] or {}
    assert "paths" not in pull_request and "paths-ignore" not in pull_request, (
        "Ein paths-Filter am pull_request lässt ci-ergebnis ausbleiben; Pfade gehören in den Job changes"
    )


def _ausloeser() -> dict[str, Any]:
    # PyYAML liest den Schlüssel "on" als True (YAML 1.1)
    workflow = _workflow()
    ausloeser: dict[str, Any] = workflow.get("on") or workflow[True]
    return ausloeser


def test_merge_queue_loest_die_ci_aus() -> None:
    """Issue #737: Ohne merge_group berichtet "CI-Ergebnis" in der Queue nie, und kein PR würde gemergt."""
    merge_group = _ausloeser()["merge_group"] or {}
    assert merge_group.get("types", ["checks_requested"]) == ["checks_requested"]
    assert "paths" not in merge_group and "paths-ignore" not in merge_group
    assert "branches" not in merge_group, "Die Queue jedes geschützten Zweigs soll prüfen"
    # Läufe der Queue werden nicht von einem neueren Push abgebrochen (eigene Ref je Eintrag)
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def _bash() -> str | None:
    if os.name == "nt":
        kandidat = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe"
        return str(kandidat) if kandidat.exists() else None
    return shutil.which("bash")


def _ergebnis_schritt_ausfuehren(
    tmp_path: Path, ereignis: str, filter_json: str, migrationstests: str = ""
) -> dict[str, str]:
    """Führt das Skript des Schritts "Ergebnis festlegen" wie in GitHub Actions aus (bash, jq)."""
    bash = _bash()
    if bash is None or shutil.which("jq") is None:
        pytest.skip("bash oder jq nicht vorhanden (in der CI beides da)")
    tmp_path.mkdir(parents=True, exist_ok=True)
    skript = tmp_path / "ergebnis.sh"
    skript.write_text(_schritt("changes", "ergebnis")["run"], encoding="utf-8", newline="\n")
    ausgabe = tmp_path / "output"
    env = {
        **os.environ,
        "EREIGNIS": ereignis,
        "ZIELZWEIG": "",
        "FILTER": filter_json,
        "MIGRATIONSTESTS": migrationstests,
        "CODEQL_ERWEITERT": "",
        "GITHUB_OUTPUT": str(ausgabe),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    subprocess.run([bash, "-e", str(skript)], env=env, check=True, capture_output=True, timeout=60)  # noqa: S603
    return dict(zeile.split("=", 1) for zeile in ausgabe.read_text(encoding="utf-8").splitlines())


def test_merge_queue_laesst_alle_jobs_ausser_den_migrationstests_laufen(tmp_path: Path) -> None:
    # In der Queue laufen alle Jobs ohne Rücksicht auf den Filter; nur die Migrationstests folgen ihm (Issue #935)
    werte = _ergebnis_schritt_ausfuehren(tmp_path, "merge_group", "{}")
    bereiche = _bereiche_im_ergebnis_schritt()
    assert {b: werte[b] for b in bereiche} == {b: "false" if b == "migrationen" else "true" for b in bereiche}

    werte = _ergebnis_schritt_ausfuehren(tmp_path / "pr", "pull_request", '{"verweise": "true"}')
    assert werte["verweise"] == "true" and werte["test"] == "false", "Gegenprobe: im PR wirkt der Filter"


@pytest.mark.parametrize("ereignis", ["pull_request", "merge_group"])
@pytest.mark.parametrize(
    ("filter_json", "migrationstests", "erwartet"),
    [
        ('{"test": "true"}', "false", "false"),
        ('{"test": "true", "migrationen": "true"}', "false", "true"),
        ('{"test": "true"}', "true", "true"),  # geänderte Testdatei mit Migrationstest (Schritt davor)
        ('{"sicherheitsnetz": "true"}', "false", "true"),
    ],
)
def test_migrationstests_im_pull_request_und_in_der_queue_nach_derselben_regel(
    tmp_path: Path, ereignis: str, filter_json: str, migrationstests: str, erwartet: str
) -> None:
    werte = _ergebnis_schritt_ausfuehren(tmp_path, ereignis, filter_json, migrationstests)
    assert werte["migrationen"] == erwartet


@pytest.mark.parametrize("ereignis", ["push", "schedule", "workflow_dispatch"])
def test_volllaeufe_fuehren_auch_die_migrationstests_aus(tmp_path: Path, ereignis: str) -> None:
    werte = _ergebnis_schritt_ausfuehren(tmp_path, ereignis, "{}")
    assert werte["migrationen"] == "true" and werte["test"] == "true"


def test_kein_lauf_bei_push_auf_dev() -> None:
    """Issue #935: Die Queue hat genau das Commit geprüft, das auf dev landet; main läuft weiter bei jedem Push."""
    push = _ausloeser()["push"]
    assert push["branches"] == ["main"]
    assert "workflow_dispatch" in _ausloeser() and "schedule" in _ausloeser()
    nachtlauf = yaml.safe_load((REPO / ".github" / "workflows" / "nachtlauf.yml").read_text(encoding="utf-8"))
    schritte = [s["run"] for job in nachtlauf["jobs"].values() for s in job["steps"] if "run" in s]
    assert schritte == ['gh workflow run pr-check.yml --repo "$GITHUB_REPOSITORY" --ref dev']
    assert (nachtlauf.get("on") or nachtlauf[True])["schedule"], "Nachtlauf braucht einen Zeitplan"


def _pytest_aufruf(job: str) -> str:
    aufrufe = [s["run"] for s in _jobs()[job]["steps"] if re.search(r"^\s*pytest ", s.get("run", ""), re.M)]
    assert len(aufrufe) == 1, f"{job}: genau ein pytest-Aufruf erwartet"
    return " ".join(aufrufe[0].replace("\\\n", " ").split())


def test_test_job_in_teilen_ohne_migrationstests_mit_vorlage() -> None:
    job = _jobs()["test"]
    teile = job["strategy"]["matrix"]["teil"]
    assert teile == list(range(1, len(teile) + 1)) and len(teile) >= 2
    assert job["strategy"]["fail-fast"] is False
    aufruf = _pytest_aufruf("test")
    assert '-m "not migrationen"' in aufruf
    assert f"--teil ${{{{ matrix.teil }}}}/{len(teile)}" in aufruf
    assert "--teil-protokoll" in aufruf and "--cov=apps" in aufruf
    vorlage = [s for s in job["steps"] if "MANDARI_TEST_DB_VORLAGE" in s.get("env", {})]
    assert len(vorlage) == 1 and "pytest " in vorlage[0]["run"]
    # Das Ergebnis prüft genau so viele Teile, wie die Matrix startet
    pruefung = [s["run"] for s in _jobs()["test-ergebnis"]["steps"] if "ci_testteile.py" in s.get("run", "")]
    assert len(pruefung) == 1 and f"--teile {len(teile)}" in pruefung[0]


def test_migrationstests_im_eigenen_job() -> None:
    job = _jobs()["migrationstests"]
    assert job["if"] == "needs.changes.outputs.migrationen == 'true'"
    aufruf = _pytest_aufruf("migrationstests")
    assert "-m migrationen" in aufruf and "--teil-protokoll" in aufruf and "--cov=apps" in aufruf
    assert "postgres" in job["services"]


def test_test_ergebnis_prueft_coverage_auch_ohne_migrationsjob() -> None:
    """Ohne !cancelled() übersprünge GitHub den Job mit dem nicht nötigen Migrationsjob – samt Coverage-Grenze."""
    job = _jobs()["test-ergebnis"]
    assert _needs(job) == {"changes", "test", "migrationstests"}
    assert job["if"].startswith("${{ !cancelled() && (")
    assert "needs.changes.outputs.test == 'true' || needs.changes.outputs.journal == 'true'" in job["if"]
    befehle = "\n".join(s.get("run", "") for s in job["steps"])
    grenze = re.search(r"coverage report .*--fail-under=(\d+)", befehle)
    assert grenze, "Coverage-Grenze fehlt im Job Test-Ergebnis"
    # Issue #157: Die Schwelle darf nur steigen
    assert int(grenze.group(1)) >= 38


def _ci_ergebnis_ausfuehren(tmp_path: Path, ergebnisse: dict[str, str], test: str, migrationen: str) -> int:
    bash = _bash()
    if bash is None or shutil.which("jq") is None:
        pytest.skip("bash oder jq nicht vorhanden (in der CI beides da)")
    skript = tmp_path / "ci_ergebnis.sh"
    skript.write_text(_jobs()["ci-ergebnis"]["steps"][0]["run"], encoding="utf-8", newline="\n")
    alle = dict.fromkeys(_needs(_jobs()["ci-ergebnis"]), "skipped") | {"changes": "success"} | ergebnisse
    env = {
        **os.environ,
        "ERGEBNISSE": json.dumps({job: {"result": ergebnis} for job, ergebnis in alle.items()}),
        "TEST_NOETIG": test,
        "MIGRATIONEN_NOETIG": migrationen,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    lauf = subprocess.run([bash, str(skript)], env=env, capture_output=True, timeout=60, check=False)  # noqa: S603
    return lauf.returncode


def test_ci_ergebnis_verlangt_noetige_test_jobs(tmp_path: Path) -> None:
    gruen = {"test": "success", "test-ergebnis": "success"}
    assert _ci_ergebnis_ausfuehren(tmp_path, gruen, "true", "false") == 0
    # Übersprungen zählt nicht, wenn der Job nötig war
    assert _ci_ergebnis_ausfuehren(tmp_path, {"test": "success"}, "true", "false") == 1
    assert _ci_ergebnis_ausfuehren(tmp_path, gruen, "true", "true") == 1
    assert _ci_ergebnis_ausfuehren(tmp_path, gruen | {"migrationstests": "success"}, "true", "true") == 0
    # Nicht nötig: übersprungen ist in Ordnung, ein Fehlschlag nie
    assert _ci_ergebnis_ausfuehren(tmp_path, {}, "false", "false") == 0
    assert _ci_ergebnis_ausfuehren(tmp_path, {"migrationstests": "failure"}, "false", "false") == 1


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
    # und nicht in der Merge-Queue: ein langsamer fremder Server soll keinen Merge aufhalten (Issue #737)
    assert "github.event_name != 'merge_group'" in extern["if"]


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
        # Ereignisverträge sind JSON: Sie lösen die Prüfung der Verträge aus (Issue #519)
        (
            "mandari/hub/contracts/schemas/ris.paper.changed/v1.json",
            {"qualitaet", "vertrag", "test", "smoke", "oparl"},
        ),
        ("scripts/check_event_contracts.py", {"qualitaet", "vertrag", "test", "codeql_python"}),
        ("scripts/smoke_tombstones.py", {"qualitaet", "test", "smoke", "codeql_python"}),
        ("mandari/Dockerfile", {"qualitaet", "docker"}),
        # Migrationen und die Werkzeuge der Migrationstests lösen den Job "Migrationstests" aus (Issue #935)
        (
            "mandari/apps/tenants/migrations/0001_initial.py",
            {"qualitaet", "python", "test", "migrationen", "smoke", "codeql_python"},
        ),
        ("mandari/conftest.py", {"qualitaet", "python", "test", "migrationen", "smoke", "e2e", "codeql_python"}),
        (".github/workflows/pr-check.yml", {"sicherheitsnetz", "qualitaet", "test"}),
        # Lock-Datei der Django-Anwendung und ihr Export: Daraus installieren alle Jobs (Sicherheitsnetz); dazu
        # die SHACL-Prüfung der Datenkataloge, weil ein Update von rdflib die Serialisierung ändern kann
        ("mandari/uv.lock", {"sicherheitsnetz", "qualitaet", "test", "smoke", "oparl"}),
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
