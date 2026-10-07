# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter und Warteschlange des Auftrags ``file.extract_text`` (Issues #530, #919).

Die Logik des Auftrags (Beanspruchen, Lesen aus der Ablage, bedingtes Speichern) und des Zeitplans
``texterkennung_einplanen`` liegt in der Drehscheibe, ``hub/ris/erkennung.py`` (``docs/adr/20261007-dokumentkette.md``,
Abschnitte 1, 2 und 4). Hier bleiben, was der RIS-Bestand selbst braucht:

- ``TEXT_EXTRACTION_RUNNER`` (``runner_is_worker``): ``ingestor`` (Standard) – der OCR-Worker des Ingestors erkennt
  den Text; ``worker`` – Aufträge ``file.extract_text`` in der Warteschlange ``ocr`` (braucht
  ``TASKS_BACKEND=journal``, sonst startet die Anwendung nicht).
- Importpfad und Warteschlange des Auftrags (``TASK_PATH``, ``TASK_QUEUE``) und die wartenden Aufträge
  (``queued_file_ids``, ``waiting_tasks``) für Zeitplan und Prüfung ``texterkennung``.
"""

from __future__ import annotations

import uuid
from typing import Final

from django.conf import settings

RUNNER_WORKER: Final = "worker"
#: Importpfad des Auftrags (``insight_core.background_tasks.file_extract_text``), für den Rückstau
TASK_PATH: Final = "insight_core.background_tasks.file_extract_text"
TASK_QUEUE: Final = "ocr"


def runner_is_worker() -> bool:
    return str(getattr(settings, "TEXT_EXTRACTION_RUNNER", "ingestor")).strip().lower() == RUNNER_WORKER


def queued_file_ids() -> list[str]:
    """
    Dateien, deren Auftrag eingereiht ist, aber noch nicht läuft (``wartend``, auch nach einem Abbruch wieder
    eingereiht oder für später neu eingeplant). Sie warten auf einen Platz in der Warteschlange ``ocr``, nicht auf
    einen toten Worker.
    """
    from apps.events.models import Task, TaskStatus

    ids: list[str] = []
    for args in Task.objects.filter(queue=TASK_QUEUE, task_path=TASK_PATH, status=TaskStatus.WARTEND).values_list(
        "args", flat=True
    ):
        werte = args.get("args") if isinstance(args, dict) else None
        if not werte:
            continue
        try:
            ids.append(str(uuid.UUID(str(werte[0]))))
        except ValueError:
            continue
    return ids


def waiting_tasks() -> int:
    """Eingereihte und laufende Aufträge ``file.extract_text`` (Rückstau der Warteschlange ``ocr``)."""
    from apps.events.models import Task, TaskStatus

    return Task.objects.filter(
        queue=TASK_QUEUE, task_path=TASK_PATH, status__in=[TaskStatus.WARTEND, TaskStatus.LAEUFT]
    ).count()


def extract_file(file_id: str) -> str:
    """Auftrag ``file.extract_text`` für eine Datei (``hub.ris.erkennung.erkennen``); Rückgabe: Ergebniscode."""
    from hub.ris.erkennung import erkennen

    return erkennen(file_id)
