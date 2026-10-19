# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kleine ASGI-Anwendung für die Tests mehrerer Anwendungsprozesse (``test_app_processes.py``, Issue #718).

Läuft in echten Daphne-Prozessen unter ``mandari.app_processes`` und nutzt denselben Channel-Layer wie
der Betrieb (``mandari.redis_config``, Redis aus ``REDIS_URL``):

- HTTP antwortet mit der Prozessnummer (PID). ``/health/live/`` antwortet 503, solange es die Datei
  ``krank-<pid>`` im Verzeichnis ``MEHRPROZESS_MARKER_DIR`` gibt (festgefahrener Prozess).
- WebSocket ``/ws/<gruppe>/``: Beim Verbinden ``{"pid": …}``; jede Nachricht geht über den Channel-Layer
  an alle Verbindungen der Gruppe, gleich in welchem Prozess, als ``{"text": …, "von": …, "pid": …}``.

Kein Testmodul (wird nicht gesammelt) und nur in diesen Prozessen importiert: Es konfiguriert Django selbst.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import django
from django.conf import settings

from mandari.redis_config import build_channel_layers

settings.configure(CHANNEL_LAYERS=build_channel_layers(os.environ.get("REDIS_URL", "")), INSTALLED_APPS=[])
django.setup()

from channels.generic.websocket import AsyncJsonWebsocketConsumer  # noqa: E402 – erst nach django.setup()
from channels.routing import ProtocolTypeRouter  # noqa: E402

Scope = dict[str, Any]
Receive = Any
Send = Any


def _krank() -> bool:
    verzeichnis = os.environ.get("MEHRPROZESS_MARKER_DIR", "")
    return bool(verzeichnis) and (Path(verzeichnis) / f"krank-{os.getpid()}").exists()


async def http_app(scope: Scope, receive: Receive, send: Send) -> None:
    if (await receive())["type"] != "http.request":
        return
    status = 503 if scope["path"] == "/health/live/" and _krank() else 200
    body = json.dumps({"pid": os.getpid()}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


class GruppenConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]  # channels ohne Typangaben
    gruppe = ""

    async def connect(self) -> None:
        self.gruppe = str(self.scope["path"]).strip("/").split("/")[-1]
        await self.channel_layer.group_add(self.gruppe, self.channel_name)
        await self.accept()
        await self.send_json({"pid": os.getpid()})

    async def receive_json(self, content: dict[str, Any], **kwargs: Any) -> None:
        nachricht = {"type": "gruppe.nachricht", "text": content["text"], "von": os.getpid()}
        await self.channel_layer.group_send(self.gruppe, nachricht)

    async def gruppe_nachricht(self, event: dict[str, Any]) -> None:
        await self.send_json({"text": event["text"], "von": event["von"], "pid": os.getpid()})

    async def disconnect(self, code: int) -> None:
        await self.channel_layer.group_discard(self.gruppe, self.channel_name)


application = ProtocolTypeRouter({"http": http_app, "websocket": GruppenConsumer.as_asgi()})
