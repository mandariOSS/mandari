# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ladung mit Empfangsbestätigung und Rückmeldung (Issue #225).

Geprüft werden der signierte Rückmeldelink (keine Bestätigung durch bloßes Aufrufen, Missbrauch,
Ablauf, fremde Sitzung, Ratenbegrenzung), die Rückmeldung in die Anwesenheit mit Zeitstempel und
Herkunft, die automatische Vertretungsanfrage, der Zustellweg Brief mit Serienbrief, die Übersicht
im Sitzungsdienst mit Erinnerung und der Ladungsnachweis.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core import mail, signing
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionInvitationDispatch,
    SessionInvitationRecipient,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import (
    invitation_response_service,
    invitation_service,
    invitation_token,
    privacy_service,
    reminder_service,
)

pytestmark = pytest.mark.django_db

GRUND = "Arzttermin, bitte vertraulich behandeln"


@dataclass
class Welt:
    tenant: SessionTenant
    meeting: SessionMeeting
    staff: SessionUser
    staff_client: Client
    member: SessionPerson
    substitute: SessionPerson
    chair: SessionPerson
    letter: SessionPerson


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


def _person(tenant: SessionTenant, given: str, family: str, email: str = "", **extra: Any) -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name=given, family_name=family, email=email, **extra)


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", contact_email="rathaus@musterstadt.example"
    )
    role = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
        can_manage_attendance=True,
    )
    user = cast(Any, UserFactory)(email="sitzungsdienst@example.org")
    staff = SessionUser.objects.create(user=user, tenant=tenant)
    staff.roles.add(role)
    staff_client = Client()
    staff_client.force_login(user)

    org = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", invitation_period_days=7)
    member = _person(tenant, "Max", "Mitglied", "mitglied@example.org", form_of_address="Herr")
    substitute = _person(tenant, "Vera", "Vertretung", "vertretung@example.org")
    chair = _person(tenant, "Clara", "Vorsitz", "vorsitz@example.org")
    letter = _person(tenant, "Bruno", "Brief", "", delivery_channel="letter", form_of_address="Herr")
    cast(Any, letter).set_address_encrypted("Postweg 1\n12345 Musterstadt")
    letter.save()
    SessionOrganizationMembership.objects.create(organization=org, person=member, role="member")
    SessionOrganizationMembership.objects.create(organization=org, person=chair, role="chair")
    SessionOrganizationMembership.objects.create(organization=org, person=letter, role="member")
    SessionOrganizationMembership.objects.create(
        organization=org, person=substitute, role="member", substitute_for=member, has_voting_rights=False
    )

    start = (timezone.now() + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0)
    meeting = SessionMeeting.objects.create(
        tenant=tenant,
        name="Sitzung des Hauptausschusses",
        organization=org,
        start=start,
        location="Rathaus",
        meeting_state="scheduled",
    )
    SessionAgendaItem.objects.create(meeting=meeting, number="1", order=1, name="OEFFENTLICHER-TOP", is_public=True)
    SessionAgendaItem.objects.create(meeting=meeting, number="2", order=2, name="GEHEIMER-TOP", is_public=False)
    return Welt(tenant, meeting, staff, staff_client, member, substitute, chair, letter)


def _versenden(welt: Welt) -> SessionInvitationDispatch:
    mail.outbox = []
    return invitation_service.send_invitations(welt.meeting, sent_by=welt.staff)


def _empfaenger(dispatch: SessionInvitationDispatch, person: SessionPerson) -> SessionInvitationRecipient:
    return SessionInvitationRecipient.objects.select_related("dispatch").get(dispatch=dispatch, person=person)


def _link(recipient: SessionInvitationRecipient) -> str:
    return reverse("session_invitation_response", kwargs={"token": invitation_token.make_token(recipient)})


def _mail_an(address: str) -> Any:
    treffer = [m for m in mail.outbox if m.to == [address]]
    assert treffer, f"keine Mail an {address}"
    return treffer[-1]


def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)


# =============================================================================
# Versand: Link in der Mail, Zustellweg Brief
# =============================================================================


def test_ladung_enthaelt_persoenlichen_link_ohne_tracking(welt: Welt) -> None:
    dispatch = _versenden(welt)

    recipient = _empfaenger(dispatch, welt.member)
    nachricht = _mail_an("mitglied@example.org")
    html = str(nachricht.alternatives[0][0])
    assert recipient.channel == "email" and recipient.status == "sent" and recipient.sent_at is not None
    assert "/ladung/" in nachricht.body and "/ladung/" in html
    assert "<img" not in html, "Kein Tracking-Pixel in der Ladungsmail"
    assert sorted(name for name, _c, _m in nachricht.attachments) == ["einladung-tagesordnung.pdf", "sitzung.ics"]
    assert recipient.acknowledged_at is None, "Versand bestätigt nichts"


def test_fehlgeschlagene_zustellung_wird_nicht_als_versandt_gefuehrt(
    welt: Welt, monkeypatch: pytest.MonkeyPatch
) -> None:
    def kaputt(**_kwargs: Any) -> bool:
        raise ConnectionError("SMTP nicht erreichbar")

    monkeypatch.setattr(invitation_response_service, "send_email", kaputt)

    dispatch = _versenden(welt)

    recipient = _empfaenger(dispatch, welt.member)
    assert recipient.status == "failed" and "SMTP" in recipient.error and recipient.delivered_at is None
    assert _empfaenger(dispatch, welt.letter).status == "letter_pending"


def test_zustellweg_brief_ohne_mail_mit_serienbrief(welt: Welt) -> None:
    dispatch = _versenden(welt)

    recipient = _empfaenger(dispatch, welt.letter)
    assert recipient.channel == "letter" and recipient.status == "letter_pending"
    assert not [m for m in mail.outbox if "Brief" in " ".join(m.to)]
    assert len(mail.outbox) == 3  # Mitglied, Vorsitz, Stellvertretung – nicht der Brief-Empfänger

    base = f"/session/{welt.tenant.slug}/meetings/{welt.meeting.id}/invitation/serienbrief/{dispatch.id}"
    pdf = welt.staff_client.get(f"{base}/pdf/")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
    text = _pdf_text(pdf.content)
    assert "Postweg 1" in text and "Sehr geehrter Herr Brief" in text
    assert "GEHEIMER-TOP" in text, "Mitglied erhält den NÖ-Teil auch im Brief"

    csv = welt.staff_client.get(f"{base}/csv/").content.decode("utf-8-sig")
    assert "Postweg 1" in csv and "/ladung/" in csv and "Mitglied" not in csv.split("\r\n")[1].split(";")[3]

    response = welt.staff_client.post(f"{base}/versandt/")
    assert response.status_code == 302
    recipient.refresh_from_db()
    assert recipient.status == "letter_sent" and recipient.sent_at is not None


def test_zustellweg_im_personenformular(welt: Welt) -> None:
    role = SessionRole.objects.create(tenant=welt.tenant, name="Stammdaten", can_manage_organizations=True)
    welt.staff.roles.add(role)
    url = f"/session/{welt.tenant.slug}/persons/create/"

    ohne = welt.staff_client.post(url, {"given_name": "Ohne", "family_name": "Angabe", "is_active": "on"})
    brief = welt.staff_client.post(
        url, {"given_name": "Per", "family_name": "Post", "is_active": "on", "delivery_channel": "letter"}
    )

    assert ohne.status_code == 302 and brief.status_code == 302
    assert SessionPerson.objects.get(family_name="Angabe").delivery_channel == "email"
    assert SessionPerson.objects.get(family_name="Post").delivery_channel == "letter"


# =============================================================================
# Rückmeldelink
# =============================================================================


def test_aufruf_allein_bestaetigt_nichts(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)

    response = Client().get(_link(recipient))

    assert response.status_code == 200
    assert "Sitzung des Hauptausschusses" in response.content.decode()
    assert response["Cache-Control"] == "no-store" and response["Referrer-Policy"] == "no-referrer"
    recipient.refresh_from_db()
    assert recipient.acknowledged_at is None
    assert not SessionAttendance.objects.filter(meeting=welt.meeting, person=welt.member).exists()


def test_erhalt_bestaetigen_per_link(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)

    response = Client().post(_link(recipient), {"action": "acknowledge"})

    assert response.status_code == 302 and "gespeichert=1" in response["Location"]
    recipient.refresh_from_db()
    assert recipient.acknowledged_at is not None and recipient.acknowledged_via == "link"


def test_zusage_schreibt_anwesenheit_mit_zeitstempel_und_herkunft(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)

    Client().post(_link(recipient), {"action": "confirm"})

    attendance = SessionAttendance.objects.get(meeting=welt.meeting, person=welt.member)
    assert attendance.status == "confirmed"
    assert attendance.response_source == "link" and attendance.responded_at is not None
    recipient.refresh_from_db()
    assert recipient.acknowledged_at is not None, "Rückmeldung bestätigt zugleich den Empfang"


def test_absage_mit_grund_ist_verschluesselt_und_benachrichtigt_stellvertretung(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    mail.outbox = []

    Client().post(_link(recipient), {"action": "decline", "reason": GRUND, "substitute": "1"})

    attendance = SessionAttendance.objects.get(meeting=welt.meeting, person=welt.member)
    assert attendance.status == "declined" and attendance.substitute_requested
    assert GRUND.encode() not in bytes(attendance.response_reason_encrypted or b"")
    assert invitation_response_service.response_reason(attendance) == GRUND
    assert attendance.substitutes_notified_at is not None
    assert GRUND not in Client().get(_link(recipient)).content.decode(), "Grund nicht über den Link lesbar"

    anfrage = SessionInvitationDispatch.objects.get(meeting=welt.meeting, dispatch_type="substitution")
    vertretung = _empfaenger(anfrage, welt.substitute)
    assert vertretung.substitute_for == welt.member
    nachricht = _mail_an("vertretung@example.org")
    assert "Vertretungsanfrage" in nachricht.subject and "Max Mitglied" in nachricht.body
    assert GRUND not in nachricht.body, "Der Grund geht nicht an die Stellvertretung"
    assert invitation_token.make_token(vertretung).split(":")[0] in nachricht.body

    # Die Stellvertretung meldet über ihren eigenen Link zurück
    Client().post(_link(vertretung), {"action": "confirm"})
    assert SessionAttendance.objects.get(meeting=welt.meeting, person=welt.substitute).status == "confirmed"
    assert SessionAttendance.objects.get(meeting=welt.meeting, person=welt.member).status == "declined"


def test_vertretungsanfrage_nur_einmal_und_entwarnung_bei_zusage(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    link = _link(recipient)
    Client().post(link, {"action": "decline", "substitute": "1"})
    Client().post(link, {"action": "decline", "reason": "anderer Grund", "substitute": "1"})
    assert SessionInvitationDispatch.objects.filter(meeting=welt.meeting, dispatch_type="substitution").count() == 1

    mail.outbox = []
    Client().post(link, {"action": "confirm"})

    assert "nicht mehr erforderlich" in _mail_an("vertretung@example.org").subject
    attendance = SessionAttendance.objects.get(meeting=welt.meeting, person=welt.member)
    assert attendance.substitutes_notified_at is None and not attendance.substitute_requested
    assert invitation_response_service.response_reason(attendance) == ""


def test_wechselnde_rueckmeldungen_loesen_keine_mailflut_aus(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    link = _link(recipient)

    for _ in range(invitation_response_service.MAX_SUBSTITUTION_ROUNDS + 2):
        Client().post(link, {"action": "decline", "substitute": "1"})
        Client().post(link, {"action": "confirm"})

    anfragen = SessionInvitationDispatch.objects.filter(meeting=welt.meeting, dispatch_type="substitution")
    assert anfragen.count() == invitation_response_service.MAX_SUBSTITUTION_ROUNDS


def test_ohne_stellvertretung_wird_der_sitzungsdienst_informiert(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.chair)
    mail.outbox = []

    Client().post(_link(recipient), {"action": "decline", "reason": GRUND, "substitute": "1"})

    hinweis = _mail_an("sitzungsdienst@example.org")
    assert "Vertretung gesucht" in hinweis.subject and "Clara Vorsitz" in hinweis.body
    assert GRUND not in hinweis.body


@pytest.mark.parametrize(
    "manipulation",
    ["signatur", "fremdes_salt", "sitzung_getauscht", "schluessel_erneuert", "unsinn"],
)
def test_manipulierte_links_werden_abgewiesen(welt: Welt, manipulation: str) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    other_meeting = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Andere Sitzung", organization=welt.meeting.organization, start=welt.meeting.start
    )
    token = invitation_token.make_token(recipient)
    signer = signing.TimestampSigner(salt=invitation_token.SALT)
    if manipulation == "signatur":
        token = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    elif manipulation == "fremdes_salt":
        token = signing.TimestampSigner(salt="anderer-zweck").sign(token.rsplit(":", 2)[0])
    elif manipulation == "sitzung_getauscht":
        token = signer.sign(f"{recipient.pk.hex}.{other_meeting.pk.hex}.{recipient.response_nonce}")
    elif manipulation == "schluessel_erneuert":
        SessionInvitationRecipient.objects.filter(pk=recipient.pk).update(response_nonce="neu")
    else:
        token = "kein-gueltiges-token"

    url = reverse("session_invitation_response", kwargs={"token": token})
    assert Client().get(url).status_code == 404
    assert Client().post(url, {"action": "confirm"}).status_code == 404
    assert not SessionAttendance.objects.exists()


def test_link_wirkt_nur_auf_die_eigene_sitzung(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    other_meeting = SessionMeeting.objects.create(
        tenant=welt.tenant, name="Andere Sitzung", organization=welt.meeting.organization, start=welt.meeting.start
    )

    Client().post(_link(recipient), {"action": "confirm"})

    assert not SessionAttendance.objects.filter(meeting=other_meeting).exists()
    assert SessionAttendance.objects.filter(meeting=welt.meeting, person=welt.member).count() == 1


def test_abgelaufener_link(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    monkeypatch.setattr(invitation_token, "MAX_AGE", timedelta(seconds=-1))

    response = Client().post(_link(recipient), {"action": "confirm"})

    assert response.status_code == 410 and "abgelaufen" in response.content.decode()
    assert not SessionAttendance.objects.exists()


def test_nach_sitzungsbeginn_und_bei_absage_keine_rueckmeldung(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    link = _link(recipient)

    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(start=timezone.now() - timedelta(minutes=5))
    response = Client().post(link, {"action": "confirm"})
    assert response.status_code == 200 and "Sitzung hat begonnen" in response.content.decode()

    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(start=welt.meeting.start, cancelled=True)
    response = Client().post(link, {"action": "confirm"})
    assert "Sitzung abgesagt" in response.content.decode()
    assert not SessionAttendance.objects.exists()


def test_ratenbegrenzung_nach_ungueltigen_links(welt: Welt, monkeypatch: pytest.MonkeyPatch) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    monkeypatch.setattr(invitation_token, "RATE_LIMIT_FAILURES", 3)
    client = Client()
    for _ in range(3):
        client.get(reverse("session_invitation_response", kwargs={"token": "falsch"}))

    response = client.post(_link(recipient), {"action": "confirm"})

    assert response.status_code == 429
    assert not SessionAttendance.objects.exists()


# =============================================================================
# Sitzungsdienst: Übersicht, Erinnerung, Einträge, Nachweis
# =============================================================================


def _status_url(welt: Welt, meeting: SessionMeeting | None = None) -> str:
    meeting = meeting or welt.meeting
    return f"/session/{welt.tenant.slug}/meetings/{meeting.id}/invitation/rueckmeldungen/"


def test_uebersicht_zeigt_status_und_grund_nur_dem_sitzungsdienst(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    Client().post(_link(recipient), {"action": "decline", "reason": GRUND})

    html = welt.staff_client.get(_status_url(welt)).content.decode()
    assert "Max Mitglied" in html and "Abgesagt" in html and GRUND in html
    assert "Bruno Brief" in html and "Serienbrief (PDF)" in html

    viewer_role = SessionRole.objects.create(tenant=welt.tenant, name="Lesen", can_view_meetings=True)
    viewer_user = cast(Any, UserFactory)(email="lesen@example.org")
    viewer = SessionUser.objects.create(user=viewer_user, tenant=welt.tenant)
    viewer.roles.add(viewer_role)
    client = Client()
    client.force_login(viewer_user)
    assert client.get(_status_url(welt)).status_code == 403
    detail = client.get(f"/session/{welt.tenant.slug}/meetings/{welt.meeting.id}/")
    assert GRUND not in detail.content.decode()


def test_uebersicht_fremder_mandant_404(welt: Welt) -> None:
    other = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt")
    org = SessionOrganization.objects.create(tenant=other, name="Fremdausschuss")
    fremd = SessionMeeting.objects.create(tenant=other, name="Fremd", organization=org, start=welt.meeting.start)

    assert welt.staff_client.get(_status_url(welt, fremd)).status_code == 404
    proof = f"/session/{welt.tenant.slug}/meetings/{fremd.id}/invitation/ladungsnachweis.pdf"
    assert welt.staff_client.get(proof).status_code == 404


def test_erinnerung_nur_an_empfaenger_ohne_bestaetigung(welt: Welt) -> None:
    dispatch = _versenden(welt)
    Client().post(_link(_empfaenger(dispatch, welt.member)), {"action": "acknowledge"})
    Client().post(_link(_empfaenger(dispatch, welt.chair)), {"action": "confirm"})
    mail.outbox = []

    welt.staff_client.post(f"/session/{welt.tenant.slug}/meetings/{welt.meeting.id}/invitation/erinnern/")

    assert [m.to for m in mail.outbox] == [["vertretung@example.org"]]
    assert "/ladung/" in mail.outbox[0].body
    assert _empfaenger(dispatch, welt.substitute).reminder_count == 1


def test_sitzungsdienst_traegt_rueckmeldung_und_empfang_ein(welt: Welt) -> None:
    dispatch = _versenden(welt)
    url = f"{_status_url(welt)}{welt.letter.id}/"
    welt.staff_client.post(url, {"action": "acknowledge"})
    assert SessionInvitationRecipient.objects.get(person=welt.letter).acknowledged_at is None, "Brief noch nicht raus"
    invitation_response_service.mark_letters_sent(dispatch)

    welt.staff_client.post(url, {"action": "acknowledge"})
    welt.staff_client.post(url, {"action": "decline", "reason": "telefonisch"})

    attendance = SessionAttendance.objects.get(meeting=welt.meeting, person=welt.letter)
    assert attendance.status == "declined" and attendance.response_source == "staff"
    assert invitation_response_service.response_reason(attendance) == "telefonisch"
    row = SessionInvitationRecipient.objects.get(person=welt.letter)
    assert row.acknowledged_via == "staff"

    # Fremde Person (ohne Ladung zu dieser Sitzung) ist nicht eintragbar
    fremd = _person(welt.tenant, "Nicht", "Geladen")
    assert welt.staff_client.post(f"{_status_url(welt)}{fremd.id}/", {"action": "confirm"}).status_code == 404


def test_anwesenheitsliste_vermerkt_herkunft_sitzungsdienst(welt: Welt) -> None:
    attendance = SessionAttendance.objects.create(meeting=welt.meeting, person=welt.member)

    welt.staff_client.post(
        f"/session/{welt.tenant.slug}/attendance/{attendance.id}/update/", {"status": "confirmed", "notes": ""}
    )

    attendance.refresh_from_db()
    assert attendance.status == "confirmed" and attendance.response_source == "staff"
    assert attendance.responded_at is not None


def test_ladungsnachweis_je_person_ohne_gruende(welt: Welt) -> None:
    dispatch = _versenden(welt)
    Client().post(_link(_empfaenger(dispatch, welt.member)), {"action": "decline", "reason": GRUND})
    Client().post(_link(_empfaenger(dispatch, welt.chair)), {"action": "acknowledge"})

    response = welt.staff_client.get(
        f"/session/{welt.tenant.slug}/meetings/{welt.meeting.id}/invitation/ladungsnachweis.pdf"
    )

    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    text = _pdf_text(response.content)
    assert "Ladungsnachweis" in text
    for name in ("Max Mitglied", "Clara Vorsitz", "Bruno Brief", "Vera Vertretung"):
        assert name in text
    assert "Abgesagt" in text and "Rückmeldelink" in text and "Brief noch nicht" in text
    assert GRUND not in text
    assert SessionAuditLog.objects.filter(action="download", object_id=welt.meeting.id).exists()


# =============================================================================
# Erinnerungslauf, DSGVO-Auskunft
# =============================================================================


def test_erinnerungslauf_mit_link_auch_ohne_anwesenheitsliste(welt: Welt) -> None:
    SessionMeeting.objects.filter(pk=welt.meeting.pk).update(start=timezone.now() + timedelta(days=2))
    welt.meeting.refresh_from_db()
    _versenden(welt)
    mail.outbox = []

    counts = reminder_service.run_for_tenant(welt.tenant)

    empfaenger = sorted(addr for m in mail.outbox if "Rückmeldung" in m.subject for addr in m.to)
    assert counts["attendance_rsvp"] == 3
    assert empfaenger == ["mitglied@example.org", "vertretung@example.org", "vorsitz@example.org"]
    assert all("/ladung/" in m.body for m in mail.outbox if "Rückmeldung" in m.subject)
    assert reminder_service.run_for_tenant(welt.tenant)["attendance_rsvp"] == 0


def test_auskunft_enthaelt_ladungen_und_rueckmeldung(welt: Welt) -> None:
    recipient = _empfaenger(_versenden(welt), welt.member)
    Client().post(_link(recipient), {"action": "decline", "reason": GRUND})

    daten = privacy_service.subject_access_export(welt.tenant, welt.member)

    assert daten["ladungen"][0]["empfang_bestaetigt_ueber"] == "Rückmeldelink"
    assert daten["anwesenheiten"][0]["grund"] == GRUND
