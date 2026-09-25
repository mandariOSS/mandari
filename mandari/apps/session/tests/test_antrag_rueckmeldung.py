# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldestand eines eingereichten Antrags (Issue #316): Ö/NÖ-Regeln der Aufbereitung, Umwandlung in
eine Vorlage und Abruf über die Session-API v1 mit dem einreichenden Token.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, OrganizationFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAPIToken,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)
from apps.session.services import application_feedback
from apps.session.services.application_service import ConversionError, convert_to_paper
from apps.work.motions.ris_submission import connect_with_token

pytestmark = pytest.mark.django_db

BASE = "/api/v1/session"
PAYLOAD = {
    "title": "Mehr Bänke im Stadtpark",
    "justification": "Aufenthaltsqualität.",
    "resolution_proposal": "Zehn Bänke aufstellen.",
    "submitter_name": "Fraktion",
    "submitter_email": "fraktion@example.org",
}


@pytest.fixture(autouse=True)
def _cache() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def rat(tenant: SessionTenant) -> SessionOrganization:
    return SessionOrganization.objects.create(tenant=tenant, name="Rat der Stadt", organization_type="council")


def antrag(tenant: SessionTenant, **kwargs: Any) -> SessionApplication:
    return SessionApplication.objects.create(
        tenant=tenant,
        title="Bänke",
        justification="Weil.",
        resolution_proposal="Aufstellen.",
        submitter_name="Fraktion",
        submitter_email="f@example.org",
        **kwargs,
    )


def sitzung(tenant: SessionTenant, gremium: SessionOrganization, **kwargs: Any) -> SessionMeeting:
    kwargs.setdefault("name", "Ratssitzung")
    return SessionMeeting.objects.create(
        tenant=tenant, organization=gremium, start=timezone.now() + timedelta(days=5), **kwargs
    )


def station(paper: SessionPaper, gremium: SessionOrganization, **kwargs: Any) -> SessionConsultation:
    return SessionConsultation.objects.create(paper=paper, organization=gremium, **kwargs)


# =============================================================================
# Aufbereitung nach Ö/NÖ-Regeln
# =============================================================================


class TestAufbereitung:
    def test_umgewandelter_antrag_mit_oeffentlicher_beratung(
        self, tenant: SessionTenant, rat: SessionOrganization
    ) -> None:
        application = antrag(tenant)
        paper, created = convert_to_paper(application)
        assert created and paper.is_public and paper.status == "approved"
        meeting = sitzung(tenant, rat)
        item = SessionAgendaItem.objects.create(meeting=meeting, number="7", name="Bänke", paper=paper)
        station(paper, rat, meeting=meeting, agenda_item=item, role="decision")
        # Ergebnis am TOP erfasst: signals.sync_consultation_result schreibt es an die Station zurück
        item.vote_result = "approved"
        item.resolution_number = "B/2026/0003"
        item.save()

        feedback = application_feedback.build(application)
        assert feedback.paper_reference == paper.reference and feedback.paper_public
        (only,) = feedback.stations
        assert (only.public, only.organization, only.role_label, only.agenda_number) == (
            True,
            "Rat der Stadt",
            "Entscheidung",
            "7",
        )
        assert feedback.decision is not None
        assert (feedback.decision.result, feedback.decision.resolution_number) == ("approved", "B/2026/0003")

    def test_vorlage_im_entwurf_ist_nicht_veroeffentlicht(
        self, tenant: SessionTenant, rat: SessionOrganization
    ) -> None:
        application = antrag(tenant)
        paper = SessionPaper.objects.create(tenant=tenant, name="Bänke", source_application=application, status="draft")
        station(paper, rat, meeting=sitzung(tenant, rat), role="decision", result="approved")
        feedback = application_feedback.build(application)
        assert feedback.converted and not feedback.paper_public and feedback.paper_reference == ""
        assert [s.public for s in feedback.stations] == [False]
        assert feedback.decision is None

    def test_noe_top_in_oeffentlicher_sitzung(self, tenant: SessionTenant, rat: SessionOrganization) -> None:
        application = antrag(tenant)
        paper, _ = convert_to_paper(application)
        meeting = sitzung(tenant, rat)
        item = SessionAgendaItem.objects.create(
            meeting=meeting, number="N1", name="Bänke", paper=paper, is_public=False
        )
        station(paper, rat, meeting=meeting, agenda_item=item, role="decision", result="rejected")
        feedback = application_feedback.build(application)
        (only,) = feedback.stations
        assert not only.public
        assert only.as_dict() == {"order": 1, "public": False, "label": "nicht-öffentlich beraten"}
        assert feedback.decision is None

    def test_sitzung_eines_fremden_mandanten_wird_nicht_preisgegeben(
        self, tenant: SessionTenant, rat: SessionOrganization
    ) -> None:
        fremd = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
        fremd_rat = SessionOrganization.objects.create(tenant=fremd, name="Fremder Rat")
        application = antrag(tenant)
        paper, _ = convert_to_paper(application)
        station(paper, rat, meeting=sitzung(fremd, fremd_rat, name="Fremde Sitzung"), role="decision")
        station(paper, fremd_rat, role="preliminary")
        feedback = application_feedback.build(application)
        assert [s.public for s in feedback.stations] == [False, False]

    def test_beschluss_nur_aus_entscheidender_station(self, tenant: SessionTenant, rat: SessionOrganization) -> None:
        bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
        application = antrag(tenant)
        paper, _ = convert_to_paper(application)
        station(paper, bau, meeting=sitzung(tenant, bau), role="preliminary", result="approved", order=1)
        entscheidung = station(paper, rat, meeting=sitzung(tenant, rat), role="decision", order=2)
        assert application_feedback.build(application).decision is None
        entscheidung.result = "deferred"
        entscheidung.save()
        assert application_feedback.build(application).decision is None
        entscheidung.result = "noted"
        entscheidung.save()
        decision = application_feedback.build(application).decision
        assert decision is not None and decision.result == "noted" and decision.organization == "Rat der Stadt"

    def test_top_ohne_station_zaehlt_als_beratung(self, tenant: SessionTenant, rat: SessionOrganization) -> None:
        application = antrag(tenant)
        paper, _ = convert_to_paper(application)
        SessionAgendaItem.objects.create(
            meeting=sitzung(tenant, rat), number="3", name="Bänke", paper=paper, vote_result="approved"
        )
        feedback = application_feedback.build(application)
        (only,) = feedback.stations
        assert (only.public, only.role_label, only.decisive, only.result) == (True, "Beratung", True, "approved")
        assert feedback.decision is not None and feedback.decision.result == "approved"

    def test_interna_der_verwaltung_fehlen(self, tenant: SessionTenant) -> None:
        application = antrag(tenant, processing_notes="Intern: Amt 61 fragen")
        daten = application_feedback.build(application).as_dict()
        assert "Amt 61" not in str(daten)
        assert set(daten) == {
            "id",
            "reference",
            "status",
            "status_label",
            "submitted_at",
            "received_at",
            "paper",
            "stations",
            "decision",
        }


class TestUmwandlung:
    def test_gremium_eines_fremden_mandanten_wird_abgelehnt(self, tenant: SessionTenant) -> None:
        fremd = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
        fremd_gremium = SessionOrganization.objects.create(tenant=fremd, name="Fremd")
        application = antrag(tenant)
        with pytest.raises(ConversionError):
            convert_to_paper(application, main_organization_id=fremd_gremium.pk)
        with pytest.raises(ConversionError):
            convert_to_paper(application, main_organization_id="keine-uuid")
        assert not SessionPaper.objects.filter(source_application=application).exists()
        application.refresh_from_db()
        assert application.status == "submitted"

    def test_umwandlung_ist_idempotent(self, tenant: SessionTenant) -> None:
        application = antrag(tenant)
        erste, neu = convert_to_paper(application)
        zweite, nochmal = convert_to_paper(application)
        assert neu and not nochmal and erste.pk == zweite.pk
        application.refresh_from_db()
        assert application.status == "converted"


# =============================================================================
# Session-API v1: Einreichen und Rückmeldestand abrufen
# =============================================================================


def token(tenant: SessionTenant, **flags: Any) -> tuple[SessionAPIToken, str]:
    flags.setdefault("can_submit_applications", True)
    obj, raw = SessionAPIToken.create_token(tenant, "Fraktion", **flags)
    return obj, str(raw)


def bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def einreichen(client: Client, tenant: SessionTenant, raw: str) -> dict[str, Any]:
    response = client.post(
        f"{BASE}/{tenant.slug}/applications/submit/", PAYLOAD, content_type="application/json", headers=bearer(raw)
    )
    assert response.status_code == 201, response.content
    body: dict[str, Any] = response.json()
    return body


class TestApi:
    def test_einreichen_und_rueckmeldestand_abrufen(
        self, client: Client, tenant: SessionTenant, rat: SessionOrganization
    ) -> None:
        _obj, raw = token(tenant)
        created = einreichen(client, tenant, raw)
        application = SessionApplication.objects.get(pk=created["id"])
        assert created["feedback"].endswith(f"{BASE}/{tenant.slug}/applications/{application.pk}/feedback/")

        antwort = client.get(created["feedback"], headers=bearer(raw))
        assert antwort.status_code == 200, antwort.content
        body = antwort.json()
        assert (body["reference"], body["status"], body["stations"]) == (application.reference, "submitted", [])
        assert body["paper"] == {"converted": False, "public": False, "reference_label": tenant.reference_label}

        paper, _ = convert_to_paper(application)
        meeting = sitzung(tenant, rat, name="12. Ratssitzung")
        station(paper, rat, meeting=meeting, role="decision", result="approved", order=1)
        station(paper, rat, meeting=sitzung(tenant, rat, name="Geheim", is_public=False), role="hearing", order=2)
        body = client.get(created["feedback"], headers=bearer(raw)).json()
        assert body["status"] == "converted" and body["paper"]["reference"] == paper.reference
        oeffentlich, geheim = body["stations"]
        assert oeffentlich["meeting_name"] == "12. Ratssitzung" and oeffentlich["result"] == "approved"
        assert geheim == {"order": 2, "public": False, "label": "nicht-öffentlich beraten"}
        assert body["decision"]["result"] == "approved"

    def test_fremde_tokens_finden_den_antrag_nicht(self, client: Client, tenant: SessionTenant) -> None:
        _obj, raw = token(tenant)
        created = einreichen(client, tenant, raw)
        url = created["feedback"]

        _other, other_raw = token(tenant)
        assert client.get(url, headers=bearer(other_raw)).status_code == 404
        fremd = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
        _fremd, fremd_raw = token(fremd)
        assert client.get(url, headers=bearer(fremd_raw)).status_code == 401
        fremd_url = f"{BASE}/{fremd.slug}/applications/{created['id']}/feedback/"
        assert client.get(fremd_url, headers=bearer(fremd_raw)).status_code == 404
        assert client.get(url).status_code == 401
        _read_only, read_raw = token(tenant, can_submit_applications=False)
        assert client.get(url, headers=bearer(read_raw)).status_code == 403

    def test_neues_token_derselben_organisation_darf_abrufen(self, client: Client, tenant: SessionTenant) -> None:
        fraktion = cast(Any, OrganizationFactory)()
        mitglied = cast(Any, MembershipFactory)(organization=fraktion)
        _alt, alt_raw = token(tenant)
        connect_with_token(fraktion, alt_raw, mitglied)
        created = einreichen(client, tenant, alt_raw)

        _neu, neu_raw = token(tenant)
        connect_with_token(fraktion, neu_raw, mitglied)  # Tokenwechsel in den Organisationseinstellungen
        assert client.get(created["feedback"], headers=bearer(neu_raw)).status_code == 200
        _fremd, fremd_raw = token(tenant)
        assert client.get(created["feedback"], headers=bearer(fremd_raw)).status_code == 404

    def test_ratenlimit_gilt_auch_fuer_den_abruf(self, client: Client, tenant: SessionTenant) -> None:
        _obj, raw = token(tenant, rate_limit_per_minute=2)
        created = einreichen(client, tenant, raw)
        assert client.get(created["feedback"], headers=bearer(raw)).status_code == 200
        limited = client.get(created["feedback"], headers=bearer(raw))
        assert limited.status_code == 429

    def test_openapi_beschreibt_den_rueckmeldestand(self, client: Client) -> None:
        body = client.get(f"{BASE}/openapi.json").json()
        assert f"{BASE}/{{tenant_slug}}/applications/{{application_id}}/feedback/" in body["paths"]
        assert "ApplicationFeedbackOut" in body["components"]["schemas"]
