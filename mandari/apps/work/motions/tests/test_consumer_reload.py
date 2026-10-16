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


def _communicator(user: Any, motion: Motion, fenster: str = "") -> WebsocketCommunicator:
    pfad = f"/ws/documents/{motion.id}/" + (f"?fenster={fenster}" if fenster else "")
    communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), pfad)
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
    return communicator


def _yjs_save(html: str) -> dict[str, str]:
    return {"type": "yjs_save", "data": base64.b64encode(b"veralteter-zustand").decode("ascii"), "html": html}


async def _verbinden(user: Any, motion: Motion, fenster: str = "") -> WebsocketCommunicator:
    communicator = _communicator(user, motion, fenster)
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


@pytest.mark.django_db(transaction=True)
def test_ausloesendes_fenster_bekommt_keine_aufforderung(author: Any, motion: Motion) -> None:
    """
    Statuswechsel (#1018): Das Fenster, das ihn auslöst, hat vorher gesichert und lädt selbst neu. Es bekommt keine
    Aufforderung, und sein letztes Sichern beim Verlassen verwirft der Server nicht; andere Fenster laden neu.
    """

    async def lauf() -> None:
        ausloesend = await _verbinden(author.user, motion, "fenster-a-0001")
        anderes = await _verbinden(author.user, motion, "fenster-b-0002")
        await get_channel_layer().group_send(
            f"doc_{motion.id}", {"type": "doc.reload", "version": None, "ausser_fenster": "fenster-a-0001"}
        )
        assert (await anderes.receive_json_from())["type"] == "reload"
        assert await ausloesend.receive_nothing(timeout=0.3)

        await ausloesend.send_json_to(_yjs_save("<p>Letzter Satz</p>"))
        assert (await ausloesend.receive_json_from())["type"] == "yjs_saved"
        await anderes.send_json_to(_yjs_save("<p>Veraltet</p>"))
        assert (await anderes.receive_json_from())["type"] == "yjs_save_rejected"
        await ausloesend.disconnect()
        await anderes.disconnect()

    asyncio.run(lauf())
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Letzter Satz</p>"


@pytest.mark.django_db(transaction=True)
def test_ungueltige_fensterkennung_gilt_als_keine(author: Any, motion: Motion) -> None:
    async def lauf() -> None:
        communicator = await _verbinden(author.user, motion, "%3Cx%3E")
        await get_channel_layer().group_send(
            f"doc_{motion.id}", {"type": "doc.reload", "version": None, "ausser_fenster": ""}
        )
        assert (await communicator.receive_json_from())["type"] == "reload"
        await communicator.disconnect()

    asyncio.run(lauf())


def test_fenster_kennung() -> None:
    from apps.work.motions.consumers import fenster_kennung

    assert fenster_kennung("0f8b6c1e-2d3a-4b5c-8d9e-0a1b2c3d4e5f") == "0f8b6c1e-2d3a-4b5c-8d9e-0a1b2c3d4e5f"
    assert fenster_kennung(" lw3k2-abcdef1234 ") == "lw3k2-abcdef1234"
    assert fenster_kennung("") == ""
    assert fenster_kennung(None) == ""
    assert fenster_kennung("kurz") == ""
    assert fenster_kennung("<script>") == ""
    assert fenster_kennung("a" * 65) == ""
