# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenbank-Engines des Ingestors – so erzeugt, dass die SQLAlchemy-Instrumentierung greift (Issue #699).

Die OpenTelemetry-Instrumentierung (``src/observability.py``) ersetzt ``create_async_engine`` im Modul
``sqlalchemy.ext.asyncio``, sobald ``setup_opentelemetry()`` läuft. Das geschieht im CLI-Callback, also erst
nachdem ``src.main`` den Orchestrator und mit ihm den Speicher geladen hat. Ein beim Import
gebundener Name (``from sqlalchemy.ext.asyncio import create_async_engine``) zeigt dann weiter auf das
Original: Die Engine bekommt keine Listener der Instrumentierung, und es entstehen nur ``connect``-Spans statt
eines Spans je Anweisung.

Deshalb erzeugt der Ingestor jede Engine über ``engine_erzeugen()``, das die Funktion erst beim Aufruf aus dem
Modul liest. ``tests/test_tracing_startweg.py`` prüft das über den Startweg der CLI und verbietet den früh
gebundenen Import im Quellcode.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy.ext.asyncio as sa_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine


def engine_erzeugen(url: str, **optionen: Any) -> AsyncEngine:
    """``create_async_engine(url, **optionen)`` – aufgelöst zum Zeitpunkt des Aufrufs, nicht des Imports."""
    return sa_asyncio.create_async_engine(url, **optionen)
