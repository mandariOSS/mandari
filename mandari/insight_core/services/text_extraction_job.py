# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter und Warteschlangen der Aufträge der Texterkennung (Issues #530, #919, #1107).

Die Logik der Aufträge (Beanspruchen, Lesen aus der Ablage, bedingtes Speichern) und des Zeitplans
``texterkennung_einplanen`` liegt in der Drehscheibe, ``hub/ris/erkennung.py`` (``docs/adr/20261007-dokumentkette.md``,
Abschnitte 1, 2 und 4). Hier bleiben, was der RIS-Bestand selbst braucht:

- ``TEXT_EXTRACTION_RUNNER`` (``runner_is_worker``): ``ingestor`` (Standard) – der OCR-Worker des Ingestors erkennt
  den Text; ``worker`` – Aufträge in zwei Stufen (braucht ``TASKS_BACKEND=journal``, sonst startet die Anwendung
  nicht): ``file.read_text_layer`` in der Warteschlange ``text`` liest nur die Textebene (Dienst ``worker``),
  ``file.extract_text`` in ``ocr`` erkennt den Text aus dem Bild (Dienst ``worker-heavy``).
- Importpfade und Warteschlangen der Aufträge (``TASK_PATH``/``TASK_QUEUE``, ``TEXT_LAYER_TASK_PATH``/
  ``TEXT_LAYER_QUEUE``) und ihre wartenden Aufträge (``queued_file_ids``, ``waiting_tasks``, ``task_file_ids``) für
  Zeitplan und Prüfung ``texterkennung``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final

from django.conf import settings

RUNNER_WORKER: Final = "worker"
#: Importpfad des Auftrags ``file.extract_text`` (zweite Stufe, ``insight_core.background_tasks.file_extract_text``)
TASK_PATH: Final = "insight_core.background_tasks.file_extract_text"
TASK_QUEUE: Final = "ocr"
#: Importpfad des Auftrags ``file.read_text_layer`` (erste Stufe, Issue #1107)
TEXT_LAYER_TASK_PATH: Final = "insight_core.background_tasks.file_read_text_layer"
TEXT_LAYER_QUEUE: Final = "text"
#: Warteschlange → Importpfad ihres Auftrags der Texterkennung
STAGES: Final = {TEXT_LAYER_QUEUE: TEXT_LAYER_TASK_PATH, TASK_QUEUE: TASK_PATH}


def runner_is_worker() -> bool:
    return str(getattr(settings, "TEXT_EXTRACTION_RUNNER", "ingestor")).strip().lower() == RUNNER_WORKER


def task_file_ids(queues: Iterable[str], statuses: Iterable[str]) -> list[str]:
    """Dateien der Aufträge der Texterkennung in ``queues`` mit einem der ``statuses`` (je Auftrag eine Kennung)."""
    from django.db.models import Q

    from apps.events.models import Task

    bedingung = Q()
    for queue in queues:
        bedingung |= Q(queue=queue, task_path=STAGES[queue])
    if not bedingung:
        return []
    ids: list[str] = []
    for args in Task.objects.filter(bedingung, status__in=list(statuses)).values_list("args", flat=True):
        werte = args.get("args") if isinstance(args, dict) else None
        if not werte:
            continue
        try:
            ids.append(str(uuid.UUID(str(werte[0]))))
        except ValueError:
            continue
    return ids


def queued_file_ids(queues: Iterable[str] = STAGES) -> list[str]:
    """
    Dateien, deren Auftrag (erste oder zweite Stufe, bzw. nur in ``queues``) eingereiht ist, aber noch nicht läuft
    (``wartend``, auch nach einem Abbruch wieder eingereiht oder für später neu eingeplant). Sie warten auf einen
    Platz in ihrer Warteschlange, nicht auf einen toten Worker.
    """
    from apps.events.models import TaskStatus

    return task_file_ids(queues, [TaskStatus.WARTEND])


def waiting_tasks(queue: str = TASK_QUEUE) -> int:
    """Eingereihte und laufende Aufträge der Texterkennung in ``queue`` (Rückstau der Warteschlange)."""
    from apps.events.models import Task, TaskStatus

    return Task.objects.filter(
        queue=queue, task_path=STAGES[queue], status__in=[TaskStatus.WARTEND, TaskStatus.LAEUFT]
    ).count()


def extract_file(file_id: str) -> str:
    """Auftrag ``file.extract_text`` für eine Datei (``hub.ris.erkennung.erkennen``); Rückgabe: Ergebniscode."""
    from hub.ris.erkennung import erkennen

    return erkennen(file_id)
