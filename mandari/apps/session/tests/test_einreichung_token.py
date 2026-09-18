# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einreichung per API-Token: Die einreichende Organisation ergibt sich aus der Verbindung, die eine
Fraktion in mandari Work mit ihrem Token hergestellt hat – nicht aus einer Angabe in der Anfrage.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client

from apps.common.tests.factories import MembershipFactory, OrganizationFactory
from apps.session.models import SessionAPIToken, SessionApplication, SessionTenant
from apps.work.motions.ris_submission import SubmissionError, connect_with_token

pytestmark = pytest.mark.django_db

PAYLOAD = {
    "title": "Mehr Bänke im Stadtpark",
    "justification": "Aufenthaltsqualität.",
    "resolution_proposal": "Zehn Bänke aufstellen.",
    "submitter_name": "Fraktion",
    "submitter_email": "fraktion@example.org",
}


@pytest.fixture
def welt() -> dict[str, Any]:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    fraktion_a, fraktion_b = cast(Any, OrganizationFactory)(), cast(Any, OrganizationFactory)()
    _t, raw_a = SessionAPIToken.create_token(tenant, "Fraktion A", can_submit_applications=True)
    connect_with_token(fraktion_a, raw_a, cast(Any, MembershipFactory)(organization=fraktion_a))
    return {"tenant": tenant, "a": fraktion_a, "b": fraktion_b, "raw_a": raw_a}


def _post(raw: str, **extra: Any) -> Any:
    return Client().post(
        "/api/v1/session/musterstadt/applications/submit/",
        {**PAYLOAD, **extra},
        content_type="application/json",
        headers={"Authorization": f"Bearer {raw}"},
    )


def test_organisation_kommt_aus_der_verbindung(welt: dict[str, Any]) -> None:
    antwort = _post(welt["raw_a"])
    assert antwort.status_code == 201, antwort.content
    assert SessionApplication.objects.get(id=antwort.json()["id"]).submitting_organization == welt["a"]


def test_fremde_organisation_wird_abgewiesen(welt: dict[str, Any]) -> None:
    antwort = _post(welt["raw_a"], submitting_organization_id=str(welt["b"].pk))
    assert antwort.status_code == 403
    assert not SessionApplication.objects.exists()


def test_passende_angabe_ist_erlaubt(welt: dict[str, Any]) -> None:
    antwort = _post(welt["raw_a"], submitting_organization_id=str(welt["a"].pk))
    assert antwort.status_code == 201


def test_unverbundenes_token_ordnet_keine_organisation_zu(welt: dict[str, Any]) -> None:
    _t, raw = SessionAPIToken.create_token(welt["tenant"], "Extern", can_submit_applications=True)
    assert _post(raw, submitting_organization_id=str(welt["a"].pk)).status_code == 403
    antwort = _post(raw)
    assert antwort.status_code == 201
    assert SessionApplication.objects.get(id=antwort.json()["id"]).submitting_organization is None


def test_legacy_api_prueft_ebenso(welt: dict[str, Any]) -> None:
    antwort = Client().post(
        "/session/musterstadt/api/session/applications/submit/",
        {**PAYLOAD, "submitting_organization_id": str(welt["b"].pk)},
        content_type="application/json",
        headers={"Authorization": f"Bearer {welt['raw_a']}"},
    )
    assert antwort.status_code == 403
    assert not SessionApplication.objects.exists()


def test_token_verbindet_nur_eine_organisation(welt: dict[str, Any]) -> None:
    with pytest.raises(SubmissionError, match="anderen Organisation"):
        connect_with_token(welt["b"], welt["raw_a"], cast(Any, MembershipFactory)(organization=welt["b"]))
    # Dieselbe Organisation darf sich erneut verbinden (z. B. nach Neuanlage der Verbindung)
    connect_with_token(welt["a"], welt["raw_a"], cast(Any, MembershipFactory)(organization=welt["a"]))
