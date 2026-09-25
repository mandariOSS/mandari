# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hash-Kette der Fraktions-Änderungshistorie (Issue #221): dieselbe Kettenlogik wie im Session RIS,
eine Kette je Organisation, Manipulation per Raw-SQL wird erkannt, gelöschte Mitglieder brechen
die Kette nicht.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from apps.common import audit_chain
from apps.common.models import AuditChainHead
from apps.common.tests.factories import OrganizationFactory
from apps.tenants.models import Organization
from apps.work.faction.models import FactionAgendaItem, FactionAuditLog, FactionMeeting

pytestmark = pytest.mark.django_db

PERMISSIONS = ["faction.view_public", "faction.view_non_public", "faction.manage"]


def _sitzung(org: Any, chair: Any) -> FactionMeeting:
    from apps.common import audit_core

    request = type("Anfrage", (), {"membership": chair, "META": {}})()
    cast(Any, audit_core).set_current_request(request)
    try:
        meeting = FactionMeeting.objects.create(
            organization=org,
            title="Fraktionssitzung",
            start=timezone.now() + timedelta(days=2),
            status="planned",
            created_by=chair,
        )
        FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt", visibility="public")
    finally:
        cast(Any, audit_core).clear_current_request()
    return meeting


def test_eintraege_sind_je_organisation_verkettet(org: Any, make_member: Any) -> None:
    chair = make_member(org, PERMISSIONS, email="vorsitz@example.org")
    _sitzung(org, chair)
    andere = cast(Organization, OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion"))  # type: ignore[no-untyped-call]
    _sitzung(andere, make_member(andere, PERMISSIONS, email="andere@example.org"))

    eintraege = list(FactionAuditLog.objects.filter(organization=org).order_by("seq"))
    assert [e.seq for e in eintraege] == list(range(1, len(eintraege) + 1))
    assert eintraege[0].membership_ref == chair.pk
    assert audit_chain.verify(audit_chain.FACTION, org.pk).ok
    assert audit_chain.verify(audit_chain.FACTION, andere.pk).ok
    assert FactionAuditLog.objects.filter(organization=andere, seq=1).exists()


def test_manipulation_wird_erkannt(org: Any, make_member: Any) -> None:
    _sitzung(org, make_member(org, PERMISSIONS, email="vorsitz@example.org"))
    ziel = FactionAuditLog.objects.filter(organization=org).order_by("seq").first()
    assert ziel is not None
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE work_factionauditlog SET actor_label = %s WHERE id = %s",
            ["Jemand anderes", FactionAuditLog._meta.pk.get_db_prep_value(ziel.pk, connection)],
        )
    assert not audit_chain.verify(audit_chain.FACTION, org.pk).ok
    with pytest.raises(CommandError):
        call_command("verify_audit_chain", organization=org.slug, stdout=StringIO(), stderr=StringIO())


def test_geloeschtes_mitglied_bricht_die_kette_nicht(org: Any, make_member: Any) -> None:
    chair = make_member(org, PERMISSIONS, email="vorsitz@example.org")
    _sitzung(org, chair)
    chair.delete()
    assert FactionAuditLog.objects.filter(organization=org, membership__isnull=True).exists()
    assert audit_chain.verify(audit_chain.FACTION, org.pk).ok


def test_kettenkopf_verschwindet_mit_der_organisation(org: Any, make_member: Any) -> None:
    _sitzung(org, make_member(org, PERMISSIONS, email="vorsitz@example.org"))
    scope = audit_chain.FACTION.scope_key(org.pk)
    assert AuditChainHead.objects.filter(scope=scope).exists()
    org.delete()
    assert not AuditChainHead.objects.filter(scope=scope).exists()
