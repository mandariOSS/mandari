# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückweg der Kennungen von Tagesordnungspunkten aus SessionNet-Sitzungsseiten (Issue #863).

Seit #863 heißen Punkte nach ihrer Vorlage (``…#agendaitem/kvonr-<n>``); der Abruf hat bestehende Zeilen in place
umgeschrieben, ``id`` blieb (``uuid5`` der bisherigen Kennung). Ein Ingestor von vor #863 vergibt wieder die
bisherige Kennung und will mit genau dieser ``id`` eine neue Zeile anlegen: Der Primärschlüssel ist vergeben, und der
ganze Abruf der Quelle bricht ab. Vor einem solchen Rückfall schreibt ``revert_agenda_item_ids`` die Kennungen der
Quelle zurück (``plan_agenda_item_revert``); Befehl ``top-kennungen-zurueck``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mandari_oparl import IdBases

from src.scrapers.sessionnet import plan_agenda_item_revert


@dataclass
class RevertReport:
    """Ergebnis des Rückwegs für eine Quelle."""

    meetings: int = 0
    #: umgeschriebene Zeilen mit ihrer abgelösten Kennung
    restore: int = 0
    #: seit der Umstellung angelegte Punkte mit der Kennung des bisherigen Abrufs
    translate: int = 0
    #: tatsächlich geschrieben (nur mit ``apply``)
    written: int = 0
    #: umgeschriebene Zeilen ohne Rückweg; solange es sie gibt, nicht zurückfallen
    unresolved: list[str] = field(default_factory=list)


async def revert_agenda_item_ids(storage: Any, source: Any, *, apply: bool) -> RevertReport:
    """
    Kennungen der Tagesordnungspunkte einer SessionNet-Quelle auf die Form von vor Issue #863 zurückschreiben.

    Ohne ``apply`` nur zählen. Je Sitzung eine Transaktion (``rename_agenda_items`` ohne ``mandari:previousId``);
    ein zweiter Lauf findet nichts mehr. Der neue Abruf stellt danach wieder um, falls er weiterläuft.
    """
    ids = IdBases.for_source(source.url, source.sync_config)
    report = RevertReport()
    for meeting_id, agenda in await storage.get_meeting_agendas_of_source(source.id):
        stored = await storage.get_agenda_items_of_meeting(meeting_id)
        plan = plan_agenda_item_revert(agenda, stored, ids.id)
        report.unresolved.extend(plan.unresolved)
        if not plan.renames:
            continue
        report.meetings += 1
        report.restore += len(plan.restore)
        report.translate += len(plan.translate)
        if apply:
            report.written += await storage.rename_agenda_items(meeting_id, plan.renames, keep_previous=False)
    return report
