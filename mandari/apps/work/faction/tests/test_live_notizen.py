# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Mitschreiben und @-Erwähnen in den Notizen der Fraktionssitzung (Issue #874).

- Rechte im WebSocket-Consumer (apps/work/faction/live.py): fremde Organisation, Gäste und Nicht-Vereidigte bei
  nichtöffentlichen TOPs bekommen keine Verbindung, auch nicht lesend; ohne Protokollrecht und nach der Genehmigung
  nur lesen – Änderungen und Speichern dieser Verbindungen kommen nicht an.
- Persistenz: Yjs-Zustand verschlüsselt, HTML im bisherigen Notizenfeld (bereinigt); Lesende bekommen den Stand.
- Speichern ohne Verbindung verwirft den Zustand, verbundene Editoren bauen neu auf und überschreiben nichts.
- Erwähnungen: Benachrichtigung einmal je TOP und Person, nur wer den TOP sehen darf.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
from typing import Any

import pytest
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.work.faction import sitzung
from apps.work.faction.erwaehnungen import erwaehnte_kennungen
from apps.work.faction.live import ProtokollNotizenConsumer
from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionNotesState
from apps.work.notifications.models import Notification, NotificationType
from apps.work.sanitize import sanitize_editor_html

VORSITZ = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "protocols.create",
    "protocols.edit",
]
MITGLIED = ["faction.view_public"]
#: y-protocols: Änderung (Sync-Schritt 2) und Anfrage (Schritt 1)
SYNC_UPDATE = bytes([0, 2, 3, 1, 2, 3])
SYNC_STEP1 = bytes([0, 0, 1, 0])
ZUSTAND = b"yjs-zustand-" + bytes(range(64))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture(autouse=True)
def _cache_leeren() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, MITGLIED, email="mitglied@example.org")


@pytest.fixture
def meeting(org: Any, vorsitz: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 24",
        start=timezone.now() - timedelta(minutes=10),
        status="ongoing",
        created_by=vorsitz,
    )


@pytest.fixture
def top(meeting: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(
        meeting=meeting, number="2", title="Feuerwehrbedarfsplan", visibility="public", order=1
    )


@pytest.fixture
def noe_top(meeting: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(
        meeting=meeting, number="NÖ 1", title="Grundstücksangelegenheit", visibility="internal", order=2
    )


def _kommunikator(user: Any, item: FactionAgendaItem) -> WebsocketCommunicator:
    kommunikator = WebsocketCommunicator(ProtokollNotizenConsumer.as_asgi(), f"/ws/fraktion/notizen/{item.pk}/")
    kommunikator.scope["user"] = user
    kommunikator.scope["url_route"] = {"kwargs": {"item_id": str(item.pk)}}
    return kommunikator


async def _verbinden(
    user: Any, item: FactionAgendaItem
) -> tuple[WebsocketCommunicator, dict[str, Any], dict[str, Any]]:
    kommunikator = _kommunikator(user, item)
    verbunden, _ = await kommunikator.connect()
    assert verbunden
    hallo = await kommunikator.receive_json_from()
    assert hallo["type"] == "connected"
    zustand = await kommunikator.receive_json_from()
    assert zustand["type"] == "yjs_state"
    return kommunikator, hallo, zustand


async def _bis(kommunikator: WebsocketCommunicator, art: str, timeout: float = 1) -> dict[str, Any]:
    while True:
        nachricht: dict[str, Any] = await kommunikator.receive_json_from(timeout=timeout)
        if nachricht["type"] == art:
            return nachricht


async def _nichts_ausser_praesenz(kommunikator: WebsocketCommunicator) -> list[dict[str, Any]]:
    """Alles, was noch ankommt, außer Anwesenheitsmeldungen."""
    rest: list[dict[str, Any]] = []
    while not await kommunikator.receive_nothing(timeout=0.3):
        nachricht = await kommunikator.receive_json_from()
        if nachricht["type"] != "praesenz":
            rest.append(nachricht)
    return rest


def _abgelehnt(user: Any, item: FactionAgendaItem) -> bool:
    async def lauf() -> bool:
        kommunikator = _kommunikator(user, item)
        verbunden, code = await kommunikator.connect()
        await kommunikator.disconnect()
        return not verbunden and code == 4403

    return asyncio.run(lauf())


# ---- Rechte ----------------------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fremde_organisation_gast_und_nicht_vereidigte_bekommen_keine_verbindung(
    org: Any, make_member: Any, mitglied: Any, top: Any, noe_top: Any
) -> None:
    andere = OrganizationFactory(name="Andere Fraktion", slug="andere")  # type: ignore[no-untyped-call]
    fremd = make_member(andere, VORSITZ, email="fremd@example.org")
    gast = MembershipFactory(user=UserFactory(email="gast@example.org"), organization=org, is_guest=True)  # type: ignore[no-untyped-call]

    assert _abgelehnt(fremd.user, top)
    assert _abgelehnt(gast.user, top)
    # Nichtöffentlich strikt: auch lesend nichts ohne Vereidigung
    assert _abgelehnt(mitglied.user, noe_top)
    mitglied.is_sworn_in = True
    mitglied.save(update_fields=["is_sworn_in"])
    assert _abgelehnt(mitglied.user, noe_top)  # vereidigt, aber ohne faction.view_non_public


@pytest.mark.django_db(transaction=True)
def test_lesende_verbindung_aendert_nichts(vorsitz: Any, mitglied: Any, top: Any) -> None:
    async def lauf() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        schreibend, _, zustand = await _verbinden(vorsitz.user, top)
        assert zustand["seed"] is True
        lesend, hallo, lese_zustand = await _verbinden(mitglied.user, top)
        assert hallo["user"]["access_level"] == "view" and lese_zustand["seed"] is False
        await lesend.send_json_to({"type": "yjs_sync", "data": _b64(SYNC_UPDATE)})
        await lesend.send_json_to({"type": "yjs_save", "data": _b64(ZUSTAND), "html": "<p>Fremd</p>"})
        bei_schreibenden = await _nichts_ausser_praesenz(schreibend)
        # Umgekehrt: Lesende bekommen keine Yjs-Daten, aber den gespeicherten Stand
        await schreibend.send_json_to({"type": "yjs_sync", "data": _b64(SYNC_UPDATE)})
        await schreibend.send_json_to({"type": "yjs_save", "data": _b64(ZUSTAND), "html": "<p>Hilfsfrist</p>"})
        gespeichert = await _bis(schreibend, "yjs_saved")
        bei_lesenden = await _nichts_ausser_praesenz(lesend)
        await lesend.disconnect()
        await schreibend.disconnect()
        return gespeichert, bei_schreibenden, bei_lesenden

    gespeichert, bei_schreibenden, bei_lesenden = asyncio.run(lauf())

    assert bei_schreibenden == []
    assert [n["type"] for n in bei_lesenden] == ["inhalt"]
    assert "Hilfsfrist" in bei_lesenden[0]["html"]
    top.refresh_from_db()
    assert sitzung.notizen(top) == "<p>Hilfsfrist</p>"
    assert gespeichert["content_hash"] == sitzung.fingerabdruck("<p>Hilfsfrist</p>")


@pytest.mark.django_db(transaction=True)
def test_nach_genehmigung_nur_lesen(vorsitz: Any, meeting: Any, top: Any) -> None:
    meeting.status = "completed"
    meeting.protocol_approved = True
    meeting.save(update_fields=["status", "protocol_approved"])

    async def lauf() -> tuple[str, list[dict[str, Any]]]:
        kommunikator, hallo, _ = await _verbinden(vorsitz.user, top)
        await kommunikator.send_json_to({"type": "yjs_save", "data": _b64(ZUSTAND), "html": "<p>Nachher</p>"})
        rest = await _nichts_ausser_praesenz(kommunikator)
        await kommunikator.disconnect()
        return hallo["user"]["access_level"], rest

    stufe, rest = asyncio.run(lauf())

    assert stufe == "view" and rest == []
    top.refresh_from_db()
    assert sitzung.notizen(top) == ""
    assert not FactionNotesState.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_genehmigung_waehrend_der_verbindung_stoppt_das_speichern(vorsitz: Any, meeting: Any, top: Any) -> None:
    async def lauf() -> list[dict[str, Any]]:
        kommunikator, _, _ = await _verbinden(vorsitz.user, top)
        await FactionMeeting.objects.filter(pk=meeting.pk).aupdate(status="completed", protocol_approved=True)
        await kommunikator.send_json_to({"type": "yjs_save", "data": _b64(ZUSTAND), "html": "<p>Spät</p>"})
        rest = await _nichts_ausser_praesenz(kommunikator)
        await kommunikator.disconnect()
        return rest

    assert asyncio.run(lauf()) == []
    top.refresh_from_db()
    assert sitzung.notizen(top) == ""


# ---- Persistenz ------------------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_zustand_verschluesselt_und_notizen_im_bisherigen_feld(vorsitz: Any, top: Any) -> None:
    async def lauf() -> tuple[dict[str, Any], dict[str, Any]]:
        kommunikator, _, _ = await _verbinden(vorsitz.user, top)
        await kommunikator.send_json_to(
            {
                "type": "yjs_save",
                "data": _b64(ZUSTAND),
                "html": "<p><strong>Hilfsfrist</strong> im Norden<script>alert(1)</script></p>",
            }
        )
        gespeichert = await _bis(kommunikator, "yjs_saved")
        await kommunikator.disconnect()
        # Neu verbunden: der gespeicherte Zustand kommt zurück
        wieder, _, zustand = await _verbinden(vorsitz.user, top)
        await wieder.disconnect()
        return gespeichert, zustand

    gespeichert, zustand = asyncio.run(lauf())

    top.refresh_from_db()
    notizen = sitzung.notizen(top)
    assert notizen == "<p><strong>Hilfsfrist</strong> im Norden</p>"
    assert top.notes_updated_at is not None
    assert gespeichert["gespeichert_um"]
    roh = FactionNotesState.objects.get(item=top)
    assert ZUSTAND not in bytes(roh.yjs_encrypted) and roh.stand == sitzung.fingerabdruck(notizen)
    assert base64.b64decode(zustand["data"]) == ZUSTAND


@pytest.mark.django_db(transaction=True)
def test_nur_ein_editor_fuellt_den_leeren_zustand(vorsitz: Any, make_member: Any, org: Any, top: Any) -> None:
    sitzung.notizen_speichern(top, "<p>Vorhanden</p>", basis=None, membership=vorsitz)
    schrift = make_member(org, ["faction.view_public", "protocols.edit"], email="schrift@example.org")

    async def lauf() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        erster, _, z1 = await _verbinden(vorsitz.user, top)
        zweiter, _, z2 = await _verbinden(schrift.user, top)
        # Der zweite hat noch keinen Inhalt: sein Speichern zählt nicht, solange der erste füllt
        await zweiter.send_json_to({"type": "yjs_save", "data": _b64(b"leer"), "html": "<p></p>"})
        rest = await _nichts_ausser_praesenz(zweiter)
        await zweiter.disconnect()
        await erster.disconnect()
        return z1, z2, rest

    z1, z2, rest = asyncio.run(lauf())

    assert z1["data"] is None and z1["seed"] is True and "Vorhanden" in z1["html"]
    assert z2["data"] is None and z2["seed"] is False
    assert rest == []
    top.refresh_from_db()
    assert sitzung.notizen(top) == "<p>Vorhanden</p>"


@pytest.mark.django_db(transaction=True)
def test_speichern_ohne_verbindung_laesst_neu_aufbauen(
    vorsitz: Any, org: Any, meeting: Any, top: Any, client_for: Any
) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    client = client_for(vorsitz.user)
    aktion = reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})

    async def lauf() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        kommunikator, _, _ = await _verbinden(vorsitz.user, top)
        await kommunikator.send_json_to({"type": "yjs_save", "data": _b64(ZUSTAND), "html": "<p>Live</p>"})
        await _bis(kommunikator, "yjs_saved")

        def formular() -> Any:
            return client.post(
                aktion,
                {
                    "action": "notizen",
                    "item_id": top.pk,
                    "html": "<p>Live</p><p>Ohne Verbindung</p>",
                    "stand": sitzung.fingerabdruck("<p>Live</p>"),
                },
            )

        from asgiref.sync import sync_to_async

        antwort = await sync_to_async(formular)()
        assert antwort.status_code == 200
        neu = await _bis(kommunikator, "neu_aufbauen")
        # Der veraltete Editor überschreibt die neuen Notizen nicht mehr
        await kommunikator.send_json_to({"type": "yjs_save", "data": _b64(ZUSTAND), "html": "<p>Live</p>"})
        rest = await _nichts_ausser_praesenz(kommunikator)
        await kommunikator.disconnect()
        return neu, rest

    neu, rest = asyncio.run(lauf())

    assert "Ohne Verbindung" in neu["html"] and neu["vorher"] == "<p>Live</p>"
    assert rest == []
    top.refresh_from_db()
    assert sitzung.notizen(top) == "<p>Live</p><p>Ohne Verbindung</p>"
    assert not FactionNotesState.objects.filter(item=top).exists()


@pytest.mark.django_db
def test_live_speichern_mit_veraltetem_stand_ueberschreibt_nichts(vorsitz: Any, top: Any) -> None:
    sitzung.notizen_speichern(top, "<p>Neu aus dem Formular</p>", basis=None, membership=vorsitz)

    ergebnis = sitzung.notizen_speichern(
        top, "<p>Alter Editor</p>", basis=sitzung.fingerabdruck(""), membership=vorsitz, yjs=ZUSTAND
    )

    assert not ergebnis.gespeichert
    top.refresh_from_db()
    assert sitzung.notizen(top) == "<p>Neu aus dem Formular</p>"
    assert not FactionNotesState.objects.exists()


@pytest.mark.django_db
def test_seite_der_laufenden_sitzung_schaltet_live_ein(
    org: Any, vorsitz: Any, mitglied: Any, meeting: Any, top: Any, client_for: Any
) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    seite = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})

    html = client_for(vorsitz.user).get(f"{seite}?top={top.pk}").content.decode()

    assert '"live": true' in html and "Person erwähnen (@)" in html
    assert str(mitglied.pk) in html  # in der Vorschlagsliste
    lesend = client_for(mitglied.user).get(f"{seite}?top={top.pk}").content.decode()
    assert "Sie lesen live mit." in lesend and "Person erwähnen" not in lesend


# ---- Erwähnungen -----------------------------------------------------------------------------------------------


def _erwaehnung(m: Any) -> str:
    return f'<span class="erwaehnung" data-type="mention" data-id="{m.pk}">@{m.user.get_display_name()}</span>'


def test_erwaehnung_uebersteht_die_bereinigung() -> None:
    kennung = "0b5f2d7e-1c4a-4f7e-9a3b-2c1d0e9f8a7b"
    html = f'<p><span class="erwaehnung" data-type="mention" data-id="{kennung}" onclick="x()">@Paula</span></p>'
    sauber = sanitize_editor_html(html)

    assert sauber == f'<p><span class="erwaehnung" data-type="mention" data-id="{kennung}">@Paula</span></p>'
    assert 'data-id="x"' not in sanitize_editor_html('<span data-type="mention" data-id="x">@X</span>')
    assert [str(k) for k in erwaehnte_kennungen(sauber)] == [kennung]


@pytest.mark.django_db
def test_erwaehnte_person_wird_einmal_benachrichtigt(
    org: Any, make_member: Any, vorsitz: Any, mitglied: Any, top: Any
) -> None:
    andere = OrganizationFactory(name="Andere Fraktion", slug="andere")  # type: ignore[no-untyped-call]
    fremd = make_member(andere, MITGLIED, email="fremd@example.org")
    html = f"<p>{_erwaehnung(mitglied)} stellt die Rückfrage. {_erwaehnung(fremd)} {_erwaehnung(vorsitz)}</p>"

    sitzung.notizen_speichern(top, html, basis=None, membership=vorsitz)
    sitzung.notizen_speichern(top, html + "<p>Ergänzung</p>", basis=None, membership=vorsitz)
    # Speichert eine zweite Person den Text mit den bestehenden Erwähnungen, löst das nichts Neues aus
    sitzung.notizen_speichern(top, html + "<p>Noch mehr</p>", basis=None, membership=mitglied)

    benachrichtigungen = Notification.objects.filter(notification_type=NotificationType.FACTION_MENTION)
    assert benachrichtigungen.count() == 1
    n = benachrichtigungen.get()
    assert n.recipient == mitglied and n.actor == vorsitz
    assert f"?top={top.pk}" in n.link and "TOP 2" in n.message
    assert "Rückfrage" not in n.message  # kein Inhalt der Notizen
    # Entfernt und wieder erwähnt: keine zweite Benachrichtigung zum selben TOP
    sitzung.notizen_speichern(top, "<p>Ohne</p>", basis=None, membership=vorsitz)
    sitzung.notizen_speichern(top, html, basis=None, membership=vorsitz)
    assert benachrichtigungen.count() == 1


@pytest.mark.django_db
def test_erwaehnung_im_nichtoeffentlichen_top_nur_fuer_vereidigte(
    org: Any, make_member: Any, vorsitz: Any, mitglied: Any, noe_top: Any
) -> None:
    vereidigt = make_member(org, ["faction.view_public", "faction.view_non_public"], email="vereidigt@example.org")
    vereidigt.is_sworn_in = True
    vereidigt.save(update_fields=["is_sworn_in"])

    sitzung.notizen_speichern(
        noe_top, f"<p>{_erwaehnung(mitglied)} {_erwaehnung(vereidigt)}</p>", basis=None, membership=vorsitz
    )

    empfaenger = set(
        Notification.objects.filter(notification_type=NotificationType.FACTION_MENTION).values_list(
            "recipient_id", flat=True
        )
    )
    assert empfaenger == {vereidigt.pk}


@pytest.mark.django_db
def test_ausloeser_ist_wer_die_erwaehnung_eingefuegt_hat(
    org: Any, make_member: Any, vorsitz: Any, mitglied: Any, top: Any
) -> None:
    """Beim gemeinsamen Schreiben speichert oft jemand anders; ``data-von`` zählt nur mit Protokollrecht."""
    schrift = make_member(org, ["faction.view_public", "protocols.edit"], email="schrift@example.org")
    dritte = make_member(org, MITGLIED, email="dritte@example.org")

    def erwaehnt(person: Any, von: Any) -> str:
        return (
            f'<span class="erwaehnung" data-type="mention" data-id="{person.pk}" data-von="{von.pk}">'
            f"@{person.user.get_display_name()}</span>"
        )

    # Die Schriftführung erwähnt die Vorsitzende, gespeichert hat der Editor der Vorsitzenden
    html = f"<p>{erwaehnt(vorsitz, schrift)} {erwaehnt(dritte, mitglied)}</p>"
    assert 'data-von="' in sanitize_editor_html(html)
    sitzung.notizen_speichern(top, html, basis=None, membership=vorsitz)

    an_vorsitz = Notification.objects.get(recipient=vorsitz, notification_type=NotificationType.FACTION_MENTION)
    assert an_vorsitz.actor == schrift
    # Angeblich vom Mitglied ohne Protokollrecht eingefügt: Auslöser ist, wer gespeichert hat
    an_dritte = Notification.objects.get(recipient=dritte, notification_type=NotificationType.FACTION_MENTION)
    assert an_dritte.actor == vorsitz
    # Selbst erwähnt: keine Benachrichtigung
    sitzung.notizen_speichern(top, html + f"<p>{erwaehnt(schrift, schrift)}</p>", basis=None, membership=vorsitz)
    assert not Notification.objects.filter(recipient=schrift).exists()
