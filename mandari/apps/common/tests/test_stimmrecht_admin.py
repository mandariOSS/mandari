# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stimmrecht kommt nicht über die Administrator-Rolle (Issue #1166).

Vorgabe: „Admins haben nicht automatisch Stimmrecht!“ Das Stimmrecht (``voting.participate``) gibt nur eine Rolle, die
es selbst enthält, oder ein Einzelrecht; ausdrücklich verweigert geht vor. Alle übrigen Rechte der Administrator-Rolle
bleiben. Dieselbe Regel gilt in der Rechteprüfung, der Vorauswahl „Zur Abstimmung geben“ und der Rechteanzeige.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.common.permissions import (
    ADMIN_RECHTE,
    DEFAULT_ROLES,
    NICHT_DURCH_ADMIN,
    PERMISSIONS,
    STIMMRECHT,
    PermissionChecker,
)
from apps.common.tests.factories import PermissionFactory, RoleFactory

pytestmark = pytest.mark.django_db


def _standardrolle(org: Any, key: str) -> Any:
    """
    Rolle mit den Rechten der Standardrolle aus ``DEFAULT_ROLES``.

    Immer neu angelegt (eigener Name): Eine von der Organisation schon angelegte Standardrolle kann ohne Rechte dastehen,
    wenn ein Test mit ``transaction=True`` vorher die Rechtetabelle geleert hat (Reihenfolge in der CI).
    """
    definition = DEFAULT_ROLES[key]
    return cast(Any, RoleFactory)(
        organization=org,
        name=f"{definition['name']} (Test)",
        permissions=list(cast(list[str], definition["permissions"])),
        is_admin=bool(definition.get("is_admin")),
    )


def _stimmrecht() -> Any:
    return cast(Any, PermissionFactory)(codename=STIMMRECHT)


def _darf_abstimmen(membership: Any) -> bool:
    return bool(cast(Any, PermissionChecker)(membership).has_permission(STIMMRECHT))


def test_admin_allein_hat_kein_stimmrecht_aber_alle_anderen_rechte(org: Any, make_member: Any) -> None:
    admin = make_member(org, [], is_admin=True)
    checker = cast(Any, PermissionChecker)(admin)
    assert checker.is_admin()
    assert not checker.has_permission(STIMMRECHT)
    assert STIMMRECHT not in checker.permissions
    assert all(checker.has_permission(code) for code in ADMIN_RECHTE)
    assert not admin.has_permission(STIMMRECHT)


def test_gespeichertes_stimmrecht_einer_admin_rolle_zaehlt_nicht(org: Any, make_member: Any) -> None:
    """Bestehende Admin-Rollen speichern alle Rechte, auch das Stimmrecht (früherer Standard)."""
    admin = make_member(org, list(PERMISSIONS), is_admin=True)
    assert not _darf_abstimmen(admin)


def test_admin_mit_fraktionsvorsitz_hat_stimmrecht(org: Any, make_member: Any) -> None:
    admin = make_member(org, [], is_admin=True)
    admin.roles.add(_standardrolle(org, "faction_chair"))
    assert _darf_abstimmen(admin)


def test_admin_mit_geschaeftsfuehrung_hat_kein_stimmrecht(org: Any, make_member: Any) -> None:
    admin = make_member(org, [], is_admin=True)
    admin.roles.add(_standardrolle(org, "managing_director"))
    assert not _darf_abstimmen(admin)


def test_einzelrecht_gibt_stimmrecht(org: Any, make_member: Any) -> None:
    admin = make_member(org, [], is_admin=True)
    admin.individual_permissions.add(_stimmrecht())
    ohne_rolle = make_member(org, ["faction.view_public"])
    ohne_rolle.individual_permissions.add(_stimmrecht())
    assert _darf_abstimmen(admin)
    assert _darf_abstimmen(ohne_rolle)


def test_verweigertes_stimmrecht_geht_vor(org: Any, make_member: Any) -> None:
    mitglied = make_member(org, [STIMMRECHT], is_admin=False)
    mitglied.roles.add(_standardrolle(org, "admin"))
    mitglied.denied_permissions.add(_stimmrecht())
    assert not _darf_abstimmen(mitglied)


def test_standardrollen_stimmrecht_nur_fuer_mandatstraeger() -> None:
    assert frozenset({STIMMRECHT}) == NICHT_DURCH_ADMIN
    assert set(cast(list[str], DEFAULT_ROLES["admin"]["permissions"])) == set(PERMISSIONS) - NICHT_DURCH_ADMIN
    mit_stimme = {key for key, d in DEFAULT_ROLES.items() if STIMMRECHT in cast(list[str], d["permissions"])}
    assert mit_stimme == {"faction_chair", "faction_vice_chair", "faction_member"}


def test_vorauswahl_zur_abstimmung_zaehlt_wie_die_rechtepruefung(org: Any, make_member: Any) -> None:
    from apps.work.motions.ablauf import stimmberechtigte_ids

    nur_admin = make_member(org, list(PERMISSIONS), is_admin=True, email="admin@example.org")
    admin_gf = make_member(org, [], is_admin=True, email="admin-gf@example.org")
    admin_gf.roles.add(_standardrolle(org, "managing_director"))
    admin_vorsitz = make_member(org, [], is_admin=True, email="admin-vorsitz@example.org")
    admin_vorsitz.roles.add(_standardrolle(org, "faction_chair"))
    einzelrecht = make_member(org, ["faction.view_public"], email="einzel@example.org")
    einzelrecht.individual_permissions.add(_stimmrecht())
    mitglied = make_member(org, [STIMMRECHT], email="mitglied@example.org")

    ids = stimmberechtigte_ids(org)
    assert ids == {admin_vorsitz.id, einzelrecht.id, mitglied.id}
    for person in (nur_admin, admin_gf, admin_vorsitz, einzelrecht, mitglied):
        assert (person.id in ids) == _darf_abstimmen(person)


def test_rechteanzeige_nennt_kein_stimmrecht_aus_der_admin_rolle(org: Any, make_member: Any) -> None:
    from apps.work.organization.selectors import permission_matrix

    admin = make_member(org, [], is_admin=True)
    matrix = permission_matrix(admin)
    assert STIMMRECHT not in matrix["role_permission_sources"]
    assert STIMMRECHT not in matrix["effective_permission_codes"]
    assert "organization.admin" in matrix["effective_permission_codes"]

    admin.individual_permissions.add(_stimmrecht())
    assert STIMMRECHT in permission_matrix(admin)["effective_permission_codes"]


def test_rollenverwaltung_zeigt_admin_rolle_ohne_stimmrecht(org: Any, make_member: Any, client_for: Any) -> None:
    from django.urls import reverse

    admin = make_member(org, [], is_admin=True, email="verwaltung@example.org")
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    antwort = client_for(admin.user).get(reverse("work:roles", kwargs={"org_slug": org.slug}))
    assert antwort.status_code == 200
    assert "Alle Berechtigungen außer Stimmrecht" in antwort.content.decode()
