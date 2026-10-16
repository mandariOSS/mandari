# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``sync_session_insight`` nur für Session-Quellen (Teil von Issue #1027).

Der lokale Spiegel ruft mit eigener Kennung ab, ohne robots.txt und ohne Takt je Server. Mit ``--source-url`` konnte
er bisher jede registrierte Quelle abrufen, auch ein Ratsinformationssystem. Jetzt lehnt er Quellen ohne
``sync_config["session_tenant"]`` ab und verweist auf den Ingestor; ``--tenant`` und ``--all`` galten schon bisher
nur für Session-Quellen.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from insight_core.models import OParlSource
from insight_sync import session_mirror

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org/oparl/system"
SESSION = "https://mandari.example/session/nord/api/oparl/"


@pytest.fixture
def abrufe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Abrufe des Spiegels mitschreiben statt ins Netz zu gehen."""
    urls: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        urls.append(url)
        if url.endswith("/oparl/"):
            return {"body": f"{url}body/"}
        return {"data": [], "links": {}}

    monkeypatch.setattr(session_mirror, "_default_fetch", fetch)
    return urls


def test_source_url_eines_ratsinformationssystems_wird_abgelehnt(abrufe: list[str]) -> None:
    OParlSource.objects.create(name="Beispiel-RIS", url=RIS)

    with pytest.raises(CommandError, match="keine Session-Quelle") as fehler:
        call_command("sync_session_insight", source_url=RIS)

    assert "python -m src.main sync --source" in str(fehler.value)
    assert abrufe == []


def test_source_url_einer_session_quelle_wird_gespiegelt(abrufe: list[str]) -> None:
    OParlSource.objects.create(name="Nord (Session)", url=SESSION, sync_config={"session_tenant": "nord"})

    call_command("sync_session_insight", source_url=SESSION)

    assert abrufe == [SESSION, f"{SESSION}body/"]
