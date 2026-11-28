# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle der Datendrehscheibe (``docs/adr/20260929-befehle-synchron.md``, ``docs/adr/20261128-api-first-ohne-ausnahme.md``).

Wer Daten eines Moduls ändern will, schickt dem Eigentümer einen Befehl und erhält sofort eine Quittung oder einen
Fehler nach RFC 9457. Das gilt für andere Module, für Clients der Schnittstelle und für die eigene Oberfläche:

    from hub.commands import Command, InProcessClient

    receipt = InProcessClient().send(
        Command(name="submission.submit", body={...}, idempotency_key=key, tenant_ref=f"session:{id}")
    )
    receipt.reference, receipt.received_at, receipt.content_hash

- ``dispatch``/``Dispatcher``: prüft Schlüssel, Vertrag und Inhalt, führt den Handler beim Eigentümer
  in dessen Transaktion aus und speichert die Quittung zum Idempotenzschlüssel. Eine Quittung ist
  immer festgeschrieben; der Aufruf steht deshalb nie in einer offenen Transaktion des Aufrufers
  (sonst ``apps.events.idempotency.NestedTransactionError``).
- ``command_handler``: registriert den Handler des Eigentümers, mit ``Exposure`` zugleich als Operation der
  gemeinsamen Schnittstelle (``hub.api.befehle``); Mandantenbereiche der Adressen meldet der Eigentümer mit
  ``register_tenant_area`` an.
- ``ausfuehren``/``Ergebnis``: der Aufruf aus einer View, mit Quittung oder Fehlern für das Formular.
- ``InProcessClient``/``HttpClient``: dieselbe Schnittstelle für eine oder getrennte Installationen.
- ``command_urlpatterns``: HTTP-Weg zwischen Installationen mit Anmeldung durch die Installation.

Das Paket enthält keine Fachregeln; die liegen beim Eigentümer (z. B. ``apps/session/commands.py``,
``apps/session/befehle/``).
"""

from .canonical import canonical_json, content_hash
from .clients import CommandClient, HttpClient, InProcessClient
from .dispatcher import Dispatcher, command_handler, dispatch, get_dispatcher
from .exposure import Exposure, TenantArea, register_tenant_area, tenant_area, tenant_areas
from .http import Principal, command_urlpatterns
from .oberflaeche import Ergebnis, ausfuehren
from .problems import CommandError, Problem
from .types import Command, HandlerResult, Receipt

__all__ = [
    "Command",
    "CommandClient",
    "CommandError",
    "Dispatcher",
    "Ergebnis",
    "Exposure",
    "HandlerResult",
    "HttpClient",
    "InProcessClient",
    "Principal",
    "Problem",
    "Receipt",
    "TenantArea",
    "ausfuehren",
    "canonical_json",
    "command_handler",
    "command_urlpatterns",
    "content_hash",
    "dispatch",
    "get_dispatcher",
    "register_tenant_area",
    "tenant_area",
    "tenant_areas",
]
