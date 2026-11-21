# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Testhelfer: einer Person ein laufendes Amt geben (Issue #1560).

mandari zeigt nur Personen mit laufendem Amt (``services/personen_sichtbarkeit``). Tests, die eine Person öffentlich
sehen wollen, es aber nicht um ihre Mitgliedschaften geht, geben ihr hiermit eines: eine Mitgliedschaft ohne Rolle in
einem Gremium „Gremium für Tests“ ihrer Kommune.
"""

from __future__ import annotations

from typing import Any

from insight_core.models import OParlMembership, OParlOrganization, OParlPerson


def im_amt(person: OParlPerson, **felder: Any) -> OParlPerson:
    """Gibt der Person eine laufende Mitgliedschaft und liefert sie zurück (für Verkettung beim Anlegen)."""
    gremium, _ = OParlOrganization.objects.get_or_create(
        external_id=f"test-amt-gremium-{person.body_id}",
        defaults={"body_id": person.body_id, "name": "Gremium für Tests"},
    )
    OParlMembership.objects.get_or_create(
        external_id=f"test-amt-{person.pk}", defaults={"person": person, "organization": gremium, **felder}
    )
    return person
