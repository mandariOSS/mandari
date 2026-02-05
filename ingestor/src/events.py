"""
Redis Event Emission Module

Publishes sync events to Redis Pub/Sub channels for real-time notifications.
Django backend or other services can subscribe to these events.

Event Types:
- sync:started - Sync operation started
- sync:completed - Sync operation completed
- sync:failed - Sync operation failed
- entity:created - New entity synced
- entity:updated - Existing entity updated
- entity:batch - Batch of entities synced (aggregated)
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import redis.asyncio as redis
from rich.console import Console

from src.config import settings

console = Console()


class EventType(str, Enum):
    """Event types for sync operations."""

    SYNC_STARTED = "sync:started"
    SYNC_COMPLETED = "sync:completed"
    SYNC_FAILED = "sync:failed"
    ENTITY_CREATED = "entity:created"
    ENTITY_UPDATED = "entity:updated"
    ENTITY_BATCH = "entity:batch"


@dataclass
class SyncEvent:
    """Base event structure for all sync events."""

    event_type: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_url: str | None = None
    source_name: str | None = None
    body_name: str | None = None
    body_external_id: str | None = None

    # For entity events
    entity_type: str | None = None
    entity_id: str | None = None
    entity_external_id: str | None = None
    entity_name: str | None = None

    # For batch events
    entity_count: int | None = None
    entity_ids: list[str] | None = None

    # For completion events
    duration_seconds: float | None = None
    entities_synced: int | None = None
    errors_count: int | None = None

    # Additional metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Convert event to JSON string."""
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(data, default=str)


class EventEmitter:
    """
    Emits sync events to Redis Pub/Sub.

    Usage:
        async with EventEmitter() as emitter:
            await emitter.emit_sync_started(source_url="...")
            # ... sync operations ...
            await emitter.emit_entity_created(entity_type="meeting", ...)
            await emitter.emit_sync_completed(...)
    """

    # Redis channel names
    CHANNEL_SYNC = "mandari:sync"
    CHANNEL_ENTITIES = "mandari:entities"

    def __init__(self, redis_url: str | None = None, enabled: bool = True) -> None:
        """
        Initialize the event emitter.

        Args:
            redis_url: Redis connection URL (default from settings)
            enabled: Whether to emit events (can be disabled for testing)
        """
        self.redis_url = redis_url or settings.redis_url
        self.enabled = enabled and settings.events_enabled
        self._client: redis.Redis | None = None
        self._batch_buffer: list[SyncEvent] = []
        self._batch_size = 50  # Emit batch event after N entities

    async def __aenter__(self) -> "EventEmitter":
        """Async context manager entry."""
        if self.enabled:
            try:
                self._client = redis.from_url(
                    self.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
                # Test connection
                await self._client.ping()
                console.print("[dim]Event emitter connected to Redis[/dim]")
            except Exception as e:
                console.print(f"[yellow]Event emitter disabled: {e}[/yellow]")
                self.enabled = False
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        # Flush any remaining batch events
        if self._batch_buffer:
            await self._flush_batch()

        if self._client:
            await self._client.aclose()
            self._client = None

    async def _publish(self, channel: str, event: SyncEvent) -> None:
        """Publish event to Redis channel."""
        if not self.enabled or not self._client:
            return

        try:
            await self._client.publish(channel, event.to_json())
        except Exception as e:
            # Don't fail sync because of event emission
            console.print(f"[yellow]Failed to emit event: {e}[/yellow]")

    async def _flush_batch(self) -> None:
        """Flush accumulated entity events as a batch."""
        if not self._batch_buffer:
            return

        # Group by entity type
        by_type: dict[str, list[str]] = {}
        for event in self._batch_buffer:
            if event.entity_type and event.entity_external_id:
                by_type.setdefault(event.entity_type, []).append(event.entity_external_id)

        # Emit batch events per type
        for entity_type, ids in by_type.items():
            batch_event = SyncEvent(
                event_type=EventType.ENTITY_BATCH,
                entity_type=entity_type,
                entity_count=len(ids),
                entity_ids=ids[:100],  # Limit to 100 IDs in event
            )
            await self._publish(self.CHANNEL_ENTITIES, batch_event)

        self._batch_buffer.clear()

    # ========== Sync Lifecycle Events ==========

    async def emit_sync_started(
        self,
        source_url: str,
        source_name: str,
        full_sync: bool = False,
    ) -> None:
        """Emit event when sync starts."""
        event = SyncEvent(
            event_type=EventType.SYNC_STARTED,
            source_url=source_url,
            source_name=source_name,
            metadata={"full_sync": full_sync},
        )
        await self._publish(self.CHANNEL_SYNC, event)

    async def emit_sync_completed(
        self,
        source_url: str,
        source_name: str,
        duration_seconds: float,
        entities_synced: int,
        errors_count: int = 0,
    ) -> None:
        """Emit event when sync completes successfully."""
        # Flush any remaining batch events first
        await self._flush_batch()

        event = SyncEvent(
            event_type=EventType.SYNC_COMPLETED,
            source_url=source_url,
            source_name=source_name,
            duration_seconds=duration_seconds,
            entities_synced=entities_synced,
            errors_count=errors_count,
        )
        await self._publish(self.CHANNEL_SYNC, event)

    async def emit_sync_failed(
        self,
        source_url: str,
        source_name: str,
        error: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Emit event when sync fails."""
        event = SyncEvent(
            event_type=EventType.SYNC_FAILED,
            source_url=source_url,
            source_name=source_name,
            duration_seconds=duration_seconds,
            metadata={"error": error},
        )
        await self._publish(self.CHANNEL_SYNC, event)

    # ========== Entity Events ==========

    async def emit_entity_created(
        self,
        entity_type: str,
        entity_id: str,
        entity_external_id: str,
        entity_name: str | None = None,
        body_name: str | None = None,
        body_external_id: str | None = None,
        batch: bool = True,
    ) -> None:
        """
        Emit event when a new entity is created.

        Args:
            entity_type: Type of entity (meeting, paper, person, etc.)
            entity_id: Internal UUID
            entity_external_id: OParl external ID
            entity_name: Display name (optional)
            body_name: Body name (optional)
            body_external_id: Body external ID (optional)
            batch: Whether to batch events (default True for performance)
        """
        event = SyncEvent(
            event_type=EventType.ENTITY_CREATED,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_external_id=entity_external_id,
            entity_name=entity_name,
            body_name=body_name,
            body_external_id=body_external_id,
        )

        if batch:
            self._batch_buffer.append(event)
            if len(self._batch_buffer) >= self._batch_size:
                await self._flush_batch()
        else:
            await self._publish(self.CHANNEL_ENTITIES, event)

    async def emit_entity_updated(
        self,
        entity_type: str,
        entity_id: str,
        entity_external_id: str,
        entity_name: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        """Emit event when an existing entity is updated."""
        event = SyncEvent(
            event_type=EventType.ENTITY_UPDATED,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_external_id=entity_external_id,
            entity_name=entity_name,
            metadata={"changes": changes} if changes else {},
        )
        await self._publish(self.CHANNEL_ENTITIES, event)

    # ========== Convenience Methods ==========

    async def emit_new_meeting(
        self,
        meeting_id: str,
        external_id: str,
        name: str,
        body_name: str | None = None,
        start_time: datetime | None = None,
    ) -> None:
        """Emit event for a new meeting (high priority, not batched)."""
        event = SyncEvent(
            event_type=EventType.ENTITY_CREATED,
            entity_type="meeting",
            entity_id=meeting_id,
            entity_external_id=external_id,
            entity_name=name,
            body_name=body_name,
            metadata={"start_time": start_time.isoformat() if start_time else None},
        )
        # Meetings are high-priority - emit immediately
        await self._publish(self.CHANNEL_ENTITIES, event)

    async def emit_new_paper(
        self,
        paper_id: str,
        external_id: str,
        name: str,
        body_name: str | None = None,
        paper_type: str | None = None,
    ) -> None:
        """Emit event for a new paper (high priority, not batched)."""
        event = SyncEvent(
            event_type=EventType.ENTITY_CREATED,
            entity_type="paper",
            entity_id=paper_id,
            entity_external_id=external_id,
            entity_name=name,
            body_name=body_name,
            metadata={"paper_type": paper_type} if paper_type else {},
        )
        # Papers are high-priority - emit immediately
        await self._publish(self.CHANNEL_ENTITIES, event)


# Global emitter instance (lazy initialization)
_emitter: EventEmitter | None = None


async def get_emitter() -> EventEmitter:
    """Get or create the global event emitter."""
    global _emitter
    if _emitter is None:
        _emitter = EventEmitter()
        await _emitter.__aenter__()
    return _emitter


async def close_emitter() -> None:
    """Close the global event emitter."""
    global _emitter
    if _emitter:
        await _emitter.__aexit__(None, None, None)
        _emitter = None
