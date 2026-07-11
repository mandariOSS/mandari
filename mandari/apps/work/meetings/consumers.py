# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebSocket-Consumer für die Echtzeit-Sitzungsvorbereitung.

Der einheitliche Diskussions-Thread (PaperComment für TOPs mit Vorlage,
AgendaItemNote für TOPs ohne Vorlage) wird in Echtzeit an alle Mitglieder
der Organisation verteilt. Gruppen existieren je (org_id, paper_id) und je
(org_id, agenda_item_id) — die Organisation ist damit strikt die Grenze
der Verteilung. Das bestehende 5-Sekunden-Polling der UI bleibt als
Fallback funktionsfähig.

Die REST-Endpoints (AgendaNotesAPIView / PaperCommentAPIView) rufen nach
dem Speichern broadcast_preparation_event() auf; der Consumer leitet die
gerenderten Kommentar-Objekte (JSON) an die verbundenen Clients weiter.
"""

import logging

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def item_group_name(organization_id, agenda_item_id) -> str:
    """Gruppe je (org_id, agenda_item_id)."""
    return f"prep_org_{organization_id}_item_{agenda_item_id}"


def paper_group_name(organization_id, paper_id) -> str:
    """Gruppe je (org_id, paper_id)."""
    return f"prep_org_{organization_id}_paper_{paper_id}"


def broadcast_preparation_event(organization_id, payload, *, agenda_item_id=None, paper_id=None):
    """
    Sendet ein Ereignis (z.B. gerendertes Kommentar-Objekt) an die
    Vorbereitungs-Gruppen der Organisation. Sync aufrufbar (aus Views).

    Fehler beim Broadcast dürfen den Request nie brechen — Polling-Fallback.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    groups = []
    if agenda_item_id:
        groups.append(item_group_name(organization_id, agenda_item_id))
    if paper_id:
        groups.append(paper_group_name(organization_id, paper_id))
    for group in groups:
        try:
            async_to_sync(channel_layer.group_send)(group, {"type": "preparation.event", "payload": payload})
        except Exception as e:  # pragma: no cover - Layer-Ausfall
            logger.warning(f"Preparation-Broadcast an {group} fehlgeschlagen: {e}")


class PreparationConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket-Consumer für einen TOP bzw. eine Vorlage der Sitzungsvorbereitung.

    URL: ws/preparation/<org_slug>/<scope>/<object_id>/
        scope = "item"  -> object_id ist eine OParlAgendaItem-ID
        scope = "paper" -> object_id ist eine OParlPaper-ID

    Zugriffsprüfung wie die bestehenden REST-APIs: aktive Membership der
    Organisation (Org-Grenze). Server -> Client Nachrichten:
        {"type": "comment", "event": "created"|"deleted", "comment": {...}}
        {"type": "position", "event": "updated", "position": {...}}
    """

    async def connect(self):
        kwargs = self.scope["url_route"]["kwargs"]
        self.org_slug = kwargs["org_slug"]
        self.scope_type = kwargs["scope"]
        self.object_id = kwargs["object_id"]
        self.group_name = None

        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return

        organization_id = await self._check_access(user)
        if organization_id is None:
            await self.close(code=4403)
            return

        if self.scope_type == "paper":
            self.group_name = paper_group_name(organization_id, self.object_id)
        else:
            self.group_name = item_group_name(organization_id, self.object_id)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connected", "group_scope": self.scope_type})

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Schreiboperationen laufen ausschließlich über die REST-APIs;
        # der Socket ist reiner Verteilkanal.
        pass

    async def preparation_event(self, event):
        """Gruppen-Ereignis an den Client weiterleiten."""
        await self.send_json(event["payload"])

    @database_sync_to_async
    def _check_access(self, user):
        """Aktive Membership in der Organisation + Existenz des Zielobjekts prüfen."""
        from apps.tenants.models import Membership

        membership = (
            Membership.objects.filter(user=user, organization__slug=self.org_slug, is_active=True)
            .select_related("organization")
            .first()
        )
        if membership is None:
            return None

        if self.scope_type == "paper":
            from insight_core.models import OParlPaper

            if not OParlPaper.objects.filter(id=self.object_id).exists():
                return None
        else:
            from insight_core.models import OParlAgendaItem

            if not OParlAgendaItem.objects.filter(id=self.object_id).exists():
                return None

        return membership.organization_id
