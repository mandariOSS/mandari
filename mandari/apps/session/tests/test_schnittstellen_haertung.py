# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schnittstellen des Session RIS: OParl, alte Einreichungs-API und Django-Admin.

- ``modified_since`` blättert Objekte und Tombstones seitenweise, ohne die Tabellen ganz zu laden.
- Beratungsstationen in nichtöffentlichen Sitzungen nennen in OParl weder Gremium noch Rolle.
- Die alte Einreichungs-API hält das Ratenlimit des Tokens ein.
- Admin-Aktionen am Mandanten und am Antrag brauchen das Änderungsrecht; ein API-Token entsteht im Admin
  nur mit abgeschicktem Formular (POST, CSRF), wird protokolliert und erscheint einmalig auf einer eigenen
  Seite, nie in einer Meldung.
- Im Admin lässt sich ein zurückgezogener Token nicht wieder aktivieren; Deaktivieren und Änderungen
  landen mit dem Admin-Konto im Audit-Log des Mandanten.
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
    SessionAuditLog,
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
    antwort = client.post(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/", {"name": "Fraktion A"})
    assert antwort.status_code == 403
    assert not SessionAPIToken.objects.filter(tenant=tenant).exists()


def test_token_erscheint_einmalig_auf_eigener_seite(tenant: SessionTenant) -> None:
    betrieb = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(betrieb)
    antwort = client.post(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/", {"name": "Fraktion A"})
    assert antwort.status_code == 200
    token = SessionAPIToken.objects.get(tenant=tenant)
    inhalt = antwort.content.decode()
    assert token.token_prefix in inhalt
    assert "no-store" in antwort["Cache-Control"]
    assert token.token_prefix not in str(antwort.cookies.get("messages", ""))


def test_token_im_admin_nicht_anlegbar_mit_hinweis_auf_den_sitzungsdienst(tenant: SessionTenant) -> None:
    """Das Admin-Formular konnte keinen Token erzeugen; Anlage dort gesperrt, Hinweis auf den Portalweg."""
    from django.contrib.messages import get_messages

    betrieb = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(betrieb)
    SessionAPIToken.create_token(tenant=tenant, name="Fraktion A")

    liste = client.get("/admin/session/sessionapitoken/")
    assert liste.status_code == 200
    assert "/admin/session/sessionapitoken/add/" not in liste.content.decode()

    antwort = client.get("/admin/session/sessionapitoken/add/")
    assert antwort.status_code == 302
    assert antwort["Location"] == "/admin/session/sessionapitoken/"
    assert "Einreichungs-Zugänge" in " ".join(str(m) for m in get_messages(antwort.wsgi_request))

    client.post(
        "/admin/session/sessionapitoken/add/",
        {"tenant": str(tenant.pk), "name": "Ohne Token", "rate_limit_per_minute": "60", "is_active": "on"},
    )
    assert list(SessionAPIToken.objects.filter(tenant=tenant).values_list("name", flat=True)) == ["Fraktion A"]


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


# =============================================================================
# Django-Admin: API-Tokens erzeugen, deaktivieren, nicht wieder aktivieren
# =============================================================================


def _betrieb_client(**client_kwargs: Any) -> Client:
    betrieb = cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)
    client = Client(**client_kwargs)
    client.force_login(betrieb)
    return client


def test_token_aufruf_ohne_formular_erzeugt_nichts(tenant: SessionTenant) -> None:
    antwort = _betrieb_client().get(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/")

    assert antwort.status_code == 200
    assert 'method="post"' in antwort.content.decode()
    assert not SessionAPIToken.objects.filter(tenant=tenant).exists()


def test_token_ohne_namen_wird_nicht_erzeugt(tenant: SessionTenant) -> None:
    antwort = _betrieb_client().post(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/", {"name": "  "})

    assert antwort.status_code == 200
    assert not SessionAPIToken.objects.filter(tenant=tenant).exists()


def test_token_erzeugen_braucht_csrf(tenant: SessionTenant) -> None:
    client = _betrieb_client(enforce_csrf_checks=True)

    antwort = client.post(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/", {"name": "Fraktion A"})

    assert antwort.status_code == 403
    assert not SessionAPIToken.objects.filter(tenant=tenant).exists()


def test_token_erzeugen_haelt_ersteller_und_audit_fest(tenant: SessionTenant) -> None:
    _betrieb_client().post(f"/admin/session/sessiontenant/{tenant.pk}/generate-token/", {"name": "Fraktion A"})

    token = SessionAPIToken.objects.get(tenant=tenant)
    assert token.name == "Fraktion A"
    assert "betrieb@example.org" in token.description
    eintrag = SessionAuditLog.objects.get(tenant=tenant, model_name="SessionAPIToken", action="create")
    assert eintrag.object_id == token.pk
    assert "betrieb@example.org" in eintrag.changes["durch"]
    assert eintrag.changes["token_prefix"] == token.token_prefix
    assert token.token not in json.dumps(eintrag.changes), "kein Hash im Protokoll"


def test_zurueckgezogener_token_laesst_sich_im_admin_nicht_reaktivieren(tenant: SessionTenant) -> None:
    token, _roh = SessionAPIToken.create_token(tenant=tenant, name="Fraktion A")
    token.is_active = False
    token.save(update_fields=["is_active", "updated_at"])
    client = _betrieb_client()

    # Keine Sammelaktion zum Aktivieren
    liste = client.get("/admin/session/sessionapitoken/")
    assert "activate_tokens" not in liste.content.decode().replace("deactivate_tokens", "")
    client.post(
        "/admin/session/sessionapitoken/",
        {"action": "activate_tokens", "_selected_action": [str(token.pk)], "index": "0"},
    )
    # Im Formular ist „Aktiv“ schreibgeschützt
    client.post(
        f"/admin/session/sessionapitoken/{token.pk}/change/",
        {
            "name": "Fraktion A",
            "description": "",
            "can_submit_applications": "on",
            "can_read_meetings": "on",
            "can_read_papers": "on",
            "is_active": "on",
            "rate_limit_per_minute": "60",
            "allowed_ips": "",
            "expires_at_0": "",
            "expires_at_1": "",
        },
    )

    token.refresh_from_db()
    assert token.is_active is False


def test_formular_aenderung_wird_protokolliert_mandant_bleibt_fest(tenant: SessionTenant) -> None:
    anderer = SessionTenant.objects.create(name="Andere Stadt", slug="andere-stadt")
    token, _roh = SessionAPIToken.create_token(tenant=tenant, name="Fraktion A")

    antwort = _betrieb_client().post(
        f"/admin/session/sessionapitoken/{token.pk}/change/",
        {
            "tenant": str(anderer.pk),
            "name": "Fraktion B",
            "description": "",
            "can_submit_applications": "on",
            "can_read_meetings": "on",
            "can_read_papers": "on",
            "rate_limit_per_minute": "60",
            "allowed_ips": "",
            "expires_at_0": "",
            "expires_at_1": "",
        },
    )

    assert antwort.status_code == 302, antwort.content.decode()[:2000]
    token.refresh_from_db()
    assert token.name == "Fraktion B"
    assert token.tenant_id == tenant.pk
    eintrag = SessionAuditLog.objects.get(tenant=tenant, model_name="SessionAPIToken", action="update")
    assert eintrag.changes["felder"] == ["name"]
    assert "betrieb@example.org" in eintrag.changes["durch"]


def test_deaktivieren_im_admin_wird_protokolliert(tenant: SessionTenant) -> None:
    token, _roh = SessionAPIToken.create_token(tenant=tenant, name="Fraktion A")
    schon_aus, _ = SessionAPIToken.create_token(tenant=tenant, name="Fraktion B", is_active=False)

    _betrieb_client().post(
        "/admin/session/sessionapitoken/",
        {"action": "deactivate_tokens", "_selected_action": [str(token.pk), str(schon_aus.pk)], "index": "0"},
    )

    token.refresh_from_db()
    assert token.is_active is False
    eintraege = SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionAPIToken", action="update")
    assert [e.object_id for e in eintraege] == [token.pk], "nur tatsächlich deaktivierte Tokens"
    assert eintraege[0].changes["is_active"] == {"alt": True, "neu": False}
