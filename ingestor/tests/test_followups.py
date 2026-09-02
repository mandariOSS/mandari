# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests für die Folgearbeiten aus Issue #22 (Speicherbudget/Capability-Cache).

Abgedeckt:
1. Persistenter Capability-Cache für modified_since (Seed aus der DB,
   Persistenz neu erkannter Hosts je Quelle)
2. Konfigurierbare Drosseln für Quellen- und Entity-Typ-Parallelität
3. Leerung der FK-UUID-Caches nach einem Sync-Zyklus
"""

import pytest

from src.client.oparl_client import OParlClient
from src.config import Settings
from src.storage.database import DatabaseStorage


@pytest.fixture(autouse=True)
def _reset_capability_cache():
    """Prozessweiten Capability-Cache pro Test isolieren."""
    original = set(OParlClient._modified_since_unsupported)
    OParlClient._modified_since_unsupported.clear()
    yield
    OParlClient._modified_since_unsupported.clear()
    OParlClient._modified_since_unsupported.update(original)


class TestConcurrencySettings:
    """Punkt 2: Drosseln auf Quellen- und Entity-Typ-Ebene sind konfigurierbar."""

    def test_source_concurrency_default(self):
        settings = Settings(_env_file=None)
        assert settings.sync_source_concurrency == 2

    def test_entity_concurrency_default(self):
        settings = Settings(_env_file=None)
        assert settings.sync_entity_concurrency == 2

    def test_env_overridable(self, monkeypatch):
        monkeypatch.setenv("SYNC_SOURCE_CONCURRENCY", "1")
        monkeypatch.setenv("SYNC_ENTITY_CONCURRENCY", "4")
        settings = Settings(_env_file=None)
        assert settings.sync_source_concurrency == 1
        assert settings.sync_entity_concurrency == 4


class TestClientCapabilityCacheAPI:
    """Punkt 1: Seed/Snapshot-API am Client."""

    def test_add_and_get_roundtrip(self):
        OParlClient.add_modified_since_unsupported({"ris.example.org", "oparl.example.com"})
        assert OParlClient.get_modified_since_unsupported() == {
            "ris.example.org",
            "oparl.example.com",
        }

    def test_get_returns_copy(self):
        OParlClient.add_modified_since_unsupported({"ris.example.org"})
        snapshot = OParlClient.get_modified_since_unsupported()
        snapshot.add("neu.example.org")
        assert "neu.example.org" not in OParlClient._modified_since_unsupported

    def test_empty_hosts_are_ignored(self):
        OParlClient.add_modified_since_unsupported({"", "ris.example.org"})
        assert OParlClient.get_modified_since_unsupported() == {"ris.example.org"}


class TestUuidCacheClearing:
    """Punkt 3: FK-UUID-Caches sind leerbar (Aufruf nach jedem Zyklus)."""

    def test_clear_uuid_caches(self):
        storage = DatabaseStorage("postgresql+asyncpg://unused:unused@localhost:5/unused")
        from uuid import uuid4

        storage._body_uuid_cache["b"] = uuid4()
        storage._meeting_uuid_cache["m"] = uuid4()
        storage._paper_uuid_cache["p"] = uuid4()
        storage._person_uuid_cache["pe"] = uuid4()
        storage._organization_uuid_cache["o"] = uuid4()

        storage.clear_uuid_caches()

        assert storage._body_uuid_cache == {}
        assert storage._meeting_uuid_cache == {}
        assert storage._paper_uuid_cache == {}
        assert storage._person_uuid_cache == {}
        assert storage._organization_uuid_cache == {}


class _StubStorage:
    """Storage-Stub für die Orchestrator-Seed/Persist-Logik."""

    def __init__(self, stored_hosts=None, raise_on_load=False):
        self.stored_hosts = set(stored_hosts or set())
        self.raise_on_load = raise_on_load
        self.persist_calls: list[tuple[str, set[str]]] = []

    async def get_modified_since_unsupported_hosts(self):
        if self.raise_on_load:
            raise RuntimeError("DB nicht erreichbar")
        return set(self.stored_hosts)

    async def add_modified_since_unsupported_hosts(self, source_url, hosts):
        self.persist_calls.append((source_url, set(hosts)))
        self.stored_hosts |= set(hosts)


def _make_orchestrator(stub):
    from src.sync.orchestrator import SyncOrchestrator

    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = stub
    return orchestrator


class TestOrchestratorCapabilityPersistence:
    """Punkt 1: Orchestrator lädt und persistiert den Capability-Cache."""

    @pytest.mark.asyncio
    async def test_seed_loads_persisted_hosts(self):
        stub = _StubStorage(stored_hosts={"ris.example.org"})
        orchestrator = _make_orchestrator(stub)

        await orchestrator._seed_modified_since_cache()

        assert {"ris.example.org"} <= OParlClient.get_modified_since_unsupported()

    @pytest.mark.asyncio
    async def test_seed_survives_storage_errors(self):
        stub = _StubStorage(raise_on_load=True)
        orchestrator = _make_orchestrator(stub)

        # darf keine Exception propagieren (Sync soll weiterlaufen)
        await orchestrator._seed_modified_since_cache()

        assert OParlClient.get_modified_since_unsupported() == set()

    @pytest.mark.asyncio
    async def test_persist_writes_only_new_hosts(self):
        stub = _StubStorage()
        orchestrator = _make_orchestrator(stub)

        # Snapshot vor dem Sync, danach entdeckt der Client einen neuen Host
        snapshot = OParlClient.get_modified_since_unsupported()
        OParlClient.add_modified_since_unsupported({"neu.example.org"})

        await orchestrator._persist_modified_since_cache("https://neu.example.org/oparl", snapshot)

        assert stub.persist_calls == [("https://neu.example.org/oparl", {"neu.example.org"})]

    @pytest.mark.asyncio
    async def test_persist_skips_when_nothing_new(self):
        stub = _StubStorage()
        orchestrator = _make_orchestrator(stub)

        OParlClient.add_modified_since_unsupported({"alt.example.org"})
        snapshot = OParlClient.get_modified_since_unsupported()

        await orchestrator._persist_modified_since_cache("https://alt.example.org/oparl", snapshot)

        assert stub.persist_calls == []

    @pytest.mark.asyncio
    async def test_roundtrip_seed_after_restart(self):
        """Nach 'Neustart' (leerer Prozess-Cache) liefert der Seed den Befund zurück."""
        stub = _StubStorage()
        orchestrator = _make_orchestrator(stub)

        snapshot = OParlClient.get_modified_since_unsupported()
        OParlClient.add_modified_since_unsupported({"ris.example.org"})
        await orchestrator._persist_modified_since_cache("https://ris.example.org/oparl", snapshot)

        # Daemon-Neustart simulieren
        OParlClient._modified_since_unsupported.clear()
        assert OParlClient.get_modified_since_unsupported() == set()

        await orchestrator._seed_modified_since_cache()
        assert OParlClient.get_modified_since_unsupported() == {"ris.example.org"}
