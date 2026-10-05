# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements des Work-Moduls (``apps.events.registry.load_subscribers`` importiert dieses Modul).

``benachrichtigung`` (``apps.work.notifications.abonnement``) wird nur registriert, wenn
``WORK_NOTIFICATION_SUBSCRIPTION`` nicht ``aus`` ist. Mit ``schatten`` startet ein neu angelegtes
Abonnement im Schattenbetrieb und vergleicht nur; danach gilt der Zustand in der Datenbank, der Handler
legt aber mit ``schatten`` nie Benachrichtigungen an. Ein neues Abonnement beginnt am Ende des Journals.
"""

from __future__ import annotations

from django.conf import settings

from apps.events import subscriber
from apps.work.notifications import abonnement


def register() -> bool:
    """Registriert ``benachrichtigung`` je nach Schalter; ``True``, wenn registriert."""
    modus = settings.WORK_NOTIFICATION_SUBSCRIPTION
    if modus == "aus":
        return False
    subscriber(
        abonnement.NAME,
        types=abonnement.TYPES,
        batch=abonnement.BATCH,
        queue=abonnement.QUEUE,
        transactional=True,
        shadow=modus == "schatten",
    )(abonnement.benachrichtigung)
    return True


register()
