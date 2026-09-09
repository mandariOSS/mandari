# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session-API v1 (django-ninja, Issue #163): Sichtbarkeit, Auth-Wege, RFC-9457-Fehler, OpenAPI,
Deprecation der alten Pfade.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAPIToken,
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

BASE = "/api/v1/session"


@pytest.fixture
def tenant() -> SessionTenant:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    now = timezone.now()
    SessionMeeting.objects.create(tenant=tenant, name="OEFFENTLICH", organization=org, start=now, is_public=True)
    SessionMeeting.objects.create(tenant=tenant, name="GEHEIM", organization=org, start=now, is_public=False)
    SessionPaper.objects.create(tenant=tenant, reference="V/1", name="OEFFENTLICHE-VORLAGE", is_public=True)
    SessionPaper.objects.create(tenant=tenant, reference="V/2", name="GEHEIME-VORLAGE", is_public=False)
    return tenant


@pytest.fixture
def other_tenant() -> SessionTenant:
    tenant = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
    org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    SessionMeeting.objects.create(tenant=tenant, name="FREMD", organization=org, start=timezone.now(), is_public=True)
    return tenant


def make_session_client(tenant: SessionTenant, **role_flags: bool) -> Client:
    user = cast(User, UserFactory())  # type: ignore[no-untyped-call]
    role = SessionRole.objects.create(tenant=tenant, name=f"Testrolle-{SessionRole.objects.count() + 1}", **role_flags)
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def make_token(tenant: SessionTenant, **flags: Any) -> str:
    _token, raw = SessionAPIToken.create_token(tenant, "Integration", **flags)
    return str(raw)


def bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def names(payload: dict[str, Any]) -> set[str]:
    return {row["name"] for row in payload["data"]}


# ---------------------------------------------------------------------------


class TestRoot:
    def test_root_links(self, client: Client, tenant: SessionTenant) -> None:
        response = client.get(f"{BASE}/{tenant.slug}/")
        assert response.status_code == 200
        body = response.json()
        assert body["version"] == "1"
        assert body["meetings"].endswith(f"{BASE}/{tenant.slug}/meetings/")
        assert body["openapi"].endswith(f"{BASE}/openapi.json")
        assert body["oparl"].endswith(f"/session/{tenant.slug}/api/oparl/")

    def test_unknown_tenant_is_problem_json(self, client: Client) -> None:
        response = client.get(f"{BASE}/gibt-es-nicht/")
        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/problem+json")
        body = response.json()
        assert body["status"] == 404
        assert body["title"] == "Nicht gefunden"
        assert body["instance"] == f"{BASE}/gibt-es-nicht/"
        assert "request_id" in body


class TestVisibility:
    def test_anonymous_sees_only_public(
        self, client: Client, tenant: SessionTenant, other_tenant: SessionTenant
    ) -> None:
        meetings = client.get(f"{BASE}/{tenant.slug}/meetings/").json()
        assert names(meetings) == {"OEFFENTLICH"}
        assert meetings["meta"] == {"total": 1, "limit": 100, "offset": 0, "authenticated": False}
        papers = client.get(f"{BASE}/{tenant.slug}/papers/").json()
        assert names(papers) == {"OEFFENTLICHE-VORLAGE"}
        assert "main_text" not in papers["data"][0] or papers["data"][0]["main_text"] is None

    def test_session_user_with_permission_sees_non_public(self, tenant: SessionTenant) -> None:
        client = make_session_client(tenant, can_view_non_public_meetings=True, can_view_non_public_papers=True)
        meetings = client.get(f"{BASE}/{tenant.slug}/meetings/").json()
        assert names(meetings) == {"OEFFENTLICH", "GEHEIM"}
        assert meetings["meta"]["authenticated"] is True
        papers = client.get(f"{BASE}/{tenant.slug}/papers/").json()
        assert names(papers) == {"OEFFENTLICHE-VORLAGE", "GEHEIME-VORLAGE"}
        assert papers["data"][0]["main_text"] is not None

    def test_session_user_without_permission_sees_only_public(self, tenant: SessionTenant) -> None:
        client = make_session_client(tenant)
        assert names(client.get(f"{BASE}/{tenant.slug}/meetings/").json()) == {"OEFFENTLICH"}

    def test_token_flags_grant_non_public_reads(self, client: Client, tenant: SessionTenant) -> None:
        raw = make_token(tenant, can_read_meetings=True, can_read_papers=False)
        auth = bearer(raw)
        assert names(client.get(f"{BASE}/{tenant.slug}/meetings/", headers=auth).json()) == {"OEFFENTLICH", "GEHEIM"}
        assert names(client.get(f"{BASE}/{tenant.slug}/papers/", headers=auth).json()) == {"OEFFENTLICHE-VORLAGE"}

    def test_token_of_other_tenant_is_rejected(
        self, client: Client, tenant: SessionTenant, other_tenant: SessionTenant
    ) -> None:
        raw = make_token(other_tenant, can_read_meetings=True)
        response = client.get(f"{BASE}/{tenant.slug}/meetings/", headers=bearer(raw))
        assert response.status_code == 401
        assert response["Content-Type"].startswith("application/problem+json")

    def test_pagination(self, client: Client, tenant: SessionTenant) -> None:
        org = SessionOrganization.objects.get(tenant=tenant)
        for i in range(3):
            SessionMeeting.objects.create(
                tenant=tenant, name=f"S{i}", organization=org, start=timezone.now(), is_public=True
            )
        body = client.get(f"{BASE}/{tenant.slug}/meetings/?limit=2&offset=1").json()
        assert body["meta"]["total"] == 4
        assert len(body["data"]) == 2
        assert body["meta"]["limit"] == 2 and body["meta"]["offset"] == 1

    def test_invalid_query_is_validation_problem(self, client: Client, tenant: SessionTenant) -> None:
        response = client.get(f"{BASE}/{tenant.slug}/meetings/?limit=0")
        assert response.status_code == 422
        body = response.json()
        assert body["type"].endswith("/validierung")
        assert body["errors"]


class TestApplications:
    def test_list_requires_permission(self, client: Client, tenant: SessionTenant) -> None:
        anonymous = client.get(f"{BASE}/{tenant.slug}/applications/")
        assert anonymous.status_code == 401
        assert anonymous.json()["type"].endswith("/nicht-authentifiziert")
        forbidden = make_session_client(tenant, can_view_applications=False).get(f"{BASE}/{tenant.slug}/applications/")
        assert forbidden.status_code == 403
        allowed = make_session_client(tenant, can_view_applications=True).get(f"{BASE}/{tenant.slug}/applications/")
        assert allowed.status_code == 200
        assert allowed.json()["meta"]["total"] == 0

    def test_submit_requires_token_with_flag(self, client: Client, tenant: SessionTenant) -> None:
        payload = {
            "title": "Spielplatz",
            "justification": "Weil.",
            "resolution_proposal": "Bauen.",
            "submitter_name": "Fraktion A",
            "submitter_email": "a@example.org",
        }
        url = f"{BASE}/{tenant.slug}/applications/submit/"
        assert client.post(url, payload, content_type="application/json").status_code == 401
        raw_no_flag = make_token(tenant, can_submit_applications=False)
        response = client.post(url, payload, content_type="application/json", headers=bearer(raw_no_flag))
        assert response.status_code == 403
        raw = make_token(tenant, can_submit_applications=True)
        response = client.post(url, payload, content_type="application/json", headers=bearer(raw))
        assert response.status_code == 201, response.content
        body = response.json()
        application = SessionApplication.objects.get(id=body["id"])
        assert application.title == "Spielplatz"
        assert application.tenant == tenant
        assert body["reference"] == application.reference
        token = SessionAPIToken.objects.get(tenant=tenant, token_prefix=raw[:8])
        assert token.usage_count == 1

    def test_submit_validation_and_legacy_alias(self, client: Client, tenant: SessionTenant) -> None:
        raw = make_token(tenant, can_submit_applications=True)
        auth = bearer(raw)
        url = f"{BASE}/{tenant.slug}/applications/submit/"
        bad = client.post(url, {"title": "x"}, content_type="application/json", headers=auth)
        assert bad.status_code == 422
        errors = bad.json()["errors"]
        assert any("justification" in str(err["loc"]) for err in errors)
        bad_mail = client.post(
            url,
            {
                "title": "x",
                "justification": "y",
                "resolution_proposal": "z",
                "submitter_name": "n",
                "submitter_email": "kein-mail",
            },
            content_type="application/json",
            headers=auth,
        )
        assert bad_mail.status_code == 422
        ok = client.post(
            url,
            {
                "title": "x",
                "justification": "y",
                "resolution_proposal": "z",
                "submitter_name": "n",
                "submitter_email": "n@example.org",
                "application_type": "urgent_motion",
            },
            content_type="application/json",
            headers=auth,
        )
        assert ok.status_code == 201
        assert SessionApplication.objects.get(id=ok.json()["id"]).application_type == "urgent"

    def test_rate_limit_per_token(self, client: Client, tenant: SessionTenant) -> None:
        raw = make_token(tenant, can_read_meetings=True, rate_limit_per_minute=2)
        auth = bearer(raw)
        url = f"{BASE}/{tenant.slug}/meetings/"
        assert client.get(url, headers=auth).status_code == 200
        assert client.get(url, headers=auth).status_code == 200
        limited = client.get(url, headers=auth)
        assert limited.status_code == 429
        assert limited["Retry-After"] == "60"


class TestOpenApi:
    def test_schema_and_docs(self, client: Client) -> None:
        schema = client.get(f"{BASE}/openapi.json")
        assert schema.status_code == 200
        body = schema.json()
        assert body["info"]["title"] == "mandari Session-API"
        paths = body["paths"]
        assert f"{BASE}/{{tenant_slug}}/applications/submit/" in paths
        assert "ApplicationIn" in body["components"]["schemas"]
        docs = client.get(f"{BASE}/docs")
        assert docs.status_code == 200
        html = docs.content.decode()
        assert "swagger-ui" in html
        assert "cdn.jsdelivr.net" not in html


class TestDeprecatedPaths:
    def test_old_session_api_announces_successor(self, client: Client, tenant: SessionTenant) -> None:
        response = client.get(f"/session/{tenant.slug}/api/session/meetings/")
        assert response.status_code == 200
        assert response["Deprecation"] == "true"
        assert response["Sunset"].endswith("GMT")
        assert response["Link"] == f'</api/v1/session/{tenant.slug}/meetings/>; rel="successor-version"'
        root = client.get(f"/session/{tenant.slug}/api/").json()
        assert root["v1"].endswith(f"{BASE}/{tenant.slug}/")
