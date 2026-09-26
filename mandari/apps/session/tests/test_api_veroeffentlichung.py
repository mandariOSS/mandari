# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session-APIs (alte Pfade und v1) ohne Leserecht auf nichtöffentliche Vorlagen:
Es gilt dieselbe Veröffentlichungsregel wie in der OParl-Schnittstelle
(``oparl_publication.visible_papers``) – öffentlich UND freigegeben. Vorlagen im
Entwurf oder in der Prüfung sind Verwaltungsinterna.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import SessionAPIToken, SessionPaper, SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db

V1 = "/api/v1/session/{slug}/papers/"
ALT = "/session/{slug}/api/session/papers/"
OPARL = "/session/{slug}/api/oparl/papers/"


@pytest.fixture
def tenant() -> SessionTenant:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    for status in ("draft", "review", "approved"):
        SessionPaper.objects.create(
            tenant=tenant,
            reference=f"V/{status}",
            name=f"VORLAGE-{status.upper()}",
            is_public=True,
            status=status,
            main_text=f"TEXT-{status.upper()}",
        )
    return tenant


def _client_mit_rolle(tenant: SessionTenant, **rechte: bool) -> Client:
    user = cast(User, UserFactory())  # type: ignore[no-untyped-call]
    rolle = SessionRole.objects.create(tenant=tenant, name=f"Rolle-{SessionRole.objects.count() + 1}", **rechte)
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    session_user.roles.add(rolle)
    client = Client()
    client.force_login(user)
    return client


def _namen(antwort: Any) -> set[str]:
    assert antwort.status_code == 200
    return {zeile["name"] for zeile in antwort.json()["data"]}


@pytest.mark.parametrize("pfad", [V1, ALT])
def test_anonym_nur_freigegebene_vorlagen(client: Client, tenant: SessionTenant, pfad: str) -> None:
    antwort = client.get(pfad.format(slug=tenant.slug))
    assert _namen(antwort) == {"VORLAGE-APPROVED"}
    assert b"VORLAGE-DRAFT" not in antwort.content
    assert b"VORLAGE-REVIEW" not in antwort.content
    assert b"TEXT-" not in antwort.content


def test_apis_und_oparl_zeigen_dieselben_vorlagen(client: Client, tenant: SessionTenant) -> None:
    oparl = {zeile["name"] for zeile in client.get(OPARL.format(slug=tenant.slug)).json()["data"]}
    assert oparl == {"VORLAGE-APPROVED"}
    assert _namen(client.get(V1.format(slug=tenant.slug))) == oparl
    assert _namen(client.get(ALT.format(slug=tenant.slug))) == oparl


@pytest.mark.parametrize("pfad", [V1, ALT])
def test_angemeldet_ohne_noe_recht_nur_freigegebene(tenant: SessionTenant, pfad: str) -> None:
    client = _client_mit_rolle(tenant, can_view_papers=True)
    assert _namen(client.get(pfad.format(slug=tenant.slug))) == {"VORLAGE-APPROVED"}


def test_token_ohne_leserecht_nur_freigegebene(client: Client, tenant: SessionTenant) -> None:
    _token, roh = SessionAPIToken.create_token(tenant, "Integration", can_read_papers=False)
    antwort = client.get(V1.format(slug=tenant.slug), headers={"Authorization": f"Bearer {roh}"})
    assert _namen(antwort) == {"VORLAGE-APPROVED"}


@pytest.mark.parametrize("pfad", [V1, ALT])
def test_mit_noe_recht_weiterhin_alle(tenant: SessionTenant, pfad: str) -> None:
    client = _client_mit_rolle(tenant, can_view_papers=True, can_view_non_public_papers=True)
    assert _namen(client.get(pfad.format(slug=tenant.slug))) == {"VORLAGE-DRAFT", "VORLAGE-REVIEW", "VORLAGE-APPROVED"}


def test_v1_gesamtzahl_zaehlt_nur_sichtbare(client: Client, tenant: SessionTenant) -> None:
    antwort = client.get(V1.format(slug=tenant.slug)).json()
    assert antwort["meta"]["total"] == 1
