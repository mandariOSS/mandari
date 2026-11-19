# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gemeinsames für die Tests der Termine und Terminumfragen."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import transaction


@pytest.fixture(autouse=True)
def nach_commit_sofort(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Benachrichtigungen zu Terminen und Umfragen gehen erst nach dem Commit hinaus (``transaction.on_commit``). Ein
    Test läuft in einer Transaktion, die nie festgeschrieben wird; hier wird der Aufruf deshalb sofort ausgeführt.
    Tests, die den Zeitpunkt selbst prüfen (Fixture ``django_capture_on_commit_callbacks``), bleiben unberührt.
    """
    if "django_capture_on_commit_callbacks" in request.fixturenames:
        return

    def sofort(func: Any, using: Any = None, robust: bool = False) -> None:
        func()

    monkeypatch.setattr(transaction, "on_commit", sofort)
