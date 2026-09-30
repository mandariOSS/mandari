# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Workflow mit Pfadfilter und Sammel-Ergebnis (Issue #621).

Die Jobs in ``.github/workflows/pr-check.yml`` laufen nur, wenn der Job ``changes`` ihren Bereich
als betroffen meldet; ``ci-ergebnis`` fasst alles zusammen und soll später der einzige Pflicht-Check
sein. Fehler in dieser Verdrahtung fallen in keinem Lauf auf – ein Tippfehler im Bereichsnamen lässt
einen Job nie mehr starten, ein vergessener Eintrag unter ``needs`` lässt einen roten Job am
Pflicht-Check vorbei. Diese Tests halten die Verdrahtung fest.
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


def test_sicherheitsnetz_nennt_nur_vorhandene_dateien() -> None:
    netz = _filter()["sicherheitsnetz"]
    assert ".github/**" in netz
    for muster in netz:
        if "*" in muster:
            continue
        assert (REPO / muster).is_file(), f"{muster} existiert nicht – umbenannt? Dann fällt es aus dem Sicherheitsnetz"
