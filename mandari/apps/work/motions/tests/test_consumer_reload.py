# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kollaborations-Consumer nach einer Reload-Aufforderung (#184, gefunden über #289).

Speichert eine Person ohne Verbindung per POST, verwirft der Server den Yjs-Zustand und
fordert verbundene Clients zum Neuladen auf. Ein verbundener Client schickt aber vor dem
Neuladen noch ein ``yjs_save`` (Tab wird verborgen, ``beforeunload``, ``destroy``) – mit
seinem veralteten Stand. Das darf den gerade gespeicherten Inhalt nicht überschreiben.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, cast

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion

PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def motion(org: Any, author: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=author, title="Antrag", summary="Kurzfassung", visibility="organization"
    )
    cast(Any, motion).set_content_encrypted("<p>Alt Alpha Offline</p>")
    motion.save()
    return motion


def _communicator(user: Any, motion: Motion) -> WebsocketCommunicator:
    communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
    return communicator


def _yjs_save(html: str) -> dict[str, str]:
    return {"type": "yjs_save", "data": base64.b64encode(b"veralteter-zustand").decode("ascii"), "html": html}


async def _verbinden(user: Any, motion: Motion) -> WebsocketCommunicator:
    communicator = _communicator(user, motion)
    connected, _ = await communicator.connect()
    assert connected
    assert (await communicator.receive_json_from())["type"] == "connected"
    assert (await communicator.receive_json_from())["type"] == "yjs_state"
    return communicator


@pytest.mark.django_db(transaction=True)
def test_yjs_save_vor_reload_wird_gespeichert(author: Any, motion: Motion) -> None:
    async def lauf() -> None:
        communicator = await _verbinden(author.user, motion)
        await communicator.send_json_to(_yjs_save("<p>Alt Alpha Beta</p>"))
        antwort = await communicator.receive_json_from()
        assert antwort["type"] == "yjs_saved"
        await communicator.disconnect()

    asyncio.run(lauf())
    motion.refresh_from_db()
    assert motion.get_yjs_state() == b"veralteter-zustand"
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt Alpha Beta</p>"


@pytest.mark.django_db(transaction=True)
def test_yjs_save_nach_reload_wird_verworfen(author: Any, motion: Motion) -> None:
    async def lauf() -> None:
        communicator = await _verbinden(author.user, motion)
        # Der POST-Speichern-Pfad broadcastet doc.reload an die Dokumentgruppe
        await get_channel_layer().group_send(f"doc_{motion.id}", {"type": "doc.reload", "version": None})
        assert (await communicator.receive_json_from())["type"] == "reload"

        # Der Client sichert beim Verbergen/Verlassen noch seinen veralteten Stand
        await communicator.send_json_to(_yjs_save("<p>Alt Alpha</p>"))
        antwort = await communicator.receive_json_from()
        assert antwort == {"type": "yjs_save_rejected", "reason": "reload_pending"}
        await communicator.disconnect()

    asyncio.run(lauf())
    motion.refresh_from_db()
    assert motion.get_yjs_state() is None, "veralteter Yjs-Zustand darf nach reload nicht persistiert werden"
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt Alpha Offline</p>"
