# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ziele der gemeinsamen RIS-Bausteine in Work (Issue #853): Sitzung, Vorgang, Person und Gremium öffnen die
Recherche-Seiten der Organisation statt der Seiten von Insight (``insight_core.ris_links``).
"""

from __future__ import annotations

from typing import Any

from django.urls import reverse

from insight_core.ris_links import RisLinks

#: Adressname und Name der Kennung je Art
ZIELE: dict[str, tuple[str, str]] = {
    "sitzung": ("work:ris_meeting_detail", "meeting_id"),
    "vorgang": ("work:ris_paper_detail", "paper_id"),
    "person": ("work:ris_person_detail", "person_id"),
    "gremium": ("work:ris_organization_detail", "org_id"),
}


class WorkRisLinks(RisLinks):
    """Ziele in der Recherche einer Organisation."""

    def __init__(self, org_slug: str) -> None:
        self.org_slug = org_slug

    def url(self, art: str, kennung: Any) -> str:
        name, schluessel = ZIELE[art]
        return reverse(name, kwargs={"org_slug": self.org_slug, schluessel: kennung})
