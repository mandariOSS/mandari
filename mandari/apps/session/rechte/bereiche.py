# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Geltungsbereiche eines Session-Mandanten (Issue #772, ADR „Rechte mit Geltungsbereich“).

Zwei Bäume je Mandant:

- **Körperschaft → Gremien:** Eine Körperschaft umfasst ihre Gremien, nie eine andere Körperschaft – auch nicht
  die Mitgliedsgemeinden einer Samtgemeinde (``SessionBody.parent`` ist eine Rechtsbeziehung, keine Vererbung).
  Gremien ohne Körperschaft (Nachzügler eines älteren Images) zählen zur Standardkörperschaft.
- **Verwaltungsaufbau:** Ein Amt umfasst die Ämter und Sachgebiete darunter (``SessionOrganization.parent``,
  nur Typ „Amt/Fachbereich“). Ein Gremium umfasst nur sich selbst.

Ämter gehören zur Standardkörperschaft, stehen aber nicht in ihrem Teilbaum: Sonst wirkte eine Zuweisung für die
Körperschaft der Verwaltung über die federführenden Ämter auf Vorlagen anderer Körperschaften.
"""

from __future__ import annotations

from typing import Any

from .kern import Baum, Bereich

KOERPERSCHAFT = "koerperschaft"
GREMIUM = "gremium"
AMT = "amt"

#: Gremientyp der Ämter und Fachbereiche
DEPARTMENT_TYPE = "department"


def bereich(art: str, kennung: Any) -> Bereich:
    """Bereich aus Art und Kennung (UUID oder Text)."""
    return Bereich(art=art, kennung=str(kennung))


def bereichsbaum(tenant: Any) -> Baum:
    """Beide Bäume des Mandanten mit zwei Abfragen (Körperschaften, Gremien)."""
    from apps.session.models import SessionBody, SessionOrganization

    koerperschaften = list(SessionBody.objects.filter(tenant=tenant).values_list("pk", "is_default"))
    standard = next((pk for pk, is_default in koerperschaften if is_default), None)
    kinder: dict[Bereich, list[Bereich]] = {bereich(KOERPERSCHAFT, pk): [] for pk, _ in koerperschaften}

    gremien = SessionOrganization.objects.filter(tenant=tenant).values_list(
        "pk", "organization_type", "parent_id", "body_id"
    )
    aemter: dict[Any, Any] = {}
    for pk, typ, parent_id, body_id in gremien:
        if typ == DEPARTMENT_TYPE:
            aemter[pk] = parent_id
            continue
        knoten = bereich(GREMIUM, pk)
        kinder.setdefault(knoten, [])
        koerperschaft = body_id or standard
        if koerperschaft is not None:
            kinder.setdefault(bereich(KOERPERSCHAFT, koerperschaft), []).append(knoten)

    for pk, parent_id in aemter.items():
        kinder.setdefault(bereich(AMT, pk), [])
        if parent_id in aemter:
            kinder.setdefault(bereich(AMT, parent_id), []).append(bereich(AMT, pk))
    return Baum(kinder)
