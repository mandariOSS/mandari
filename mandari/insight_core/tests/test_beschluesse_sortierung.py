# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Beschlussliste „Was wurde aus …?“ in fester Reihenfolge (Issue #653).

Neueste Sitzung zuerst, TOPs einer Sitzung in Tagesordnungsreihenfolge – auch bei gleichen
Sortierwerten. Sonst läge die Folge im Belieben der Datenbank und Beschlüsse könnten beim Blättern
doppelt erscheinen oder fehlen.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
from insight_core.models import OParlBody, OParlSource
from insight_core.services import decision_tracking

pytestmark = pytest.mark.django_db


def _schluessel_absteigend(anzahl: int) -> list[uuid.UUID]:
    """Primärschlüssel in absteigender Folge: Ohne eindeutigen Nachrang bliebe die Anlagefolge stehen."""
    return sorted((uuid.uuid4() for _ in range(anzahl)), reverse=True)


def test_beschluesse_gleichzeitiger_sitzungen_in_tagesordnungsfolge() -> None:
    # Veröffentlichender Mandant mit freigeschalteter OParl-Schnittstelle: Die Quelle entsteht beim Anlegen
    tenant = SessionTenant.objects.create(
        name="Bezirk Nord",
        slug="nord",
        insight_publish=True,
        implementation_publish=True,
        oparl_public_since=timezone.now(),
    )
    source = OParlSource.objects.get(sync_config__session_tenant="nord")
    body = OParlBody.objects.create(external_id=f"{source.url}body/", source=source, name="Bezirk Nord", slug="nord")
    tenant.oparl_body = body
    tenant.save(update_fields=["oparl_body"])
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")

    # Zwei Sitzungen beginnen gleichzeitig; die TOPs entstehen gegen die Tagesordnung, alle mit order = 0
    start = timezone.now()
    sitzungen = [
        SessionMeeting.objects.create(
            id=pk, tenant=tenant, organization=rat, name=name, start=start - timedelta(days=tage), is_public=True
        )
        for (name, tage), pk in zip((("A", 0), ("B", 0), ("Vorwoche", 7)), _schluessel_absteigend(3), strict=True)
    ]
    for sitzung in sitzungen:
        for nummer, pk in zip(("3", "2", "1"), _schluessel_absteigend(3), strict=True):
            SessionAgendaItem.objects.create(
                id=pk,
                meeting=sitzung,
                number=nummer,
                name=f"{sitzung.name}/{nummer}",
                vote_result="approved",
                implementation_public=True,
            )

    erwartet = sorted(sitzungen, key=lambda s: (-s.start.timestamp(), s.pk))
    namen = [item.name for item in decision_tracking.public_decisions(body)]
    assert namen == [f"{s.name}/{nummer}" for s in erwartet for nummer in ("1", "2", "3")]
