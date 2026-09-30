# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsformat in Ladung, Tagesordnung und Öffentlichkeit (Issue #138, Teil 2).

- Ladungs-PDF nennt Format, Zugang (nur Fassung für Mitglieder) und Rechtsgrundlage
- Ladungsmail und Kalendereintrag nennen das Format, nie den Zugangsweg im Kalender
- Versand gesperrt, solange das Format nach dem Landesprofil nicht zulässig ist
- Detailseite zeigt Format, Rechtsgrundlage und Warnungen
- OParl-API liefert Format und Hinweis für die Öffentlichkeit, nie den Zugangsweg
- Bürgerportal zeigt den Hinweis; Links aus fremden Quellen nur mit http(s)
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core import mail
from django.template.loader import render_to_string
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionInvitationDispatch,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionRole,
    SessionStateProfile,
    SessionTenant,
    SessionUser,
)
from apps.session.services import invitation_service
from apps.session.services import meeting_format_service as mfs
from insight_core.views.meetings import _broadcast_info

pytestmark = pytest.mark.django_db

ZUGANG = "Konferenzraum 4711, PIN 2468"


@dataclass
class Welt:
    tenant: SessionTenant
    bau: SessionOrganization
    haupt: SessionOrganization
    meeting: SessionMeeting
    staff: SessionUser
    client: Client


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
    return " ".join(text.split())


@pytest.fixture
def welt() -> Welt:
    mfs.sync_profiles()
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt",
        slug="muster",
        state_profile=SessionStateProfile.objects.get(code="NW"),
        hybrid_basis_kind="hauptsatzung",
        hybrid_basis_date=date(2024, 3, 12),
        hybrid_basis_reference="§ 7 Hauptsatzung",
        digital_public_registration_days=2,
    )
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
    haupt = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", committee_kind="main")
    person = SessionPerson.objects.create(
        tenant=tenant, given_name="Mia", family_name="Mitglied", email="mitglied@example.org"
    )
    SessionOrganizationMembership.objects.create(organization=bau, person=person, role="member")
    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        organization=bau,
        name="12. Sitzung des Bauausschusses",
        start=(timezone.now() + timedelta(days=20)).replace(microsecond=0),
        meeting_state="scheduled",
        format="hybrid",
        public_access_url="https://stream.example.org/bau",
        public_access_note="Aufzeichnung abrufbar bis zur nächsten Sitzung",
    )
    cast(Any, meeting).set_remote_access_encrypted(ZUGANG)
    meeting.save()
    role = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_create_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
    )
    user = cast(Any, UserFactory)(email="sitzungsdienst@muster.example")
    staff = SessionUser.objects.create(user=user, tenant=tenant)
    staff.roles.add(role)
    client = Client()
    client.force_login(user)
    return Welt(tenant, bau, haupt, meeting, staff, client)


# =============================================================================
# Ladung
# =============================================================================


def test_ladungs_pdf_nennt_format_zugang_und_rechtsgrundlage(welt: Welt) -> None:
    """Akzeptanzkriterium: Ladungs-PDF nennt Format, Zugang und Rechtsgrundlage."""
    voll = _pdf_text(invitation_service.build_agenda_pdf(welt.meeting, include_non_public=True))
    assert "Hybride Sitzung" in voll
    assert "Konferenzraum 4711" in voll
    assert "§ 58a GO NRW" in voll
    assert "Hauptsatzung vom 12.03.2024" in voll
    assert "https://stream.example.org/bau" in voll

    # Öffentliche Fassung (Gäste, öffentliche Sitzungsmappe): Format und Rechtsgrundlage, kein Zugangsweg
    oeffentlich = _pdf_text(invitation_service.build_agenda_pdf(welt.meeting, include_non_public=False))
    assert "Hybride Sitzung" in oeffentlich
    assert "§ 58a GO NRW" in oeffentlich
    assert "4711" not in oeffentlich
    assert "übertragen" in oeffentlich


def test_praesenzsitzung_ohne_formatzeilen(welt: Welt) -> None:
    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(
        format="presence", public_access_url="", public_access_note=""
    )
    text = _pdf_text(
        invitation_service.build_agenda_pdf(SessionMeeting.objects.get(pk=welt.meeting.pk), include_non_public=True)
    )
    assert "Format:" not in text
    assert "Rechtsgrundlage:" not in text


def test_ladungsmail_nennt_format_und_zugang_kalender_nur_format(welt: Welt) -> None:
    mail.outbox = []
    invitation_service.send_invitations(welt.meeting, sent_by=welt.staff)
    nachricht = next(m for m in mail.outbox if m.to == ["mitglied@example.org"])
    assert "Hybride Sitzung" in nachricht.body
    assert "Konferenzraum 4711" in nachricht.body
    assert "§ 58a GO NRW" in nachricht.body
    anhaenge = {name: inhalt for name, inhalt, _typ in nachricht.attachments}
    ics = anhaenge["sitzung.ics"]
    ics_text = ics.decode("utf-8") if isinstance(ics, bytes) else str(ics)
    assert "Hybride Sitzung" in " ".join(ics_text.replace("\r\n ", "").split())
    assert "4711" not in ics_text


def test_versand_gesperrt_wenn_format_nicht_mehr_zulaessig(welt: Welt) -> None:
    # Nachweis entfällt: hybride Sitzung im Regelbetrieb nicht mehr gedeckt
    SessionTenant.objects.filter(pk=welt.tenant.pk).update(hybrid_basis_reference="")
    antwort = welt.client.post(
        f"/session/muster/meetings/{welt.meeting.pk}/invitation/", {"dispatch_type": "invitation"}, follow=True
    )
    assert "nicht zulässig" in antwort.content.decode()
    assert not SessionInvitationDispatch.objects.filter(meeting=welt.meeting).exists()


def test_detailseite_zeigt_format_rechtsgrundlage_und_warnung(welt: Welt) -> None:
    inhalt = welt.client.get(f"/session/muster/meetings/{welt.meeting.pk}/").content.decode()
    assert "Hybride Sitzung" in inhalt
    assert "§ 58a GO NRW" in inhalt
    assert "Konferenzraum 4711" in inhalt
    assert "nicht zulässig" not in inhalt

    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(organization=welt.haupt)
    inhalt = welt.client.get(f"/session/muster/meetings/{welt.meeting.pk}/").content.decode()
    assert "Sitzungsformat nach dem Landesprofil nicht zulässig" in inhalt


# =============================================================================
# Öffentlichkeit: OParl und Bürgerportal
# =============================================================================


def test_oparl_liefert_format_und_hinweis_nie_den_zugangsweg(welt: Welt) -> None:
    antwort = Client().get(f"/session/muster/api/oparl/meeting/{welt.meeting.pk}/")
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["mandari:meetingFormat"] == "hybrid"
    assert daten["mandari:meetingFormatLabel"] == "Hybride Sitzung"
    assert daten["mandari:publicAccess"]["url"] == "https://stream.example.org/bau"
    assert "übertragen" in daten["mandari:publicAccess"]["hint"]
    assert "registrationRequired" not in daten["mandari:publicAccess"]
    assert "4711" not in json.dumps(daten, ensure_ascii=False)
    liste = Client().get("/session/muster/api/oparl/meetings/").content.decode()
    assert "4711" not in liste


def test_oparl_digital_nrw_mit_anmeldung_und_praesenz_unveraendert(welt: Welt) -> None:
    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(
        format="digital", format_reason="Unwetter", public_access_url="", public_access_note=""
    )
    daten = Client().get(f"/session/muster/api/oparl/meeting/{welt.meeting.pk}/").json()
    zugang = daten["mandari:publicAccess"]
    assert zugang["registrationRequired"] is True
    assert zugang["registrationDays"] == 2
    assert "Anmeldung bis 2 Tag(e) vor der Sitzung" in zugang["hint"]

    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(format="presence")
    daten = Client().get(f"/session/muster/api/oparl/meeting/{welt.meeting.pk}/").json()
    assert not any(key.startswith("mandari:meetingFormat") or key == "mandari:publicAccess" for key in daten)


def test_buergerportal_zeigt_hinweis_und_prueft_links() -> None:
    meeting = SimpleNamespace(
        raw_json={
            "mandari:meetingFormat": "hybrid",
            "mandari:meetingFormatLabel": "<script>",
            "mandari:publicAccess": {
                "url": "https://stream.example.org/bau",
                "hint": "Die öffentliche Sitzung wird übertragen.",
            },
        }
    )
    info = _broadcast_info(meeting)
    assert info is not None
    assert info["label"].startswith("Hybride Sitzung")
    html = render_to_string("pages/meetings/_broadcast_card.html", {"broadcast": info})
    assert 'href="https://stream.example.org/bau"' in html
    assert "Die öffentliche Sitzung wird übertragen." in html

    boese = _broadcast_info(
        SimpleNamespace(raw_json={"mandari:publicAccess": {"url": "javascript:alert(1)", "hint": 42}})
    )
    assert boese is None
    assert _broadcast_info(SimpleNamespace(raw_json={"mandari:meetingFormat": "presence"})) is None
    assert _broadcast_info(SimpleNamespace(raw_json=None)) is None
