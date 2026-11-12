# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine für öffentliche Personenlisten und -filter.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, OuterRef, Q

from ..models import OParlMembership


def bestehende_mitgliedschaft(person: str, *bedingungen: Q, **felder: Any) -> Exists:
    """
    Unterabfrage „die Person hat eine bestehende Mitgliedschaft, die alle Bedingungen erfüllt“.

    Bestehend heißt: weder die Mitgliedschaft noch ihr Gremium ist in der Quelle gelöscht oder zurückgenommen.
    Öffentliche Filter und Suchen gehen nur darüber; ein Verbund über ``memberships__…`` träfe auch gelöschte
    Zeilen und ließe an den Treffern erkennen, was dort stand. ``person`` nennt das Feld der äußeren Abfrage
    (``"pk"`` für Personen, ``"recipient"`` für Fragen); alle Bedingungen gelten für dieselbe Mitgliedschaft.
    """
    return Exists(
        OParlMembership.objects.filter(
            *bedingungen, person=OuterRef(person), deleted=False, organization__deleted=False, **felder
        )
    )
