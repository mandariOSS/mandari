# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertragsregister der Datendrehscheibe (``docs/adr/20260929-ereignisvertraege.md``).

- ``Envelope``: Ereignishülle; ihr Schema liegt in ``envelope/v1.json``.
- ``get_registry()``: Schemas je Typ und Version aus ``schemas/<typ>/v<n>.json``, geprüft gegen
  die Namensregeln (``naming.py``) und die Vertragsregeln (``rules.py``).
- ``ContractViolationError``: Daten passen nicht zum Vertrag; die Meldung enthält keine Werte.

Welche Ereignisse es gibt, legt der Eigentümer (``x-owner``) fest. Änderungen an einem Schema
sind nur additiv; alles andere ergibt eine neue Version.
"""

from .envelope import ENVELOPE_VERSION, OPERATIONS, VISIBILITIES, Envelope, envelope_schema
from .naming import COMMAND, EVENT, check_name
from .registry import (
    SCHEMA_ROOT,
    Contract,
    ContractError,
    Registry,
    UnknownContractError,
    get_registry,
    load_registry,
)
from .validation import ContractViolationError, validate_envelope

__all__ = [
    "COMMAND",
    "ENVELOPE_VERSION",
    "EVENT",
    "OPERATIONS",
    "SCHEMA_ROOT",
    "VISIBILITIES",
    "Contract",
    "ContractError",
    "ContractViolationError",
    "Envelope",
    "Registry",
    "UnknownContractError",
    "check_name",
    "envelope_schema",
    "get_registry",
    "load_registry",
    "validate_envelope",
]
