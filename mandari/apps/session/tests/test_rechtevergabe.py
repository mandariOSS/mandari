# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechte vergeben nur im eigenen Umfang.

Wer Benutzer verwaltet, vergibt über Rollen, Rollenzuweisungen, Einladungen und Vertretungen
höchstens die Rechte, die er aus seinen eigenen Rollen selbst hat. Die Administrator-Rolle und die
Kontrollrechte (Protokoll einsehen und exportieren) vergibt nur, wer selbst Administrator ist.
Rechte aus einer Vertretung zählen dabei nicht.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionDelegation,
    SessionInvitation,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.permissions import SessionPermissionChecker

pytestmark = pytest.mark.django_db

HEUTE = timezone.localdate()


ALLE_RECHTE = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]


def _rolle(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionRole:
    # Alle Häkchen ausdrücklich setzen – einige Sichtrechte sind am Modell standardmäßig an
    flags = {feld: feld[4:] in rechte for feld in ALLE_RECHTE}
    return SessionRole.objects.create(tenant=tenant, name=name, is_admin=admin, **flags)


def _nutzer(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionUser:
    role = _rolle(tenant, f"Rolle {name}", *rechte, admin=admin)
    user = cast(Any, UserFactory)(email=f"{name}@example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    return session_user


def _client(session_user: SessionUser) -> Client:
    client = Client()
    client.force_login(session_user.user)
    return client


def _frisch(session_user: SessionUser) -> SessionUser:
    return SessionUser.objects.get(pk=session_user.pk)


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Rechte", slug="rechte")


@pytest.fixture
def verwaltung(tenant: SessionTenant) -> SessionUser:
    """Benutzerverwaltung mit Sachbearbeitungsrechten, aber ohne Admin-, Freigabe- oder Kontrollrecht."""
    return _nutzer(tenant, "verwaltung", "manage_users", "view_papers", "edit_papers")


@pytest.fixture
def admin(tenant: SessionTenant) -> SessionUser:
    return _nutzer(tenant, "admin", admin=True)


def _rolle_speichern(client: Client, tenant: SessionTenant, **daten: str) -> Any:
    return client.post(f"/session/{tenant.slug}/settings/roles/save/", daten)


# =============================================================================
# Rollen anlegen und bearbeiten
# =============================================================================


def test_keine_admin_rolle_ohne_admin_recht(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    _rolle_speichern(_client(verwaltung), tenant, name="Hintertür", is_admin="1")
    assert not SessionRole.objects.filter(tenant=tenant, is_admin=True, name="Hintertür").exists()


def test_keine_kontrollrechte_ohne_admin_recht(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    _rolle_speichern(_client(verwaltung), tenant, name="Revision", can_view_audit_log="1", can_export_audit_log="1")
    assert not SessionRole.objects.filter(tenant=tenant, name="Revision", can_view_audit_log=True).exists()
    assert not SessionRole.objects.filter(tenant=tenant, name="Revision", can_export_audit_log=True).exists()


def test_keine_fremden_rechte_in_neuer_rolle(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    _rolle_speichern(_client(verwaltung), tenant, name="Freigabe", can_approve_papers="1", can_view_papers="1")
    assert not SessionRole.objects.filter(tenant=tenant, name="Freigabe", can_approve_papers=True).exists()


def test_eigene_rechte_bleiben_vergebbar(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    _rolle_speichern(_client(verwaltung), tenant, name="Sachbearbeitung", can_view_papers="1", can_edit_papers="1")
    rolle = SessionRole.objects.get(tenant=tenant, name="Sachbearbeitung")
    assert rolle.can_view_papers and rolle.can_edit_papers and not rolle.is_admin


def test_bearbeiten_laesst_fremde_rechte_unveraendert_zu(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    rolle = _rolle(tenant, "Leitung", "approve_papers", "view_papers")
    _rolle_speichern(
        _client(verwaltung),
        tenant,
        role_id=str(rolle.id),
        name="Leitung",
        description="neu beschrieben",
        can_approve_papers="1",
        can_view_papers="1",
        can_edit_papers="1",
    )
    rolle.refresh_from_db()
    assert rolle.description == "neu beschrieben"
    assert rolle.can_approve_papers and rolle.can_edit_papers


def test_fremde_rechte_nicht_entziehbar(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    rolle = _rolle(tenant, "Revision", "view_audit_log")
    _rolle_speichern(_client(verwaltung), tenant, role_id=str(rolle.id), name="Revision")
    rolle.refresh_from_db()
    assert rolle.can_view_audit_log is True


def test_admin_vergibt_admin_und_kontrollrechte(tenant: SessionTenant, admin: SessionUser) -> None:
    _rolle_speichern(_client(admin), tenant, name="Revision", can_view_audit_log="1", can_export_audit_log="1")
    _rolle_speichern(_client(admin), tenant, name="Zweitadmin", is_admin="1")
    assert SessionRole.objects.get(tenant=tenant, name="Revision").can_export_audit_log
    assert SessionRole.objects.get(tenant=tenant, name="Zweitadmin").is_admin


# =============================================================================
# Rollen zuweisen und einladen
# =============================================================================


def test_keine_selbstzuweisung_der_admin_rolle(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    admin_rolle = SessionRole.objects.create(tenant=tenant, name="Administrator", is_admin=True)
    eigene = list(verwaltung.roles.all())
    _client(verwaltung).post(
        f"/session/{tenant.slug}/settings/users/{verwaltung.id}/roles/",
        {"roles": [str(r.id) for r in eigene] + [str(admin_rolle.id)]},
    )
    assert not _frisch(verwaltung).is_admin()
    assert _client(verwaltung).get(f"/session/{tenant.slug}/settings/").status_code == 403


def test_keine_zuweisung_von_rollen_mit_fremden_rechten(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    revision = _rolle(tenant, "Revision", "view_audit_log")
    ziel = _nutzer(tenant, "ziel")
    _client(verwaltung).post(f"/session/{tenant.slug}/settings/users/{ziel.id}/roles/", {"roles": [str(revision.id)]})
    assert revision not in _frisch(ziel).roles.all()
    assert _client(verwaltung).get(f"/session/{tenant.slug}/audit/").status_code == 403


def test_zuweisung_im_eigenen_umfang_bleibt_moeglich(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    sachbearbeitung = _rolle(tenant, "Sachbearbeitung", "edit_papers")
    ziel = _nutzer(tenant, "ziel")
    _client(verwaltung).post(
        f"/session/{tenant.slug}/settings/users/{ziel.id}/roles/", {"roles": [str(sachbearbeitung.id)]}
    )
    assert list(_frisch(ziel).roles.all()) == [sachbearbeitung]


def test_admin_rolle_eines_admins_nicht_entziehbar(
    tenant: SessionTenant, verwaltung: SessionUser, admin: SessionUser
) -> None:
    _nutzer(tenant, "zweitadmin", admin=True)
    _client(verwaltung).post(f"/session/{tenant.slug}/settings/users/{admin.id}/roles/", {"roles": []})
    assert _frisch(admin).is_admin()


def test_deaktiviertes_admin_konto_nicht_reaktivierbar(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    altadmin = _nutzer(tenant, "altadmin", admin=True)
    altadmin.is_active = False
    altadmin.save()
    _client(verwaltung).post(f"/session/{tenant.slug}/settings/users/{altadmin.id}/deactivate/")
    assert _frisch(altadmin).is_active is False


def test_einladung_nicht_mit_admin_rolle(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    admin_rolle = SessionRole.objects.create(tenant=tenant, name="Administrator", is_admin=True)
    _client(verwaltung).post(
        f"/session/{tenant.slug}/settings/users/invite/",
        {"email": "zweitkonto@example.org", "roles": [str(admin_rolle.id)]},
    )
    einladung = SessionInvitation.objects.filter(tenant=tenant, email="zweitkonto@example.org").first()
    assert einladung is None or admin_rolle not in einladung.roles.all()


def test_bestehendes_konto_nicht_mit_admin_rolle_aufnehmen(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    admin_rolle = SessionRole.objects.create(tenant=tenant, name="Administrator", is_admin=True)
    cast(Any, UserFactory)(email="bestand@example.org")
    _client(verwaltung).post(
        f"/session/{tenant.slug}/settings/users/invite/",
        {"email": "bestand@example.org", "roles": [str(admin_rolle.id)]},
    )
    aufgenommen = SessionUser.objects.filter(tenant=tenant, user__email="bestand@example.org").first()
    assert aufgenommen is None or not aufgenommen.is_admin()


# =============================================================================
# Vertretungen
# =============================================================================


def _vertretung_eintragen(client: Client, tenant: SessionTenant, principal: SessionUser, deputy: SessionUser) -> Any:
    return client.post(
        f"/session/{tenant.slug}/settings/delegations/create/",
        {
            "principal": str(principal.id),
            "deputy": str(deputy.id),
            "start_date": HEUTE.isoformat(),
            "end_date": (HEUTE + timedelta(days=7)).isoformat(),
            "scopes": ["approvals", "worklist", "notifications"],
        },
    )


def test_keine_selbsteingetragene_vertretung_mit_freigaberechten(
    tenant: SessionTenant, verwaltung: SessionUser, admin: SessionUser
) -> None:
    _vertretung_eintragen(_client(verwaltung), tenant, admin, verwaltung)
    assert not SessionDelegation.objects.filter(deputy=verwaltung).exists()
    assert "approve_papers" not in SessionPermissionChecker(_frisch(verwaltung)).permissions


def test_keine_selbsteingetragene_vertretung_auch_im_eigenen_umfang(tenant: SessionTenant) -> None:
    # Arbeitsvorrat und Benachrichtigungen einer anderen Person übernimmt man nicht auf eigene Anordnung
    verwaltung = _nutzer(tenant, "leitstelle", "manage_users", "approve_papers")
    leitung = _nutzer(tenant, "leitung", "approve_papers")
    _vertretung_eintragen(_client(verwaltung), tenant, leitung, verwaltung)
    assert not SessionDelegation.objects.filter(deputy=verwaltung).exists()


def test_keine_vertretung_mit_rechten_ueber_den_eigenen_umfang(tenant: SessionTenant, verwaltung: SessionUser) -> None:
    leitung = _nutzer(tenant, "leitung", "approve_papers")
    kollegin = _nutzer(tenant, "kollegin")
    _vertretung_eintragen(_client(verwaltung), tenant, leitung, kollegin)
    assert not SessionDelegation.objects.filter(deputy=kollegin).exists()


def test_vertretung_im_eigenen_umfang_bleibt_moeglich(tenant: SessionTenant) -> None:
    verwaltung = _nutzer(tenant, "leitstelle", "manage_users", "approve_papers")
    leitung = _nutzer(tenant, "leitung", "approve_papers")
    kollegin = _nutzer(tenant, "kollegin")
    _vertretung_eintragen(_client(verwaltung), tenant, leitung, kollegin)
    assert SessionDelegation.objects.filter(deputy=kollegin, scope_approvals=True).exists()


def test_admin_traegt_vertretungen_ein(tenant: SessionTenant, admin: SessionUser) -> None:
    leitung = _nutzer(tenant, "leitung", "approve_papers")
    kollegin = _nutzer(tenant, "kollegin")
    _vertretung_eintragen(_client(admin), tenant, leitung, kollegin)
    assert "approve_papers" in SessionPermissionChecker(_frisch(kollegin)).permissions
