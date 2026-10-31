# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live-Mitschreiben in den Notizen eines TOPs der Fraktionssitzung (Issue #874).

Mehrere Personen schreiben gleichzeitig in die Notizen eines TOPs (Yjs über Channels, wie der Dokumenteditor in
:mod:`apps.work.motions.consumers`), sehen die Cursor der anderen und wer gerade im Protokoll ist. Wer nur lesen
darf, bekommt den gespeicherten Stand live, ohne Editor.

Rechte serverseitig, bei jeder Verbindung und spätestens alle ``ZUGRIFF_NEU_PRUEFEN`` Sekunden neu:

- aktive Mitgliedschaft der Organisation der Sitzung (keine Gäste, kein deaktiviertes Konto) mit
  ``faction.view_public``; nichtöffentliche TOPs (auch als Unterpunkt) nur für Vereidigte – auch lesend;
- schreiben nur mit Protokollrecht (``sitzung.darf_protokollieren``): nicht mehr nach der Genehmigung.

Gespeichert wird über :func:`apps.work.faction.sitzung.notizen_speichern`: der Yjs-Zustand verschlüsselt
(``FactionNotesState``) und das HTML im bisherigen Notizenfeld, damit Protokoll, PDF, Versand und Fassungen
unverändert bleiben. Speichert jemand ohne Verbindung (Rückfall auf das Formular, Protokollseite), verwirft der
Server den Zustand und die verbundenen Editoren bauen ihn aus den neuen Notizen auf (``neu_aufbauen``).

Protokoll (JSON, Binärdaten base64 wie im Dokumenteditor):

Client → Server
    ``{"type": "yjs_sync", "data": …}``, ``{"type": "awareness", "data": …}`` (Cursor),
    ``{"type": "yjs_save", "data": <Zustand>, "html": <Notizen>}``
Server → Client
    ``connected`` (eigene Person, Stufe ``edit``/``view``), ``yjs_state`` (``data`` oder ``null`` mit ``html`` und
    ``seed``: genau ein Editor füllt den leeren Zustand aus den Notizen), ``yjs_sync``, ``awareness``,
    ``yjs_saved`` (``content_hash`` = Stand, ``gespeichert_um``), ``inhalt`` (gespeicherte Notizen für Lesende),
    ``praesenz`` (``da``/``weg`` einer Verbindung), ``neu_aufbauen`` (``html``, ``vorher``),
    ``reload`` mit ``reason`` ``access_changed``/``access_revoked`` (danach Trennung mit 4403).
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
import time
from typing import Any

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.utils import timezone

from apps.work.motions.consumers import CURSOR_COLORS, ist_lesende_sync_nachricht

logger = logging.getLogger(__name__)

#: Rechte spätestens nach dieser Zeit neu prüfen (bei der nächsten Nachricht), etwa nach der Genehmigung
ZUGRIFF_NEU_PRUEFEN = 30
#: So lange gilt die Zusage „Sie füllen den leeren Zustand“ für einen Editor (Sekunden)
SEED_SPERRE = 20
#: Höchstgröße eines gespeicherten Zustands (base64), schützt vor Riesennachrichten
ZUSTAND_MAX_ZEICHEN = 4_000_000


def gruppe(item_id: Any) -> str:
    return f"fs_notizen_{item_id}"


def _seed_schluessel(item_id: Any) -> str:
    return f"fs-notizen-seed:{item_id}"


def _senden(item_id: Any, nachricht: dict[str, Any]) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(gruppe(item_id), nachricht)
    except Exception:  # pragma: no cover - Ausfall des Kanal-Layers: Editoren prüfen beim Speichern selbst
        logger.warning("Live-Notizen: Nachricht an TOP %s nicht verteilt", item_id)


def neu_aufbauen(item_id: Any, html: str, *, vorher: str = "") -> None:
    """
    Verbundene Editoren den Zustand aus den gespeicherten Notizen neu aufbauen lassen (ohne Verbindung gespeichert).

    ``vorher`` = Notizen vor dieser Speicherung: Weicht der Inhalt eines Editors davon ab, hatte er noch nicht
    gespeicherte Eingaben; er stellt sie unter die neuen Notizen (nichts geht verloren).
    """
    cache.delete(_seed_schluessel(item_id))
    _senden(item_id, {"type": "notizen.neu", "html": html, "vorher": vorher})


def _kuerzel(name: str) -> str:
    teile = [t for t in name.replace("-", " ").split() if t[:1].isalpha()]
    if not teile:
        return "?"
    if len(teile) == 1:
        return teile[0][:2].upper()
    return (teile[0][0] + teile[-1][0]).upper()


class ProtokollNotizenConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]  # channels ohne Typangaben
    """WebSocket ``ws/fraktion/notizen/<item_id>/``: Live-Notizen eines TOPs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.item_id: str = ""
        self.group_name: str | None = None
        self.user: Any = None
        self.membership: Any = None
        self.stufe: str | None = None
        self.person: dict[str, Any] = {}
        # Zufällige Kennung dieser Verbindung für die Anwesenheit (nicht der interne Kanalname)
        self.kennung = secrets.token_hex(6)
        # Stand der Notizen beim Verbinden bzw. nach dem letzten eigenen Speichern (sitzung.notizen_speichern)
        self.basis: str | None = None
        # Nach „neu_aufbauen“ ist der Zustand dieser Verbindung veraltet: nichts mehr speichern
        self.veraltet = False
        self.entzogen = False
        self._geprueft_um = 0.0

    # -- Verbindung ------------------------------------------------------------------------------------------

    async def connect(self) -> None:
        self.item_id = str(self.scope["url_route"]["kwargs"]["item_id"])
        self.user = self.scope.get("user")
        if not self.user or self.user.is_anonymous:
            await self.close(code=4401)
            return
        zugriff = await self._zugriff()
        if zugriff is None:
            await self.close(code=4403)
            return
        self.stufe, self.membership, name = zugriff
        self.person = {
            "id": self.kennung,
            "user": str(self.user.id),
            "name": name,
            "kuerzel": _kuerzel(name),
            # Gleiche Farbe je Person in allen Prozessen (hash() ist je Prozess zufällig)
            "color": CURSOR_COLORS[sum(str(self.user.id).encode()) % len(CURSOR_COLORS)],
        }
        self.group_name = gruppe(self.item_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        self._geprueft_um = time.monotonic()
        await self.accept()
        await self.send_json({"type": "connected", "user": {**self.person, "access_level": self.stufe}})

        zustand, html, stand = await self._laden()
        self.basis = stand
        if zustand is not None:
            await self.send_json({"type": "yjs_state", "data": base64.b64encode(zustand).decode("ascii")})
        else:
            # Kein passender Zustand: genau ein schreibender Editor füllt ihn aus den Notizen, die anderen
            # bekommen den Inhalt über die Synchronisierung
            seed = self.stufe == "edit" and cache.add(_seed_schluessel(self.item_id), self.kennung, SEED_SPERRE)
            await self.send_json({"type": "yjs_state", "data": None, "html": html, "seed": bool(seed)})
        await self._praesenz("da", frage=True)

    async def disconnect(self, code: int) -> None:
        if self.group_name:
            await self._praesenz("weg")
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        # Wer den leeren Zustand füllen sollte und geht, gibt die Zusage frei (der Nächste füllt ihn)
        if self.item_id and cache.get(_seed_schluessel(self.item_id)) == self.kennung:
            cache.delete(_seed_schluessel(self.item_id))

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        if not isinstance(content, dict) or self.entzogen or not await self._gedrosselt_pruefen():
            return
        art = content.get("type")
        if art == "yjs_sync":
            daten = str(content.get("data") or "")
            # Lesende leiten nur Sync-Anfragen und Cursor weiter, keine Änderungen
            if self.stufe != "edit" and not ist_lesende_sync_nachricht(daten):
                return
            await self.channel_layer.group_send(
                self.group_name, {"type": "notizen.sync", "data": daten, "von": self.channel_name}
            )
        elif art == "awareness":
            if self.stufe != "edit":
                return
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "notizen.cursor", "data": str(content.get("data") or ""), "von": self.channel_name},
            )
        elif art == "yjs_save":
            await self._speichern(content.get("data"), content.get("html"))

    # -- Speichern -------------------------------------------------------------------------------------------

    async def _speichern(self, data: Any, html: Any) -> None:
        if self.stufe != "edit" or self.veraltet or not isinstance(data, str) or not isinstance(html, str):
            return
        if len(data) > ZUSTAND_MAX_ZEICHEN:
            return
        try:
            zustand = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return
        # Solange ein anderer Editor den leeren Zustand füllt, speichert nur er (die anderen hätten noch einen leeren
        # oder halben Stand)
        fuellt = cache.get(_seed_schluessel(self.item_id))
        if fuellt and fuellt != self.kennung:
            logger.debug("Live-Notizen %s: Speichern von %s übersprungen, %s füllt", self.item_id, self.kennung, fuellt)
            return
        ergebnis = await self._persistieren(zustand, html)
        if ergebnis is None:
            logger.debug("Live-Notizen %s: Speichern von %s ohne Recht", self.item_id, self.kennung)
            return
        if ergebnis.get("abweichung"):
            # Die Notizen wurden ohne Verbindung geändert: alle verbundenen Editoren bauen neu auf (einmal)
            if cache.add(f"fs-notizen-neu:{self.item_id}", 1, 5):
                await database_sync_to_async(neu_aufbauen)(self.item_id, ergebnis["html"])
            return
        logger.debug("Live-Notizen %s: gespeichert von %s (%s)", self.item_id, self.kennung, ergebnis["stand"])
        self.basis = ergebnis["stand"]
        cache.delete(_seed_schluessel(self.item_id))
        await self.send_json({"type": "yjs_saved", "content_hash": ergebnis["stand"], "gespeichert_um": ergebnis["um"]})
        if ergebnis["geaendert"]:
            await self.channel_layer.group_send(
                self.group_name, {"type": "notizen.inhalt", "html": ergebnis["html"], "von": self.channel_name}
            )

    @database_sync_to_async  # type: ignore[untyped-decorator]
    def _persistieren(self, zustand: bytes, html: str) -> dict[str, Any] | None:
        from apps.work.sanitize import safe_editor_html

        from . import sitzung

        if len(html) > sitzung.NOTIZEN_MAX_ZEICHEN:
            return None
        item = self._top()
        # Genehmigung oder Statuswechsel gelten sofort, nicht erst bei der nächsten Rechteprüfung
        if item is None or not sitzung.darf_protokollieren(self.membership, item.meeting):
            return None
        vorher = sitzung.fingerabdruck(sitzung.notizen(item))
        ergebnis = sitzung.notizen_speichern(item, html, basis=self.basis, membership=self.membership, yjs=zustand)
        if not ergebnis.gespeichert:
            return {"abweichung": True, "html": ergebnis.html}
        um = timezone.localtime(ergebnis.geaendert_um).strftime("%H:%M") if ergebnis.geaendert_um else ""
        return {
            "stand": ergebnis.stand,
            "um": um,
            "geaendert": ergebnis.stand != vorher,
            "html": str(safe_editor_html(ergebnis.html)),
        }

    # -- Gruppen-Nachrichten ---------------------------------------------------------------------------------

    async def notizen_sync(self, event: dict[str, Any]) -> None:
        if event.get("von") == self.channel_name or self.entzogen or not await self._gedrosselt_pruefen():
            return
        if self.stufe == "edit":
            await self.send_json({"type": "yjs_sync", "data": event["data"]})

    async def notizen_cursor(self, event: dict[str, Any]) -> None:
        if event.get("von") == self.channel_name or self.entzogen or self.stufe != "edit":
            return
        await self.send_json({"type": "awareness", "data": event["data"]})

    async def notizen_inhalt(self, event: dict[str, Any]) -> None:
        """Gespeicherte Notizen an Lesende (ohne Editor)."""
        if self.entzogen or self.stufe == "edit" or not await self._gedrosselt_pruefen():
            return
        await self.send_json({"type": "inhalt", "html": event["html"]})

    async def notizen_neu(self, event: dict[str, Any]) -> None:
        if self.entzogen:
            return
        self.veraltet = self.stufe == "edit"
        await self.send_json({"type": "neu_aufbauen", "html": event.get("html", ""), "vorher": event.get("vorher", "")})

    async def notizen_praesenz(self, event: dict[str, Any]) -> None:
        if event.get("von") == self.channel_name or self.entzogen:
            return
        await self.send_json({"type": "praesenz", "art": event["art"], "person": event["person"]})
        if event.get("frage"):
            # Neu verbunden: Wer schon da ist, meldet sich bei allen
            await self._praesenz("da")

    async def _praesenz(self, art: str, *, frage: bool = False) -> None:
        if not self.group_name:
            return
        person = {**self.person, "schreibt": self.stufe == "edit"}
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "notizen.praesenz", "art": art, "person": person, "frage": frage, "von": self.channel_name},
        )

    # -- Rechte ----------------------------------------------------------------------------------------------

    async def _gedrosselt_pruefen(self) -> bool:
        if time.monotonic() - self._geprueft_um >= ZUGRIFF_NEU_PRUEFEN:
            await self._neu_pruefen()
        return not self.entzogen

    async def _neu_pruefen(self) -> None:
        self._geprueft_um = time.monotonic()
        zugriff = await self._zugriff()
        if zugriff is None:
            self.entzogen = True
            if self.group_name:
                await self._praesenz("weg")
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            await self.send_json({"type": "reload", "reason": "access_revoked"})
            await self.close(code=4403)
            return
        stufe, self.membership, _name = zugriff
        if stufe != self.stufe:
            bisher, self.stufe = self.stufe, stufe
            if bisher == "edit":
                await self.send_json({"type": "reload", "reason": "access_changed"})

    def _top(self) -> Any:
        from django.core.exceptions import ValidationError

        from .models import FactionAgendaItem

        try:
            return (
                FactionAgendaItem.objects.select_related("meeting__organization", "parent")
                .filter(pk=self.item_id, proposal_status="active")
                .first()
            )
        except (ValueError, TypeError, ValidationError):
            return None

    @database_sync_to_async  # type: ignore[untyped-decorator]
    def _zugriff(self) -> tuple[str, Any, str] | None:
        """(Stufe, Mitgliedschaft, Anzeigename) oder ``None`` ohne Zugriff."""
        from apps.tenants.models import Membership

        from . import sitzung

        item = self._top()
        if item is None:
            return None
        membership = (
            Membership.objects.select_related("user", "organization")
            .filter(
                user=self.user,
                organization_id=item.meeting.organization_id,
                is_active=True,
                is_guest=False,
                user__is_active=True,
            )
            .first()
        )
        if membership is None or not membership.has_permission("faction.view_public"):
            return None
        # Nichtöffentlich strikt: auch lesend nichts für Nicht-Vereidigte (Issue #64)
        if not sitzung.darf_top_sehen(item, membership):
            return None
        stufe = "edit" if sitzung.darf_protokollieren(membership, item.meeting) else "view"
        return stufe, membership, membership.user.get_display_name()

    @database_sync_to_async  # type: ignore[untyped-decorator]
    def _laden(self) -> tuple[bytes | None, str, str]:
        """(Zustand, falls er zu den Notizen passt; Notizen als HTML; Stand der Notizen)."""
        from apps.work.sanitize import safe_editor_html

        from . import sitzung
        from .models import FactionNotesState

        item = self._top()
        if item is None:
            return None, "", sitzung.fingerabdruck("")
        notizen = sitzung.notizen(item)
        stand = sitzung.fingerabdruck(notizen)
        zustand = FactionNotesState.objects.filter(item=item).first()
        daten = None
        if zustand is not None and zustand.stand == stand and self.stufe == "edit":
            zustand.item = item
            daten = zustand.get_state()
        return daten, str(safe_editor_html(notizen)), stand
