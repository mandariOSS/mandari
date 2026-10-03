"""
Pytest configuration and fixtures.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from src import observability

# Make sure tests can find the sample data
SAMPLE_DATA_PATH = Path(__file__).parent.parent.parent.parent.parent / "_OParl Muster Data"


@pytest.fixture(scope="session")
def sample_data_path() -> Path:
    """Return path to sample data directory."""
    return SAMPLE_DATA_PATH


@pytest.fixture(autouse=True)
def _root_logging_zuruecksetzen() -> Iterator[None]:
    """CLI-Aufrufe richten das Root-Logging ein (Callback in src/main.py).

    Der dabei angelegte Handler schreibt in den stderr-Ersatz von CliRunner, der nach dem Aufruf geschlossen
    ist. Deshalb nach jedem Test den vorherigen Zustand wiederherstellen.
    """
    root = logging.getLogger()
    handlers, level, ready = root.handlers[:], root.level, observability._logging_ready
    yield
    root.handlers = handlers
    root.setLevel(level)
    observability._logging_ready = ready


@pytest.fixture(autouse=True)
def _robots_zwischenspeicher(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """robots.txt-Zwischenspeicher je Test leeren; ohne Fixture ``echte_robots`` gilt „keine robots.txt“.

    Der Ingestor fragt vor jedem Abruf die robots.txt des Hosts an (src/client/robots.py). Tests, die nur
    den Abruf selbst prüfen, sollen dafür keine Antwort nachbilden müssen; die robots-Tests fordern
    ``echte_robots`` an und bekommen die echte Prüfung.
    """
    from mandari_oparl.robots import STATE_UNAVAILABLE, RobotsTxt

    from src.client.robots import RobotsGate, robots_gate

    robots_gate.clear()
    if "echte_robots" not in request.fixturenames:

        async def _keine_robots(self: RobotsGate, *args: object, **kwargs: object) -> RobotsTxt:
            return RobotsTxt(state=STATE_UNAVAILABLE, status_code=404)

        monkeypatch.setattr(RobotsGate, "_load", _keine_robots)
    yield
    robots_gate.clear()


@pytest.fixture(autouse=True)
def _drossel_je_host(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drossel je Host ohne Redis und Zustand je Test; ohne Fixture ``echte_drossel`` ist sie abgeschaltet.

    Standard im Betrieb ist eine Anfrage je Sekunde und Host (src/client/host_pacing.py). Tests, die nur den
    Abruf prüfen, sollen nicht sekundenweise warten; die Drossel-Tests fordern ``echte_drossel`` an.
    """
    from src.client.host_pacing import host_pacer
    from src.config import settings

    host_pacer.reset()
    monkeypatch.setattr(host_pacer, "redis_enabled", False)
    if "echte_drossel" not in request.fixturenames:
        monkeypatch.setattr(settings, "request_interval", 0.0)
    yield
    host_pacer.reset()


@pytest.fixture
def echte_drossel() -> None:
    """Drossel je Host im Test mit dem Standardabstand (siehe ``_drossel_je_host``)."""


@pytest.fixture
def echte_robots() -> None:
    """Echte robots.txt-Prüfung im Test (siehe ``_robots_zwischenspeicher``)."""


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest."""
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
