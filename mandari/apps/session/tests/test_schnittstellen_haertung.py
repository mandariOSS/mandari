# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schnittstellen des Session RIS: OParl, alte Einreichungs-API und Django-Admin.

- ``modified_since`` blättert Objekte und Tombstones seitenweise, ohne die Tabellen ganz zu laden.
- Beratungsstationen in nichtöffentlichen Sitzungen nennen in OParl weder Gremium noch Rolle.
- Die alte Einreichungs-API hält das Ratenlimit des Tokens ein.
- Admin-Aktionen am Mandanten und am Antrag brauchen das Änderungsrecht; ein erzeugter API-Token
  erscheint einmalig auf einer eigenen Seite, nie in einer Meldung.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db.models.signals import post_init
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAPIToken,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionOParlTombstone,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Schnittstelle", slug="schnittstelle")


@pytest.fixture
def gremium(tenant: SessionTenant) -> SessionOrganization:
    return SessionOrganization.objects.create(tenant=tenant, name="Rat")


# =============================================================================
# OParl: modified_since
# =============================================================================


def test_modified_since_laedt_nur_die_seite(tenant: SessionTenant, gremium: SessionOrganization, settings: Any) -> None:
    settings.OPARL_API_PAGE_SIZE = 5
    basis = timezone.now() - timedelta(days=30)
    for nummer in range(30):
        meeting = SessionMeeting.objects.create(
            tenant=tenant, name=f"Sitzung {nummer:02d}", organization=gremium, start=basis
        )
        SessionMeeting.objects.filter(pk=meeting.pk).update(updated_at=basis + timedelta(hours=2 * nummer))
    for nummer in range(4):
        SessionOParlTombstone.objects.create(
            tenant=tenant,
            oparl_type="meeting",
            object_id=uuid.uuid4(),
            object_created_at=basis,
            deleted_at=basis + timedelta(hours=2 * nummer + 1),
        )

    geladen: list[Any] = []

    def zaehlen(sender: Any, instance: Any, **kwargs: Any) -> None:
        geladen.append(instance)

    post_init.connect(zaehlen, sender=SessionMeeting)
    try:
        antwort = Client().get(
            f"/session/{tenant.slug}/api/oparl/meetings/", {"modified_since": "2000-01-01T00:00:00Z"}
        )
    finally:
        post_init.disconnect(zaehlen, sender=SessionMeeting)
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["pagination"]["totalElements"] == 34
    assert len(daten["data"]) == 5
    assert len(geladen) <= 5, f"{len(geladen)} Sitzungen geladen – erwartet höchstens eine Seite"


def test_modified_since_blaettert_luecken_und_doppelungsfrei(
    tenant: SessionTenant, gremium: SessionOrganization, settings: Any
) -> None:
    settings.OPARL_API_PAGE_SIZE = 3
    basis = timezone.now() - timedelta(days=5)
    erwartet = []
    for nummer in range(5):
        meeting = SessionMeeting.objects.create(tenant=tenant, name=f"S{nummer}", organization=gremium, start=basis)
        stempel = basis + timedelta(minutes=10 * nummer)
        SessionMeeting.objects.filter(pk=meeting.pk).update(updated_at=stempel)
        erwartet.append((stempel, str(meeting.pk)))
    for nummer in range(3):
        grab = SessionOParlTombstone.objects.create(
            tenant=tenant,
            oparl_type="meeting",
            object_id=uuid.uuid4(),
            object_created_at=basis,
            deleted_at=basis + timedelta(minutes=10 * nummer + 5),
        )
        erwartet.append((grab.deleted_at, str(grab.object_id)))
    erwartet.sort()

    gesehen = []
    for seite in (1, 2, 3):
        antwort = Client().get(
            f"/session/{tenant.slug}/api/oparl/meetings/", {"modified_since": "2000-01-01T00:00:00Z", "page": seite}
        )
        assert antwort.status_code == 200
        gesehen += [eintrag["id"].rstrip("/").rsplit("/", 1)[-1] for eintrag in antwort.json()["data"]]
    assert gesehen == [kennung for _, kennung in erwartet]


# =============================================================================
# OParl: Beratungsstationen in NÖ-Sitzungen
# =============================================================================


def test_noe_station_nennt_weder_gremium_noch_rolle(tenant: SessionTenant, gremium: SessionOrganization) -> None:
    vorlage = SessionPaper.objects.create(tenant=tenant, name="Vorlage", is_public=True, status="approved")
    klausur = SessionMeeting.objects.create(
        tenant=tenant, name="Klausur", organization=gremium, start=timezone.now(), is_public=False
    )
    SessionConsultation.objects.create(paper=vorlage, organization=gremium, meeting=klausur, role="decision", order=1)
    antwort = Client().get(f"/session/{tenant.slug}/api/oparl/paper/{vorlage.id}/")
    assert antwort.status_code == 200
    station = antwort.json()["consultation"][0]
    assert "organization" not in station and "role" not in station and "meeting" not in station


# =============================================================================
# Alte Einreichungs-API: Ratenlimit
# =============================================================================


def test_alte_einreichungs_api_haelt_das_ratenlimit(tenant: SessionTenant) -> None:
    cache.clear()
    _token, roh = SessionAPIToken.create_token(tenant, "Fraktion", rate_limit_per_minute=1)
    antrag = {
        "title": "Antrag",
        "justification": "Begründung",
        "resolution_proposal": "Beschluss",
        "submitter_name": "Fraktion",
        "submitter_email": "fraktion@example.org",
    }
    url = f"/session/{tenant.slug}/api/session/applications/submit/"
    kopf = {"Authorization": f"Bearer {roh}"}
    erste = Client().post(url, json.dumps(antrag), content_type="application/json", headers=kopf)
    zweite = Client().post(url, json.dumps(antrag), content_type="application/json", headers=kopf)
    assert erste.status_code == 201
    assert zweite.status_code == 429
    assert zweite["Retry-After"] == "60"
    assert SessionApplication.objects.filter(tenant=tenant).count() == 1


# =============================================================================
# Django-Admin: Detailaktionen
# =============================================================================


def test_token_aktion_braucht_das_aenderungsrecht(tenant: SessionTenant) -> None:
    staff = cast(Any, UserFactory)(email="staff@example.org", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="view_sessiontenant"))
    client = Client(raise_request_exception=False)
    client.force_login(staff)
    client.get(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/")
    assert not SessionAPIToken.objects.filter(tenant=tenant).exists()


def test_token_erscheint_einmalig_auf_eigener_seite(tenant: SessionTenant) -> None:
    betrieb = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(betrieb)
    antwort = client.get(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/")
    assert antwort.status_code == 200
    token = SessionAPIToken.objects.get(tenant=tenant)
    inhalt = antwort.content.decode()
    assert token.token_prefix in inhalt
    assert "no-store" in antwort["Cache-Control"]
    assert token.token_prefix not in str(antwort.cookies.get("messages", ""))


def test_antrag_umwandeln_braucht_das_aenderungsrecht(tenant: SessionTenant) -> None:
    antrag = SessionApplication.objects.create(
        tenant=tenant,
        title="Antrag",
        justification="x",
        resolution_proposal="y",
        submitter_name="N",
        submitter_email="n@example.org",
    )
    staff = cast(Any, UserFactory)(email="lesend@example.org", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="view_sessionapplication"))
    client = Client(raise_request_exception=False)
    client.force_login(staff)
    client.get(f"/admin/session/sessionapplication/{antrag.pk}/create-paper/")
    assert not SessionPaper.objects.filter(source_application=antrag).exists()
