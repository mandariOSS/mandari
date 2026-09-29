# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitglied endgültig entfernen (Issue #420).

Bisher löschte das Entfernen per Kaskade alles, was die Person angelegt hatte: organisationsweit
geteilte Dokumente, Aufgaben, Fraktionssitzungen samt Protokoll, Support-Tickets. Jetzt bleiben
Inhalte der Organisation erhalten und zeigen „Ehemaliges Mitglied“; rein persönliche Daten entfallen.
Dasselbe gilt, wenn das Konto der Person gelöscht wird.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.utils import timezone

from apps.common.formatting import FORMER_MEMBER
from apps.tenants.models import Membership, Organization
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionAttendanceCertificate,
    FactionDecision,
    FactionMeeting,
    FactionProtocolEntry,
)
from apps.work.meetings import serializers as meeting_serializers
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
    PaperComment,
)
from apps.work.motions.models import Motion, MotionApproval, MotionComment, MotionDocument, MotionRevision
from apps.work.notifications.models import Notification, NotificationPreference
from apps.work.organization import services
from apps.work.organization.models import MemberAbsence
from apps.work.support.models import SupportTicket, SupportTicketMessage
from apps.work.tasks.models import Task, TaskActivity, TaskComment, TaskShare
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlPaper, OParlSource

#: Verweise auf die Mitgliedschaft, die bewusst per Kaskade mit ihr entfallen (rein persönliche Daten).
#: Jeder andere Verweis aus Work muss beim Löschen der Mitgliedschaft erhalten bleiben (SET_NULL).
PERSOENLICH = {
    "AgendaPrivateNote.author",
    "DataExport.membership",
    "MemberAbsence.membership",
    "MemberChangeRequest.requester",
    "Notification.recipient",
    "NotificationPreference.membership",
    "TaskShare.membership",
}


def test_jeder_verweis_auf_die_mitgliedschaft_ist_eingeordnet() -> None:
    """Neue Verweise dürfen Organisationsinhalte nicht wieder per Kaskade löschen."""
    kaskade = set()
    for model in django_apps.get_app_config("work").get_models():
        for field in model._meta.concrete_fields:
            if getattr(field, "related_model", None) is not Membership:
                continue
            on_delete = cast(Any, field.remote_field).on_delete
            if on_delete is models.CASCADE:
                kaskade.add(f"{model.__name__}.{field.name}")
            else:
                assert on_delete is models.SET_NULL and field.null, f"{model.__name__}.{field.name}"
    assert kaskade == PERSOENLICH


# ---------------------------------------------------------------------------
# Szenario
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, [], email="admin@example.org", is_admin=True)


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "motions.view", "tasks.view"], email="geht@example.org")


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "motions.view", "tasks.view"], email="bleibt@example.org")


@pytest.fixture
def ris(org: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=timezone.now()
    )
    item = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )
    paper = OParlPaper.objects.create(
        external_id="https://ris.example.org/paper/1", body=body, name="Antrag Spielplatz", reference="V/2026/1"
    )
    return {"meeting": meeting, "item": item, "paper": paper}


def _motion(org: Any, author: Any, title: str, visibility: str = "organization") -> Any:
    motion: Any = Motion(organization=org, author=author, title=title, visibility=visibility)
    motion.set_content_encrypted("<p>Der Rat beschließt …</p>")
    motion.save()
    return motion


def _ticket(org: Any, author: Any) -> Any:
    ticket: Any = SupportTicket(organization=org, subject="Frage zum Export", created_by=author)
    ticket.set_description_encrypted("Wie exportiere ich?")
    ticket.save()
    message: Any = SupportTicketMessage(ticket=ticket, author_membership=author)
    message.set_content_encrypted("Noch eine Ergänzung")
    message.save()
    return ticket


@pytest.fixture
def bestand(org: Any, admin: Any, mitglied: Any, kollegin: Any, ris: dict[str, Any]) -> dict[str, Any]:
    """Alles, was das Mitglied angelegt hat oder was es betrifft."""
    jetzt = timezone.now()
    b: dict[str, Any] = {}

    # Dokumente
    b["dokument"] = _motion(org, mitglied, "Mehr Bäume")
    b["privates_dokument"] = _motion(org, mitglied, "Entwurf", visibility="private")
    b["anhang"] = MotionDocument.objects.create(
        motion=b["dokument"],
        file=SimpleUploadedFile("baeume.pdf", b"%PDF-1.4"),
        filename="baeume.pdf",
        mime_type="application/pdf",
        uploaded_by=mitglied,
    )
    b["version"] = MotionRevision.objects.create(motion=b["dokument"], version=1, changed_by=mitglied)
    b["kommentar"] = MotionComment.objects.create(motion=b["dokument"], content="Gute Idee", author=mitglied)
    fremdes_dokument = _motion(org, kollegin, "Radwege")
    b["entschiedene_freigabe"] = MotionApproval.objects.create(
        motion=fremdes_dokument, approver=mitglied, approval_type="chair", approved=True, decided_at=jetzt
    )
    b["offene_freigabe"] = MotionApproval.objects.create(
        motion=fremdes_dokument, approver=mitglied, approval_type="council"
    )
    b["offene_freigabe_an_admin"] = MotionApproval.objects.create(
        motion=b["dokument"], approver=admin, approval_type="chair"
    )

    # Aufgaben
    b["zugewiesene_aufgabe"] = Task.objects.create(
        organization=org, title="Pressemitteilung", created_by=admin, assigned_to=mitglied, visibility="organization"
    )
    b["angelegte_aufgabe"] = Task.objects.create(
        organization=org, title="Ortstermin", created_by=mitglied, assigned_to=kollegin, visibility="private"
    )
    b["geteilte_aufgabe"] = Task.objects.create(
        organization=org, title="Recherche", created_by=mitglied, assigned_to=mitglied, visibility="shared"
    )
    b["freigabe"] = TaskShare.objects.create(task=b["geteilte_aufgabe"], membership=kollegin, shared_by=mitglied)
    b["persoenliche_aufgabe"] = Task.objects.create(
        organization=org, title="Zahnarzt", created_by=mitglied, assigned_to=mitglied, visibility="private"
    )
    b["aufgabenkommentar"] = TaskComment.objects.create(
        task=b["zugewiesene_aufgabe"], content="Entwurf liegt vor", author=mitglied
    )
    b["aufgabenverlauf"] = TaskActivity.objects.create(
        task=b["zugewiesene_aufgabe"], actor=mitglied, activity_type="completed"
    )

    # Fraktionssitzungen
    b["sitzung"] = FactionMeeting.objects.create(
        organization=org, title="Klausur", start=jetzt - timedelta(days=7), created_by=mitglied
    )
    top = FactionAgendaItem.objects.create(meeting=b["sitzung"], title="Haushalt")
    eintrag: Any = FactionProtocolEntry(meeting=b["sitzung"], agenda_item=top, entry_type="note", created_by=mitglied)
    eintrag.set_content_encrypted("Einigkeit über den Haushalt")
    eintrag.save()
    b["protokolleintrag"] = eintrag
    b["beschluss"] = FactionDecision.objects.create(agenda_item=top, result="accepted", recorded_by=mitglied)
    b["teilnahme_vergangen"] = FactionAttendance.objects.create(
        meeting=b["sitzung"], membership=mitglied, status="present"
    )
    kuenftig = FactionMeeting.objects.create(organization=org, title="Nächste", start=jetzt + timedelta(days=7))
    b["teilnahme_kuenftig"] = FactionAttendance.objects.create(meeting=kuenftig, membership=mitglied)
    b["nachweis"] = FactionAttendanceCertificate.objects.create(
        organization=org, membership=mitglied, period_start=date(2026, 1, 1), period_end=date(2026, 6, 30)
    )

    # Sitzungsvorbereitung
    item, paper = ris["item"], ris["paper"]
    b["vorbereitung"] = MeetingPreparation.objects.create(organization=org, meeting=ris["meeting"], membership=mitglied)
    b["diskussion"] = AgendaItemNote.objects.create(organization=org, agenda_item=item, author=mitglied)
    b["anlage"] = AgendaSupplementaryDocument.objects.create(
        organization=org, agenda_item=item, title="Gutachten", document_type="link", url="https://example.org/g"
    )
    AgendaSupplementaryDocument.objects.filter(pk=b["anlage"].pk).update(added_by=mitglied)
    b["anmerkung"] = FileAnnotation.objects.create(
        organization=org, supplementary_document=b["anlage"], author=mitglied
    )
    b["geteilte_rede"] = AgendaSpeechNote.objects.create(
        organization=org, agenda_item=item, author=mitglied, is_shared=True
    )
    b["private_rede"] = AgendaSpeechNote.objects.create(
        organization=org,
        agenda_item=OParlAgendaItem.objects.create(
            external_id="https://ris.example.org/agenda/2",
            meeting=ris["meeting"],
            number="2",
            name="Sonstiges",
            order=2,
        ),
        author=mitglied,
        is_shared=False,
    )
    b["kommentar_org"] = PaperComment.objects.create(
        paper=paper, organization=org, author=mitglied, visibility="organization"
    )
    b["kommentar_privat"] = PaperComment.objects.create(
        paper=paper, organization=org, author=mitglied, visibility="private"
    )
    b["private_notiz"] = AgendaPrivateNote.objects.create(organization=org, author=mitglied, agenda_item=item)

    # Support
    b["ticket"] = _ticket(org, mitglied)

    # Persönliches
    b["benachrichtigung"] = Notification.objects.create(recipient=mitglied, title="Hallo", message="…")
    b["einstellungen"] = NotificationPreference.objects.create(membership=mitglied)
    b["abwesenheit"] = MemberAbsence.objects.create(
        organization=org, membership=mitglied, start_date=date(2026, 8, 1), end_date=date(2026, 8, 14)
    )
    return b


def _entfernen(art: str, org: Any, admin: Any, mitglied: Any) -> None:
    if art == "mitglied_entfernen":
        services.remove_member(org, mitglied, admin.user)
    else:  # Konto gelöscht: die Mitgliedschaft entfällt per Kaskade am Benutzer
        mitglied.user.delete()


def _existiert(obj: Any) -> bool:
    return bool(type(obj).objects.filter(pk=obj.pk).exists())


def _neu(obj: Any) -> Any:
    return type(obj).objects.get(pk=obj.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("art", ["mitglied_entfernen", "konto_loeschen"])
def test_inhalte_der_organisation_bleiben_erhalten(
    art: str, org: Any, admin: Any, mitglied: Any, bestand: dict[str, Any]
) -> None:
    _entfernen(art, org, admin, mitglied)
    b = bestand

    assert not Membership.objects.filter(pk=mitglied.pk).exists()
    # Dokumente samt Anhang, Version, Kommentar, getroffener Freigabe
    for key, feld in [
        ("dokument", "author"),
        ("privates_dokument", "author"),
        ("anhang", "uploaded_by"),
        ("version", "changed_by"),
        ("kommentar", "author"),
        ("entschiedene_freigabe", "approver"),
        # Aufgaben
        ("zugewiesene_aufgabe", "assigned_to"),
        ("angelegte_aufgabe", "created_by"),
        ("geteilte_aufgabe", "created_by"),
        ("freigabe", "shared_by"),
        ("aufgabenkommentar", "author"),
        ("aufgabenverlauf", "actor"),
        # Fraktionssitzungen
        ("sitzung", "created_by"),
        ("protokolleintrag", "created_by"),
        ("beschluss", "recorded_by"),
        ("teilnahme_vergangen", "membership"),
        ("nachweis", "membership"),
        # Sitzungsvorbereitung
        ("vorbereitung", "membership"),
        ("diskussion", "author"),
        ("anlage", "added_by"),
        ("anmerkung", "author"),
        ("geteilte_rede", "author"),
        ("kommentar_org", "author"),
        # Support
        ("ticket", "created_by"),
    ]:
        assert _existiert(b[key]), key
        assert getattr(_neu(b[key]), f"{feld}_id") is None, key
    assert SupportTicketMessage.objects.get(ticket=b["ticket"]).author_membership_id is None
    assert _neu(b["zugewiesene_aufgabe"]).created_by_id == admin.pk
    assert _neu(b["angelegte_aufgabe"]).assigned_to_id is not None
    assert _neu(b["dokument"]).get_content_decrypted() == "<p>Der Rat beschließt …</p>"


@pytest.mark.django_db
@pytest.mark.parametrize("art", ["mitglied_entfernen", "konto_loeschen"])
def test_persoenliche_daten_entfallen(art: str, org: Any, admin: Any, mitglied: Any, bestand: dict[str, Any]) -> None:
    _entfernen(art, org, admin, mitglied)

    for key in [
        "offene_freigabe",
        "persoenliche_aufgabe",
        "teilnahme_kuenftig",
        "private_rede",
        "kommentar_privat",
        "private_notiz",
        "benachrichtigung",
        "einstellungen",
        "abwesenheit",
    ]:
        assert not _existiert(bestand[key]), key
    # Offene Anfragen anderer an andere bleiben
    assert _existiert(bestand["offene_freigabe_an_admin"])


@pytest.mark.django_db
def test_organisation_loeschen_funktioniert_weiter(org: Any, mitglied: Any, bestand: dict[str, Any]) -> None:
    Organization.objects.get(pk=org.pk).delete()
    assert not Motion.objects.filter(organization_id=org.pk).exists()
    assert not Task.objects.filter(organization_id=org.pk).exists()
    assert not Membership.objects.filter(pk=mitglied.pk).exists()


# ---------------------------------------------------------------------------
# Anzeige „Ehemaliges Mitglied“
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_oberflaeche_zeigt_ehemaliges_mitglied(
    org: Any, admin: Any, mitglied: Any, bestand: dict[str, Any], client_for: Any
) -> None:
    b = bestand
    services.remove_member(org, mitglied, admin.user)
    client = client_for(admin.user)

    editor = client.get(f"/work/{org.slug}/documents/{b['dokument'].id}/")
    assert editor.status_code == 200
    assert FORMER_MEMBER in editor.content.decode()

    versionen = client.get(f"/work/{org.slug}/documents/{b['dokument'].id}/revisions/")
    assert versionen.status_code == 200
    assert json.loads(versionen.content)["revisions"][0]["changed_by"] == FORMER_MEMBER

    panel = client.get(f"/work/{org.slug}/tasks/{b['zugewiesene_aufgabe'].id}/panel/")
    assert panel.status_code == 200
    assert FORMER_MEMBER in panel.content.decode()

    ticket = client.get(f"/work/{org.slug}/support/{b['ticket'].id}/")
    assert ticket.status_code == 200
    assert FORMER_MEMBER in ticket.content.decode()

    sitzung = client.get(f"/work/{org.slug}/faction/{b['sitzung'].id}/")
    assert sitzung.status_code == 200

    Motion.objects.filter(pk=b["dokument"].pk).update(status="deleted", deleted_at=timezone.now())
    papierkorb = client.get(f"/work/{org.slug}/documents/trash/")
    assert papierkorb.status_code == 200
    assert FORMER_MEMBER in papierkorb.content.decode()


@pytest.mark.django_db
def test_darstellungen_ohne_mitglied(org: Any, admin: Any, mitglied: Any, bestand: dict[str, Any]) -> None:
    b = bestand
    services.remove_member(org, mitglied, admin.user)

    assert _neu(b["teilnahme_vergangen"]).get_display_name() == FORMER_MEMBER
    assert FORMER_MEMBER in str(_neu(b["teilnahme_vergangen"]))
    assert FORMER_MEMBER in str(_neu(b["kommentar"]))
    assert FORMER_MEMBER in str(_neu(b["aufgabenkommentar"]))
    assert _neu(b["aufgabenverlauf"]).description.startswith(FORMER_MEMBER)
    assert meeting_serializers.serialize_paper_comment(_neu(b["kommentar_org"]), admin)["author"] == FORMER_MEMBER
    assert meeting_serializers.serialize_agenda_note(_neu(b["diskussion"]), admin)["author"] == FORMER_MEMBER
    assert meeting_serializers.serialize_shared_speech(_neu(b["geteilte_rede"]))["author"] == FORMER_MEMBER
    assert meeting_serializers.serialize_file_annotation(_neu(b["anmerkung"]), admin)["author"] == FORMER_MEMBER
    assert meeting_serializers.serialize_document(_neu(b["anlage"]), None, 0)["added_by"] == FORMER_MEMBER


@pytest.mark.django_db
def test_benachrichtigungen_ohne_ehemalige_autorin(
    org: Any, admin: Any, mitglied: Any, bestand: dict[str, Any], client_for: Any
) -> None:
    """Kommentieren und Freigeben an Dokumenten ehemaliger Mitglieder bricht nicht ab."""
    b = bestand
    services.remove_member(org, mitglied, admin.user)
    client = client_for(admin.user)

    kommentar = client.post(
        f"/work/{org.slug}/documents/{b['dokument'].id}/comment/",
        {"content": "Übernehmen wir"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert kommentar.status_code == 200, kommentar.content

    freigabe = client.post(
        f"/work/{org.slug}/documents/{b['dokument'].id}/approvals/{b['offene_freigabe_an_admin'].id}/decide/",
        {"decision": "approve"},
    )
    assert freigabe.status_code == 200, freigabe.content
    assert _neu(b["offene_freigabe_an_admin"]).approved is True
