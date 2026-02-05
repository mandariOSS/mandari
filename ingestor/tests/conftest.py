"""
Pytest configuration and fixtures.
"""

import pytest
from pathlib import Path


# Make sure tests can find the sample data
SAMPLE_DATA_PATH = Path(__file__).parent.parent.parent.parent.parent / "_OParl Muster Data"


@pytest.fixture(scope="session")
def sample_data_path() -> Path:
    """Return path to sample data directory."""
    return SAMPLE_DATA_PATH


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
