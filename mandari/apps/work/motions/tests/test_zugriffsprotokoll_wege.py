# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugriffe auf Dokumente in „Nichtöffentliche Vorgänge“ stehen in der Änderungshistorie (Issue #873).

Neben Editoraufruf, Download, Export und früheren Fassungen (``test_nichtoeffentliche_vorgaenge.py``) liefern zwei
weitere Wege den Inhalt, ohne dass die Editorseite vorher geöffnet sein muss:

- die Verbindung zur Live-Bearbeitung (sie sendet den Editorzustand),
- die Einreichungsvorschau.

Beide schreiben einen Eintrag mit Person und Art des Zugriffs. Dokumente außerhalb des Ordners bleiben ohne Eintrag.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from channels.testing import WebsocketCommunicator
from django.urls import reverse

from apps.work.faction.models import FactionAuditLog
from apps.work.motions import non_public
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import DocumentFolder, Motion

RECHTE = [
    "motions.view",
    "motions.view_drafts",
    "motions.edit",
    "motions.edit_all",
    "motions.comment",
    "motions.submit_to_ris",
    "faction.view_non_public",
]


def _vereidigt(org: Any, make_member: Any, email: str) -> Any:
    member = make_member(org, RECHTE, email=email)
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return _vereidigt(org, make_member, "autorin@example.org")


@pytest.fixture
def kollege(org: Any, make_member: Any) -> Any:
    return _vereidigt(org, make_member, "kollege@example.org")


@pytest.fixture
def nicht_vereidigt(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="nicht-vereidigt@example.org")


@pytest.fixture
def unterlage(org: Any, autorin: Any) -> Motion:
    ordner = cast(Any, DocumentFolder).non_public_folder(org, created_by=autorin)
    motion = Motion(
        organization=org, author=autorin, title="Unterlage", status="archived", visibility="private", folder=ordner
    )
    cast(Any, motion).set_content_encrypted("<p>Grundstück Musterweg</p>")
    motion.save()
    return motion


@pytest.fixture
def offenes_dokument(org: Any, autorin: Any) -> Motion:
    motion = Motion(organization=org, author=autorin, title="Antrag", visibility="organization")
    cast(Any, motion).set_content_encrypted("<p>Öffentlich</p>")
    motion.save()
    return motion


def _zugriffe(org: Any) -> list[tuple[str, Any]]:
    return [
        (eintrag.changes["zugriff"], eintrag.membership_id)
        for eintrag in FactionAuditLog.objects.filter(
            organization=org, action="internal_document_access", is_internal=True
        ).order_by("created_at")
    ]


def _verbinden(user: Any, motion: Motion) -> bool:
    async def lauf() -> bool:
        communicator = WebsocketCommunicator(DocumentCollaborationConsumer.as_asgi(), f"/ws/documents/{motion.id}/")
        communicator.scope["user"] = user
        communicator.scope["url_route"] = {"kwargs": {"document_id": str(motion.id)}}
        connected, _ = await communicator.connect()
        if connected:
            assert (await communicator.receive_json_from())["type"] == "connected"
            assert (await communicator.receive_json_from())["type"] == "yjs_state"
            await communicator.disconnect()
        return bool(connected)

    return asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_verbindung_zur_live_bearbeitung_steht_in_der_aenderungshistorie(
    org: Any, kollege: Any, unterlage: Motion
) -> None:
    """Ohne vorherigen Editoraufruf: Die Verbindung allein liefert den Editorzustand."""
    assert _verbinden(kollege.user, unterlage)

    assert _zugriffe(org) == [(non_public.ACCESS_COLLABORATION, kollege.id)]
    eintrag = FactionAuditLog.objects.get(organization=org, action="internal_document_access")
    assert eintrag.object_id == unterlage.pk


@pytest.mark.django_db(transaction=True)
def test_abgewiesene_verbindung_und_offene_dokumente_bleiben_ohne_eintrag(
    org: Any, kollege: Any, nicht_vereidigt: Any, unterlage: Motion, offenes_dokument: Motion
) -> None:
    assert not _verbinden(nicht_vereidigt.user, unterlage)
    assert _verbinden(kollege.user, offenes_dokument)

    assert _zugriffe(org) == []


@pytest.mark.django_db
def test_einreichungsvorschau_steht_in_der_aenderungshistorie(
    org: Any, kollege: Any, unterlage: Motion, offenes_dokument: Motion, client_for: Any
) -> None:
    client = client_for(kollege.user)

    antwort = client.get(reverse("work:document_submit_ris", kwargs={"org_slug": org.slug, "motion_id": unterlage.id}))

    assert antwort.status_code == 200 and "Grundstück Musterweg" in antwort.content.decode()
    assert _zugriffe(org) == [(non_public.ACCESS_SUBMISSION_PREVIEW, kollege.id)]

    offen = client.get(
        reverse("work:document_submit_ris", kwargs={"org_slug": org.slug, "motion_id": offenes_dokument.id})
    )
    assert offen.status_code == 200
    assert len(_zugriffe(org)) == 1
