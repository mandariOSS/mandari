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


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest."""
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
