# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufträge aus dem Admin im Worker (Issue #515): Löschen einer Kommune; der Sync einer Quelle nur noch als leere Hülle.

Beides lief vorher in einem Faden im Webprozess. Den Sync einer Quelle macht seit Issue #554 nur der Ingestor; der
Auftrag ``quelle_synchronisieren`` bleibt registriert, damit vor dem Deploy eingereihte Aufträge nicht scheitern.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from django.core.cache import cache

from apps.events.models import Task, TaskStatus
from apps.events.tasks_backend import journal_backend, journal_options
from insight_core import background_tasks
from insight_core.models import OParlBody, OParlSource
from insight_sync.models import SyncLog

RIS = "https://ris.example.org/oparl"


@pytest.fixture
def source(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Beispiel-RIS", url=f"{RIS}/system")


@pytest.mark.django_db
def test_wartender_sync_auftrag_bleibt_ausfuehrbar(source: OParlSource) -> None:
    """Vor dem Deploy eingereihte Aufträge: Pfad und Optionen bleiben, damit sie danach nicht scheitern."""
    journal_backend().enqueue(background_tasks.quelle_synchronisieren, [str(source.pk)], {"full": True})

    auftrag = Task.objects.get()
    assert auftrag.task_path == "insight_core.background_tasks.quelle_synchronisieren"
    assert auftrag.status == TaskStatus.WARTEND
    assert auftrag.max_attempts == 1
    optionen, _ = journal_options()
    assert optionen.timeout_for(auftrag.task_path, auftrag.queue) == 3600


@pytest.mark.django_db
def test_sync_auftrag_gleicht_nicht_mehr_ab(source: OParlSource, caplog: pytest.LogCaptureFixture) -> None:
    """Issue #554: Den Sync macht nur der Ingestor; der Auftrag warnt nur im Log."""
    with caplog.at_level(logging.WARNING, logger="insight_core.background_tasks"):
        background_tasks.quelle_synchronisieren.call(str(source.pk), full=True)
        background_tasks.quelle_synchronisieren.call("00000000-0000-0000-0000-000000000000")

    assert not SyncLog.objects.exists(), "kein Abgleich, kein Protokoll"
    warnungen = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnungen) == 2
    assert str(source.pk) in warnungen[0]
    assert "nur noch der Ingestor" in warnungen[0]


@pytest.mark.django_db
def test_loeschauftrag_gibt_den_doppelklick_schutz_frei(source: OParlSource, monkeypatch: pytest.MonkeyPatch) -> None:
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Beispielstadt")
    schluessel = background_tasks.deletion_lock_key(str(body.pk))
    cache.add(schluessel, "running", timeout=60)
    geloescht: list[str] = []
    monkeypatch.setattr("insight_core.services.body_deletion.delete_body_data", geloescht.append)

    background_tasks.kommune_loeschen.call(str(body.pk))

    assert geloescht == [str(body.pk)]
    assert cache.get(schluessel) is None
