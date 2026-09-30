# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aufträge für die Tests von Backend und Runner (auf Modulebene, wie Django-Tasks es verlangt)."""

from __future__ import annotations

import threading
import time
from typing import Any

from django.tasks import TaskContext, task

from apps.events.tasks_backend import PermanentTaskError

#: Aufrufe der Test-Aufträge in Aufrufreihenfolge
aufrufe: list[tuple[str, Any]] = []
#: gibt ``haengen`` frei
FREIGABE = threading.Event()
#: gesetzt, sobald ``haengen`` läuft
HAENGT = threading.Event()
#: Treffpunkt für ``treffen``: gelingt nur, wenn zwei Aufträge gleichzeitig laufen
TREFFPUNKT = threading.Barrier(2, timeout=10)


def zuruecksetzen() -> None:
    aufrufe.clear()
    FREIGABE.clear()
    HAENGT.clear()
    TREFFPUNKT.reset()


@task
def merken(kennung: str, *, zusatz: int = 0) -> str:
    aufrufe.append(("merken", (kennung, zusatz)))
    return kennung


@task(queue_name="mail")
def mail_merken(kennung: str) -> None:
    aufrufe.append(("mail_merken", kennung))


@task(priority=10)
def wichtig(kennung: str) -> None:
    aufrufe.append(("wichtig", kennung))


@task
def scheitern(kennung: str) -> None:
    aufrufe.append(("scheitern", kennung))
    raise RuntimeError("absichtlich gescheitert")


@task
def endgueltig(kennung: str) -> None:
    aufrufe.append(("endgueltig", kennung))
    raise PermanentTaskError("nicht wiederholen")


@task(takes_context=True)
def mit_kontext(context: TaskContext[Any, Any], kennung: str) -> None:
    aufrufe.append(("mit_kontext", (kennung, context.attempt, context.task_result.id)))


@task(queue_name="ai")
def haengen(kennung: str) -> None:
    HAENGT.set()
    FREIGABE.wait(30)
    aufrufe.append(("haengen", kennung))


@task
def langsam(sekunden: float) -> None:
    time.sleep(sekunden)
    aufrufe.append(("langsam", sekunden))


@task
def treffen(kennung: str) -> None:
    TREFFPUNKT.wait()
    aufrufe.append(("treffen", kennung))


def keine_task(kennung: str) -> None:
    """Gewöhnliche Funktion ohne ``@task``: der Runner darf sie nicht ausführen."""
    aufrufe.append(("keine_task", kennung))


def journal_einstellungen(**optionen: Any) -> dict[str, Any]:
    """``TASKS`` mit ``JournalBackend`` und den Warteschlangen der Plattform."""
    from django.conf import settings

    return {
        "default": {
            "BACKEND": "apps.events.tasks_backend.JournalBackend",
            "QUEUES": list(settings.TASK_QUEUES),
            "OPTIONS": optionen,
        }
    }
