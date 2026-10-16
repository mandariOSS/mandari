# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests für die Folgearbeiten aus Issue #22 (Speicherbudget).

Abgedeckt:
2. Konfigurierbare Drosseln für Quellen- und Entity-Typ-Parallelität
3. Leerung der FK-UUID-Caches nach einem Sync-Zyklus

Punkt 1, der prozessweite Capability-Cache für modified_since, ist seit Issue #1041 durch Vermerke je Liste und je
Quelle ersetzt (``tests/test_filtervermerk_je_liste.py``).
"""

from src.config import Settings
from src.storage.database import DatabaseStorage


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
