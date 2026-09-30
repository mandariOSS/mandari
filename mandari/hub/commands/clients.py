# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Clients für Befehle: eine Schnittstelle, zwei Wege (``docs/adr/20260929-befehle-synchron.md``).

- ``InProcessClient``: gleiche Installation, ruft den Dispatcher direkt auf.
- ``HttpClient``: getrennte Installationen (z. B. Work gehostet, Session im Rechenzentrum einer
  Kommune), sendet den Befehl an den HTTP-Weg des Eigentümers (``hub.commands.http``).

Beide liefern dieselbe ``Receipt`` bzw. werfen dasselbe ``CommandError`` mit einem Problem nach
RFC 9457; das sichert eine gemeinsame Vertragssuite (``hub/commands/tests/test_vertrag.py``).
Ist der Eigentümer nicht erreichbar, meldet der ``HttpClient`` ein 503 (``retryable``): Der Aufrufer
zeigt das an und wiederholt später mit demselben Idempotenzschlüssel.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Protocol

import httpx

from .dispatcher import KEY_MISSING, Dispatcher, checked_content_hash, get_dispatcher, valid_idempotency_key
from .http import CORRELATION_HEADER, IDEMPOTENCY_HEADER, TENANT_HEADER
from .problems import CommandError, Problem
from .types import Command, Receipt

logger = logging.getLogger("hub.commands")

UNREACHABLE: Final = "Der Eigentümer ist nicht erreichbar. Bitte später mit demselben Idempotenzschlüssel wiederholen."
BAD_RESPONSE: Final = (
    "Der Eigentümer hat unerwartet geantwortet. Bitte später mit demselben Idempotenzschlüssel wiederholen."
)


class CommandClient(Protocol):
    def send(self, command: Command) -> Receipt:
        """Sendet den Befehl; ``CommandError`` mit Problem nach RFC 9457, wenn er scheitert."""
        ...


class InProcessClient:
    """Befehle in derselben Installation (Dispatcher Standard: der der Installation)."""

    def __init__(self, dispatcher: Dispatcher | None = None) -> None:
        self._dispatcher = dispatcher

    def send(self, command: Command) -> Receipt:
        return (self._dispatcher or get_dispatcher()).dispatch(command)


class HttpClient:
    """
    Befehle an eine andere Installation über HTTP.

    ``base_url`` ist die Adresse, unter der die Installation ``command_urlpatterns`` einbindet (z. B.
    ``https://ris.beispiel.example/api/v1/befehle/``); ``token`` meldet an. Mandant und Auslöser
    bestimmt die Gegenseite aus der Anmeldung; der Mandant des Befehls geht als ``Mandari-Tenant``
    mit und muss dazu passen.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def send(self, command: Command) -> Receipt:
        # Ein ungültiger Schlüssel ließe sich nicht einmal als Header senden; gleiches Problem wie beim Dispatcher.
        if not valid_idempotency_key(command.idempotency_key):
            raise CommandError.of(400, "idempotenzschluessel-fehlt", KEY_MISSING)
        # Ebenso ein Inhalt, der sich nicht als JSON darstellen lässt: dasselbe 422 wie beim Dispatcher.
        checked_content_hash(command.body)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, application/problem+json",
            TENANT_HEADER: command.tenant_ref,
            CORRELATION_HEADER: str(command.correlation_id),
        }
        headers[IDEMPOTENCY_HEADER] = f'"{command.idempotency_key}"'
        url = f"{self._base_url}/{command.name}/v{command.version}"
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client:
                response = client.post(url, json=dict(command.body), headers=headers)
        except httpx.HTTPError:
            logger.warning("Befehl %s v%s: Eigentümer nicht erreichbar", command.name, command.version)
            raise CommandError.of(503, "eigentuemer-nicht-erreichbar", UNREACHABLE) from None
        return self._receipt(response)

    @staticmethod
    def _receipt(response: httpx.Response) -> Receipt:
        data = _json(response)
        if response.status_code == 201 and data is not None:
            try:
                return Receipt.from_dict(data)
            except (KeyError, TypeError, ValueError):
                pass
        elif response.status_code >= 400 and data is not None and "type" in data:
            raise CommandError(Problem.from_dict(data, status=response.status_code))
        raise CommandError.of(502, "unerwartete-antwort", BAD_RESPONSE)


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
