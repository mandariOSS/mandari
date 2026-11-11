# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebSocket-Zugriff folgt denselben Grenzen wie die Seiten über HTTP.

- Konten mit offener Pflicht zum zweiten Faktor bekommen keine Verbindung (ASGI-Hülle) und verlieren offene
  Verbindungen bei der nächsten Nachprüfung (Zugriffsprüfung der Consumer).
- Mitglieder einer gesperrten oder beendeten Organisation bekommen keine Verbindung; offene Verbindungen prüfen
  beim Sperren sofort neu.
- Der Verteilkanal der Sitzungsvorbereitung prüft den Zugriff während der Verbindung nach.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.websocket import TwoFactorWebSocketMiddleware, requirement_open
from apps.work.faction.live import ProtokollNotizenConsumer
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.meetings import consumers as vorbereitung
from apps.work.meetings.consumers import PreparationConsumer, item_group_name
from apps.work.motions import freigaben
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion

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
def top(org: Any, mitglied: Any) -> FactionAgendaItem:
    sitzung = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(minutes=10),
        status="ongoing",
        created_by=mitglied,
    )
    return FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public", order=1)


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


def _notizen(user: Any, item: FactionAgendaItem) -> WebsocketCommunicator:
    kommunikator = WebsocketCommunicator(ProtokollNotizenConsumer.as_asgi(), f"/ws/fraktion/notizen/{item.pk}/")
    kommunikator.scope["user"] = user
    kommunikator.scope["url_route"] = {"kwargs": {"item_id": str(item.pk)}}
    return kommunikator


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
def objekt(monkeypatch: pytest.MonkeyPatch) -> str:
    """Kennung eines Tagesordnungspunkts im RIS; seine Existenz ist hier nicht Thema."""
    monkeypatch.setattr("hub.ris.selectors.agenda_item_exists", lambda kennung: True)
    return str(uuid.uuid4())


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
    org: Any, mitglied: Any, top: FactionAgendaItem, objekt: str, settings: Any
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
    assert _abgelehnt(_notizen(mitglied.user, top))


# --- Gesperrte oder beendete Organisation ------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gesperrte_organisation_bekommt_keine_verbindung(
    org: Any, mitglied: Any, top: FactionAgendaItem, objekt: str
) -> None:
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")

    assert _dokument_zugriff(mitglied.user, dokument) == "edit"
    assert _vorbereitung_zugriff(mitglied.user, org, objekt) == org.id

    _sperren(org)

    assert _dokument_zugriff(mitglied.user, dokument) is None
    assert _vorbereitung_zugriff(mitglied.user, org, objekt) is None
    assert _abgelehnt(_notizen(mitglied.user, top))
    assert _abgelehnt(_vorbereitung(mitglied.user, org, objekt))


@pytest.mark.django_db(transaction=True)
def test_sperre_laesst_offene_verbindungen_der_mitglieder_neu_pruefen(
    org: Any, mitglied: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    weitere = make_member(org, VORSITZ, email="weitere@example.org")
    gesendet: list[list[str]] = []
    monkeypatch.setattr(freigaben, "_send", gesendet.append)

    org.name = "Neuer Name"
    org.save(update_fields=["name"])
    assert gesendet == []

    _sperren(org)

    assert gesendet == [sorted([freigaben.user_group(mitglied.user_id), freigaben.user_group(weitere.user_id)])]


@pytest.mark.django_db(transaction=True)
def test_sperre_beendet_offene_notizen_verbindung(org: Any, mitglied: Any, top: FactionAgendaItem) -> None:
    async def lauf() -> None:
        kommunikator = _notizen(mitglied.user, top)
        verbunden, _ = await kommunikator.connect()
        assert verbunden
        while not await kommunikator.receive_nothing(timeout=0.3):
            await kommunikator.receive_json_from()

        await database_sync_to_async(_sperren)(org)

        nachricht: dict[str, Any] = {}
        while nachricht.get("type") != "reload":
            nachricht = await kommunikator.receive_json_from(timeout=5)
        assert nachricht == {"type": "reload", "reason": "access_revoked"}
        assert await kommunikator.receive_output(timeout=5) == {"type": "websocket.close", "code": 4403}

    asyncio.run(lauf())


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
def test_vorbereitung_trennt_sofort_nach_entzug_der_mitgliedschaft(org: Any, mitglied: Any, objekt: str) -> None:
    def entziehen() -> None:
        mitglied.is_active = False
        mitglied.save(update_fields=["is_active"])

    async def lauf() -> None:
        kommunikator = await _vorbereitung_verbinden(mitglied.user, org, objekt)

        await database_sync_to_async(entziehen)()

        assert await kommunikator.receive_output(timeout=5) == {"type": "websocket.close", "code": 4403}
        # Danach kommt nichts mehr an, auch wenn die Gruppe noch ein Ereignis erhält
        await _ereignis(org, objekt)
        assert await kommunikator.receive_nothing(timeout=0.5)

    asyncio.run(lauf())


@pytest.mark.django_db(transaction=True)
def test_vorbereitung_prueft_spaetestens_nach_der_frist_vor_dem_senden(
    org: Any, mitglied: Any, objekt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entzug ohne Signal (direkt in der Datenbank): Das nächste Ereignis nach der Frist wird nicht mehr gesendet."""
    monkeypatch.setattr(vorbereitung, "ACCESS_RECHECK_INTERVAL_SECONDS", 0)
    from apps.tenants.models import Membership

    async def lauf() -> None:
        kommunikator = await _vorbereitung_verbinden(mitglied.user, org, objekt)

        await database_sync_to_async(Membership.objects.filter(pk=mitglied.pk).update)(is_active=False)
        await _ereignis(org, objekt)

        assert await kommunikator.receive_output(timeout=5) == {"type": "websocket.close", "code": 4403}

    asyncio.run(lauf())
