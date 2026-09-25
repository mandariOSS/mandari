# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldung der Verwaltung an die Fraktion (Issue #316).

Geprüft wird der ganze Weg Einreichen → Umwandeln → Beratung → Beschluss → Rückmeldung in Work,
außerdem: Status nur über die definierten Übergänge, Eingangsbestätigung, „Beratung terminiert“
genau einmal je Termin, nicht-öffentliche Stationen ohne Details, Admin-Sammelaktionen und die
Trennung der Organisationen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.admin import SessionApplicationAdmin, SessionMeetingAdmin
from apps.session.models import (
    SessionAgendaItem,
    SessionAPIToken,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import resolution_service
from apps.work.motions import administration_feedback, ris_submission
from apps.work.motions.models import Motion, MotionAdministrationEvent, StatusTransitionError
from apps.work.notifications.models import Notification

pytestmark = pytest.mark.django_db

AUTHOR_PERMISSIONS = ["motions.view", "motions.edit", "motions.submit_to_ris", "motions.approve"]
INHALT = "<h2>Beschlussvorschlag</h2><p>Die Verwaltung plant einen Radweg.</p><h2>Begründung</h2><p>Sicherheit.</p>"
Commit = Callable[..., Any]


# =============================================================================
# Aufbau
# =============================================================================


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Iterator[Commit]:
    """``with commit(): …`` führt die on_commit-Rückmeldungen am Ende des Blocks aus."""

    def run() -> Any:
        return django_capture_on_commit_callbacks(execute=True)

    yield run


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def gremien(tenant: SessionTenant) -> dict[str, SessionOrganization]:
    return {
        "bau": SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee"),
        "rat": SessionOrganization.objects.create(tenant=tenant, name="Rat der Stadt", organization_type="council"),
    }


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, AUTHOR_PERMISSIONS, email="autorin@example.org", first_name="Anna", last_name="Autorin")


@pytest.fixture
def connected(org: Any, author: Any, tenant: SessionTenant) -> Any:
    _token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
    return ris_submission.connect_with_token(org, raw, author)


@pytest.fixture
def motion(org: Any, author: Any, connected: Any) -> Motion:
    antrag = Motion.objects.create(
        organization=org, author=author, title="Radweg Hauptstraße", visibility="organization", status="approved"
    )
    cast(Any, antrag).set_content_encrypted(INHALT)
    antrag.save()
    return antrag


def clerk_client(tenant: SessionTenant, **perms: bool) -> Client:
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle {SessionRole.objects.count()}", **perms)
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


def submit(motion: Motion, author: Any, commit: Commit) -> SessionApplication:
    with commit():
        application = ris_submission.submit_motion(motion, author, ris_submission.build_prefill(motion))
    return cast(SessionApplication, application)


def convert(application: SessionApplication, commit: Commit, **kwargs: Any) -> SessionPaper:
    from apps.session.services.application_service import convert_to_paper

    with commit():
        paper, _created = convert_to_paper(application, **kwargs)
    return paper


def meeting(tenant: SessionTenant, gremium: SessionOrganization, days: int = 14, **kwargs: Any) -> SessionMeeting:
    return SessionMeeting.objects.create(
        tenant=tenant,
        organization=gremium,
        name=kwargs.pop("name", f"Sitzung {gremium.name}"),
        start=timezone.now() + timedelta(days=days),
        **kwargs,
    )


def notifications(member: Any, title: str | None = None) -> list[Notification]:
    qs = Notification.objects.filter(recipient=member).order_by("created_at")
    if title is not None:
        qs = qs.filter(title=title)
    return list(qs)


def status_page(client: Client, motion: Motion) -> str:
    response = client.get(f"/work/{motion.organization.slug}/documents/{motion.id}/submit-ris/")
    assert response.status_code == 200
    return response.content.decode()


# =============================================================================
# Übergänge
# =============================================================================


def test_uebergaenge_nur_ueber_die_matrix(motion: Motion) -> None:
    with pytest.raises(StatusTransitionError):
        motion.transition_to("adopted")  # Freigegeben → Beschlossen ist nicht vorgesehen
    motion.refresh_from_db()
    assert motion.status == "approved"

    motion.status = "draft"
    motion.save()
    assert motion.transition_path("submitted", via={"internal_review", "approved"}) == [
        "internal_review",
        "approved",
        "submitted",
    ]
    assert motion.transition_path("submitted") is None  # ohne Zwischenschritte kein Weg
    assert motion.advance_to("submitted", via={"internal_review", "approved"}) == [
        "internal_review",
        "approved",
        "submitted",
    ]
    motion.refresh_from_db()
    assert motion.status == "submitted" and motion.submitted_at is not None


def test_statusaenderung_im_editor_nutzt_uebergaenge(motion: Motion, author: Any, client_for: Any) -> None:
    client = client_for(author.user)
    url = f"/work/{motion.organization.slug}/documents/{motion.id}/status/"
    response = client.post(url, {"status": "adopted"}, headers={"x-requested-with": "XMLHttpRequest"})
    assert response.status_code == 400
    response = client.post(url, {"status": "submitted"}, headers={"x-requested-with": "XMLHttpRequest"})
    assert response.status_code == 200 and response.json()["status"] == "submitted"


# =============================================================================
# Einreichung
# =============================================================================


def test_einreichung_setzt_status_ueber_uebergaenge_und_bestaetigt_per_mail(
    motion: Motion, author: Any, commit: Commit
) -> None:
    motion.status = "draft"  # Einreichen mit Freigaberecht gibt zugleich frei
    motion.save()
    mail.outbox.clear()

    application = submit(motion, author, commit)

    motion.refresh_from_db()
    assert motion.status == "submitted" and motion.administration_status == "submitted"
    assert motion.session_application_id == application.pk
    assert MotionAdministrationEvent.objects.filter(motion=motion, kind="submitted").count() == 1

    bestaetigung = [m for m in mail.outbox if author.user.email in m.to]
    assert len(bestaetigung) == 1
    assert application.reference in bestaetigung[0].subject
    assert "Stadt Musterstadt" in bestaetigung[0].body
    assert f"/documents/{motion.id}/submit-ris/" in bestaetigung[0].body


def test_mailfehler_bricht_die_einreichung_nicht_ab(
    motion: Motion, author: Any, commit: Commit, monkeypatch: pytest.MonkeyPatch
) -> None:
    def kaputt(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("SMTP nicht erreichbar")

    monkeypatch.setattr("apps.work.organization.emails.send_org_email", kaputt)
    application = submit(motion, author, commit)
    motion.refresh_from_db()
    assert motion.status == "submitted" and motion.session_application_id == application.pk


# =============================================================================
# Ende-zu-Ende: Einreichen → Umwandeln → Beratung → Beschluss → Rückmeldung
# =============================================================================


def test_e2e_einreichen_umwandeln_beschluss_rueckmeldung(
    org: Any,
    author: Any,
    motion: Motion,
    tenant: SessionTenant,
    gremien: dict[str, SessionOrganization],
    commit: Commit,
    client_for: Any,
) -> None:
    fraktion = client_for(author.user)
    verwaltung = clerk_client(
        tenant,
        can_view_applications=True,
        can_process_applications=True,
        can_create_papers=True,
        can_edit_papers=True,
        can_view_papers=True,
        can_edit_meetings=True,
        can_view_meetings=True,
    )

    # 1. Work: einreichen über die Oberfläche
    with commit():
        response = fraktion.post(
            f"/work/{org.slug}/documents/{motion.id}/submit-ris/",
            {
                "title": motion.title,
                "application_type": "motion",
                "resolution_proposal": "Die Verwaltung plant einen Radweg.",
                "justification": "Sicherheit.",
                "confirm": "on",
            },
        )
    assert response.status_code == 302
    application = SessionApplication.objects.get(tenant=tenant)
    motion.refresh_from_db()
    assert motion.status == "submitted"

    # 2. Session: Eingang bestätigen und in eine Vorlage umwandeln
    with commit():
        verwaltung.post(
            f"/session/{tenant.slug}/applications/{application.id}/process/",
            {"status": "received", "processing_notes": "Intern: Rücksprache mit Amt 61 nötig."},
        )
    motion.refresh_from_db()
    assert motion.status == "at_admin"
    eingang = notifications(author, "Verwaltung: Eingegangen")
    assert len(eingang) == 1
    assert "Amt 61" not in eingang[0].message  # Bearbeitungsnotizen sind intern

    with commit():
        response = verwaltung.post(
            f"/session/{tenant.slug}/applications/{application.id}/convert/",
            {"main_organization": str(gremien["bau"].id)},
        )
    assert response.status_code == 302
    paper = SessionPaper.objects.get(source_application=application)
    assert paper.reference
    umgewandelt = notifications(author, "Verwaltung: In Vorlage umgewandelt")
    assert len(umgewandelt) == 1 and paper.reference in umgewandelt[0].message
    assert not notifications(author, f"{tenant.reference_label} {paper.reference}")  # keine Doppelmeldung

    # 3. Session: Beratungsfolge anlegen und terminieren
    bau_sitzung = meeting(tenant, gremien["bau"], days=10, name="3. Sitzung Bauausschuss")
    rat_sitzung = meeting(tenant, gremien["rat"], days=30, name="12. Ratssitzung")
    with commit():
        verwaltung.post(
            f"/session/{tenant.slug}/papers/{paper.id}/consultations/add/",
            {"organization": str(gremien["bau"].id), "role": "preliminary", "meeting": str(bau_sitzung.id)},
        )
        verwaltung.post(
            f"/session/{tenant.slug}/papers/{paper.id}/consultations/add/",
            {"organization": str(gremien["rat"].id), "role": "decision", "meeting": str(rat_sitzung.id)},
        )
    vorberatung, entscheidung = SessionConsultation.objects.filter(paper=paper).order_by("order")
    with commit():
        verwaltung.post(f"/session/{tenant.slug}/consultations/{vorberatung.id}/schedule/")
        verwaltung.post(f"/session/{tenant.slug}/consultations/{entscheidung.id}/schedule/")
    motion.refresh_from_db()
    assert motion.status == "on_agenda"
    assert len(notifications(author, "Beratung terminiert")) == 2

    # 4. Session: Ergebnisse und Beschluss mit Beschlussnummer
    vorberatung.refresh_from_db()
    entscheidung.refresh_from_db()
    top_bau = cast(SessionAgendaItem, vorberatung.agenda_item)
    top_rat = cast(SessionAgendaItem, entscheidung.agenda_item)
    with commit():
        top_bau.vote_result = "approved"
        top_bau.save()
    motion.refresh_from_db()
    assert motion.status == "on_agenda"  # Vorberatung entscheidet nicht
    assert len(notifications(author, "Beratungsergebnis: Angenommen")) == 1
    with commit():
        top_rat.vote_result = "approved"
        top_rat.save()
        resolution_service.assign_resolution_number(top_rat)
    top_rat.refresh_from_db()
    motion.refresh_from_db()
    assert motion.status == "adopted" and motion.administration_status == "adopted"
    beschluss = notifications(author, "Beschluss: Angenommen")
    assert len(beschluss) == 1 and "Rat der Stadt" in beschluss[0].message

    # 5. Work zeigt Drucksachennummer, Stationen mit Ergebnis und den Beschluss
    seite = status_page(fraktion, motion)
    assert paper.reference in seite
    assert "Bauausschuss" in seite and "Vorberatung" in seite
    assert "Rat der Stadt" in seite and "Entscheidung" in seite
    assert "3. Sitzung Bauausschuss" in seite and "Angenommen" in seite
    assert top_rat.resolution_number and top_rat.resolution_number in seite
    assert 'data-testid="beschluss"' in seite
    assert "Amt 61" not in seite

    editor = fraktion.get(f"/work/{org.slug}/documents/{motion.id}/").content.decode()
    assert f"{tenant.reference_label} {paper.reference}" in editor
    liste = fraktion.get(f"/work/{org.slug}/documents/").content.decode()
    assert 'data-testid="liste-drucksachennummer"' in liste and paper.reference in liste

    # Nichts wird doppelt gemeldet, wenn sich nichts Neues ergibt
    anzahl = Notification.objects.filter(recipient=author).count()
    with commit():
        administration_feedback.schedule_sync(application.pk)
    assert Notification.objects.filter(recipient=author).count() == anzahl


# =============================================================================
# „Beratung terminiert“ genau einmal je Termin
# =============================================================================


def test_beratung_terminiert_genau_einmal_je_station_und_sitzung(
    motion: Motion,
    author: Any,
    tenant: SessionTenant,
    gremien: dict[str, SessionOrganization],
    commit: Commit,
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    erste, zweite = meeting(tenant, gremien["bau"], days=7), meeting(tenant, gremien["bau"], days=21)

    with commit():
        station = SessionConsultation.objects.create(
            paper=paper, organization=gremien["bau"], meeting=erste, role="preliminary"
        )
    assert len(notifications(author, "Beratung terminiert")) == 1

    # Speichern, Umsortieren, TOP anlegen und neu nummerieren, Ergebnis: kein neuer Termin
    with commit():
        station.order = 3
        station.save()
        station.save()
        top = SessionAgendaItem.objects.create(meeting=erste, number="1", name="Radweg", paper=paper)
        station.agenda_item = top
        station.save()
        top.number = "4"
        top.save()
        top.vote_result = "deferred"
        top.save()
    assert len(notifications(author, "Beratung terminiert")) == 1

    # Auf eine andere Sitzung verschoben: neuer Termin
    with commit():
        station.agenda_item = None
        station.meeting = zweite
        station.result = "pending"
        station.save()
    termine = notifications(author, "Beratung terminiert")
    assert len(termine) == 2
    assert timezone.localtime(zweite.start).strftime("%d.%m.%Y") in termine[1].message

    # Dieselbe Sitzung nochmals gespeichert: bleibt bei zwei Meldungen
    with commit():
        station.save()
        zweite.name = "Sitzung (verlegt)"
        zweite.save()
    assert len(notifications(author, "Beratung terminiert")) == 2


# =============================================================================
# Nicht-öffentliche Beratung
# =============================================================================


def test_noe_station_zeigt_nur_nicht_oeffentlich_beraten(
    motion: Motion,
    author: Any,
    tenant: SessionTenant,
    gremien: dict[str, SessionOrganization],
    commit: Commit,
    client_for: Any,
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    geheim = meeting(tenant, gremien["rat"], name="Geheime Sondersitzung", is_public=False)
    oeffentlich = meeting(tenant, gremien["bau"], name="Öffentliche Bausitzung")

    with commit():
        SessionConsultation.objects.create(
            paper=paper, organization=gremien["bau"], meeting=oeffentlich, role="preliminary", order=1
        )
        noe_top = SessionAgendaItem.objects.create(
            meeting=oeffentlich, number="N1", name="Nichtöffentlicher Teil", paper=paper, is_public=False
        )
        entscheidung = SessionConsultation.objects.create(
            paper=paper, organization=gremien["rat"], meeting=geheim, role="decision", authoritative=True, order=2
        )
    feedback = administration_feedback.feedback_for(motion)
    assert feedback is not None
    stationen = feedback.stations
    assert [s.public for s in stationen] == [True, False, False]
    for station in stationen[1:]:
        assert (station.organization, station.meeting_name, station.start, station.result) == ("", "", None, "")

    # Beschluss in nicht-öffentlicher Sitzung: weder Status noch Benachrichtigung verraten das Ergebnis
    with commit():
        entscheidung.result = "rejected"
        entscheidung.save()
        noe_top.vote_result = "approved"
        noe_top.save()
    motion.refresh_from_db()
    assert motion.status == "on_agenda"  # nur die öffentliche Vorberatung ist terminiert
    titel = [n.title for n in notifications(author)]
    assert titel.count("Beratung terminiert") == 1
    assert not [t for t in titel if t.startswith(("Beschluss", "Beratungsergebnis"))]
    for nachricht in notifications(author):
        assert "Geheime Sondersitzung" not in nachricht.message and "Rat der Stadt" not in nachricht.message

    seite = status_page(client_for(author.user), motion)
    assert seite.count('data-testid="station-nicht-oeffentlich"') == 2
    assert "nicht-öffentlich beraten" in seite
    assert "Geheime Sondersitzung" not in seite
    assert "Rat der Stadt" not in seite
    assert "Abgelehnt" not in seite and 'data-testid="beschluss"' not in seite


def test_noe_vorlage_zeigt_weder_nummer_noch_stationen(
    motion: Motion,
    author: Any,
    tenant: SessionTenant,
    gremien: dict[str, SessionOrganization],
    commit: Commit,
    client_for: Any,
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    with commit():
        paper.is_public = False
        paper.save()
        SessionConsultation.objects.create(
            paper=paper, organization=gremien["bau"], meeting=meeting(tenant, gremien["bau"]), role="decision"
        )
    feedback = administration_feedback.feedback_for(motion)
    assert feedback is not None
    assert feedback.converted and not feedback.paper_public and feedback.paper_reference == ""
    assert [s.public for s in feedback.stations] == [False]
    assert not notifications(author, "Beratung terminiert")

    fraktion = client_for(author.user)
    seite = status_page(fraktion, motion)
    assert paper.reference not in seite and "Vorlage nicht öffentlich" in seite
    liste = fraktion.get(f"/work/{motion.organization.slug}/documents/").content.decode()
    assert paper.reference not in liste


# =============================================================================
# Beschlussergebnisse, Korrekturen, Hand-Korrekturen
# =============================================================================


@pytest.mark.parametrize(
    ("ergebnis", "status"),
    [("approved", "adopted"), ("rejected", "rejected"), ("noted", "completed"), ("withdrawn", "withdrawn")],
)
def test_beschlussergebnis_setzt_work_status(
    motion: Motion,
    author: Any,
    tenant: SessionTenant,
    gremien: dict[str, SessionOrganization],
    commit: Commit,
    ergebnis: str,
    status: str,
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    with commit():
        station = SessionConsultation.objects.create(
            paper=paper, organization=gremien["rat"], meeting=meeting(tenant, gremien["rat"]), role="decision"
        )
    with commit():
        station.result = ergebnis
        station.save()
    motion.refresh_from_db()
    assert motion.status == status


def test_abgelehnt_und_zurueckgezogen_werden_unterschieden(
    motion: Motion, author: Any, commit: Commit, org: Any
) -> None:
    application = submit(motion, author, commit)
    with commit():
        application.status = "withdrawn"
        cast(Any, application).save()
    motion.refresh_from_db()
    assert motion.status == "withdrawn" and motion.get_status_display() == "Zurückgezogen"

    zweiter = Motion.objects.create(organization=org, author=author, title="Zweiter Antrag", status="approved")
    cast(Any, zweiter).set_content_encrypted(INHALT)
    zweiter.save()
    antrag = submit(zweiter, author, commit)
    with commit():
        antrag.status = "rejected"
        cast(Any, antrag).save()
    zweiter.refresh_from_db()
    assert zweiter.status == "rejected" and zweiter.get_status_display() == "Abgelehnt"


def test_korrigiertes_ergebnis_folgt_ueber_die_uebergaenge(
    motion: Motion, author: Any, tenant: SessionTenant, gremien: dict[str, SessionOrganization], commit: Commit
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    with commit():
        station = SessionConsultation.objects.create(
            paper=paper, organization=gremien["rat"], meeting=meeting(tenant, gremien["rat"]), role="decision"
        )
        station.result = "rejected"
        station.save()
    motion.refresh_from_db()
    assert motion.status == "rejected"
    with commit():
        station.result = "approved"
        station.save()
    motion.refresh_from_db()
    assert motion.status == "adopted"


def test_hand_korrektur_der_fraktion_bleibt_stehen(
    motion: Motion, author: Any, tenant: SessionTenant, gremien: dict[str, SessionOrganization], commit: Commit
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    with commit():
        station = SessionConsultation.objects.create(
            paper=paper, organization=gremien["bau"], meeting=meeting(tenant, gremien["bau"]), role="preliminary"
        )
    motion.refresh_from_db()
    assert motion.status == "on_agenda"
    motion.transition_to("completed")  # Fraktion hält den Antrag für erledigt

    with commit():
        station.save()  # nichts Neues in Session
        administration_feedback.schedule_sync(application.pk)
    motion.refresh_from_db()
    assert motion.status == "completed"


def test_bestand_ohne_ereignisse_wird_still_uebernommen(
    motion: Motion, author: Any, tenant: SessionTenant, gremien: dict[str, SessionOrganization], commit: Commit
) -> None:
    """Vor Issue #316 eingereichte Anträge: erster Abgleich ohne Benachrichtigungsflut."""
    application = submit(motion, author, commit)
    MotionAdministrationEvent.objects.filter(motion=motion).delete()
    Motion.objects.filter(pk=motion.pk).update(administration_status="")
    with commit():
        application.status = "received"
        cast(Any, application).save()
    motion.refresh_from_db()
    assert motion.status == "at_admin"
    assert not notifications(author)
    assert MotionAdministrationEvent.objects.filter(motion=motion).exists()


# =============================================================================
# Admin-Sammelaktionen
# =============================================================================


def _admin_request() -> Any:
    request: Any = RequestFactory().post("/admin/")
    request.user = cast(Any, UserFactory)(is_staff=True, is_superuser=True)
    request.session = cast(Any, {})
    request._messages = FallbackStorage(request)
    return request


def test_admin_sammelaktion_loest_rueckmeldung_aus(motion: Motion, author: Any, commit: Commit) -> None:
    application = submit(motion, author, commit)
    model_admin: Any = SessionApplicationAdmin(SessionApplication, admin.site)
    with commit():
        model_admin.mark_in_review(_admin_request(), SessionApplication.objects.filter(pk=application.pk))
    application.refresh_from_db()
    motion.refresh_from_db()
    assert application.status == "in_review"
    assert motion.status == "at_admin"
    assert len(notifications(author, "Verwaltung: In Prüfung")) == 1

    with commit():
        model_admin.mark_rejected(_admin_request(), SessionApplication.objects.filter(pk=application.pk))
    motion.refresh_from_db()
    assert motion.status == "rejected"
    assert len(notifications(author, "Verwaltung: Abgelehnt")) == 1


def test_admin_vorlage_erstellen_nutzt_die_umwandlung(motion: Motion, author: Any, commit: Commit) -> None:
    application = submit(motion, author, commit)
    model_admin: Any = SessionApplicationAdmin(SessionApplication, admin.site)
    with commit():
        model_admin.create_paper_from_application(_admin_request(), application.pk)
    application.refresh_from_db()
    paper = SessionPaper.objects.get(source_application=application)
    assert application.status == "converted" and paper.is_public and paper.reference
    assert len(notifications(author, "Verwaltung: In Vorlage umgewandelt")) == 1


def test_admin_absage_der_sitzung_erreicht_die_zeitleiste(
    motion: Motion, author: Any, tenant: SessionTenant, gremien: dict[str, SessionOrganization], commit: Commit
) -> None:
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    sitzung = meeting(tenant, gremien["bau"])
    with commit():
        SessionConsultation.objects.create(paper=paper, organization=gremien["bau"], meeting=sitzung)
    motion.refresh_from_db()
    assert motion.status == "on_agenda"
    with commit():
        cast(Any, SessionMeetingAdmin(SessionMeeting, admin.site)).cancel_meetings(
            _admin_request(), SessionMeeting.objects.filter(pk=sitzung.pk)
        )
    motion.refresh_from_db()
    feedback = administration_feedback.feedback_for(motion)
    assert feedback is not None and feedback.stations[0].cancelled
    assert motion.status == "at_admin"


# =============================================================================
# Trennung der Organisationen
# =============================================================================


def test_fremde_organisation_sieht_nichts(
    motion: Motion,
    author: Any,
    tenant: SessionTenant,
    gremien: dict[str, SessionOrganization],
    commit: Commit,
    make_member: Any,
    client_for: Any,
) -> None:
    from apps.common.tests.factories import OrganizationFactory

    fremd = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    fremdes_mitglied = make_member(fremd, AUTHOR_PERMISSIONS + ["faction.manage"], email="fremd@example.org")
    application = submit(motion, author, commit)
    paper = convert(application, commit)
    with commit():
        SessionConsultation.objects.create(
            paper=paper, organization=gremien["bau"], meeting=meeting(tenant, gremien["bau"]), role="decision"
        )

    fremd_client = client_for(fremdes_mitglied.user)
    fremde_org_url = f"/work/{fremd.slug}/documents/{motion.id}/submit-ris/"
    assert fremd_client.get(fremde_org_url).status_code == 404
    assert fremd_client.get(f"/work/{motion.organization.slug}/documents/{motion.id}/submit-ris/").status_code in (
        302,
        403,
        404,
    )
    liste = fremd_client.get(f"/work/{fremd.slug}/documents/").content.decode()
    assert paper.reference not in liste
    assert not Notification.objects.filter(recipient=fremdes_mitglied).exists()
    assert Notification.objects.filter(recipient=author, title="Beratung terminiert").exists()


def test_rueckmeldung_nur_bei_passender_einreichender_organisation(
    motion: Motion, author: Any, tenant: SessionTenant, commit: Commit
) -> None:
    from apps.common.tests.factories import OrganizationFactory

    application = submit(motion, author, commit)
    # Wird der Antrag in Session einer anderen Organisation zugeordnet, bekommt das Dokument nichts mehr
    SessionApplication.objects.filter(pk=application.pk).update(
        submitting_organization=cast(Any, OrganizationFactory)(name="Dritte", slug="dritte")
    )
    with commit():
        application.refresh_from_db()
        application.status = "received"
        cast(Any, application).save()
    motion.refresh_from_db()
    assert motion.status == "submitted"
    assert administration_feedback.feedback_for(motion) is None
    assert not notifications(author)
