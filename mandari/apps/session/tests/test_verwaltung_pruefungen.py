# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollenpflege, Einladungen und Schnittstellenzugang: einheitliche Rechteprüfung und Protokoll.

- Rollen löschen wie Rollen bearbeiten: nur im eigenen Rechteumfang; Standardrollen bleiben.
- Einladungen erneut senden nur im eigenen Rechteumfang und nur, solange sie gültig sind.
- Session-API: „API-Zugang“ für die eigene Anmeldung; vor der Freischaltung liest einen Bereich nur,
  wer dessen Sichtrecht bzw. Lese-Flag hat.
- Einladungen und Einreichungs-Zugänge stehen im Audit-Log, Rollen aus einer Einladung mit der
  einladenden Person als Vergebende.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAPIToken,
    SessionAuditLog,
    SessionInvitation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

FLAGS = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]


def _rolle(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionRole:
    flags = {feld: feld[4:] in rechte for feld in FLAGS}
    return SessionRole.objects.create(tenant=tenant, name=name, is_admin=admin, **flags)


def _nutzer(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionUser:
    user = cast(User, cast(Any, UserFactory)(email=f"{name}@example.org"))
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(_rolle(tenant, f"Rolle {name}", *rechte, admin=admin))
    return session_user


def _client(session_user: SessionUser) -> Client:
    client = Client()
    client.force_login(session_user.user)
    return client


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def benutzerverwaltung(tenant: SessionTenant) -> SessionUser:
    """Benutzerverwaltung ohne Administrator- und Kontrollrechte."""
    return _nutzer(tenant, "benutzer", "view_dashboard", "view_meetings", "manage_users")


@pytest.fixture
def admin(tenant: SessionTenant) -> SessionUser:
    return _nutzer(tenant, "verwaltung", admin=True)


# ---------------------------------------------------------------------------
# Rollen löschen
# ---------------------------------------------------------------------------


class TestRollenLoeschen:
    def _loeschen(self, client: Client, tenant: SessionTenant, rolle: SessionRole) -> None:
        client.post(f"/session/{tenant.slug}/settings/roles/delete/", {"role_id": str(rolle.pk)})

    def test_nur_im_eigenen_umfang(self, tenant: SessionTenant, benutzerverwaltung: SessionUser) -> None:
        client = _client(benutzerverwaltung)
        admin_rolle = _rolle(tenant, "Zweiter Administrator", admin=True)
        kontrolle = _rolle(tenant, "Prüfung", "view_audit_log", "export_audit_log")
        eigene = _rolle(tenant, "Lesende", "view_meetings")
        for rolle in (admin_rolle, kontrolle, eigene):
            self._loeschen(client, tenant, rolle)
        assert SessionRole.objects.filter(pk=admin_rolle.pk).exists()
        assert SessionRole.objects.filter(pk=kontrolle.pk).exists()
        assert not SessionRole.objects.filter(pk=eigene.pk).exists()

    def test_standardrollen_bleiben(self, tenant: SessionTenant, admin: SessionUser) -> None:
        standard = SessionRole.create_default_roles(tenant)
        client = _client(admin)
        self._loeschen(client, tenant, standard["recorder"])
        assert SessionRole.objects.filter(pk=standard["recorder"].pk).exists()
        seite = client.get(f"/session/{tenant.slug}/settings/roles/").content.decode()
        assert f'name="role_id" value="{standard["recorder"].pk}"' not in seite
        assert "Standardrolle" in seite


# ---------------------------------------------------------------------------
# Einladungen
# ---------------------------------------------------------------------------


class TestEinladungen:
    def _erneut(self, client: Client, tenant: SessionTenant, einladung: SessionInvitation) -> None:
        client.post(f"/session/{tenant.slug}/settings/invitations/{einladung.pk}/resend/")

    def test_erneut_senden_nur_im_eigenen_umfang(self, tenant: SessionTenant, benutzerverwaltung: SessionUser) -> None:
        admin_rolle = _rolle(tenant, "Administrator", admin=True)
        einladung = SessionInvitation.create_for_tenant(tenant, "neu@example.org", roles=[admin_rolle], valid_days=1)
        vorher = einladung.expires_at
        self._erneut(_client(benutzerverwaltung), tenant, einladung)
        einladung.refresh_from_db()
        assert einladung.expires_at == vorher

    def test_abgelaufene_einladung_wird_nicht_verlaengert(self, tenant: SessionTenant, admin: SessionUser) -> None:
        einladung = SessionInvitation.create_for_tenant(tenant, "alt@example.org")
        einladung.expires_at = timezone.now() - timedelta(days=30)
        einladung.save()
        self._erneut(_client(admin), tenant, einladung)
        einladung.refresh_from_db()
        assert not einladung.is_valid

    def test_einladen_erneut_senden_zurueckziehen_im_protokoll(self, tenant: SessionTenant, admin: SessionUser) -> None:
        rolle = _rolle(tenant, "Sachbearbeitung", "view_meetings")
        client = _client(admin)
        client.post(f"/session/{tenant.slug}/settings/users/invite/", {"email": "neu@example.org", "roles": [rolle.pk]})
        einladung = SessionInvitation.objects.get(email="neu@example.org")
        self._erneut(client, tenant, einladung)
        client.post(f"/session/{tenant.slug}/settings/invitations/{einladung.pk}/cancel/")
        eintraege = SessionAuditLog.objects.filter(model_name="SessionInvitation").order_by("seq")
        assert [e.action for e in eintraege] == ["create", "update", "delete"]
        assert {e.user_id for e in eintraege} == {admin.pk}
        assert eintraege[0].changes["rollen"] == ["Sachbearbeitung"]

    def test_annahme_nennt_die_einladende_person(self, tenant: SessionTenant, admin: SessionUser) -> None:
        rolle = _rolle(tenant, "Sachbearbeitung", "view_meetings")
        einladung = SessionInvitation.create_for_tenant(
            tenant, "neu.person@example.org", invited_by=admin, roles=[rolle]
        )
        neu = cast(User, cast(Any, UserFactory)(email="neu.person@example.org"))
        client = Client()
        client.force_login(neu)
        client.post(f"/session/invite/{einladung.plain_token}/")
        rollen = SessionAuditLog.objects.get(action="roles_changed", object_id=SessionUser.objects.get(user=neu).pk)
        assert rollen.user_id == admin.pk
        assert rollen.changes["eingeladen_von"] == admin.user.email
        annahme = SessionAuditLog.objects.get(model_name="SessionInvitation", action="update")
        assert annahme.changes["angenommen"] is True
        assert annahme.user_id == SessionUser.objects.get(user=neu).pk


# ---------------------------------------------------------------------------
# Einreichungs-Zugänge im Protokoll
# ---------------------------------------------------------------------------


def test_zugang_anlegen_und_zurueckziehen_im_protokoll(tenant: SessionTenant, admin: SessionUser) -> None:
    client = _client(admin)
    client.post(
        f"/session/{tenant.slug}/settings/api-tokens/create/", {"name": "Fraktion A", "can_read_meetings": "on"}
    )
    token = SessionAPIToken.objects.get(tenant=tenant)
    client.post(f"/session/{tenant.slug}/settings/api-tokens/{token.pk}/revoke/")
    eintraege = list(SessionAuditLog.objects.filter(model_name="SessionAPIToken").order_by("seq"))
    assert [e.action for e in eintraege] == ["create", "update"]
    assert eintraege[0].changes["sitzungen_lesen"] is True
    assert eintraege[1].changes["zurueckgezogen"] is True
    assert all(token.token_prefix not in e.object_repr for e in eintraege)


# ---------------------------------------------------------------------------
# Session-API
# ---------------------------------------------------------------------------


@pytest.fixture
def gesperrt() -> SessionTenant:
    """Mandant ohne Freischaltung der OParl-Schnittstelle, mit einer öffentlichen Sitzung und Vorlage."""
    tenant = SessionTenant.objects.create(name="Stadt Gesperrt", slug="gesperrt")
    org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    SessionMeeting.objects.create(tenant=tenant, organization=org, name="OEFFENTLICH", start=timezone.now())
    SessionPaper.objects.create(tenant=tenant, reference="V/1", name="VORLAGE", is_public=True, status="approved")
    return tenant


V1 = "/api/v1/session/{slug}/{bereich}/"
ALT = "/session/{slug}/api/session/{bereich}/"


class TestSchnittstelle:
    @pytest.mark.parametrize("bereich", ["meetings", "papers"])
    def test_ohne_api_zugang_wie_anonym(self, gesperrt: SessionTenant, bereich: str) -> None:
        # Kontrollrolle ohne Sicht- und API-Recht
        client = _client(_nutzer(gesperrt, "datenschutz", "view_dashboard", "view_audit_log"))
        assert client.get(V1.format(slug=gesperrt.slug, bereich=bereich)).status_code == 404
        assert client.get(ALT.format(slug=gesperrt.slug, bereich=bereich)).status_code == 404
        # Sichtrecht ohne API-Zugang
        lesend = _client(_nutzer(gesperrt, "lesend", "view_meetings", "view_papers"))
        assert lesend.get(V1.format(slug=gesperrt.slug, bereich=bereich)).status_code == 404

    @pytest.mark.parametrize("bereich", ["meetings", "papers"])
    def test_api_zugang_braucht_das_sichtrecht(self, gesperrt: SessionTenant, bereich: str) -> None:
        ohne = _client(_nutzer(gesperrt, "ohne", "access_api"))
        assert ohne.get(V1.format(slug=gesperrt.slug, bereich=bereich)).status_code == 403
        assert ohne.get(ALT.format(slug=gesperrt.slug, bereich=bereich)).status_code == 404
        mit = _client(_nutzer(gesperrt, "mit", "access_api", "view_meetings", "view_papers"))
        assert mit.get(V1.format(slug=gesperrt.slug, bereich=bereich)).status_code == 200
        assert mit.get(ALT.format(slug=gesperrt.slug, bereich=bereich)).status_code == 200

    @pytest.mark.parametrize(("bereich", "flag"), [("meetings", "can_read_meetings"), ("papers", "can_read_papers")])
    def test_token_lese_flags(self, client: Client, gesperrt: SessionTenant, bereich: str, flag: str) -> None:
        _ohne, roh_ohne = SessionAPIToken.create_token(gesperrt, "Ohne", **{flag: False})
        _mit, roh_mit = SessionAPIToken.create_token(gesperrt, "Mit", **{flag: True})
        url = V1.format(slug=gesperrt.slug, bereich=bereich)
        assert client.get(url, headers={"Authorization": f"Bearer {roh_ohne}"}).status_code == 403
        assert client.get(url, headers={"Authorization": f"Bearer {roh_mit}"}).status_code == 200

    def test_noe_nur_mit_api_zugang(self) -> None:
        tenant = SessionTenant.objects.create(name="Stadt Offen", slug="offen", oparl_public_since=timezone.now())
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        SessionMeeting.objects.create(
            tenant=tenant, organization=org, name="GEHEIM", start=timezone.now(), is_public=False
        )
        rechte = ("view_meetings", "view_non_public_meetings")
        ohne = _client(_nutzer(tenant, "ohne", *rechte)).get(V1.format(slug=tenant.slug, bereich="meetings"))
        assert b"GEHEIM" not in ohne.content
        assert ohne.json()["meta"]["authenticated"] is False
        mit = _client(_nutzer(tenant, "mit", *rechte, "access_api")).get(
            V1.format(slug=tenant.slug, bereich="meetings")
        )
        assert b"GEHEIM" in mit.content


def test_rechte_matrix_ohne_wirkungslose_haekchen(tenant: SessionTenant, admin: SessionUser) -> None:
    seite = _client(admin).get(f"/session/{tenant.slug}/settings/roles/").content.decode()
    assert 'name="can_access_api"' in seite
    assert 'name="can_access_oparl_api"' not in seite
