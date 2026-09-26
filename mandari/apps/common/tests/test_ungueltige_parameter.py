# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ungültige Anfrageparameter führen nie zu HTTP 500.

Kaputte IDs, Datumswerte, Seitenzahlen oder JSON-Körper ergeben eine normale Seite (Filter
ohne Treffer bzw. ignoriert), eine Weiterleitung oder 400/404 – aber keinen Serverfehler.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.common.params import date_param, int_param, json_body, uuid_param
from apps.common.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

KAPUTT = "kaputt"


def _kein_serverfehler(antwort: Any) -> None:
    assert antwort.status_code < 500, antwort.status_code


# ---------------------------------------------------------------- Helfer


def test_helfer() -> None:
    assert uuid_param(KAPUTT) is None
    assert uuid_param("") is None
    assert uuid_param("0b8a4a5e-4f5e-4a8e-9a55-4f0f0e0e0e0e") == "0b8a4a5e-4f5e-4a8e-9a55-4f0f0e0e0e0e"
    assert date_param("2026-13-40") is None
    assert date_param("2026-09-26").isoformat() == "2026-09-26"  # type: ignore[union-attr]
    assert int_param("x", 3) == 3
    assert int_param("500", 3, maximum=20) == 20
    assert int_param("-5", 1, minimum=1) == 1
    fabrik = RequestFactory()
    assert json_body(fabrik.post("/", data="{kaputt", content_type="application/json")) is None
    assert json_body(fabrik.post("/", data="[1, 2]", content_type="application/json")) is None
    assert json_body(fabrik.post("/", data='{"a": 1}', content_type="application/json")) == {"a": 1}


# ---------------------------------------------------------------- Session


@pytest.fixture
def session_client() -> tuple[Client, Any]:
    from apps.session.models import SessionOrganization, SessionRole, SessionTenant, SessionUser

    tenant = SessionTenant.objects.create(name="Stadt Parameter", slug="stadt-parameter")
    SessionOrganization.objects.create(tenant=tenant, name="Rat")
    rolle = SessionRole.objects.create(
        tenant=tenant, name="Sachbearbeitung Test", can_view_meetings=True, can_view_papers=True, can_edit_meetings=True
    )
    user = cast(Any, UserFactory)()
    zugang = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    zugang.roles.add(rolle)
    client = Client()
    client.force_login(user)
    return client, tenant


@pytest.mark.parametrize(
    "pfad",
    [
        "meetings/?term=kaputt",
        "meetings/?from=kaputt",
        "meetings/?to=2026-99-99",
        "papers/?organization=kaputt",
        "papers/?term=kaputt",
        "organizations/?term=kaputt",
        "resolutions/?organization=kaputt",
        "resolutions/export.csv?organization=kaputt",
    ],
)
def test_session_listen(session_client: tuple[Client, Any], pfad: str) -> None:
    client, tenant = session_client
    _kein_serverfehler(client.get(f"/session/{tenant.slug}/{pfad}"))


def test_session_umlauf_stimme_mit_kaputter_person(session_client: tuple[Client, Any]) -> None:
    from apps.session.models import SessionCircularResolution, SessionOrganization

    client, tenant = session_client
    umlauf = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=SessionOrganization.objects.get(tenant=tenant),
        title="Umlauf",
        resolution_text="x",
        deadline=timezone.localdate(),
    )
    antwort = client.post(f"/session/{tenant.slug}/circulars/{umlauf.id}/vote/", {"person": KAPUTT, "vote": "yes"})
    _kein_serverfehler(antwort)


def test_alte_einreichungs_api_mit_kaputten_werten(client: Client) -> None:
    from apps.session.models import SessionAPIToken, SessionTenant

    tenant = SessionTenant.objects.create(name="Stadt API", slug="stadt-api")
    _token, roh = SessionAPIToken.create_token(tenant, "Integration", can_submit_applications=True)
    daten = {
        "title": "Antrag",
        "justification": "Weil.",
        "resolution_proposal": "Bauen.",
        "submitter_name": "Fraktion",
        "submitter_email": "fraktion@example.org",
    }
    for kaputt in ({"deadline": KAPUTT}, {"target_organization_id": KAPUTT}, {"title": ["liste"]}):
        antwort = client.post(
            f"/session/{tenant.slug}/api/session/applications/submit/",
            {**daten, **kaputt},
            content_type="application/json",
            headers={"Authorization": f"Bearer {roh}"},
        )
        assert antwort.status_code == 400, kaputt
    antwort = client.post(
        f"/session/{tenant.slug}/api/session/applications/submit/",
        "[1, 2]",
        content_type="application/json",
        headers={"Authorization": f"Bearer {roh}"},
    )
    assert antwort.status_code == 400


# ---------------------------------------------------------------- Work


def test_work_benachrichtigungen(org: Any, make_member: Any, client_for: Any) -> None:
    client = client_for(make_member(org, ["dashboard.view"], email="benachrichtigt@example.org").user)
    _kein_serverfehler(client.get(f"/work/{org.slug}/notifications/?page={KAPUTT}"))
    _kein_serverfehler(client.get(f"/work/{org.slug}/notifications/?page=-3"))
    _kein_serverfehler(client.get(f"/work/{org.slug}/notifications/latest/?limit={KAPUTT}"))


def test_work_briefkopf_mit_kaputten_zahlen(org: Any, make_member: Any, client_for: Any) -> None:
    client = client_for(make_member(org, ["organization.edit"], email="briefkopf@example.org").user)
    antwort = client.post(
        f"/work/{org.slug}/organization/documents/letterheads/create/",
        {"name": "Kopf", "kind": "generated", "content_margin_top": KAPUTT, "font_size": "x"},
    )
    _kein_serverfehler(antwort)


def test_work_ratsfraktion_mit_kaputter_reihenfolge(org: Any, make_member: Any, client_for: Any) -> None:
    client = client_for(make_member(org, ["organization.edit"], email="fraktion@example.org").user)
    antwort = client.post(
        f"/work/{org.slug}/organization/parties/",
        {"action": "add_party", "name": "Partei", "coalition_order": KAPUTT},
    )
    _kein_serverfehler(antwort)


def test_work_fraktionshistorie_mit_kaputten_filtern(org: Any, make_member: Any, client_for: Any) -> None:
    client = client_for(make_member(org, ["faction.view_audit"], email="historie@example.org").user)
    _kein_serverfehler(client.get(f"/work/{org.slug}/faction/historie/?object={KAPUTT}&meeting={KAPUTT}"))


def test_work_fremdes_supportticket(org: Any, make_member: Any, client_for: Any) -> None:
    from apps.work.support.models import SupportTicket

    erstellt = make_member(org, ["support.view", "support.create"], email="ticket@example.org")
    ticket = SupportTicket.objects.create(organization=org, created_by=erstellt, subject="Hilfe")
    fremd = client_for(make_member(org, ["support.view"], email="fremd@example.org").user)
    antwort = fremd.get(f"/work/{org.slug}/support/{ticket.id}/")
    _kein_serverfehler(antwort)
    assert antwort.status_code in (302, 403, 404)


def test_work_sitzungsvorbereitung_json_payload() -> None:
    from apps.work.meetings.views._helpers import request_payload

    anfrage = RequestFactory().post("/", data="{kaputt", content_type="application/json")
    assert dict(request_payload(anfrage)) == {}


# ---------------------------------------------------------------- Insight


@pytest.fixture
def kommune() -> Any:
    from insight_core.models import OParlBody, OParlSource

    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.invalid/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.example.invalid/oparl/bodies/1", source=quelle, name="Stadt", slug="stadt"
    )


def test_insight_seitenzahlen(client: Client, kommune: Any) -> None:
    session = client.session
    session["active_body_id"] = str(kommune.id)
    session.save()
    _kein_serverfehler(client.get(f"/insight/suche/partials/results/?q=Spielplatz&page={KAPUTT}"))
    _kein_serverfehler(client.get(f"/insight/dokumente/?page={KAPUTT}"))
    _kein_serverfehler(client.get(f"/insight/beschluesse/?gremium={KAPUTT}"))


def test_insight_merkliste_mit_kaputten_ids(client: Client) -> None:
    _kein_serverfehler(client.get(f"/insight/merkliste/api/entities/?type=paper&ids={KAPUTT},123"))
    user = cast(Any, UserFactory)()
    client.force_login(user)
    antwort = client.post(
        "/insight/merkliste/api/toggle/", {"type": "paper", "id": KAPUTT}, content_type="application/json"
    )
    assert antwort.status_code == 400
