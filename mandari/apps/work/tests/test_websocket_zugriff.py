# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebSocket-Zugriff folgt denselben Grenzen wie die Seiten über HTTP.

- Konten mit offener Pflicht zum zweiten Faktor bekommen keine Verbindung (ASGI-Hülle, Zugriffsprüfung der
  Consumer).
- Mitglieder einer gesperrten oder beendeten Organisation bekommen keine Verbindung.
- Der Verteilkanal der Sitzungsvorbereitung prüft den Zugriff vor jedem Ereignis nach.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache

from apps.accounts.websocket import TwoFactorWebSocketMiddleware, requirement_open
from apps.work.meetings.consumers import PreparationConsumer, item_group_name
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

VORSITZ = [
    "faction.view_public",
    "faction.manage",
    "protocols.create",
    "protocols.edit",
    "meetings.prepare",
    "motions.view",
    "motions.create",
    "motions.edit",
]
GEHEIM = {"type": "comment", "event": "created", "comment": {"text": "interne Position"}}


@pytest.fixture(autouse=True)
def _cache_leeren() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, VORSITZ, email="vorsitz@example.org")


@pytest.fixture
def pflicht(settings: Any, org: Any) -> None:
    """Die Organisation verlangt den zweiten Faktor; das Mitglied hat keinen eingerichtet."""
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = []
    org.require_2fa = True
    org.save(update_fields=["require_2fa"])


def _sperren(org: Any) -> None:
    org.is_active = False
    org.save(update_fields=["is_active"])


# --- Verbindungen aufbauen -----------------------------------------------------------------------------------


def _vorbereitung(user: Any, org: Any, objekt: str) -> WebsocketCommunicator:
    kommunikator = WebsocketCommunicator(PreparationConsumer.as_asgi(), f"/ws/preparation/{org.slug}/item/{objekt}/")
    kommunikator.scope["user"] = user
    kommunikator.scope["url_route"] = {"kwargs": {"org_slug": org.slug, "scope": "item", "object_id": objekt}}
    return kommunikator


def _abgelehnt(kommunikator: WebsocketCommunicator) -> bool:
    async def lauf() -> bool:
        verbunden, code = await kommunikator.connect()
        await kommunikator.disconnect()
        return not verbunden and code == 4403

    return asyncio.run(lauf())


def _dokument_zugriff(user: Any, dokument: Motion) -> Any:
    consumer = cast(Any, DocumentCollaborationConsumer)()
    consumer.document_id = str(dokument.pk)
    consumer.user = user
    return async_to_sync(consumer._check_access)()[0]


def _vorbereitung_zugriff(user: Any, org: Any, objekt: str) -> Any:
    consumer = cast(Any, PreparationConsumer)()
    consumer.org_slug = org.slug
    consumer.scope_type = "item"
    consumer.object_id = objekt
    return async_to_sync(consumer._check_access)(user)


@pytest.fixture
def objekt(db: None) -> str:
    """Kennung eines Tagesordnungspunkts im RIS."""
    basis = f"https://ris.example.org/{uuid.uuid4().hex}/"
    source = OParlSource.objects.create(name="RIS", url=f"{basis}system")
    body = OParlBody.objects.create(external_id=f"{basis}body", source=source, name="Musterstadt")
    sitzung = OParlMeeting.objects.create(external_id=f"{basis}meeting", body=body, name="Rat")
    punkt = OParlAgendaItem.objects.create(external_id=f"{basis}agendaitem", meeting=sitzung, name="Haushalt")
    return str(punkt.pk)


# --- Pflicht zum zweiten Faktor -------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_huelle_lehnt_konten_mit_offener_pflicht_ab(mitglied: Any, pflicht: None) -> None:
    gestartet: list[str] = []

    async def innen(scope: Any, receive: Any, send: Any) -> None:
        gestartet.append(scope["type"])
        await receive()
        await send({"type": "websocket.accept"})

    async def lauf(user: Any) -> tuple[bool, Any]:
        kommunikator = WebsocketCommunicator(TwoFactorWebSocketMiddleware(innen), "/ws/irgendwas/")
        kommunikator.scope["user"] = user
        ergebnis: tuple[bool, Any] = await kommunikator.connect()
        await kommunikator.disconnect()
        return ergebnis

    assert requirement_open(mitglied.user)
    assert asyncio.run(lauf(mitglied.user)) == (False, 4403)
    assert gestartet == []


@pytest.mark.django_db(transaction=True)
def test_huelle_laesst_konten_ohne_pflicht_und_anonyme_durch(mitglied: Any) -> None:
    from django.contrib.auth.models import AnonymousUser

    gestartet: list[str] = []

    async def innen(scope: Any, receive: Any, send: Any) -> None:
        gestartet.append(scope["type"])
        await receive()
        await send({"type": "websocket.accept"})

    async def lauf(user: Any) -> bool:
        kommunikator = WebsocketCommunicator(TwoFactorWebSocketMiddleware(innen), "/ws/irgendwas/")
        kommunikator.scope["user"] = user
        verbunden, _ = await kommunikator.connect()
        await kommunikator.disconnect()
        return bool(verbunden)

    # Ohne Pflicht verbunden; anonyme Verbindungen weist weiter der Consumer selbst ab (4401)
    assert asyncio.run(lauf(mitglied.user))
    assert asyncio.run(lauf(AnonymousUser()))
    assert gestartet == ["websocket", "websocket"]


@pytest.mark.django_db(transaction=True)
def test_zugriffspruefungen_verlangen_die_erfuellte_pflicht(
    org: Any, mitglied: Any, objekt: str, settings: Any
) -> None:
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")
    assert _dokument_zugriff(mitglied.user, dokument) == "edit"
    assert _vorbereitung_zugriff(mitglied.user, org, objekt) == org.id

    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = []
    org.require_2fa = True
    org.save(update_fields=["require_2fa"])

    assert _dokument_zugriff(mitglied.user, dokument) is None
    assert _vorbereitung_zugriff(mitglied.user, org, objekt) is None


# --- Gesperrte oder beendete Organisation ------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gesperrte_organisation_bekommt_keine_verbindung(org: Any, mitglied: Any, objekt: str) -> None:
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")

    assert _dokument_zugriff(mitglied.user, dokument) == "edit"
    assert _vorbereitung_zugriff(mitglied.user, org, objekt) == org.id

    _sperren(org)

    assert _dokument_zugriff(mitglied.user, dokument) is None
    assert _vorbereitung_zugriff(mitglied.user, org, objekt) is None
    assert _abgelehnt(_vorbereitung(mitglied.user, org, objekt))


# --- Sitzungsvorbereitung: Nachprüfung während der Verbindung ----------------------------------------------------


async def _vorbereitung_verbinden(user: Any, org: Any, objekt: str) -> WebsocketCommunicator:
    kommunikator = _vorbereitung(user, org, objekt)
    verbunden, _ = await kommunikator.connect()
    assert verbunden
    assert (await kommunikator.receive_json_from())["type"] == "connected"
    return kommunikator


async def _ereignis(org: Any, objekt: str) -> None:
    await get_channel_layer().group_send(
        item_group_name(org.id, objekt), {"type": "preparation.event", "payload": GEHEIM}
    )


@pytest.mark.django_db(transaction=True)
def test_vorbereitung_verteilt_weiter_an_berechtigte(org: Any, mitglied: Any, objekt: str) -> None:
    async def lauf() -> None:
        kommunikator = await _vorbereitung_verbinden(mitglied.user, org, objekt)
        await _ereignis(org, objekt)
        assert await kommunikator.receive_json_from(timeout=5) == GEHEIM
        await kommunikator.disconnect()

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_vorbereitung_prueft_den_zugriff_vor_jedem_ereignis(org: Any, mitglied: Any, objekt: str) -> None:
    """Nach dem Entzug der Mitgliedschaft wird das nächste Ereignis nicht mehr gesendet, die Verbindung endet."""
    from apps.tenants.models import Membership

    async def lauf() -> None:
        kommunikator = await _vorbereitung_verbinden(mitglied.user, org, objekt)

        await database_sync_to_async(Membership.objects.filter(pk=mitglied.pk).update)(is_active=False)
        await _ereignis(org, objekt)

        assert await kommunikator.receive_output(timeout=5) == {"type": "websocket.close", "code": 4403}
        # Danach kommt nichts mehr an, auch wenn die Gruppe noch ein Ereignis erhält
        await _ereignis(org, objekt)
        assert await kommunikator.receive_nothing(timeout=0.5)

    asyncio.run(lauf())
