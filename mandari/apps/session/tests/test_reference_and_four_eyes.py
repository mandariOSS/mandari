# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vier-Augen-Prinzip bei Monatspauschalen (Vorlagennummern: test_numbering.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apps.session.models import (
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionPerson,
    SessionTenant,
    SessionUser,
)
from apps.session.services import allowance_service
from apps.session.tests._niederschrift import nutzer

pytestmark = pytest.mark.django_db


class TestMonthlyAllowanceFourEyes:
    def test_eigene_posten_werden_nicht_genehmigt(self) -> None:
        # Gespeicherte Posten: Die Genehmigung prüft den Stand in der Datenbank, nicht das übergebene Objekt
        tenant = SessionTenant.objects.create(name="Stadt Nord", slug="nord")
        approver = nutzer(tenant, "pruefung", "manage_allowances")
        lauf = nutzer(tenant, "lauf", "manage_allowances")
        person = SessionPerson.objects.create(tenant=tenant, given_name="P", family_name="Amsel")
        rate = SessionMonthlyRate.objects.create(tenant=tenant, name="Pauschale", amount=Decimal("100.00"))

        def posten(monat: int, created_by: SessionUser | None, status: str = "pending") -> SessionMonthlyAllowance:
            return SessionMonthlyAllowance.objects.create(
                tenant=tenant,
                person=person,
                rate=rate,
                period=date(2026, monat, 1),
                amount=rate.amount,
                created_by=created_by,
                status=status,
            )

        own = posten(1, approver)
        foreign = posten(2, lauf)
        system = posten(3, None)
        done = posten(4, lauf, status="approved")

        result = allowance_service.approve_monthly_allowances([own, foreign, system, done], approver)

        assert result == {"approved": 2, "blocked_four_eyes": 1}
        assert own.status == "pending"
        assert foreign.status == "approved" and foreign.approved_by == approver
        assert system.status == "approved"
        gespeichert = dict(SessionMonthlyAllowance.objects.filter(tenant=tenant).values_list("pk", "status"))
        assert gespeichert == {own.pk: "pending", foreign.pk: "approved", system.pk: "approved", done.pk: "approved"}
        assert SessionMonthlyAllowance.objects.get(pk=done.pk).approved_by_id is None, "bereits genehmigt: unverändert"
