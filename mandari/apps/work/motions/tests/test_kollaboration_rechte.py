# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Kollaboration: Die Zugriffsstufe folgt dem HTTP-Editor, Lesende senden keine Änderungen.

Die Stufe im WebSocket-Consumer entsteht an genau einer Stelle (``Motion.editor_access_level``)
wie im Editor: Autor:in, ``motions.edit_all`` oder eine persönliche Freigabe „Bearbeiten“
(mit ``motions.edit``) dürfen schreiben, ``motions.view`` ist Voraussetzung. Verbindungen ohne
Schreibrecht leiten nur Sync-Anfragen und Cursor weiter, keine Yjs-Änderungen.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from channels.testing import WebsocketCommunicator

from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion, MotionShare

# y-protocols: [Nachrichtentyp, Sync-Schritt, …] – 0/0 Anfrage, 0/1 Antwort, 0/2 Änderung; 1 = Cursor
SYNC_STEP1 = bytes([0, 0, 1, 0])
SYNC_UPDATE = bytes([0, 2, 3, 1, 2, 3])
SYNC_STEP2 = bytes([0, 1, 3, 1, 2, 3])
AWARENESS = bytes([1, 1, 0])


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view", "motions.edit", "motions.comment"], email="autorin@example.org")


@pytest.fixture
def dokument(org: Any, autorin: Any) -> Motion:
    return Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="organization")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("rechte", "erwartet"),
    [
        (["motions.view"], "view"),  # Standardrolle „Parteimitglied“
        (["motions.view", "motions.comment"], "comment"),
        (["motions.view", "motions.edit", "motions.comment"], "comment"),  # fremdes Dokument
        (["motions.view", "motions.edit_all"], "edit"),
        (["dashboard.view"], None),  # ohne Leserecht keine Verbindung
    ],
)
def test_stufe_folgt_dem_editor(
    org: Any, make_member: Any, dokument: Motion, rechte: list[str], erwartet: str | None
) -> None:
    mitglied = make_member(org, rechte, email="mitglied@example.org")

    assert dokument.get_collab_access_level(mitglied) == erwartet


@pytest.mark.django_db
def test_autorin_und_freigabe_bearbeiten(org: Any, make_member: Any, autorin: Any, dokument: Motion) -> None:
    kollegin = make_member(org, ["motions.view", "motions.edit", "motions.comment"], email="kollegin@example.org")
    MotionShare.objects.create(motion=dokument, scope="user", user=kollegin.user, level="edit", created_by=autorin.user)
    ohne_bearbeitungsrecht = make_member(org, ["motions.view"], email="nur-lesen@example.org")
    MotionShare.objects.create(
        motion=dokument, scope="user", user=ohne_bearbeitungsrecht.user, level="edit", created_by=autorin.user
    )

    assert dokument.get_collab_access_level(autorin) == "edit"
    assert dokument.get_collab_access_level(kollegin) == "edit"
    assert dokument.get_collab_access_level(ohne_bearbeitungsrecht) == "view"
    assert dokument.editor_access_level(autorin) == "admin"


@pytest.mark.django_db
def test_gast_erhaelt_nur_die_freigabestufe(org: Any, autorin: Any, dokument: Motion) -> None:
    from apps.common.tests.factories import MembershipFactory, UserFactory

    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    assert dokument.get_collab_access_level(gast) is None
    MotionShare.objects.create(motion=dokument, scope="user", user=gast.user, level="view", created_by=autorin.user)

    assert dokument.get_collab_access_level(gast) == "view"


async def _verbinden(user: Any, motion: Motion) -> WebsocketCommunicator:
    communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
    communicator.scope["user"] = user
    communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
    connected, _ = await communicator.connect()
    assert connected
    assert (await communicator.receive_json_from())["type"] == "connected"
    assert (await communicator.receive_json_from())["type"] == "yjs_state"
    return communicator


@pytest.mark.django_db(transaction=True)
def test_lesende_verbindung_sendet_keine_aenderungen(
    org: Any, make_member: Any, autorin: Any, dokument: Motion
) -> None:
    leser = make_member(org, ["motions.view"], email="leser@example.org")

    async def lauf() -> list[bytes]:
        schreiberin = await _verbinden(autorin.user, dokument)
        lesend = await _verbinden(leser.user, dokument)
        for data in (SYNC_UPDATE, SYNC_STEP2, SYNC_STEP1, AWARENESS):
            await lesend.send_json_to({"type": "yjs_sync", "data": _b64(data)})
        empfangen = []
        while not await schreiberin.receive_nothing(timeout=0.3):
            nachricht = await schreiberin.receive_json_from()
            if nachricht["type"] == "yjs_sync":
                empfangen.append(base64.b64decode(nachricht["data"]))
        # Umgekehrt kommen Änderungen der Schreibenden bei Lesenden an
        await schreiberin.send_json_to({"type": "yjs_sync", "data": _b64(SYNC_UPDATE)})
        weiter = await lesend.receive_json_from()
        assert base64.b64decode(weiter["data"]) == SYNC_UPDATE
        await lesend.disconnect()
        await schreiberin.disconnect()
        return empfangen

    empfangen = asyncio.run(lauf())

    assert SYNC_UPDATE not in empfangen
    assert SYNC_STEP2 not in empfangen
    assert SYNC_STEP1 in empfangen
    assert AWARENESS in empfangen
