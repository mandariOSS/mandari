# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse der Aufgaben für die Datendrehscheibe (Issue #529, ``docs/adr/20260929-ereignistechnik-postgres.md``).

Die Fachfunktionen in ``apps.work.tasks.services`` melden hier, dass eine Aufgabe zugewiesen, erledigt
oder kommentiert wurde. Das Ereignis entsteht in derselben Transaktion wie die Änderung; das Abonnement
``benachrichtigung`` legt daraus die Benachrichtigungen an.

Schalter ``WORK_NOTIFICATION_SUBSCRIPTION``:

- ``aus`` (Standard): kein Ereignis; die Fachfunktion benachrichtigt wie bisher selbst.
- ``schatten``: Ereignis und bisheriger Weg; das Abonnement vergleicht nur. Scheitert das Ereignis,
  bleibt die Änderung bestehen (Sicherungspunkt, Protokolleintrag).
- ``aktiv``: nur das Ereignis; Änderung und Ereignis sind atomar, die Benachrichtigung entsteht im Worker.

``melden`` liefert ``True``, wenn das Abonnement die Benachrichtigung übernimmt; dann entfällt der
direkte Aufruf.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from django.conf import settings
from django.db import transaction

from apps.events import CanonicalRef, publish, tenant_ref, user_ref

logger = logging.getLogger(__name__)

ZUGEWIESEN: Final = "work.task.assigned"
ERLEDIGT: Final = "work.task.completed"
KOMMENTIERT: Final = "work.task.commented"
VERSION: Final = 1


def modus() -> str:
    return str(getattr(settings, "WORK_NOTIFICATION_SUBSCRIPTION", "aus"))


def _schreiben(typ: str, task: Any, actor: Any, felder: dict[str, Any]) -> None:
    publish(
        typ,
        version=VERSION,
        aggregate=CanonicalRef("Task", task.id),
        tenant=tenant_ref("org", task.organization_id),
        visibility="intern",
        payload={"task": str(task.id), "organization": str(task.organization_id), **felder},
        actor_ref=user_ref(actor.user_id) if actor is not None else None,
    )


def melden(typ: str, task: Any, actor: Any, **felder: Any) -> bool:
    """Ereignis ``typ`` zur Aufgabe schreiben; ``True``, wenn der direkte Aufruf entfällt (``aktiv``)."""
    aktuell = modus()
    if aktuell == "aus":
        return False
    werte = {name: str(wert) if not isinstance(wert, bool) else wert for name, wert in felder.items()}
    if aktuell == "aktiv":
        _schreiben(typ, task, actor, werte)
        return True
    try:
        with transaction.atomic():
            _schreiben(typ, task, actor, werte)
    except Exception:  # noqa: BLE001 – im Schatten darf das Ereignis die Änderung nicht verhindern
        logger.exception("Schatten: Ereignis %s zur Aufgabe %s nicht geschrieben", typ, task.id)
    return False
