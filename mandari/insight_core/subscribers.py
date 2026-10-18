# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements von Insight (``apps.events.registry.load_subscribers`` importiert dieses Modul).

``insight.fraktionen_live`` (``insight_core.services.fraktionen_live``) leitet aus den Wortmeldungen der
Live-Übertragungen die Fraktion von Personen ab (Issue #915, #916). Es wird registriert, solange
``LIVE_UEBERTRAGUNG_AKTIV`` an ist; ohne Live-Übertragungen gibt es keine Wortmeldungen. Ein neues Abonnement beginnt
am Ende des Journals; ältere Wortmeldungen verbucht ``manage.py fraktionen_aus_wortmeldungen``.

``insight.indexnow`` (``insight_core.services.indexnow``) meldet geänderte Seiten an Suchmaschinen (Issue #939). Es wird
registriert, sobald ``INDEXNOW_KEY`` gesetzt ist, und beginnt am Ende des Journals: Den Bestand finden die
Suchmaschinen über die Sitemaps.

``insight.verortung`` (``insight_core.services.georef_abonnement``) stößt nach neuem Text die Verortung des Vorgangs
an (Issue #919). Es wird nur registriert, wenn ``GEOREF_SUBSCRIPTION`` nicht ``aus`` ist; mit ``schatten`` startet
ein neu angelegtes Abonnement im Schattenbetrieb und zählt nur. Ein neues Abonnement beginnt am Ende des Journals;
Vorgänge mit älterem Text verortet der Zeitplan ``verortung_automatisch`` wie bisher.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.events import subscriber
from insight_core.services import fraktionen_live, georef_abonnement, indexnow

logger = logging.getLogger(__name__)


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


def register_indexnow() -> bool:
    """Registriert ``insight.indexnow``, wenn ein gültiger Schlüssel gesetzt ist; ``True``, wenn registriert."""
    if not indexnow.schluessel():
        if getattr(settings, "INDEXNOW_KEY", ""):
            logger.warning("IndexNow aus: INDEXNOW_KEY genügt nicht dem Protokoll (8–128 Zeichen A–Z, a–z, 0–9, -)")
        return False
    subscriber(
        indexnow.NAME,
        types=indexnow.TYPES,
        batch=indexnow.BATCH,
        queue=indexnow.QUEUE,
        # Fremdsystem: außerhalb der Transaktion, Zustellung mindestens einmal (eine doppelte Meldung schadet nicht)
        transactional=False,
    )(indexnow.indexnow)
    return True


def register_verortung() -> bool:
    """Registriert ``insight.verortung`` je nach Schalter; ``True``, wenn registriert."""
    modus = getattr(settings, "GEOREF_SUBSCRIPTION", "aus")
    if modus == "aus":
        return False
    subscriber(
        georef_abonnement.NAME,
        types=georef_abonnement.TYPES,
        batch=georef_abonnement.BATCH,
        queue=georef_abonnement.QUEUE,
        transactional=True,
        shadow=modus == "schatten",
    )(georef_abonnement.verortung)
    return True


register()
register_indexnow()
register_verortung()
