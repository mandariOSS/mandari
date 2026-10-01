# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebSocket für das Sitzungscockpit (Issue #140): Hinweis „Stand geändert“ an alle offenen Ansichten.

Über den Socket geht bewusst kein Inhalt. Jede Ansicht holt nach dem Hinweis ihren Stand per HTTP mit den
eigenen Rechten ab (nichtöffentliche TOPs, interne Vermerke) – so kann eine Gruppe nie mehr verteilen, als
die einzelne Person sehen darf. Fällt der Channel-Layer aus, bleibt das Polling der Ansicht als Rückfall.

Zugang wie die Seite (``cockpit_service.meeting_for_viewer``): aktives Konto im Mandanten, Sichtrecht für
Sitzungen, nichtöffentliche Sitzungen nur mit dem NÖ-Recht. Gruppe je Sitzung.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

#: Schließcodes: nicht angemeldet bzw. kein Zugang (wie die Kollaboration im Work-Modul)
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403


def cockpit_group(meeting_id: Any) -> str:
    """Gruppe aller offenen Cockpit-Ansichten einer Sitzung."""
    return f"session_cockpit_{meeting_id}"


def broadcast_cockpit(meeting_id: Any) -> None:
    """
    Offene Ansichten der Sitzung benachrichtigen. Synchron aufrufbar (nach dem Commit einer Aktion).

    Ein Fehler des Channel-Layers darf die Aktion nie brechen: Die Ansichten holen den Stand dann über
    ihr Polling ab.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(cockpit_group(meeting_id), {"type": "cockpit.changed"})
    except Exception:  # noqa: BLE001 – Layer-Ausfall: Polling-Rückfall der Ansichten
        logger.warning("Cockpit-Hinweis für Sitzung %s nicht zugestellt", meeting_id, exc_info=True)


def _meeting_for_viewer(user: Any, tenant_slug: str, meeting_id: str) -> Any:
    from apps.session.services import cockpit_service

    return cockpit_service.meeting_for_viewer(user, tenant_slug, meeting_id)


class CockpitConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]  # channels ohne Typangaben
    """
    ``ws/session/<tenant_slug>/cockpit/<meeting_id>/`` – nur lesend.

    Server → Client: ``{"type": "connected"}`` nach der Anmeldung, ``{"type": "stand"}`` bei jeder Änderung.
    Nachrichten des Clients werden ignoriert; gesteuert wird ausschließlich über HTTP.
    """

    group_name: str | None = None

    async def connect(self) -> None:
        kwargs = self.scope["url_route"]["kwargs"]
        user = self.scope.get("user")
        if user is None or not getattr(user, "is_authenticated", False):
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return
        meeting_id = await database_sync_to_async(_meeting_for_viewer)(
            user, str(kwargs.get("tenant_slug", "")), str(kwargs.get("meeting_id", ""))
        )
        if meeting_id is None:
            await self.close(code=CLOSE_FORBIDDEN)
            return
        self.group_name = cockpit_group(meeting_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connected"})

    async def disconnect(self, close_code: int) -> None:
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        # Nur lesend: Aktionen laufen über HTTP mit Rechteprüfung und Audit-Log
        return

    async def cockpit_changed(self, event: dict[str, Any]) -> None:
        await self.send_json({"type": "stand"})
