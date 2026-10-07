# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements von Insight (``apps.events.registry.load_subscribers`` importiert dieses Modul).

``insight.fraktionen_live`` (``insight_core.services.fraktionen_live``) leitet aus den Wortmeldungen der
Live-Übertragungen die Fraktion von Personen ab (Issue #915, #916). Es wird registriert, solange
``LIVE_UEBERTRAGUNG_AKTIV`` an ist; ohne Live-Übertragungen gibt es keine Wortmeldungen. Ein neues Abonnement beginnt
am Ende des Journals; ältere Wortmeldungen verbucht ``manage.py fraktionen_aus_wortmeldungen``.
"""

from __future__ import annotations

from django.conf import settings

from apps.events import subscriber
from insight_core.services import fraktionen_live


def register() -> bool:
    """Registriert ``insight.fraktionen_live`` je nach Schalter; ``True``, wenn registriert."""
    if not getattr(settings, "LIVE_UEBERTRAGUNG_AKTIV", False):
        return False
    subscriber(
        fraktionen_live.NAME,
        types=fraktionen_live.TYPES,
        batch=fraktionen_live.BATCH,
        queue=fraktionen_live.QUEUE,
        transactional=True,
    )(fraktionen_live.fraktionen_live)
    return True


register()
