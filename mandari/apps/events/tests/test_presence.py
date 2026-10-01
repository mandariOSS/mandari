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


def test_auftraege_ueber_das_journal_brauchen_die_rolle_tasks(ohne_bedarf: Any) -> None:
    ohne_bedarf.TASKS = journal_einstellungen()
    assert presence.required_roles() == {"tasks"}


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
    presence.announce("w2", ["tasks"], ["ocr"])
    stand = presence.worker_status()
    assert not stand.degraded
    assert stand.roles == presence.ALL_ROLES
    assert [worker.holder for worker in stand.workers] == ["w1", "w2"]

    presence.withdraw("w2")
    assert presence.worker_status().missing == {"tasks"}
