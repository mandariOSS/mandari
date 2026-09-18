# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechteprüfung in Work: einmal laden je Mitgliedschaft statt einer Abfrage je Prüfung, Admin-Status
aus den geladenen Rollen, Gäste nie mit Rechten – auch nicht mit einer Admin-Rolle.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.common.tests.factories import RoleFactory


@pytest.mark.django_db
def test_wiederholte_pruefungen_ohne_neue_abfragen(
    org: Any, make_member: Any, django_assert_max_num_queries: Any
) -> None:
    mitglied = make_member(org, ["motions.view", "motions.create"])
    assert mitglied.has_permission("motions.view")
    with django_assert_max_num_queries(0):
        assert mitglied.has_permission("motions.create")
        assert not mitglied.has_permission("organization.edit")
        assert not mitglied.has_permission("organization.manage_roles")


@pytest.mark.django_db
def test_admin_hat_alle_rechte(org: Any, make_member: Any) -> None:
    admin = make_member(org, [], is_admin=True)
    assert admin.has_permission("organization.manage_roles") and admin.has_permission("motions.create")


@pytest.mark.django_db
def test_gast_mit_admin_rolle_hat_keine_rechte(org: Any, make_member: Any) -> None:
    gast = make_member(org, [], is_admin=True)
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    gast.__dict__.pop("_permission_checker", None)
    assert not gast.has_permission("motions.view")


@pytest.mark.django_db
def test_rollenwechsel_wirkt_sofort(org: Any, make_member: Any) -> None:
    mitglied = make_member(org, ["motions.view"])
    assert not mitglied.has_permission("organization.edit")
    mitglied.roles.add(cast(Any, RoleFactory)(organization=org, permissions=["organization.edit"]))
    assert mitglied.has_permission("organization.edit")
