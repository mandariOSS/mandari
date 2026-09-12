# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aktenzeichen bei Antrag→Vorlage und Vier-Augen-Prinzip bei Monatspauschalen."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.session.models import SessionPaper, SessionTenant
from apps.session.services import allowance_service


class _Allowance:
    def __init__(self, created_by_id: int | None, status: str = "pending") -> None:
        self.created_by_id = created_by_id
        self.status = status
        self.approved_by = None
        self.approved_at = None

    def save(self, update_fields: list[str] | None = None) -> None:
        pass


class TestPaperReference:
    @pytest.mark.django_db
    def test_fortlaufend_je_jahr_und_mandant(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt A", slug="stadt-a")
        other = SessionTenant.objects.create(name="Stadt B", slug="stadt-b")
        SessionPaper.objects.create(tenant=tenant, reference="V/2026/0001", name="Eins")
        SessionPaper.objects.create(tenant=tenant, reference="V/2026/0009", name="Neun")
        SessionPaper.objects.create(tenant=tenant, reference="V/2025/0042", name="Vorjahr")
        SessionPaper.objects.create(tenant=tenant, reference="V/2026/sonder", name="Freitext")

        assert SessionPaper.next_reference(tenant, 2026) == "V/2026/0010"
        assert SessionPaper.next_reference(tenant, 2027) == "V/2027/0001"
        assert SessionPaper.next_reference(other, 2026) == "V/2026/0001"


class TestMonthlyAllowanceFourEyes:
    def test_eigene_posten_werden_nicht_genehmigt(self) -> None:
        approver = SimpleNamespace(pk=7)
        own = _Allowance(created_by_id=7)
        foreign = _Allowance(created_by_id=8)
        system = _Allowance(created_by_id=None)
        done = _Allowance(created_by_id=8, status="approved")

        result = allowance_service.approve_monthly_allowances([own, foreign, system, done], approver)

        assert result == {"approved": 2, "blocked_four_eyes": 1}
        assert own.status == "pending"
        assert foreign.status == "approved" and foreign.approved_by is approver
        assert system.status == "approved"
