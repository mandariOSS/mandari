# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene Anwendungen einer Kommune und Scopes für Session (Issue #1591).

- Einstellungen › Anwendungen: nur bei eingeschaltetem Anbieter, nur mit „Einstellungen verwalten“; Anlegen zeigt
  das Client-Secret genau in dieser Antwort, Sperren und Löschen wirken nur auf Anwendungen der eigenen Kommune.
- Session-API v1: Anträge nur mit ``session:lesen`` (und dem Recht der Person), ``ratsdaten:lesen`` reicht dafür
  nicht; Nichtöffentliches gibt es auch mit ``session:lesen`` nicht. Eine Anwendung einer anderen Kommune bekommt 403,
  über die Grenze je Minute hinaus 429, Listen höchstens so viele Einträge wie die Anwendung je Seite darf.
- Profil Einreichung und Befehle (#1588): Anwendung einer anderen Kommune bekommt 403.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.oidc import bindings, catalog, registration
from apps.accounts.oidc.models import OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import enable_2fa
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.tests.test_api_v1 import BASE, bearer, names

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def anbieter() -> Iterator[None]:
    cache.clear()
    with h.eingeschaltet():
        yield
    cache.clear()


@pytest.fixture
def tenant() -> SessionTenant:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
    )
    org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    now = timezone.now()
    SessionMeeting.objects.create(tenant=tenant, name="OEFFENTLICH", organization=org, start=now, is_public=True)
    SessionMeeting.objects.create(tenant=tenant, name="ZWEITE", organization=org, start=now, is_public=True)
    SessionMeeting.objects.create(tenant=tenant, name="GEHEIM", organization=org, start=now, is_public=False)
    SessionApplication.objects.create(
        tenant=tenant,
        title="Radweg",
        justification="Weil.",
        resolution_proposal="Bauen.",
        submitter_name="Petra Person",
        submitter_email="petra@example.org",
    )
    return tenant


@pytest.fixture
def andere() -> SessionTenant:
    return SessionTenant.objects.create(name="Andere Stadt", slug="andere", oparl_public_since=timezone.now())


def _person(tenant: SessionTenant, *, email: str = "", **rechte: bool) -> User:
    user = cast(User, UserFactory(**({"email": email} if email else {})))  # type: ignore[no-untyped-call]
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle-{SessionRole.objects.count() + 1}", **rechte)
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    session_user.roles.add(role)
    return user


ALLE_RECHTE: dict[str, Any] = {
    "can_access_api": True,
    "can_view_non_public_meetings": True,
    "can_view_non_public_papers": True,
    "can_view_applications": True,
}


def _kommunen_app(tenant: SessionTenant, user: User, **angaben: Any) -> OAuthApplication:
    werte: dict[str, Any] = {
        "name": "Rats-App",
        "confidential": True,
        "redirect_uris": [h.REDIRECT_URI],
        "scopes": [catalog.OPENID, catalog.RATSDATEN_LESEN, catalog.SESSION_LESEN, catalog.EINREICHUNG_SCHREIBEN],
        **angaben,
    }
    owner = bindings.compose(bindings.TENANT, str(tenant.pk))
    return registration.register(None, owner, user=user, **werte).application


def _get(pfad: str, raw: str) -> Any:
    return Client().get(f"{BASE}/{pfad}", headers=bearer(raw))


class TestEinstellungen:
    def _verwaltung(self, tenant: SessionTenant) -> Client:
        client = Client()
        client.force_login(_person(tenant, email="verwaltung@example.org", can_manage_settings=True))
        return client

    def _url(self, tenant: SessionTenant) -> str:
        return reverse("session:settings_applications", kwargs={"tenant_slug": tenant.slug})

    def test_nur_mit_eingeschaltetem_anbieter(self, tenant: SessionTenant) -> None:
        client = self._verwaltung(tenant)
        assert client.get(self._url(tenant)).status_code == 200
        with override_settings(OIDC_PROVIDER_ENABLED=False):
            assert client.get(self._url(tenant)).status_code == 404

    def test_nur_mit_recht_einstellungen(self, tenant: SessionTenant) -> None:
        client = Client()
        client.force_login(_person(tenant, email="rat@example.org", can_view_meetings=True))
        assert client.get(self._url(tenant)).status_code == 403

    def test_anlegen_zeigt_secret_einmal(self, tenant: SessionTenant) -> None:
        client = self._verwaltung(tenant)
        seite = client.get(self._url(tenant)).content.decode()
        assert catalog.SESSION_LESEN in seite and catalog.WORK_LESEN not in seite
        assert catalog.RATSDATEN_NICHTOEFFENTLICH not in seite
        antwort = client.post(
            self._url(tenant),
            {
                "aktion": "anlegen",
                "name": "Rats-App",
                "art": "vertraulich",
                "redirect_uris": h.REDIRECT_URI,
                "scopes": [catalog.OPENID, catalog.SESSION_LESEN],
            },
        )
        assert antwort.status_code == 200
        app = OAuthApplication.objects.get(name="Rats-App")
        assert app.owner == bindings.compose(bindings.TENANT, str(tenant.pk))
        assert app.scope_list == [catalog.OPENID, catalog.SESSION_LESEN]
        html = antwort.content.decode()
        assert 'data-testid="neue-zugangsdaten"' in html and app.client_id in html
        assert 'id="neues-secret"' in html
        # Beim nächsten Aufruf steht das Secret nicht mehr da
        assert 'id="neues-secret"' not in client.get(self._url(tenant)).content.decode()

    def test_fehler_im_formular(self, tenant: SessionTenant) -> None:
        client = self._verwaltung(tenant)
        antwort = client.post(
            self._url(tenant),
            {
                "aktion": "anlegen",
                "name": "Rats-App",
                "art": "vertraulich",
                "redirect_uris": "http://app.example.org/",
                "scopes": [catalog.OPENID],
            },
        )
        assert antwort.status_code == 200 and "https" in antwort.content.decode()
        assert not OAuthApplication.objects.exists()

    def test_sperren_und_loeschen_nur_eigene(self, tenant: SessionTenant, andere: SessionTenant) -> None:
        client = self._verwaltung(tenant)
        eigene = _kommunen_app(tenant, _person(tenant, email="a@example.org"))
        fremde = _kommunen_app(andere, _person(andere, email="b@example.org"), name="Fremd")
        assert client.post(self._url(tenant), {"aktion": "sperren", "anwendung": fremde.pk}).status_code == 302
        assert client.post(self._url(tenant), {"aktion": "loeschen", "anwendung": fremde.pk}).status_code == 302
        assert OAuthApplication.objects.get(pk=fremde.pk).is_active
        assert "Fremd" not in client.get(self._url(tenant)).content.decode()
        client.post(self._url(tenant), {"aktion": "sperren", "anwendung": eigene.pk})
        assert not OAuthApplication.objects.get(pk=eigene.pk).is_active
        client.post(self._url(tenant), {"aktion": "loeschen", "anwendung": eigene.pk})
        assert not OAuthApplication.objects.filter(pk=eigene.pk).exists()


class TestSessionApi:
    def test_antraege_nur_mit_session_lesen(self, tenant: SessionTenant) -> None:
        person = _person(tenant, **ALLE_RECHTE)
        app = _kommunen_app(tenant, person)
        mit = _get(f"{tenant.slug}/applications/", h.access_token(app, person, scope=catalog.SESSION_LESEN))
        assert mit.status_code == 200 and [row["title"] for row in mit.json()["data"]] == ["Radweg"]
        ohne = _get(f"{tenant.slug}/applications/", h.access_token(app, person, scope=catalog.RATSDATEN_LESEN))
        assert ohne.status_code == 403
        assert 'error="insufficient_scope"' in ohne["WWW-Authenticate"]
        assert catalog.SESSION_LESEN in ohne["WWW-Authenticate"]

    def test_antraege_nur_mit_recht_der_person(self, tenant: SessionTenant) -> None:
        person = _person(tenant, can_access_api=True, can_view_applications=False)
        app = _kommunen_app(tenant, person)
        assert (
            _get(f"{tenant.slug}/applications/", h.access_token(app, person, scope=catalog.SESSION_LESEN)).status_code
            == 403
        )

    def test_nie_nichtoeffentliches(self, tenant: SessionTenant) -> None:
        person = _person(tenant, **ALLE_RECHTE)
        app = _kommunen_app(tenant, person)
        sitzungen = _get(f"{tenant.slug}/meetings/", h.access_token(app, person, scope=catalog.SESSION_LESEN))
        assert sitzungen.status_code == 200 and "GEHEIM" not in names(sitzungen.json())

    def test_anwendung_einer_anderen_kommune(self, tenant: SessionTenant, andere: SessionTenant) -> None:
        person = _person(tenant, **ALLE_RECHTE)
        app = _kommunen_app(andere, _person(andere, email="b@example.org"))
        antwort = _get(f"{tenant.slug}/meetings/", h.access_token(app, person, scope=catalog.RATSDATEN_LESEN))
        assert antwort.status_code == 403
        assert "andere Kommune" in antwort.json()["detail"]

    def test_anwendung_des_betreibers_gilt_ueberall(self, tenant: SessionTenant) -> None:
        person = _person(tenant, **ALLE_RECHTE)
        app = h.anwendung(scopes=[catalog.OPENID, catalog.SESSION_LESEN])[0]
        raw = h.access_token(app, person, scope=catalog.SESSION_LESEN)
        assert _get(f"{tenant.slug}/applications/", raw).status_code == 200

    def test_grenze_je_minute(self, tenant: SessionTenant, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
        person = _person(tenant, **ALLE_RECHTE)
        app = _kommunen_app(tenant, person)
        OAuthApplication.objects.filter(pk=app.pk).update(rate_limit_per_minute=1)
        raw = h.access_token(app, person, scope=catalog.RATSDATEN_LESEN)
        assert _get(f"{tenant.slug}/meetings/", raw).status_code == 200
        zuviel = _get(f"{tenant.slug}/meetings/", raw)
        assert zuviel.status_code == 429 and zuviel["Retry-After"] == "60"

    def test_eintraege_je_seite(self, tenant: SessionTenant) -> None:
        person = _person(tenant, **ALLE_RECHTE)
        app = _kommunen_app(tenant, person)
        OAuthApplication.objects.filter(pk=app.pk).update(max_page_size=1)
        antwort = _get(f"{tenant.slug}/meetings/?limit=50", h.access_token(app, person, scope=catalog.RATSDATEN_LESEN))
        assert antwort.status_code == 200
        assert len(antwort.json()["data"]) == 1 and antwort.json()["meta"]["limit"] == 1


class TestEinreichung:
    def test_anwendung_einer_anderen_kommune(self, tenant: SessionTenant, andere: SessionTenant) -> None:
        person = _person(tenant, **ALLE_RECHTE)
        app = _kommunen_app(andere, _person(andere, email="b@example.org"))
        raw = h.access_token(app, person, scope=catalog.EINREICHUNG_SCHREIBEN)
        antwort = Client().get(f"/session/{tenant.slug}/api/oparl/body/kommparl/submission-rules/", headers=bearer(raw))
        assert antwort.status_code == 403
        assert "andere Kommune" in antwort.json()["detail"]


class TestBefehle:
    def test_nur_bei_der_eigenen_kommune(self, tenant: SessionTenant, andere: SessionTenant) -> None:
        person = _person(tenant, email="v@example.org", can_manage_settings=True, can_access_api=True)
        enable_2fa(person)
        scopes = [catalog.OPENID, catalog.SITZUNG_SCHREIBEN]
        fremde = _kommunen_app(andere, _person(andere, email="b@example.org"), name="Fremd", scopes=scopes)
        eigene = _kommunen_app(tenant, _person(tenant, email="c@example.org"), scopes=scopes)
        url = f"/api/mandari/v1/session/{tenant.slug}/befehle/session.textblock.create/v1"
        for app, erwartet in ((fremde, 403), (eigene, 201)):
            raw = h.access_token(app, person, scope=f"openid {catalog.SITZUNG_SCHREIBEN}")
            antwort = Client().post(
                url,
                data=json.dumps({"title": "Einstimmig", "content": "Text"}),
                content_type="application/json",
                headers={"Authorization": f"Bearer {raw}", "Idempotency-Key": str(uuid.uuid4())},
            )
            assert antwort.status_code == erwartet, antwort.content
