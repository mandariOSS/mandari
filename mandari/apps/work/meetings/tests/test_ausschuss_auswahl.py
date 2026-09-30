# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ausschüsse im Filter der Sitzungsliste.

OParl kennt als ``organizationType`` nur „Gremium“; ob ein Gremium ein Ausschuss ist, steht in
``classification``. Die Session-Schnittstelle liefert inzwischen die Werte der Spezifikation; ältere
Spiegelungen eines Session-Mandanten tragen noch dessen Schlüssel ``committee``. Beide Stände müssen
im Filter erscheinen.
"""

from __future__ import annotations

import pytest

from apps.work.meetings import selectors
from insight_core.models import OParlBody, OParlOrganization, OParlSource

pytestmark = pytest.mark.django_db

RIS = "https://ris.example/oparl"


def test_ausschuesse_nach_spezifikation_und_aus_aelteren_spiegelungen() -> None:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt", slug="musterstadt")

    def gremium(nummer: int, name: str, art: str, einordnung: str | None = None) -> None:
        OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/{nummer}",
            body=body,
            name=name,
            organization_type=art,
            classification=einordnung,
        )

    gremium(1, "Bauausschuss", "Gremium", "Ausschuss")
    gremium(2, "Schulausschuss", "committee", "Ausschuss")  # ältere Spiegelung eines Session-Mandanten
    gremium(3, "Rat", "Gremium", "Rat")
    gremium(4, "Fraktion Mitte", "Fraktion", "Fraktion")

    auswahl = selectors.committee_choices(OParlBody.objects.filter(pk=body.pk))

    assert [eintrag["name"] for eintrag in auswahl] == ["Bauausschuss", "Schulausschuss"]
