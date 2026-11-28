# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebSocket-Routen der eingeschalteten Module (``mandari/asgi.py``).

Ein abgeschaltetes Modul (``apps.common.module``, Issue #1594) hat keine Routen; seine Consumer werden dann nicht
einmal geladen. Reihenfolge wie bisher: Dokumente, Sitzungsvorbereitung und Fraktionssitzungen in Work, danach das
Sitzungscockpit von Session.
"""

from __future__ import annotations

from django.urls import URLPattern, URLResolver

from apps.common import module


def websocket_urlpatterns() -> list[URLPattern | URLResolver]:
    """Routen aller eingeschalteten Module."""
    muster: list[URLPattern | URLResolver] = []
    if module.aktiv(module.WORK):
        from apps.work.faction.routing import websocket_urlpatterns as fraktion
        from apps.work.meetings.routing import websocket_urlpatterns as sitzungen
        from apps.work.motions.routing import websocket_urlpatterns as dokumente

        muster += [*dokumente, *sitzungen, *fraktion]
    if module.aktiv(module.SESSION):
        from apps.session.routing import websocket_urlpatterns as cockpit

        muster += cockpit
    return muster
