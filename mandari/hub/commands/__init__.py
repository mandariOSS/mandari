# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle der Datendrehscheibe (``docs/adr/20260929-befehle-synchron.md``).

Wer Daten eines anderen Moduls ändern will, schickt dem Eigentümer einen Befehl und erhält sofort
eine Quittung oder einen Fehler nach RFC 9457:

    from hub.commands import Command, InProcessClient

    receipt = InProcessClient().send(
        Command(name="submission.submit", body={...}, idempotency_key=key, tenant_ref=f"session:{id}")
    )
    receipt.reference, receipt.received_at, receipt.content_hash

- ``dispatch``/``Dispatcher``: prüft Schlüssel, Vertrag und Inhalt, führt den Handler beim Eigentümer
  in dessen Transaktion aus und speichert die Quittung zum Idempotenzschlüssel.
- ``command_handler``: registriert den Handler des Eigentümers.
- ``InProcessClient``/``HttpClient``: dieselbe Schnittstelle für eine oder getrennte Installationen.
- ``command_urlpatterns``: HTTP-Weg mit Anmeldung durch die Installation.

Das Paket enthält keine Fachregeln; die liegen beim Eigentümer (z. B. ``apps/session/commands.py``).
"""

from .canonical import canonical_json, content_hash
from .clients import CommandClient, HttpClient, InProcessClient
from .dispatcher import Dispatcher, command_handler, dispatch, get_dispatcher
from .http import Principal, command_urlpatterns
from .problems import CommandError, Problem
from .types import Command, HandlerResult, Receipt

__all__ = [
    "Command",
    "CommandClient",
    "CommandError",
    "Dispatcher",
    "HandlerResult",
    "HttpClient",
    "InProcessClient",
    "Principal",
    "Problem",
    "Receipt",
    "canonical_json",
    "command_handler",
    "command_urlpatterns",
    "content_hash",
    "dispatch",
    "get_dispatcher",
]
