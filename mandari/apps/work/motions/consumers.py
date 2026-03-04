# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebSocket Consumer for real-time document collaboration.

Handles Yjs synchronization, awareness (cursor positions), and
presence tracking for the document editor.

Protocol: JSON messages with base64-encoded binary Yjs data.
"""

import base64
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)

# Colors for collaborator cursors (deterministic from user ID)
CURSOR_COLORS = [
    "#3b82f6",  # blue
    "#ef4444",  # red
    "#22c55e",  # green
    "#f59e0b",  # amber
    "#8b5cf6",  # violet
    "#ec4899",  # pink
    "#14b8a6",  # teal
    "#f97316",  # orange
]


class DocumentCollaborationConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer for real-time document collaboration.

    Client ↔ Server protocol (JSON messages):
    - yjs_sync:  {"type": "yjs_sync", "data": "<base64>"}  — Yjs sync/update data
    - awareness: {"type": "awareness", "data": "<base64>"} — Cursor/selection state
    - yjs_save:  {"type": "yjs_save", "data": "<base64>"}  — Explicit save request
    - connected: {"type": "connected", "user": {...}}       — Server → Client on connect
    - yjs_state: {"type": "yjs_state", "data": "<base64>"} — Server → Client (initial state)
    - presence:  {"type": "presence", "users": [...]}       — Server → Client
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.document_id = None
        self.group_name = None
        self.user = None
        self.user_info = None
        self._save_counter = 0

    async def connect(self):
        self.document_id = self.scope["url_route"]["kwargs"]["document_id"]
        self.group_name = f"doc_{self.document_id}"
        self.user = self.scope.get("user")

        # Reject anonymous users
        if not self.user or self.user.is_anonymous:
            await self.close(code=4401)
            return

        # Check document access
        access = await self._check_access()
        if not access:
            await self.close(code=4403)
            return

        # Assign cursor color deterministically
        color_index = hash(str(self.user.id)) % len(CURSOR_COLORS)

        self.user_info = {
            "id": str(self.user.id),
            "name": await self._get_display_name(),
            "color": CURSOR_COLORS[color_index],
            "access_level": access,
        }

        # Join the document group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send connection confirmation
        await self.send_json({
            "type": "connected",
            "user": self.user_info,
        })

        # Always send yjs_state (even if null) so client knows when to seed from HTML
        yjs_state = await self._load_yjs_state()
        await self.send_json({
            "type": "yjs_state",
            "data": base64.b64encode(yjs_state).decode("ascii") if yjs_state else None,
        })

        logger.info(f"User {self.user.id} connected to document {self.document_id}")

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

        logger.info(
            f"User {getattr(self.user, 'id', '?')} disconnected from "
            f"document {self.document_id} (code={close_code})"
        )

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")

        if msg_type == "yjs_sync":
            # Broadcast Yjs sync update to all other clients
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "yjs.sync",
                    "data": content.get("data", ""),
                    "sender_channel": self.channel_name,
                },
            )

        elif msg_type == "awareness":
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "awareness.update",
                    "data": content.get("data", ""),
                    "sender_channel": self.channel_name,
                    "user_info": self.user_info,
                },
            )

        elif msg_type == "yjs_save":
            # Client sends full state for persistence
            await self._persist_yjs_state(content.get("data", ""))

    # --- Group message handlers ---

    async def yjs_sync(self, event):
        """Forward Yjs sync to client (skip sender)."""
        if event.get("sender_channel") != self.channel_name:
            await self.send_json({
                "type": "yjs_sync",
                "data": event["data"],
            })

    async def awareness_update(self, event):
        """Forward awareness update to client (skip sender)."""
        if event.get("sender_channel") != self.channel_name:
            await self.send_json({
                "type": "awareness",
                "data": event["data"],
                "user_info": event.get("user_info"),
            })

    # --- Database helpers ---

    @database_sync_to_async
    def _check_access(self):
        """Check document access. Returns access level string or None."""
        from apps.tenants.models import Membership

        from .models import Motion

        try:
            motion = Motion.objects.get(id=self.document_id)
        except Motion.DoesNotExist:
            return None

        try:
            membership = Membership.objects.get(
                user=self.user,
                organization=motion.organization,
                is_active=True,
            )
        except Membership.DoesNotExist:
            return None

        if not motion.can_access(membership):
            return None

        if motion.author == membership or membership.has_permission("motions.edit_all"):
            return "edit"
        if membership.has_permission("motions.comment"):
            return "comment"
        return "view"

    @database_sync_to_async
    def _get_display_name(self):
        return self.user.get_full_name() or self.user.email

    @database_sync_to_async
    def _load_yjs_state(self):
        """Load persisted Yjs document state."""
        from .models import Motion

        try:
            motion = Motion.objects.only("yjs_document").get(id=self.document_id)
            return bytes(motion.yjs_document) if motion.yjs_document else None
        except Motion.DoesNotExist:
            return None

    @database_sync_to_async
    def _persist_yjs_state(self, data_b64):
        """Save Yjs state to database."""
        from .models import Motion

        if not data_b64:
            return
        try:
            raw = base64.b64decode(data_b64)
            Motion.objects.filter(id=self.document_id).update(yjs_document=raw)
        except Exception as e:
            logger.warning(f"Failed to persist Yjs state for {self.document_id}: {e}")
