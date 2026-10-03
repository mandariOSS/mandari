# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Braucht die Installation einen Worker, und bedient ihn einer? (Issues #509, #515, ``apps.events.presence``)

Seit die wiederkehrende Arbeit als Zeitpläne im Worker läuft, braucht ihn jede Installation (Runner und
Zeitpläne); nur ``EVENTS_WORKER_REQUIRED=false`` schaltet die Meldung ab.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.events import presence
from apps.events.tests.auftraege import journal_einstellungen
from apps.events.worker import ROLES


@pytest.fixture
def ohne_bedarf(settings: Any) -> Any:
    settings.EVENTS_WORKER_REQUIRED = ""
    settings.INGESTOR_EVENTS_ENABLED = False
    settings.TASKS = {
        "default": {**settings.TASKS["default"], "BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}
    }
    return settings


def test_alle_rollen_wie_im_worker() -> None:
    assert frozenset(ROLES) == presence.ALL_ROLES


def test_zeitplaene_brauchen_immer_runner_und_scheduler(ohne_bedarf: Any) -> None:
    """Ohne Worker fielen Erinnerungen, Einladungen und Aufräumen still aus (Issue #515)."""
    assert presence.required_roles() == {"tasks", "scheduler"}


def test_ohne_journal_reichen_die_warteschlangen_der_zeitplaene(ohne_bedarf: Any) -> None:
    """Webprozesse führen Aufträge sofort aus; im Journal landen nur Zeitpläne und Admin-Aufträge."""
    assert presence.required_queues() == {"default"}

    ohne_bedarf.TASKS["default"]["OPTIONS"] = {**ohne_bedarf.TASKS["default"]["OPTIONS"], "concurrency": {"default": 0}}
    assert presence.required_queues() == frozenset(), "bewusst abgeschaltete Warteschlange"


def test_warteschlangen_der_zeitplaene_aus_dem_register(ohne_bedarf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presence, "schedule_queues", lambda: frozenset({"mail"}))
    assert presence.required_queues() == {"default", "mail"}


def test_auftraege_ueber_das_journal_brauchen_runner_und_zeitplaene(ohne_bedarf: Any) -> None:
    """Ohne Zeitpläne entstünden wiederkehrende Aufträge (etwa das Aufräumen) gar nicht erst."""
    ohne_bedarf.TASKS = journal_einstellungen()
    assert presence.required_roles() == {"tasks", "scheduler"}


def test_noetige_warteschlangen_ohne_abgeschaltete(ohne_bedarf: Any) -> None:
    ohne_bedarf.TASKS = journal_einstellungen(concurrency={"ai": 0})
    assert presence.required_queues() == set(ohne_bedarf.TASK_QUEUES) - {"ai"}


def test_ereignisse_des_ingestors_brauchen_den_sequenzierer(ohne_bedarf: Any) -> None:
    ohne_bedarf.INGESTOR_EVENTS_ENABLED = True
    assert presence.required_roles() == {"sequencer", "tasks", "scheduler"}


@pytest.mark.parametrize(
    ("wert", "erwartet"), [("true", presence.ALL_ROLES), ("1", presence.ALL_ROLES), ("false", frozenset())]
)
def test_einstellung_uebersteuert(ohne_bedarf: Any, wert: str, erwartet: frozenset[str]) -> None:
    ohne_bedarf.TASKS = journal_einstellungen()
    ohne_bedarf.EVENTS_WORKER_REQUIRED = wert
    assert presence.required_roles() == erwartet


@pytest.mark.django_db
def test_fehlende_rollen_gegen_lebende_worker(ohne_bedarf: Any) -> None:
    ohne_bedarf.EVENTS_WORKER_REQUIRED = "true"
    assert presence.worker_status().missing == presence.ALL_ROLES

    presence.announce("w1", ["sequencer", "dispatch", "scheduler"], [])
    presence.announce("w2", ["tasks"], [])
    stand = presence.worker_status()
    assert not stand.degraded
    assert stand.roles == presence.ALL_ROLES
    assert [worker.holder for worker in stand.workers] == ["w1", "w2"]

    presence.withdraw("w2")
    stand = presence.worker_status()
    assert stand.missing == {"tasks"}
    assert stand.missing_queues == frozenset(), "fehlt die Rolle ganz, nennt die Meldung sie nur einmal"
    assert stand.missing_summary() == "tasks"


@pytest.mark.django_db
def test_runner_muessen_zusammen_jede_warteschlange_bedienen(ohne_bedarf: Any) -> None:
    """Hauptworker ohne ocr und ai, dazu ein eigener Worker für beide (docker-compose.yml)."""
    ohne_bedarf.TASKS = journal_einstellungen()
    presence.announce("haupt", ["sequencer", "dispatch", "tasks", "scheduler"], ["default", "mail", "index", "adapter"])

    stand = presence.worker_status()
    assert stand.degraded
    assert stand.missing == frozenset()
    assert stand.missing_queues == {"ocr", "ai"}
    assert stand.missing_summary() == "tasks (Warteschlangen ai, ocr)"

    presence.announce("ocr", ["tasks"], ["ocr", "ai"])
    assert not presence.worker_status().degraded

    presence.withdraw("ocr")
    presence.announce("alle", ["tasks"], [])
    assert not presence.worker_status().degraded, "ein Worker ohne --queues bedient alle"
