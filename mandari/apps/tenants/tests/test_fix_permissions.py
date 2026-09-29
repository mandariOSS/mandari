# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``fix_permissions`` ergänzt nur Fehlendes.

- Probelauf (ohne ``--fix``) ändert nichts und zeigt, was ergänzt würde.
- ``--fix`` legt fehlende Berechtigungen und fehlende Standardrollen an; angepasste Rollen
  (auch Standardrollen, auch ohne Rechte) bleiben, wie sie sind.
- Mitgliedschaften erhalten nie automatisch eine Rolle – schon gar nicht eine mit Vollzugriff;
  ``--assign-role`` wirkt nur mit ``--org``, nie für Gäste und nie mit einer Rolle mit Vollzugriff.
"""

from __future__ import annotations

from io import StringIO
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.common.permissions import DEFAULT_ROLES
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import Membership, Organization, Permission, Role

pytestmark = pytest.mark.django_db

ANGEPASST = "Fraktionsmitglied"
GELEERT = "Sachkundige/r Bürger/in"
GELOESCHT = "Bezirksvertreter/in"
FEHLENDES_RECHT = "dashboard.view"


def _run(*args: str) -> str:
    out = StringIO()
    call_command("fix_permissions", *args, stdout=out)
    return out.getvalue()


def _codes(role: Role) -> set[str]:
    return set(role.permissions.values_list("codename", flat=True))


def _mitglied(org: Organization, email: str, **felder: Any) -> Membership:
    user = cast(Any, UserFactory)(email=email)
    return cast(Membership, cast(Any, MembershipFactory)(user=user, organization=org, **felder))


@pytest.fixture
def org() -> Organization:
    cast(Any, Permission).sync_permissions()
    organisation = cast(Organization, cast(Any, OrganizationFactory)(name="Fraktion Rollen", slug="fraktion-rollen"))
    # Standardrollen legt das Signal beim Anlegen der Organisation an
    assert Role.objects.filter(organization=organisation).count() == len(DEFAULT_ROLES)
    return organisation


@pytest.fixture
def angepasste_org(org: Organization) -> Organization:
    """Organisation mit angepassten Standardrollen, einer gelöschten Standardrolle und Lücke im Katalog."""
    mitglied = Role.objects.get(organization=org, name=ANGEPASST)
    mitglied.description = "Eigene Beschreibung"
    mitglied.priority = 42
    mitglied.color = "#123456"
    mitglied.save()
    mitglied.permissions.remove(*mitglied.permissions.filter(codename__startswith="motions."))

    Role.objects.get(organization=org, name=GELEERT).permissions.clear()
    Role.objects.filter(organization=org, name=GELOESCHT).delete()
    Permission.objects.filter(codename=FEHLENDES_RECHT).delete()
    return org


def _stand(org: Organization) -> dict[str, Any]:
    rollen = {
        role.name: (role.description, role.priority, role.color, role.is_admin, _codes(role))
        for role in Role.objects.filter(organization=org)
    }
    mitglieder = {
        m.user.email: set(m.roles.values_list("name", flat=True)) for m in Membership.objects.filter(organization=org)
    }
    return {"rollen": rollen, "mitglieder": mitglieder, "rechte": Permission.objects.count()}


def test_probelauf_aendert_nichts_und_nennt_die_ergaenzungen(angepasste_org: Organization) -> None:
    _mitglied(angepasste_org, "ohne-rolle@example.org")
    vorher = _stand(angepasste_org)

    ausgabe = _run()

    assert _stand(angepasste_org) == vorher
    assert "PROBELAUF" in ausgabe
    assert "würde ergänzen: 1 fehlende Berechtigung(en) anlegen" in ausgabe
    assert FEHLENDES_RECHT in ausgabe
    assert "würde ergänzen: 1 fehlende Standardrolle(n) anlegen" in ausgabe
    assert GELOESCHT in ausgabe
    assert "ohne-rolle@example.org" in ausgabe
    assert "Mit --fix ausführen" in ausgabe


def test_fix_ergaenzt_fehlendes_und_laesst_anpassungen_stehen(angepasste_org: Organization) -> None:
    angepasst_vorher = Role.objects.get(organization=angepasste_org, name=ANGEPASST)
    rechte_vorher = _codes(angepasst_vorher)

    ausgabe = _run("--fix")

    # Fehlendes ist ergänzt
    assert Permission.objects.filter(codename=FEHLENDES_RECHT).exists()
    wieder_da = Role.objects.get(organization=angepasste_org, name=GELOESCHT)
    definition = next(c for c in DEFAULT_ROLES.values() if c["name"] == GELOESCHT)
    assert _codes(wieder_da) == set(cast(list[str], definition["permissions"]))

    # Angepasste Standardrollen bleiben unverändert
    angepasst = Role.objects.get(organization=angepasste_org, name=ANGEPASST)
    assert (angepasst.description, angepasst.priority, angepasst.color) == ("Eigene Beschreibung", 42, "#123456")
    assert _codes(angepasst) == rechte_vorher
    assert not any(code.startswith("motions.") for code in _codes(angepasst))
    assert _codes(Role.objects.get(organization=angepasste_org, name=GELEERT)) == set()
    assert "bleiben unverändert" in ausgabe

    # Zweiter Lauf: nichts mehr zu ergänzen, die Anpassungen stehen weiter
    zweiter = _run("--fix")
    assert "Nichts zu ergänzen." in zweiter
    assert _codes(Role.objects.get(organization=angepasste_org, name=ANGEPASST)) == rechte_vorher


def test_fix_vergibt_keine_rollen_an_mitgliedschaften(org: Organization) -> None:
    ohne_rolle = _mitglied(org, "ohne-rolle@example.org")
    gast = _mitglied(org, "gast@example.org", is_guest=True)

    ausgabe = _run("--fix")

    assert not ohne_rolle.roles.exists()
    assert not gast.roles.exists()
    assert "es wird keine Rolle automatisch vergeben" in ausgabe
    assert "gast@example.org" not in ausgabe


@pytest.mark.parametrize("gast", [False, True])
def test_user_fix_vergibt_keine_rolle_mit_vollzugriff(org: Organization, gast: bool) -> None:
    mitglied = _mitglied(org, "person@example.org", is_guest=gast)

    ausgabe = _run("--user", "person@example.org", "--fix")

    assert not mitglied.roles.exists()
    assert "Vollzugriff: nein" in ausgabe


def test_assign_role_nur_ausdruecklich_und_nie_fuer_gaeste(org: Organization) -> None:
    ohne_rolle = _mitglied(org, "ohne-rolle@example.org")
    gast = _mitglied(org, "gast@example.org", is_guest=True)

    probelauf = _run("--org", org.slug, "--assign-role", "Parteimitglied")
    assert not ohne_rolle.roles.exists()
    assert "würde ergänzen: Rolle 'Parteimitglied' an 1 Mitgliedschaft(en) ohne Rolle vergeben" in probelauf

    _run("--org", org.slug, "--assign-role", "Parteimitglied", "--fix")
    assert list(ohne_rolle.roles.values_list("name", flat=True)) == ["Parteimitglied"]
    assert not gast.roles.exists()


def test_assign_role_verweigert_vollzugriff_und_fehlende_organisation(org: Organization) -> None:
    ohne_rolle = _mitglied(org, "ohne-rolle@example.org")

    with pytest.raises(CommandError, match="nur zusammen mit --org"):
        _run("--assign-role", "Parteimitglied", "--fix")
    with pytest.raises(CommandError, match="Vollzugriff"):
        _run("--org", org.slug, "--assign-role", "Administrator", "--fix")
    with pytest.raises(CommandError, match="gibt es"):
        _run("--org", org.slug, "--assign-role", "Unbekannt", "--fix")
    with pytest.raises(CommandError, match="nicht gefunden"):
        _run("--org", "gibt-es-nicht", "--fix")

    assert not ohne_rolle.roles.exists()


def test_angepasste_rolle_mit_vollzugriff_wird_nicht_vergeben(org: Organization) -> None:
    """Auch eine Standardrolle, die eine Organisation zur Vollzugriffsrolle gemacht hat, wird nicht vergeben."""
    Role.objects.filter(organization=org, name="Parteimitglied").update(is_admin=True)
    ohne_rolle = _mitglied(org, "ohne-rolle@example.org")

    with pytest.raises(CommandError, match="Vollzugriff"):
        _run("--org", org.slug, "--assign-role", "Parteimitglied", "--fix")
    assert not ohne_rolle.roles.exists()
