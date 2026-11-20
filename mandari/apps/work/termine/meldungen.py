# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen zu Terminen und Terminumfragen zum richtigen Zeitpunkt auslösen (Issue #1489).

Eine Benachrichtigung besteht aus der Zeile in Work und, je nach Einstellung der Person, einem Mailauftrag
(``NotificationHub.send``). Wann beides entstehen darf, hängt davon ab, wie Aufträge laufen:

* **Auftragsjournal** (``TASKS_BACKEND=journal``): Der Mailauftrag ist eine Zeile in der Datenbank, die der Runner
  erst nach dem Commit abarbeitet. Benachrichtigung und Auftrag entstehen deshalb in derselben Transaktion wie die
  Änderung: Scheitert das Speichern, gibt es beides nicht, und bricht der Prozess nach dem Commit ab, geht nichts
  verloren.
* **Sofort ausführendes Backend**: Der Mailauftrag läuft im Aufruf, die E-Mail ginge also hinaus, bevor die
  Änderung festgeschrieben ist. Dort läuft alles erst nach dem Commit (``transaction.on_commit``).

In beiden Fällen wird je Empfänger für sich benachrichtigt: Ein Fehler bei einer Person hält die übrigen nicht auf
und macht die Änderung nicht rückgängig.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, cast

from django.db import transaction

logger = logging.getLogger(__name__)


def _im_journal() -> bool:
    """Legt das Backend der Mailaufträge sie in der Datenbank ab (statt sie sofort auszuführen)?"""
    from apps.events.tasks_backend import JournalBackend
    from apps.work.background_tasks import send_notification_email_task

    return isinstance(cast(Any, send_notification_email_task).get_backend(), JournalBackend)


def melden(empfaenger: Iterable[Any], senden: Callable[[Any], object]) -> None:
    """``senden(mitglied)`` für jeden Empfänger aufrufen, zum Zeitpunkt, der zum Auftrags-Backend passt (siehe oben)."""
    personen = list(empfaenger)
    if not personen:
        return

    def alle() -> None:
        for person in personen:
            try:
                # Eigener Sicherungspunkt: Ein Datenbankfehler bei einer Person lässt die Transaktion benutzbar
                with transaction.atomic():
                    senden(person)
            except Exception:
                logger.exception("Benachrichtigung zu einem Termin ließ sich nicht anlegen")

    if _im_journal():
        alle()
    else:
        transaction.on_commit(alle, robust=True)
