# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die Datenauskunft (DSGVO Art. 15/20) enthält alle Beiträge und Aktivitäten der Person (Issue #453).

Bisher las der Export Aufgabenkommentare aus dem Altmodell ``TaskComment``; seit Migration 0027
entstehen sie als Aufgaben-Aktivität und fehlten in der Auskunft. Ebenso fehlten Kommentare an
Dokumenten anderer, private TOP-Notizen, Datei-Anmerkungen, Vorbereitungsnotizen und alle
Bearbeitungsvermerke.

``test_jeder_personenbezug_in_work_ist_eingeordnet`` hält die Einordnung fest: Jeder Verweis eines
Work-Modells auf Mitgliedschaft oder Konto steht im Export oder begründet außerhalb – ein neues
Modell mit Personenbezug fällt so auf.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from apps.tenants.models import Membership
from apps.work.faction.models import FactionAgendaItem, FactionDecision, FactionMeeting, FactionProtocolEntry
from apps.work.meetings.models import (
    AgendaPrivateNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
)
from apps.work.motions.models import Motion, MotionComment
from apps.work.organization.export_service import IM_EXPORT, NICHT_IM_EXPORT, VERMERKE, dsgvo_export_service
from apps.work.support.models import SupportTicket, SupportTicketMessage
from apps.work.tasks import services as task_services
from apps.work.tasks.models import Task, TaskActivity
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource


def _personenbezuege() -> set[str]:
    """Alle Verweise der App work auf Mitgliedschaft oder Konto als ``Modell.feld``."""
    ziele = {Membership, django_apps.get_model(settings.AUTH_USER_MODEL)}
    refs = set()
    for model in django_apps.get_app_config("work").get_models():
        for feld in model._meta.get_fields():
            if not (getattr(feld, "concrete", False) or getattr(feld, "many_to_many", False)):
                continue
            if getattr(feld, "auto_created", False) and not getattr(feld, "concrete", False):
                continue
            if getattr(feld, "related_model", None) in ziele:
                refs.add(f"{model.__name__}.{feld.name}")
    return refs


def test_jeder_personenbezug_in_work_ist_eingeordnet() -> None:
    refs = _personenbezuege()
    assert not set(IM_EXPORT) & set(NICHT_IM_EXPORT), "Ein Verweis ist doppelt eingeordnet"
    fehlend = refs - set(IM_EXPORT) - set(NICHT_IM_EXPORT)
    assert not fehlend, f"Nicht im DSGVO-Export eingeordnet (export_service.IM_EXPORT/NICHT_IM_EXPORT): {fehlend}"
    veraltet = (set(IM_EXPORT) | set(NICHT_IM_EXPORT)) - refs
    assert not veraltet, f"Eingeordnet, aber kein Personenbezug (mehr): {veraltet}"
    for begruendung in NICHT_IM_EXPORT.values():
        assert len(begruendung) > 20


def test_vermerke_verweisen_auf_vorhandene_felder() -> None:
    for vermerk in VERMERKE:
        model_name, feld = vermerk.ref.split(".")
        model = django_apps.get_model("work", model_name)
        assert model._meta.get_field(feld).related_model is not None
        model._meta.get_field(vermerk.when)


# ---------------------------------------------------------------------------
# Inhalte
# ---------------------------------------------------------------------------


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="person@example.org")


@pytest.fixture
def andere(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="andere@example.org")


def _export(org: Any, person: Any) -> dict[str, Any]:
    daten = dsgvo_export_service.collect_user_data(user=person.user, membership=person, organization=org)
    json.dumps(daten, default=str)  # JSON-Ausgabe bleibt serialisierbar
    return cast("dict[str, Any]", daten)


def _vermerk(daten: dict[str, Any], vorgang: str) -> list[dict[str, Any]]:
    return [v for v in daten["vermerke"] if v["action"] == vorgang]


@pytest.mark.django_db
def test_aufgabenkommentare_stammen_aus_der_aktivitaet_auch_bei_fremden_aufgaben(
    org: Any, person: Any, andere: Any
) -> None:
    fremd = Task.objects.create(organization=org, title="Plakate", created_by=andere, visibility="organization")
    eigen = Task.objects.create(organization=org, title="Rede", created_by=person)
    task_services.add_comment(fremd, person, "Ich übernehme die Hälfte")
    task_services.add_comment(eigen, person, "Entwurf steht")
    task_services.add_comment(fremd, andere, "Kommentar der anderen")

    daten = _export(org, person)
    aufgaben = {t["title"]: t for t in daten["tasks"]}
    assert [c["content"] for c in aufgaben["Plakate"]["comments"]] == ["Ich übernehme die Hälfte"]
    assert [c["content"] for c in aufgaben["Rede"]["comments"]] == ["Entwurf steht"]
    assert aufgaben["Plakate"]["description"] == "", "Beschreibung nur bei eigener Beteiligung"
    assert "Kommentar der anderen" not in json.dumps(daten, default=str)


@pytest.mark.django_db
def test_kommentare_und_rollen_an_dokumenten_anderer(org: Any, person: Any, andere: Any) -> None:
    motion = Motion.objects.create(
        organization=org, author=andere, title="Radweg", visibility="organization", responsible=person
    )
    MotionComment.objects.create(motion=motion, author=person, content="Bitte Quelle ergänzen")
    MotionComment.objects.create(motion=motion, author=andere, content="Fremder Kommentar")

    daten = _export(org, person)
    (beteiligung,) = daten["document_involvement"]
    assert beteiligung["title"] == "Radweg"
    assert beteiligung["roles"] == ["Federführung"]
    assert [c["content"] for c in beteiligung["comments"]] == ["Bitte Quelle ergänzen"]
    assert "Fremder Kommentar" not in json.dumps(daten, default=str)


@pytest.fixture
def top(org: Any) -> OParlAgendaItem:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    meeting = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=timezone.now()
    )
    return OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )


@pytest.mark.django_db
def test_notizen_anmerkungen_und_vorbereitung_mit_inhalt(org: Any, person: Any, top: OParlAgendaItem) -> None:
    privat: Any = AgendaPrivateNote(organization=org, author=person, agenda_item=top)
    privat.set_content_encrypted("Nur für mich")
    privat.save()
    anlage = AgendaSupplementaryDocument.objects.create(
        organization=org, agenda_item=top, title="Gutachten", document_type="link", url="https://example.org/g"
    )
    AgendaSupplementaryDocument.objects.filter(pk=anlage.pk).update(added_by=person)
    anmerkung: Any = FileAnnotation(organization=org, supplementary_document=anlage, author=person, page=3)
    anmerkung.set_content_encrypted("Seite 3 widerspricht dem Antrag")
    anmerkung.save()
    vorbereitung: Any = MeetingPreparation(organization=org, meeting=top.meeting, membership=person)
    vorbereitung.set_notes_encrypted("Meine Vorbereitung")
    vorbereitung.save()

    sitzungen = _export(org, person)["meetings"]
    assert [n["content"] for n in sitzungen["private_notes"]] == ["Nur für mich"]
    assert sitzungen["file_annotations"][0]["content"] == "Seite 3 widerspricht dem Antrag"
    assert sitzungen["file_annotations"][0]["file"] == "Gutachten"
    assert [d["title"] for d in sitzungen["added_documents"]] == ["Gutachten"]
    assert sitzungen["preparations"][0]["notes"] == "Meine Vorbereitung"


@pytest.mark.django_db
def test_fraktion_und_bearbeitungsvermerke(org: Any, person: Any, andere: Any) -> None:
    sitzung = FactionMeeting.objects.create(
        organization=org, title="Klausur", start=timezone.now() - timedelta(days=1), created_by=andere
    )
    top = FactionAgendaItem.objects.create(meeting=sitzung, title="Haushalt", number="1", proposed_by=person)
    FactionDecision.objects.create(agenda_item=top, result="accepted", recorded_by=person)
    rede: Any = FactionProtocolEntry(meeting=sitzung, agenda_item=top, entry_type="speech", speaker=person)
    rede.set_content_encrypted("Wir stimmen zu")
    rede.save()
    aufgabe = Task.objects.create(organization=org, title="Flyer", created_by=andere)
    TaskActivity.objects.create(task=aufgabe, actor=person, activity_type="status_changed")

    daten = _export(org, person)
    assert [p["title"] for p in daten["faction"]["proposals"]] == ["Haushalt"]
    assert [e["content"] for e in daten["faction"]["protocol_entries"]] == ["Wir stimmen zu"]
    assert [v["object"] for v in _vermerk(daten, "Abstimmungsergebnis erfasst")] == ["Klausur: TOP 1"]
    assert [v["object"] for v in _vermerk(daten, "Aufgabe bearbeitet")] == ["Flyer: Status geändert"]
    assert not _vermerk(daten, "Sitzung angelegt"), "Vermerke nur für Vorgänge der Person"


@pytest.mark.django_db
def test_eigene_nachrichten_in_tickets_anderer(org: Any, person: Any, andere: Any) -> None:
    ticket: Any = SupportTicket(organization=org, subject="Login klappt nicht", created_by=andere)
    ticket.set_description_encrypted("Beschreibung")
    ticket.save()
    nachricht: Any = SupportTicketMessage(ticket=ticket, author_membership=person)
    nachricht.set_content_encrypted("Bei mir auch")
    nachricht.save()

    daten = _export(org, person)
    assert daten["support"] == []
    assert [(m["ticket"], m["content"]) for m in daten["support_messages"]] == [("Login klappt nicht", "Bei mir auch")]


@pytest.mark.django_db
def test_pdf_zeigt_die_neuen_abschnitte(org: Any, person: Any, andere: Any) -> None:
    fremd = Task.objects.create(organization=org, title="Plakate", created_by=andere, visibility="organization")
    task_services.add_comment(fremd, person, "Kommentar im PDF")
    daten = _export(org, person)
    html = render_to_string(
        "work/profile/export/dsgvo_export.html",
        {
            "data": dsgvo_export_service.pdf_data(daten),
            "user": person.user,
            "organization": org,
            "export_date": timezone.now(),
        },
    )
    assert "Kommentar im PDF" in html
    for ueberschrift in ("12. Beteiligung an Dokumenten anderer", "13. Fraktionssitzungen", "16. Bearbeitungsvermerke"):
        assert ueberschrift in html, ueberschrift
