# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsformat in Ladung, Tagesordnung und Öffentlichkeit (Issue #138, Teil 2).

- Ladungs-PDF nennt Format, Zugang (nur Fassung für Mitglieder) und Rechtsgrundlage
- Ladungsmail und Kalendereintrag nennen das Format, nie den Zugangsweg im Kalender
- Versand gesperrt, solange das Format nach dem Landesprofil nicht zulässig ist
- Detailseite zeigt Format, Rechtsgrundlage und Warnungen
- OParl-API liefert Format und Hinweis für die Öffentlichkeit, nie den Zugangsweg – ohne Abfrage je Sitzung
- Bürgerportal zeigt den Hinweis; Links aus fremden Quellen nur mit http(s)
- Zugangsweg: Empfänger der vollständigen Ladung; im Sitzungsdienst nur mit Bearbeitungsrecht (Abruf
  protokolliert); nie in der Sitzungsmappe und nie für Gäste
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
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAuditLog,
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
from apps.session.services import invitation_service, invitation_token
from apps.session.services import meeting_format_service as mfs
from insight_core.views.meetings import _broadcast_info

pytestmark = pytest.mark.django_db

ZUGANG = "Konferenzraum 4711, PIN 2468"
#: Wortteile des Zugangswegs für die Prüfung „kein Zugangsweg“ – nie die bloße Ziffernfolge: Rückmelde-Tokens
#: und Kennungen in Links enthalten „4711“ zufällig (Issue #666).
ZUGANG_TEILE = ("Konferenzraum 4711", "PIN 2468")


def _nennt_zugangsweg(text: str) -> bool:
    """Steht der Zugangsweg (auch nur teilweise) im Text? Zeilenumbrüche und ICS-Zeilenfaltung zählen nicht."""
    einzeilig = " ".join(text.replace("\r\n ", "").split())
    return any(teil in einzeilig for teil in ZUGANG_TEILE)


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
        # Freigeschaltete OParl-Schnittstelle (Issue #319): die Tests lesen das Sitzungsformat über OParl
        oparl_public_since=timezone.now(),
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
    assert not _nennt_zugangsweg(oeffentlich)
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
    assert not _nennt_zugangsweg(ics_text)


def test_versand_ermittelt_formatangaben_einmal_je_fassung_nicht_je_empfaenger(
    welt: Welt, monkeypatch: pytest.MonkeyPatch
) -> None:
    for nummer in range(3):
        person = SessionPerson.objects.create(
            tenant=welt.tenant, given_name="Max", family_name=f"Muster{nummer}", email=f"max{nummer}@example.org"
        )
        SessionOrganizationMembership.objects.create(organization=welt.bau, person=person, role="member")
    pruefungen: list[Any] = []
    original = mfs.check_meeting

    def zaehlen(meeting: Any) -> Any:
        pruefungen.append(meeting.pk)
        return original(meeting)

    monkeypatch.setattr(mfs, "check_meeting", zaehlen)
    mail.outbox = []
    invitation_service.send_invitations(welt.meeting, sent_by=welt.staff)
    assert len([m for m in mail.outbox if "Konferenzraum 4711" in m.body]) == 4
    # Einmal für die vollständige, einmal für die öffentliche Fassung – nicht je Empfänger oder Anhang
    assert len(pruefungen) == 2


def test_gaeste_erhalten_format_aber_keinen_zugangsweg(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bewusste Entscheidung: Der Zugangsweg geht an die Empfänger der vollständigen Ladung, nicht an Gäste."""
    # Die Ziffernfolge des Zugangswegs kommt im Rückmelde-Token zufällig vor (Issue #666) – hier immer
    rueckmeldelink = "https://mandari.example/session/ladung/antwort/0c4711e9f2/"
    monkeypatch.setattr(invitation_token, "response_url", lambda recipient: rueckmeldelink)
    gast = SessionPerson.objects.create(
        tenant=welt.tenant, given_name="Gerd", family_name="Gast", email="gast@example.org"
    )
    SessionOrganizationMembership.objects.create(organization=welt.bau, person=gast, role="guest")
    mail.outbox = []
    invitation_service.send_invitations(welt.meeting, sent_by=welt.staff)
    nachricht = next(m for m in mail.outbox if m.to == ["gast@example.org"])
    assert "Hybride Sitzung" in nachricht.body
    assert rueckmeldelink in nachricht.body
    assert not _nennt_zugangsweg(str(nachricht.body))
    anhang = next(inhalt for name, inhalt, _typ in nachricht.attachments if name.endswith(".pdf"))
    assert not _nennt_zugangsweg(_pdf_text(anhang))


def test_pdf_abruf_zugangsweg_nur_mit_bearbeitungsrecht_und_protokolliert(welt: Welt) -> None:
    url = f"/session/muster/meetings/{welt.meeting.pk}/agenda.pdf"
    mit = _pdf_text(welt.client.get(url).content)
    assert "Konferenzraum 4711" in mit
    eintrag = SessionAuditLog.objects.filter(tenant=welt.tenant, action="download").latest("created_at")
    assert "Zugangsweg für Zugeschaltete" in str(eintrag.changes)

    # NÖ-Recht ohne Bearbeitungsrecht: vollständige Tagesordnung, aber kein Zugangsweg (wie auf der Detailseite)
    SessionRole.objects.filter(tenant=welt.tenant).update(can_edit_meetings=False)
    ohne = _pdf_text(welt.client.get(url).content)
    assert "Hybride Sitzung" in ohne
    assert not _nennt_zugangsweg(ohne)


def test_sitzungsmappe_nennt_format_ohne_zugangsweg(welt: Welt, tmp_path: Any) -> None:
    from apps.session.services.meeting_package_pdf import build_pdf
    from apps.session.services.meeting_package_plan import INTERNAL, build_plan

    ergebnis = build_pdf(
        build_plan(welt.meeting, INTERNAL), version=1, as_of=timezone.now(), target=tmp_path / "mappe.pdf"
    )
    tagesordnung = " ".join(_pdf_text(inhalt) for inhalt in ergebnis.generated.values())
    assert "Hybride Sitzung" in tagesordnung
    assert not _nennt_zugangsweg(tagesordnung)


def test_versand_gesperrt_fuer_nicht_eingeordneten_haupt_und_finanzausschuss(welt: Welt) -> None:
    """Die Ladung nennt keine Rechtsgrundlage des Regelbetriebs für einen nicht eingeordneten Hauptausschuss."""
    hfa = SessionOrganization.objects.create(tenant=welt.tenant, name="Haupt- und Finanzausschuss")
    SessionOrganizationMembership.objects.create(
        organization=hfa, person=SessionPerson.objects.get(email="mitglied@example.org"), role="member"
    )
    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(organization=hfa)
    inhalt = welt.client.get(f"/session/muster/meetings/{welt.meeting.pk}/").content.decode()
    assert "Sitzungsformat nach dem Landesprofil nicht zulässig" in inhalt
    assert "keiner gesetzlichen Ausschussart zugeordnet" in inhalt
    assert "§ 58a GO NRW" not in inhalt
    antwort = welt.client.post(
        f"/session/muster/meetings/{welt.meeting.pk}/invitation/", {"dispatch_type": "invitation"}, follow=True
    )
    assert "nicht zulässig" in antwort.content.decode()
    assert not SessionInvitationDispatch.objects.filter(meeting=welt.meeting).exists()


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
    # Hinweis ohne Sperre: Der Bauausschuss ist keiner gesetzlichen Ausschussart zugeordnet
    assert "„Bauausschuss“ ist keiner gesetzlichen Ausschussart zugeordnet" in inhalt

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
    assert not _nennt_zugangsweg(json.dumps(daten, ensure_ascii=False))
    liste = Client().get("/session/muster/api/oparl/meetings/").content.decode()
    assert not _nennt_zugangsweg(liste)


def test_oparl_sitzungsliste_ohne_abfrage_je_hybrider_sitzung(welt: Welt) -> None:
    """Der öffentliche Listenendpunkt fragt Mandant und Landesprofil nicht je Sitzung ab."""

    def abfragen() -> int:
        with CaptureQueriesContext(connection) as erfasst:
            antwort = Client().get("/session/muster/api/oparl/meetings/")
        assert antwort.status_code == 200
        return len(erfasst.captured_queries)

    SessionMeeting.objects.create(
        tenant=welt.tenant,
        organization=welt.bau,
        name="Präsenz mit Übertragung",
        start=welt.meeting.start + timedelta(days=1),
        public_access_url="https://stream.example.org/praesenz",
    )
    eine = abfragen()
    for nummer, meeting_format in enumerate(("hybrid", "digital", "hybrid", "presence")):
        SessionMeeting.objects.create(
            tenant=welt.tenant,
            organization=welt.bau,
            name=f"Weitere Sitzung {nummer}",
            start=welt.meeting.start + timedelta(days=2 + nummer),
            format=meeting_format,
            format_reason="Unwetter",
            public_access_url="https://stream.example.org/bau",
        )
    daten = Client().get("/session/muster/api/oparl/meetings/").json()
    assert sum(1 for m in daten["data"] if m.get("mandari:meetingFormat") in ("hybrid", "digital")) == 4
    assert abfragen() == eine


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
