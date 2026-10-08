# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ziele der gemeinsamen RIS-Bausteine (Issue #853): Wohin führt eine Sitzung, ein Vorgang, eine Person, ein Gremium?

Die Listen von Insight und Work teilen sich ihre Bausteine (``templates/cotton/liste/``). Damit sie nicht fest auf
die Adressen von Insight verdrahtet sind, bekommen sie die Ziele als Objekt: ``RisLinks.url(art, kennung)``. Ohne
Angabe gelten die Adressen von Insight (``INSIGHT``); Work reicht eigene Ziele herein (``apps.work.ris.links``).

In Vorlagen: ``{% load insight_listen %}{% ris_url links "sitzung" meeting.pk as ziel %}``.
"""

from __future__ import annotations

from typing import Any

from django.urls import reverse

#: Arten der Ziele
ARTEN = ("sitzung", "vorgang", "person", "gremium")


class RisLinks:
    """Ziele im Bürgerportal Insight; Unterklassen setzen andere Adressen ein."""

    namen: dict[str, str] = {
        "sitzung": "insight_core:insight:meeting_detail",
        "vorgang": "insight_core:insight:paper_detail",
        "person": "insight_core:insight:person_detail",
        "gremium": "insight_core:insight:organization_detail",
    }

    def url(self, art: str, kennung: Any) -> str:
        """Adresse der Detailseite von ``kennung`` (Primärschlüssel) der Art ``art``."""
        return reverse(self.namen[art], args=[kennung])


#: Vorgabe der Bausteine: Detailseiten von Insight
INSIGHT = RisLinks()
