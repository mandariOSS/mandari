# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mit der Sitzung enden auch ihre WebSocket-Verbindungen.

Ein WebSocket kennt sein Konto nur vom Verbindungsaufbau. Abmelden, „Sitzung beenden“, „Alle anderen beenden“ und
ein neues Passwort beenden die Sitzung für HTTP; die Zugriffsprüfung der Consumer liest deshalb die Sitzung bei jeder
Nachprüfung frisch, und die auslösenden Vorgänge lassen die Verbindungen der Person sofort nachprüfen.

Die Verbindung endet dann mit einem eigenen Schließcode, nicht als „Zugriff entzogen“: Der Zugriff der Person besteht,
und mit einer neuen Sitzung (erneute Anmeldung, Passwortwechsel im eigenen Profil) verbindet die Seite neu.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync
from channels.auth import AuthMiddlewareStack, UserLazyObject
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.services import PasswordService, SessionService
from apps.accounts.websocket import CLOSE_SESSION_ENDED, TwoFactorWebSocketMiddleware, session_valid
from apps.work.faction.live import ProtokollNotizenConsumer
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.faction.routing import websocket_urlpatterns as notizen_routen
from apps.work.meetings.consumers import PreparationConsumer
from apps.work.meetings.routing import websocket_urlpatterns as vorbereitung_routen
from apps.work.motions import freigaben
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion
from apps.work.motions.routing import websocket_urlpatterns as dokument_routen

PASSWORT = "Sicheres-Passwort-2026!"
NEUES_PASSWORT = "Noch-sichereres-Passwort-2027!"
RECHTE = [
    "faction.view_public",
    "faction.manage",
    "protocols.create",
    "protocols.edit",
    "meetings.prepare",
    "motions.view",
    "motions.create",
    "motions.edit",
]

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    mitglied = make_member(org, RECHTE, email="vorsitz@example.org")
    mitglied.user.set_password(PASSWORT)
    mitglied.user.save()
    return mitglied


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
def objekt(monkeypatch: pytest.MonkeyPatch) -> str:
    """Kennung eines Tagesordnungspunkts im RIS; seine Existenz ist hier nicht Thema."""
    monkeypatch.setattr("hub.ris.selectors.agenda_item_exists", lambda kennung: True)
    return str(uuid.uuid4())


def _anmelden(user: Any, ip: str = "198.51.100.7") -> Client:
    client = Client()
    antwort = client.post(reverse("accounts:login"), {"email": user.email, "password": PASSWORT}, REMOTE_ADDR=ip)
    assert antwort.status_code == 302, "Anmeldung"
    return client


def _sitzung(client: Client) -> SessionStore:
    """Die Sitzung, wie eine Verbindung sie beim Aufbau bekommt: einmal geladen, danach nicht mehr aufgefrischt."""
    sitzung = SessionStore(session_key=str(client.session.session_key))
    assert sitzung.get(SESSION_KEY), "angemeldete Sitzung"
    return sitzung


class Verbindung:
    """Steht für einen Consumer: Er hält nur den ``scope`` vom Verbindungsaufbau."""

    def __init__(self, client: Client) -> None:
        self.scope = {"session": _sitzung(client)}


def _zuruecksetzen(user: Any) -> None:
    """Neues Passwort über den Link aus der Mail „Passwort zurücksetzen“."""
    client = Client()
    user.refresh_from_db()  # der Link hängt am Stand des Kontos (letzte Anmeldung)
    link = reverse(
        "accounts:password_reset_confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        },
    )
    formular = client.get(link)
    assert formular.status_code == 302
    antwort = client.post(formular["Location"], {"new_password1": NEUES_PASSWORT, "new_password2": NEUES_PASSWORT})
    assert antwort.status_code == 302, "Passwort gesetzt"


# --- Die Prüfung selbst ---------------------------------------------------------------------------------------


def test_laufende_sitzung_gilt(mitglied: Any) -> None:
    assert session_valid(Verbindung(_anmelden(mitglied.user)), mitglied.user)


def test_abmelden_beendet_die_verbindung(mitglied: Any) -> None:
    client = _anmelden(mitglied.user)
    verbindung = Verbindung(client)

    client.post(reverse("accounts:logout"))

    assert not session_valid(verbindung, mitglied.user)


def test_sitzung_beenden_trifft_genau_diese_sitzung(mitglied: Any) -> None:
    beendet, bleibt = _anmelden(mitglied.user), _anmelden(mitglied.user, ip="198.51.100.8")
    verbindungen = Verbindung(beendet), Verbindung(bleibt)

    assert SessionService.revoke_session(mitglied.user, str(beendet.session.session_key))

    assert not session_valid(verbindungen[0], mitglied.user)
    assert session_valid(verbindungen[1], mitglied.user)


def test_alle_anderen_beenden_laesst_die_eigene_verbindung(mitglied: Any) -> None:
    andere, eigene = _anmelden(mitglied.user), _anmelden(mitglied.user, ip="198.51.100.8")
    verbindungen = Verbindung(andere), Verbindung(eigene)

    SessionService.revoke_all_sessions(mitglied.user, except_current=str(eigene.session.session_key))

    assert not session_valid(verbindungen[0], mitglied.user)
    assert session_valid(verbindungen[1], mitglied.user)


def test_neues_passwort_beendet_die_verbindungen(mitglied: Any) -> None:
    """Die Sitzungszeile steht noch (HTTP entfernt sie erst bei der nächsten Anfrage), ihr Passwortstand ist alt."""
    verbindung = Verbindung(_anmelden(mitglied.user))

    _zuruecksetzen(mitglied.user)

    assert not session_valid(verbindung, mitglied.user)


def test_passwortwechsel_im_profil_beendet_auch_verbindungen_der_eigenen_sitzung(mitglied: Any) -> None:
    """
    Der Wechsel vergibt der eigenen Sitzung einen neuen Schlüssel. Verbindungen mit dem alten enden: Wer den alten
    Schlüssel kennt, soll nach dem Wechsel nichts mehr in der Hand haben. Die Seite baut ihre Verbindung neu auf.
    """
    client = _anmelden(mitglied.user)
    verbindung = Verbindung(client)
    anfrage = client.get(reverse("accounts:logout")).wsgi_request

    geaendert, _meldung = PasswordService.change_password(mitglied.user, PASSWORT, NEUES_PASSWORT, request=anfrage)
    assert geaendert
    SessionService.keep_session_after_password_change(anfrage, mitglied.user)
    anfrage.session.save()  # sonst schreibt die Middleware am Ende der Anfrage

    assert not session_valid(verbindung, mitglied.user)
    neue = SessionStore(session_key=str(anfrage.session.session_key))
    assert session_valid(cast(Any, type("Neu", (), {"scope": {"session": neue}})()), mitglied.user)


def test_sitzung_eines_anderen_kontos_gilt_nicht(mitglied: Any, org: Any, make_member: Any) -> None:
    andere = make_member(org, RECHTE, email="andere@example.org")

    assert not session_valid(Verbindung(_anmelden(mitglied.user)), andere.user)


def test_deaktiviertes_konto_hat_keine_gueltige_sitzung(mitglied: Any) -> None:
    verbindung = Verbindung(_anmelden(mitglied.user))

    mitglied.user.is_active = False
    mitglied.user.save(update_fields=["is_active"])

    assert not session_valid(verbindung, mitglied.user)


def test_ohne_sitzungsschicht_gibt_es_nichts_zu_pruefen(mitglied: Any) -> None:
    """Consumer, die ein Test direkt anspricht, haben keinen ``scope`` bzw. keine Sitzung darin."""
    assert session_valid(object(), mitglied.user)
    assert session_valid(cast(Any, type("Ohne", (), {"scope": {"user": mitglied.user}})()), mitglied.user)
    # Mit Sitzungsschicht, aber ohne Sitzung: kein Zugriff
    assert not session_valid(cast(Any, type("Leer", (), {"scope": {"session": SessionStore()}})()), mitglied.user)


# --- Die Zugriffsprüfung der Consumer --------------------------------------------------------------------------


def _dokument_zugriff(user: Any, dokument: Motion, sitzung: SessionStore) -> Any:
    consumer = cast(Any, DocumentCollaborationConsumer)()
    consumer.scope = {"session": sitzung}
    consumer.document_id = str(dokument.pk)
    consumer.user = user
    return async_to_sync(consumer._check_access)()[0]


def _vorbereitung_zugriff(user: Any, org: Any, objekt: str, sitzung: SessionStore) -> Any:
    consumer = cast(Any, PreparationConsumer)()
    consumer.scope = {"session": sitzung}
    consumer.org_slug = org.slug
    consumer.scope_type = "item"
    consumer.object_id = objekt
    return async_to_sync(consumer._check_access)(user)


def _notizen_zugriff(user: Any, item: FactionAgendaItem, sitzung: SessionStore) -> Any:
    consumer = cast(Any, ProtokollNotizenConsumer)()
    consumer.scope = {"session": sitzung}
    consumer.item_id = str(item.pk)
    consumer.user = user
    return async_to_sync(consumer._zugriff)()


def test_zugriffspruefungen_enden_mit_der_sitzung(org: Any, mitglied: Any, top: FactionAgendaItem, objekt: str) -> None:
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")
    client = _anmelden(mitglied.user)
    sitzung = _sitzung(client)

    assert _dokument_zugriff(mitglied.user, dokument, sitzung) == "edit"
    assert _vorbereitung_zugriff(mitglied.user, org, objekt, sitzung) == org.id
    assert _notizen_zugriff(mitglied.user, top, sitzung) is not None

    client.post(reverse("accounts:logout"))

    assert _dokument_zugriff(mitglied.user, dokument, sitzung) is None
    assert _vorbereitung_zugriff(mitglied.user, org, objekt, sitzung) is None
    assert _notizen_zugriff(mitglied.user, top, sitzung) is None


# --- Sofort nachprüfen -----------------------------------------------------------------------------------------


def _abmelden(mitglied: Any) -> None:
    _anmelden(mitglied.user).post(reverse("accounts:logout"))


def _eine_beenden(mitglied: Any) -> None:
    SessionService.revoke_session(mitglied.user, str(_anmelden(mitglied.user).session.session_key))


def _alle_beenden(mitglied: Any) -> None:
    _anmelden(mitglied.user)
    SessionService.revoke_all_sessions(mitglied.user)


def _passwort_wechseln(mitglied: Any) -> None:
    assert PasswordService.change_password(mitglied.user, PASSWORT, NEUES_PASSWORT)[0]


def _passwort_zuruecksetzen(mitglied: Any) -> None:
    _zuruecksetzen(mitglied.user)


@pytest.mark.parametrize(
    "vorgang", [_abmelden, _eine_beenden, _alle_beenden, _passwort_wechseln, _passwort_zuruecksetzen]
)
def test_vorgang_laesst_die_verbindungen_der_person_sofort_nachpruefen(
    mitglied: Any, monkeypatch: pytest.MonkeyPatch, vorgang: Callable[[Any], None]
) -> None:
    gesendet: list[list[str]] = []
    monkeypatch.setattr(freigaben, "_send", gesendet.append)

    vorgang(mitglied)

    assert [freigaben.user_group(mitglied.user_id)] in gesendet


async def _bis_zum_schliessen(kommunikator: WebsocketCommunicator) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Was die Verbindung bis zu ihrem Ende noch sendet: (Schließnachricht, JSON-Nachrichten davor)."""
    nachrichten: list[dict[str, Any]] = []
    while True:
        ausgabe = await kommunikator.receive_output(timeout=5)
        if ausgabe["type"] == "websocket.close":
            return ausgabe, nachrichten
        nachrichten.append(json.loads(ausgabe["text"]))


async def _eingang_leeren(kommunikator: WebsocketCommunicator) -> None:
    while not await kommunikator.receive_nothing(timeout=0.3):
        await kommunikator.receive_json_from()


def _notizen_verbindung(mitglied: Any, top: FactionAgendaItem, sitzung: SessionStore) -> WebsocketCommunicator:
    kommunikator = WebsocketCommunicator(ProtokollNotizenConsumer.as_asgi(), f"/ws/fraktion/notizen/{top.pk}/")
    kommunikator.scope["user"] = mitglied.user
    kommunikator.scope["session"] = sitzung
    kommunikator.scope["url_route"] = {"kwargs": {"item_id": str(top.pk)}}
    return kommunikator


def test_sitzung_beenden_schliesst_die_offene_notizen_verbindung(mitglied: Any, top: FactionAgendaItem) -> None:
    """
    Eigener Schließcode und keine Nachricht „Zugriff entzogen“: Mit ihr bliebe der Notizen-Editor schreibgeschützt
    stehen. Mit dem Code verbindet er neu (frontend/editor/collaboration.ts).
    """
    client = _anmelden(mitglied.user)
    sitzung = _sitzung(client)
    schluessel = str(client.session.session_key)

    async def lauf() -> None:
        kommunikator = _notizen_verbindung(mitglied, top, sitzung)
        verbunden, _ = await kommunikator.connect()
        assert verbunden
        await _eingang_leeren(kommunikator)

        assert await database_sync_to_async(SessionService.revoke_session)(mitglied.user, schluessel)

        schluss, davor = await _bis_zum_schliessen(kommunikator)
        assert schluss == {"type": "websocket.close", "code": CLOSE_SESSION_ENDED}
        assert [nachricht for nachricht in davor if nachricht.get("type") == "reload"] == []

    asyncio.run(lauf())


def test_entzogener_zugriff_bleibt_zugriff_entzogen(mitglied: Any, top: FactionAgendaItem) -> None:
    """Gilt die Sitzung noch und fehlt der Zugriff, bleibt es bei der Nachricht und dem Code „kein Zugriff“."""
    sitzung = _sitzung(_anmelden(mitglied.user))

    def entziehen() -> None:
        mitglied.is_active = False
        mitglied.save()

    async def lauf() -> None:
        kommunikator = _notizen_verbindung(mitglied, top, sitzung)
        verbunden, _ = await kommunikator.connect()
        assert verbunden
        await _eingang_leeren(kommunikator)

        await database_sync_to_async(entziehen)()

        schluss, davor = await _bis_zum_schliessen(kommunikator)
        assert schluss == {"type": "websocket.close", "code": 4403}
        assert {"type": "reload", "reason": "access_revoked"} in davor

    asyncio.run(lauf())


# --- Der echte Weg: hinter der Anmeldeschicht ------------------------------------------------------------------


def _stapel() -> Any:
    """
    Die Schichten aus ``mandari/asgi.py`` um die Consumer: Sitzung, Konto, zweiter Faktor.

    Hinter ``AuthMiddlewareStack`` ist ``scope["user"]`` eine Hülle (``UserLazyObject``) und ``scope["session"]`` die
    Sitzung aus dem Cookie. Tests, die beides von Hand in den ``scope`` legen, prüfen diesen Weg nicht.
    """
    routen = [*dokument_routen, *vorbereitung_routen, *notizen_routen]
    return AuthMiddlewareStack(TwoFactorWebSocketMiddleware(URLRouter(routen)))


def _hinter_anmeldung(pfad: str, sitzungsschluessel: str) -> WebsocketCommunicator:
    cookie = f"{settings.SESSION_COOKIE_NAME}={sitzungsschluessel}".encode()
    return WebsocketCommunicator(_stapel(), pfad, headers=[(b"cookie", cookie)])


def test_pruefung_kommt_mit_der_huelle_der_anmeldeschicht_zurecht(mitglied: Any) -> None:
    huelle = cast(Any, UserLazyObject)()
    huelle._wrapped = mitglied.user
    client = _anmelden(mitglied.user)
    verbindung = Verbindung(client)

    assert session_valid(verbindung, huelle)

    client.post(reverse("accounts:logout"))
    assert not session_valid(verbindung, huelle)


@pytest.mark.parametrize("ziel", ["dokument", "vorbereitung", "notizen"])
def test_verbindung_hinter_der_anmeldeschicht_kommt_zustande(
    org: Any, mitglied: Any, top: FactionAgendaItem, objekt: str, ziel: str
) -> None:
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")
    pfad = {
        "dokument": f"/ws/documents/{dokument.pk}/",
        "vorbereitung": f"/ws/preparation/{org.slug}/item/{objekt}/",
        "notizen": f"/ws/fraktion/notizen/{top.pk}/",
    }[ziel]
    schluessel = str(_anmelden(mitglied.user).session.session_key)

    async def lauf() -> None:
        kommunikator = _hinter_anmeldung(pfad, schluessel)
        verbunden, _ = await kommunikator.connect()
        assert verbunden, "angemeldet und berechtigt"
        assert (await kommunikator.receive_json_from(timeout=5))["type"] == "connected"
        await kommunikator.disconnect()

    asyncio.run(lauf())


def test_ohne_sitzung_kommt_hinter_der_anmeldeschicht_keine_verbindung_zustande(org: Any, mitglied: Any) -> None:
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")
    client = _anmelden(mitglied.user)
    schluessel = str(client.session.session_key)
    client.post(reverse("accounts:logout"))

    async def lauf() -> None:
        verbunden, code = await _hinter_anmeldung(f"/ws/documents/{dokument.pk}/", schluessel).connect()
        assert not verbunden and code == 4401

    asyncio.run(lauf())


@pytest.mark.parametrize("ziel", ["dokument", "vorbereitung"])
def test_abmelden_schliesst_die_verbindung_mit_eigenem_grund(org: Any, mitglied: Any, objekt: str, ziel: str) -> None:
    """Über den echten Weg: Die Verbindung endet sofort, mit eigenem Code und ohne „Zugriff entzogen“."""
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")
    pfad = {
        "dokument": f"/ws/documents/{dokument.pk}/",
        "vorbereitung": f"/ws/preparation/{org.slug}/item/{objekt}/",
    }[ziel]
    client = _anmelden(mitglied.user)
    schluessel = str(client.session.session_key)

    async def lauf() -> None:
        kommunikator = _hinter_anmeldung(pfad, schluessel)
        verbunden, _ = await kommunikator.connect()
        assert verbunden
        await _eingang_leeren(kommunikator)

        await database_sync_to_async(client.post)(reverse("accounts:logout"))

        schluss, davor = await _bis_zum_schliessen(kommunikator)
        assert schluss == {"type": "websocket.close", "code": CLOSE_SESSION_ENDED}
        assert [nachricht for nachricht in davor if nachricht.get("type") == "reload"] == []

    asyncio.run(lauf())


def test_nach_passwortwechsel_im_profil_verbindet_die_seite_mit_der_neuen_sitzung(org: Any, mitglied: Any) -> None:
    """
    Der Wechsel im eigenen Profil beendet die Verbindung mit dem alten Sitzungsschlüssel (eigener Code). Der Browser
    hat danach den neuen Schlüssel: Der eine neue Versuch des Editors kommt damit zustande, niemand verliert den
    Zugriff auf sein Dokument.
    """
    dokument = Motion.objects.create(organization=org, author=mitglied, title="Antrag", visibility="organization")
    pfad = f"/ws/documents/{dokument.pk}/"
    client = _anmelden(mitglied.user)
    alter_schluessel = str(client.session.session_key)
    anfrage = client.get(reverse("accounts:logout")).wsgi_request

    def wechseln() -> str:
        assert PasswordService.change_password(mitglied.user, PASSWORT, NEUES_PASSWORT, request=anfrage)[0]
        SessionService.keep_session_after_password_change(anfrage, mitglied.user)
        anfrage.session.save()  # sonst schreibt die Middleware am Ende der Anfrage
        return str(anfrage.session.session_key)

    async def lauf() -> None:
        kommunikator = _hinter_anmeldung(pfad, alter_schluessel)
        verbunden, _ = await kommunikator.connect()
        assert verbunden
        await _eingang_leeren(kommunikator)

        neuer_schluessel = await database_sync_to_async(wechseln)()
        assert neuer_schluessel != alter_schluessel

        schluss, davor = await _bis_zum_schliessen(kommunikator)
        assert schluss == {"type": "websocket.close", "code": CLOSE_SESSION_ENDED}
        assert [nachricht for nachricht in davor if nachricht.get("type") == "reload"] == []

        neu = _hinter_anmeldung(pfad, neuer_schluessel)
        verbunden, _ = await neu.connect()
        assert verbunden, "mit dem neuen Cookie kommt die Verbindung wieder zustande"
        nachricht = await neu.receive_json_from(timeout=5)
        assert nachricht["type"] == "connected" and nachricht["user"]["access_level"] == "edit"
        await neu.disconnect()

        verbunden, _ = await _hinter_anmeldung(pfad, alter_schluessel).connect()
        assert not verbunden, "der alte Schlüssel gilt nicht mehr"

    asyncio.run(lauf())
