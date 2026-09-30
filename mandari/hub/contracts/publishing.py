# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertragsprüfung für ``apps.events.publish()`` (``docs/adr/20260929-ereignisvertraege.md``).

``publish()`` prüft in Tests und bei ``DEBUG`` jedes Ereignis gegen das Register: Hülle, Typ und
Version, Sichtbarkeit und Nutzlast. Die Plattform darf die Drehscheibe nicht importieren
(``docs/adr/20260929-schichtenmodell.md``); ``ContractsConfig.ready()`` hängt diese Funktion deshalb
bei ``apps.events.publishing.set_contract_validator()`` ein.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .registry import get_registry


def validate_published_event(envelope: Mapping[str, Any]) -> None:
    """Prüft die JSON-Darstellung eines Ereignisses; wirft wie ``Registry.validate_event``.

    ``UnknownContractError``, wenn es für Typ und Version kein Schema gibt, sonst
    ``ContractViolationError``. Beide Meldungen nennen Stelle und Regel, nie Werte.
    """
    get_registry().validate_event(envelope)
