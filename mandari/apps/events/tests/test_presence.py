# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Braucht die Installation einen Worker, und bedient ihn einer? (Issue #509, ``apps.events.presence``)

Ohne Bedarf meldet nichts ein Fehlen, damit bestehende Installationen ohne Worker nicht plötzlich
„degraded“ melden.
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


def test_ohne_journal_und_ereignisse_braucht_es_keinen_worker(ohne_bedarf: Any) -> None:
    assert presence.required_roles() == frozenset()


def test_auftraege_ueber_das_journal_brauchen_runner_und_zeitplaene(ohne_bedarf: Any) -> None:
    """Ohne Zeitpläne entstünden wiederkehrende Aufträge (etwa das Aufräumen) gar nicht erst."""
    ohne_bedarf.TASKS = journal_einstellungen()
    assert presence.required_roles() == {"tasks", "scheduler"}


def test_noetige_warteschlangen_ohne_abgeschaltete(ohne_bedarf: Any) -> None:
    ohne_bedarf.TASKS = journal_einstellungen(concurrency={"ai": 0})
    assert presence.required_queues() == set(ohne_bedarf.TASK_QUEUES) - {"ai"}


def test_ereignisse_des_ingestors_brauchen_den_sequenzierer(ohne_bedarf: Any) -> None:
    ohne_bedarf.INGESTOR_EVENTS_ENABLED = True
    assert presence.required_roles() == {"sequencer"}


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
