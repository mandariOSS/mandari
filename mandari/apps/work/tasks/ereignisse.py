# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse der Aufgaben für die Datendrehscheibe (Issue #529, ``docs/adr/20260929-ereignistechnik-postgres.md``).

Die Fachfunktionen in ``apps.work.tasks.services`` melden hier, dass eine Aufgabe zugewiesen, erledigt
oder kommentiert wurde. Das Ereignis entsteht in derselben Transaktion wie die Änderung; das Abonnement
``benachrichtigung`` legt daraus die Benachrichtigungen an. Schalter und Schreiben:
``apps.work.notifications.ereignisse``.
"""

from __future__ import annotations

from typing import Any, Final

from apps.events import CanonicalRef
from apps.work.notifications import ereignisse as benachrichtigung

ZUGEWIESEN: Final = "work.task.assigned"
ERLEDIGT: Final = "work.task.completed"
KOMMENTIERT: Final = "work.task.commented"
VERSION: Final = benachrichtigung.VERSION


def modus() -> str:
    return benachrichtigung.modus()


def melden(typ: str, task: Any, actor: Any, **felder: Any) -> bool:
    """Ereignis ``typ`` zur Aufgabe schreiben; ``True``, wenn der direkte Aufruf entfällt (``aktiv``)."""
    return benachrichtigung.melden(
        typ,
        aggregate=CanonicalRef("Task", task.id),
        organization_id=task.organization_id,
        actor=actor,
        payload={"task": task.id, "organization": task.organization_id, **felder},
    )
