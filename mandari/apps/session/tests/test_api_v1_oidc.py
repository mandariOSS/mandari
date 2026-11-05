# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session-API v1 mit Access-Tokens des OpenID-Connect-Anbieters (Issue #1206, ``apps/session/api/v1/auth.py``).

Regeln: Scope ``ratsdaten:lesen`` ist Pflicht. Im Namen einer Person liest ein Token höchstens, was die Person mit
eigener Anmeldung über die Schnittstelle lesen dürfte (Recht „API-Zugang“), aber nie Nichtöffentliches, keine
Anträge und reicht nichts ein (bis #565). Ein Dienst-Token (Client Credentials) liest nur, was anonym lesbar ist.
Bestehende API-Tokens (64 Zeichen, mandantengebunden) verhalten sich unverändert.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.oidc.models import OAuthAccessToken, OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.tests.test_api_v1 import BASE, bearer, make_token, names

pytestmark = pytest.mark.django_db

ALLE_RECHTE = {
    "can_access_api": True,
    "can_view_non_public_meetings": True,
    "can_view_non_public_papers": True,
    "can_view_applications": True,
}


@pytest.fixture(autouse=True)
def anbieter() -> Iterator[None]:
    with h.eingeschaltet():
        yield


@pytest.fixture
def tenant() -> SessionTenant:
    """Wie in test_api_v1: je eine öffentliche und eine geheime Sitzung bzw. Vorlage."""
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
    )
    org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    now = timezone.now()
    SessionMeeting.objects.create(tenant=tenant, name="OEFFENTLICH", organization=org, start=now, is_public=True)
    SessionMeeting.objects.create(tenant=tenant, name="GEHEIM", organization=org, start=now, is_public=False)
    SessionPaper.objects.create(
        tenant=tenant, reference="V/1", name="OEFFENTLICHE-VORLAGE", is_public=True, status="approved"
    )
    SessionPaper.objects.create(tenant=tenant, reference="V/2", name="GEHEIME-VORLAGE", is_public=False)
    return tenant


@pytest.fixture
def unveroeffentlicht(tenant: SessionTenant) -> SessionTenant:
    tenant.oparl_public_since = None
    tenant.save()
    return tenant


@pytest.fixture
def app() -> OAuthApplication:
    return h.anwendung(name="Ratsapp")[0]


@pytest.fixture
def dienst() -> OAuthApplication:
    return h.anwendung(name="Dienst", ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS, scopes=["ratsdaten:lesen"])[0]


def person(tenant: SessionTenant, **rechte: bool) -> User:
    user = cast(User, UserFactory())  # type: ignore[no-untyped-call]
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle-{SessionRole.objects.count() + 1}", **rechte)
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    session_user.roles.add(role)
    return user


def get(pfad: str, raw: str) -> Any:
    return Client().get(f"{BASE}/{pfad}", headers=bearer(raw))


class TestImNamenEinerPerson:
    def test_nie_nichtoeffentliches_auch_mit_allen_rechten(self, tenant: SessionTenant, app: OAuthApplication) -> None:
        geheim = SessionMeeting.objects.get(tenant=tenant, name="GEHEIM")
        cast(Any, geheim).set_internal_notes_encrypted("INTERNE-NOTIZ")
        geheim.save()
        raw = h.access_token(app, person(tenant, **ALLE_RECHTE))

        meetings = get(f"{tenant.slug}/meetings/", raw)
        assert meetings.status_code == 200
        assert names(meetings.json()) == {"OEFFENTLICH"}
        assert meetings.json()["meta"]["authenticated"] is True
        assert b"INTERNE-NOTIZ" not in meetings.content
        papers = get(f"{tenant.slug}/papers/", raw).json()
        assert names(papers) == {"OEFFENTLICHE-VORLAGE"}
        assert all(row.get("main_text") is None for row in papers["data"])

    def test_keine_antraege_und_keine_einreichung(self, tenant: SessionTenant, app: OAuthApplication) -> None:
        raw = h.access_token(app, person(tenant, **ALLE_RECHTE))
        assert get(f"{tenant.slug}/applications/", raw).status_code == 403
        antrag = {"title": "Radweg", "submitter_name": "Petra Person", "application_type": "motion"}
        response = Client().post(
            f"{BASE}/{tenant.slug}/applications/submit/",
            data=antrag,
            content_type="application/json",
            headers=bearer(raw),
        )
        assert response.status_code in (401, 422)
        assert not SessionApplication.objects.exists()

    def test_vor_freischaltung_wie_eigene_anmeldung_mit_api_zugang(
        self, unveroeffentlicht: SessionTenant, app: OAuthApplication
    ) -> None:
        raw = h.access_token(app, person(unveroeffentlicht, can_access_api=True, can_view_meetings=True))
        response = get(f"{unveroeffentlicht.slug}/meetings/", raw)
        assert response.status_code == 200
        assert names(response.json()) == {"OEFFENTLICH"}

    def test_ohne_api_zugang_wie_anonym(self, unveroeffentlicht: SessionTenant, app: OAuthApplication) -> None:
        raw = h.access_token(app, person(unveroeffentlicht, can_view_meetings=True, can_view_non_public_meetings=True))
        assert get(f"{unveroeffentlicht.slug}/meetings/", raw).status_code == 404

    def test_person_eines_anderen_mandanten_wie_anonym(self, tenant: SessionTenant, app: OAuthApplication) -> None:
        andere = SessionTenant.objects.create(name="Andere Stadt", slug="andere")
        raw = h.access_token(app, person(andere, **ALLE_RECHTE))
        meetings = get(f"{tenant.slug}/meetings/", raw).json()
        assert names(meetings) == {"OEFFENTLICH"} and meetings["meta"]["authenticated"] is False


class TestDienst:
    def test_liest_nur_was_anonym_lesbar_ist(self, tenant: SessionTenant, dienst: OAuthApplication) -> None:
        raw = h.access_token(dienst)
        meetings = get(f"{tenant.slug}/meetings/", raw).json()
        assert names(meetings) == {"OEFFENTLICH"}
        assert meetings["meta"]["authenticated"] is False
        assert get(f"{tenant.slug}/applications/", raw).status_code == 401

    def test_vor_freischaltung_nichts(self, unveroeffentlicht: SessionTenant, dienst: OAuthApplication) -> None:
        assert get(f"{unveroeffentlicht.slug}/meetings/", h.access_token(dienst)).status_code == 404


class TestTokenPruefung:
    def test_scope_ratsdaten_lesen_ist_pflicht(self, tenant: SessionTenant, app: OAuthApplication) -> None:
        raw = h.access_token(app, person(tenant, **ALLE_RECHTE), scope="openid profile")
        response = get(f"{tenant.slug}/meetings/", raw)
        assert response.status_code == 403
        assert response["Content-Type"].startswith("application/problem+json")
        assert 'error="insufficient_scope"' in response["WWW-Authenticate"]

    @pytest.mark.parametrize("fall", ["abgelaufen", "widerrufen", "unbekannt"])
    def test_ungueltige_tokens(self, tenant: SessionTenant, app: OAuthApplication, fall: str) -> None:
        raw = h.access_token(app, person(tenant, can_access_api=True), gueltig=-1 if fall == "abgelaufen" else 3600)
        if fall == "widerrufen":
            OAuthAccessToken.objects.all().delete()
        if fall == "unbekannt":
            raw = raw[:-4] + "abcd"
        response = get(f"{tenant.slug}/meetings/", raw)
        assert response.status_code == 401
        assert 'error="invalid_token"' in response["WWW-Authenticate"]

    def test_ausgeschalteter_anbieter_alter_weg_unveraendert(
        self, tenant: SessionTenant, app: OAuthApplication
    ) -> None:
        raw = h.access_token(app, person(tenant, **ALLE_RECHTE))
        with override_settings(OIDC_PROVIDER_ENABLED=False):
            response = get(f"{tenant.slug}/meetings/", raw)
        assert response.status_code == 401
        assert "ungültiges Format" in response.json()["detail"]


class TestBestehendeApiTokens:
    def test_unveraendert_mit_mandantenbindung(self, tenant: SessionTenant) -> None:
        raw = make_token(tenant, can_read_meetings=True)
        assert len(raw) == 64
        meetings = get(f"{tenant.slug}/meetings/", raw)
        assert meetings.status_code == 200 and names(meetings.json()) == {"OEFFENTLICH"}
        fremd = SessionTenant.objects.create(name="Fremd", slug="fremd", oparl_public_since=timezone.now())
        assert get(f"{fremd.slug}/meetings/", raw).status_code == 401
        assert get(f"{tenant.slug}/meetings/", "x" * 63).status_code == 401
