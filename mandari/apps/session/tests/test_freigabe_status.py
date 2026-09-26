# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Status einer Vorlage ändert sich bis zur Freigabe nur über den Freigabelauf.

Das Bearbeiten-Formular bietet für Vorlagen im Entwurf oder in der Prüfung keinen anderen Status an
und nimmt auch keinen an: „Abgeschlossen“ oder „Zurückgezogen“ gibt es erst nach der Freigabe.
Sonst erschiene ein Entwurf ohne Freigabe in OParl und im Bürgerportal.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from django.test import Client

from apps.common.tests.factories import UserFactory
from apps.session.models import SessionPaper, SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Freigabe", slug="freigabe")


@pytest.fixture
def bearbeitung(tenant: SessionTenant) -> Client:
    role = SessionRole.objects.create(
        tenant=tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", can_view_papers=True, can_edit_papers=True
    )
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def _vorlage(tenant: SessionTenant, status: str) -> SessionPaper:
    return SessionPaper.objects.create(
        tenant=tenant,
        name=f"Vorlage im Status {status}",
        is_public=True,
        status=status,
        has_financial_impact=False,
    )


def _speichern(client: Client, paper: SessionPaper, status: str) -> Any:
    return client.post(
        f"/session/{paper.tenant.slug}/papers/{paper.id}/edit/",
        {
            "name": paper.name,
            "paper_type": paper.paper_type,
            "is_public": "on",
            "status": status,
            "has_financial_impact": "false",
        },
    )


def _oparl_status(paper: SessionPaper) -> int:
    return Client().get(f"/session/{paper.tenant.slug}/api/oparl/paper/{paper.id}/").status_code


@pytest.mark.parametrize("ausgang", ["draft", "review"])
@pytest.mark.parametrize("ziel", ["completed", "withdrawn", "approved", "scheduled"])
def test_formular_setzt_unveroeffentlichte_vorlage_nicht_weiter(
    tenant: SessionTenant, bearbeitung: Client, ausgang: str, ziel: str
) -> None:
    paper = _vorlage(tenant, ausgang)
    _speichern(bearbeitung, paper, ziel)
    paper.refresh_from_db()
    assert paper.status == ausgang
    assert _oparl_status(paper) == 404


def test_formular_bietet_im_entwurf_keinen_anderen_status_an(tenant: SessionTenant, bearbeitung: Client) -> None:
    paper = _vorlage(tenant, "draft")
    antwort = bearbeitung.get(f"/session/{tenant.slug}/papers/{paper.id}/edit/")
    assert antwort.status_code == 200
    angeboten = [wert for wert, _text in antwort.context["form"].fields["status"].choices]
    assert angeboten == ["draft"]


def test_zurueckgezogene_vorlage_ohne_freigabe_wird_nicht_abgeschlossen(
    tenant: SessionTenant, bearbeitung: Client
) -> None:
    paper = _vorlage(tenant, "withdrawn")
    _speichern(bearbeitung, paper, "completed")
    paper.refresh_from_db()
    assert paper.status == "withdrawn"


def test_nach_der_freigabe_bleiben_abschluss_und_ruecknahme_moeglich(
    tenant: SessionTenant, bearbeitung: Client
) -> None:
    paper = _vorlage(tenant, "approved")
    _speichern(bearbeitung, paper, "completed")
    paper.refresh_from_db()
    assert paper.status == "completed"
    _speichern(bearbeitung, paper, "withdrawn")
    paper.refresh_from_db()
    assert paper.status == "withdrawn"
    _speichern(bearbeitung, paper, "draft")
    paper.refresh_from_db()
    assert paper.status == "draft"
