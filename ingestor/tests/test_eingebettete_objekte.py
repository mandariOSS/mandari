"""
Eingebettete Objekte einer Sitzung bzw. Vorlage werden genau einmal geschrieben.

``upsert_meeting`` und ``upsert_paper`` speichern ihre eingebetteten TOPs, Dateien und Beratungen
selbst. ``SyncOrchestrator._store_entity`` schrieb sie danach ein zweites Mal (jeweils mit eigener
Transaktion) und verdoppelte so die Schreiblast. Jetzt zählt der Orchestrator sie nur noch.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from mandari_oparl import (
    OParlType,
    ProcessedAgendaItem,
    ProcessedConsultation,
    ProcessedFile,
    ProcessedMeeting,
    ProcessedPaper,
)

from src.metrics import metrics
from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor

BASIS = "https://ris.example/oparl/"


class ZaehlSpeicher:
    """Speicher, der nur die Aufrufe mitschreibt."""

    def __init__(self) -> None:
        self.aufrufe: list[str] = []

    async def upsert_meeting(self, meeting: Any, body_id: Any) -> Any:
        self.aufrufe.append("meeting")
        return uuid4()

    async def upsert_paper(self, paper: Any, body_id: Any) -> Any:
        self.aufrufe.append("paper")
        return uuid4()

    def __getattr__(self, name: str) -> Any:
        async def aufruf(*args: Any, **kwargs: Any) -> Any:
            self.aufrufe.append(name)
            return uuid4()

        return aufruf


def _orchestrator(speicher: ZaehlSpeicher) -> SyncOrchestrator:
    orch = SyncOrchestrator.__new__(SyncOrchestrator)
    orch.storage = speicher  # type: ignore[assignment]
    orch.processor = OParlProcessor()
    orch.max_concurrent = 1
    orch._parallel_mode = False
    return orch


def _eingebettet(cls: Any, oparl_type: OParlType, name: str) -> Any:
    return cls(id=uuid4(), external_id=f"{BASIS}{name}", oparl_type=oparl_type, raw_json={})


def _gezaehlt(entity_type: str) -> int:
    return int(metrics.simple.entities_synced.get(entity_type, 0))


def test_sitzung_schreibt_eingebettete_objekte_nur_einmal() -> None:
    sitzung = ProcessedMeeting(
        id=uuid4(),
        external_id=f"{BASIS}meeting/1",
        oparl_type=OParlType.MEETING,
        raw_json={},
        nested_entities=[
            _eingebettet(ProcessedFile, OParlType.FILE, "file/1"),
            _eingebettet(ProcessedAgendaItem, OParlType.AGENDA_ITEM, "agendaitem/1"),
            _eingebettet(ProcessedAgendaItem, OParlType.AGENDA_ITEM, "agendaitem/2"),
        ],
    )
    speicher = ZaehlSpeicher()
    vorher = {typ: _gezaehlt(typ) for typ in ("file", "agendaitem", "meeting")}

    gespeichert = asyncio.run(_orchestrator(speicher)._store_entity(sitzung, uuid4(), "meeting", "Musterstadt"))

    assert gespeichert is True
    assert speicher.aufrufe == ["meeting"], "TOPs und Dateien schreibt upsert_meeting selbst"
    if metrics.enabled:
        assert _gezaehlt("file") - vorher["file"] == 1
        assert _gezaehlt("agendaitem") - vorher["agendaitem"] == 2
        assert _gezaehlt("meeting") - vorher["meeting"] == 1


def test_vorlage_schreibt_eingebettete_objekte_nur_einmal() -> None:
    vorlage = ProcessedPaper(
        id=uuid4(),
        external_id=f"{BASIS}paper/1",
        oparl_type=OParlType.PAPER,
        raw_json={},
        nested_entities=[
            _eingebettet(ProcessedFile, OParlType.FILE, "file/2"),
            _eingebettet(ProcessedConsultation, OParlType.CONSULTATION, "consultation/1"),
        ],
    )
    speicher = ZaehlSpeicher()
    vorher = {typ: _gezaehlt(typ) for typ in ("file", "consultation", "paper")}

    asyncio.run(_orchestrator(speicher)._store_entity(vorlage, uuid4(), "paper", "Musterstadt"))

    assert speicher.aufrufe == ["paper"], "Dateien und Beratungen schreibt upsert_paper selbst"
    if metrics.enabled:
        assert _gezaehlt("file") - vorher["file"] == 1
        assert _gezaehlt("consultation") - vorher["consultation"] == 1
        assert _gezaehlt("paper") - vorher["paper"] == 1
