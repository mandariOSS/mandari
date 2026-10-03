# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sync einer Quelle und Löschen einer Kommune aus dem Admin als Aufträge im Worker (Issue #515).

Beides lief vorher in einem Faden im Webprozess. Jetzt landet es immer im Journal, auch solange die
Webprozesse Aufträge sonst sofort ausführen (Standard ``TASKS_BACKEND=immediate``, so auch hier).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache

from apps.events.models import Task, TaskStatus
from insight_core import background_tasks
from insight_core.admin import run_sync_in_thread
from insight_core.models import OParlBody, OParlSource

RIS = "https://ris.example.org/oparl"


@pytest.fixture
def source(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Beispiel-RIS", url=f"{RIS}/system")


@pytest.mark.django_db
def test_sync_aus_dem_admin_landet_im_journal_statt_in_einem_faden(
    source: OParlSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    aufrufe: list[Any] = []
    monkeypatch.setattr("insight_sync.tasks.run_sync_with_logging", lambda **k: aufrufe.append(k))

    run_sync_in_thread(source, full=True)

    assert aufrufe == [], "nicht in der Anfrage ausführen"
    auftrag = Task.objects.get()
    assert auftrag.task_path == "insight_core.background_tasks.quelle_synchronisieren"
    assert auftrag.status == TaskStatus.WARTEND
    assert auftrag.queue == "default"
    assert auftrag.args == {"args": [str(source.pk)], "kwargs": {"full": True}}


@pytest.mark.django_db
def test_auftrag_synchronisiert_die_quelle(source: OParlSource, monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe: list[dict[str, Any]] = []
    monkeypatch.setattr("insight_sync.tasks.run_sync_with_logging", lambda **k: aufrufe.append(k))

    background_tasks.quelle_synchronisieren.call(str(source.pk), full=True)
    background_tasks.quelle_synchronisieren.call("00000000-0000-0000-0000-000000000000")

    assert aufrufe == [{"source": source, "full": True, "triggered_by": "admin"}], "gelöschte Quelle: nichts"


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
