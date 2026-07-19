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
from django.utils import timezone

logger = logging.getLogger(__name__)

# Serverseitige Revisionen im Kollaborationsmodus: höchstens alle 10 Minuten
# (zusätzlich beim Disconnect des letzten Teilnehmers).
REVISION_MIN_INTERVAL_SECONDS = 600

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
    - yjs_save:  {"type": "yjs_save", "data": "<base64>", "html": "<p>…</p>"} — Explicit save request
                 (html optional: aktueller Editor-Inhalt für content_encrypted + Revisionen)
    - connected: {"type": "connected", "user": {...}}       — Server → Client on connect
    - yjs_state: {"type": "yjs_state", "data": "<base64>"} — Server → Client (initial state)
    - presence:  {"type": "presence", "users": [...]}       — Server → Client
    - reload:    {"type": "reload"}                         — Server → Client (z.B. nach Revision-Restore)
    """

    # Teilnehmer pro Dokument (prozesslokal). Bei mehreren Workern ist die
    # Zählung pro Prozess — die Disconnect-Revision ist dann konservativ
    # (wird ggf. öfter geprüft), aber niemals falsch, da inhaltsgleiche
    # Revisionen übersprungen werden.
    _participants: dict[str, int] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.document_id = None
        self.group_name = None
        self.user = None
        self.user_info = None
        self.membership_id = None
        self._counted = False
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
        access, membership_id = await self._check_access()
        if not access:
            await self.close(code=4403)
            return
        self.membership_id = membership_id

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

        # Track participants (for last-disconnect revision snapshot)
        self._participants[self.document_id] = self._participants.get(self.document_id, 0) + 1
        self._counted = True

        # Send connection confirmation
        await self.send_json(
            {
                "type": "connected",
                "user": self.user_info,
            }
        )

        # Always send yjs_state (even if null) so client knows when to seed from HTML
        yjs_state = await self._load_yjs_state()
        await self.send_json(
            {
                "type": "yjs_state",
                "data": base64.b64encode(yjs_state).decode("ascii") if yjs_state else None,
            }
        )

        logger.info(f"User {self.user.id} connected to document {self.document_id}")

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

        remaining = None
        if self._counted and self.document_id in self._participants:
            self._participants[self.document_id] = max(0, self._participants[self.document_id] - 1)
            remaining = self._participants[self.document_id]
            if remaining == 0:
                self._participants.pop(self.document_id, None)

        # Letzter Teilnehmer weg → Revision vom aktuellen Stand sichern
        # (übersprungen, wenn sich seit der letzten Revision nichts geändert hat).
        if remaining == 0 and self.membership_id:
            try:
                await self._create_disconnect_revision()
            except Exception as e:
                logger.warning(f"Failed to create disconnect revision for {self.document_id}: {e}")

        logger.info(
            f"User {getattr(self.user, 'id', '?')} disconnected from document {self.document_id} (code={close_code})"
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
            # Client sends full state (plus aktuelles HTML) for persistence
            await self._persist_yjs_state(content.get("data", ""), content.get("html"))

    # --- Group message handlers ---

    async def yjs_sync(self, event):
        """Forward Yjs sync to client (skip sender)."""
        if event.get("sender_channel") != self.channel_name:
            await self.send_json(
                {
                    "type": "yjs_sync",
                    "data": event["data"],
                }
            )

    async def awareness_update(self, event):
        """Forward awareness update to client (skip sender)."""
        if event.get("sender_channel") != self.channel_name:
            await self.send_json(
                {
                    "type": "awareness",
                    "data": event["data"],
                    "user_info": event.get("user_info"),
                }
            )

    async def doc_reload(self, event):
        """Ask all clients to reload the document (e.g. after revision restore)."""
        await self.send_json(
            {
                "type": "reload",
                "version": event.get("version"),
            }
        )

    # --- Database helpers ---

    @database_sync_to_async
    def _check_access(self):
        """Check document access. Returns (access_level, membership_id) or (None, None)."""
        from apps.tenants.models import Membership

        from .models import Motion

        try:
            motion = Motion.objects.get(id=self.document_id)
        except Motion.DoesNotExist:
            return None, None

        try:
            membership = Membership.objects.get(
                user=self.user,
                organization=motion.organization,
                is_active=True,
            )
        except Membership.DoesNotExist:
            return None, None

        # Zentrale Stufenlogik (Motion.get_collab_access_level):
        # can_edit/can_comment berücksichtigen Autor, Berechtigungen UND
        # Share-Level (inkl. Gast-/Ordner-Freigaben) als Obergrenze - sonst
        # könnten per Freigabe Bearbeitungsberechtigte im Kollab-Modus
        # schreiben, ohne dass gespeichert wird. Zusätzlich greift die
        # Status-Sperre (gesperrte Status frieren die Bearbeitung ein,
        # motions.edit_all darf trotzdem) - dieselbe Logik wie im HTTP-Editor.
        level = motion.get_collab_access_level(membership)
        if level is None:
            return None, None
        return level, membership.id

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
    def _persist_yjs_state(self, data_b64, html=None):
        """
        Save Yjs state (and optionally the rendered HTML) to the database.

        Wenn der Client HTML mitliefert und der Nutzer Schreibrecht hat, wird
        content_encrypted aktuell gehalten (wichtig für Export/Suche) und —
        gedrosselt auf REVISION_MIN_INTERVAL_SECONDS — eine MotionRevision
        angelegt.
        """
        from .models import Motion

        if not data_b64 and html is None:
            return

        try:
            motion = Motion.objects.get(id=self.document_id)
        except Motion.DoesNotExist:
            return

        try:
            update_fields = []
            can_write = bool(self.user_info and self.user_info.get("access_level") == "edit")
            # Die Stufe stammt vom Verbindungsaufbau — wechselt das Dokument
            # währenddessen in einen gesperrten Status (Statuswechsel-View
            # broadcastet reload), darf ein noch offener Client nicht weiter
            # persistieren. Serverseitig nachprüfen statt dem Client vertrauen;
            # motions.edit_all behält über get_collab_access_level 'edit'.
            if can_write and motion.is_status_locked:
                from apps.tenants.models import Membership

                membership = Membership.objects.filter(id=self.membership_id, is_active=True).first()
                can_write = membership is not None and motion.get_collab_access_level(membership) == "edit"
            # Auch der Yjs-Binärzustand darf nur von Schreibberechtigten
            # persistiert werden - sonst könnte ein Nutzer mit view/comment den
            # gemeinsamen Dokumentzustand überschreiben (wird beim naechsten
            # Verbindungsaufbau an alle Clients ausgeliefert).
            if data_b64 and can_write:
                motion.yjs_document = base64.b64decode(data_b64)
                update_fields.append("yjs_document")

            content_changed = False
            if html is not None and can_write:
                try:
                    old_content = motion.get_content_decrypted()
                except Exception:
                    old_content = ""
                if html and html != old_content:
                    motion.set_content_encrypted(html)
                    update_fields.append("content_encrypted")
                    content_changed = True

            if update_fields:
                motion.save(update_fields=update_fields + ["updated_at"])

            if content_changed:
                self._create_revision_if_due(motion, html)
        except Exception as e:
            logger.warning(f"Failed to persist Yjs state for {self.document_id}: {e}")

    def _create_revision_if_due(self, motion, content, force=False):
        """
        Create a MotionRevision if the throttle window has passed (or force=True)
        and the content actually differs from the latest revision.

        Runs synchronously — call from within a database_sync_to_async context.
        """
        from .models import MotionRevision

        if not content or not self.membership_id:
            return

        last_revision = motion.revisions.order_by("-created_at").first()

        if not force and last_revision:
            age = (timezone.now() - last_revision.created_at).total_seconds()
            if age < REVISION_MIN_INTERVAL_SECONDS:
                return

        if last_revision:
            try:
                if last_revision.get_content_decrypted() == content:
                    return
            except Exception:
                pass

        try:
            revision = MotionRevision(
                motion=motion,
                version=motion.revisions.count() + 1,
                changed_by_id=self.membership_id,
                change_summary="Automatische Sicherung (Kollaboration)",
            )
            revision.set_content_encrypted(content)
            revision.save()
        except Exception as e:
            logger.warning(f"Failed to create collab revision for {self.document_id}: {e}")

    @database_sync_to_async
    def _create_disconnect_revision(self):
        """Snapshot the current content when the last participant leaves."""
        from .models import Motion

        try:
            motion = Motion.objects.get(id=self.document_id)
        except Motion.DoesNotExist:
            return

        try:
            content = motion.get_content_decrypted()
        except Exception:
            return
        self._create_revision_if_due(motion, content, force=True)
