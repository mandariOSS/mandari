# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mit der Sitzung enden auch ihre WebSocket-Verbindungen.

Ein WebSocket kennt sein Konto nur vom Verbindungsaufbau. Abmelden, „Sitzung beenden“, „Alle anderen beenden“ und
ein neues Passwort beenden die Sitzung für HTTP; die Zugriffsprüfung der Consumer liest deshalb die Sitzung bei jeder
Nachprüfung frisch, und die auslösenden Vorgänge lassen die Verbindungen der Person sofort nachprüfen.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
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
from apps.accounts.websocket import session_valid
from apps.work.faction.live import ProtokollNotizenConsumer
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.meetings.consumers import PreparationConsumer
from apps.work.motions import freigaben
from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion

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


def test_sitzung_beenden_schliesst_die_offene_notizen_verbindung(mitglied: Any, top: FactionAgendaItem) -> None:
    client = _anmelden(mitglied.user)
    sitzung = _sitzung(client)
    schluessel = str(client.session.session_key)

    async def lauf() -> None:
        kommunikator = WebsocketCommunicator(ProtokollNotizenConsumer.as_asgi(), f"/ws/fraktion/notizen/{top.pk}/")
        kommunikator.scope["user"] = mitglied.user
        kommunikator.scope["session"] = sitzung
        kommunikator.scope["url_route"] = {"kwargs": {"item_id": str(top.pk)}}
        verbunden, _ = await kommunikator.connect()
        assert verbunden
        while not await kommunikator.receive_nothing(timeout=0.3):
            await kommunikator.receive_json_from()

        assert await database_sync_to_async(SessionService.revoke_session)(mitglied.user, schluessel)

        nachricht: dict[str, Any] = {}
        while nachricht.get("type") != "reload":
            nachricht = await kommunikator.receive_json_from(timeout=5)
        assert nachricht == {"type": "reload", "reason": "access_revoked"}
        assert await kommunikator.receive_output(timeout=5) == {"type": "websocket.close", "code": 4403}

    asyncio.run(lauf())
