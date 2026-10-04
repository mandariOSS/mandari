# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures für die Tests des Abonnements ``suchindex``: Elasticsearch im Speicher, Kommune, Register."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from apps.events import registry
from insight_core.models import OParlBody, OParlSource
from insight_search import abonnement
from insight_search.tests.fake_es import FakeElasticsearch


@pytest.fixture
def es(monkeypatch: pytest.MonkeyPatch) -> FakeElasticsearch:
    """Elasticsearch im Speicher statt des echten Clients."""
    fake = FakeElasticsearch()
    monkeypatch.setattr(abonnement, "client", lambda: fake)
    return fake


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, registry.Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, registry.Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


@pytest.fixture
def kommune(db: Any) -> Callable[[str], OParlBody]:
    """kommune("Name") → gespeicherte Kommune mit eigener Quelle."""

    def _neu(name: str = "Beispielstadt") -> OParlBody:
        kennung = uuid.uuid4().hex[:8]
        quelle = OParlSource.objects.create(name=name, url=f"https://ris-{kennung}.example/oparl/system")
        return OParlBody.objects.create(
            external_id=f"https://ris-{kennung}.example/oparl/body/1", source=quelle, name=name, slug=f"k-{kennung}"
        )

    return _neu
