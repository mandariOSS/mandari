# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vier-Augen-Prinzip bei Monatspauschalen (Vorlagennummern: test_numbering.py)."""

from __future__ import annotations

from types import SimpleNamespace

from apps.session.services import allowance_service


class _Allowance:
    def __init__(self, created_by_id: int | None, status: str = "pending") -> None:
        self.created_by_id = created_by_id
        self.status = status
        self.approved_by = None
        self.approved_at = None

    def save(self, update_fields: list[str] | None = None) -> None:
        pass


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
