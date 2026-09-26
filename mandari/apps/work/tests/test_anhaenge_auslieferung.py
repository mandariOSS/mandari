# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anhänge im Work-Portal gehen nur über zugriffsgeprüfte Views hinaus.

Aufgaben-, TOP-, Vorbereitungs- und Support-Anhänge sowie Briefköpfe liegen unter Präfixen,
die ``/media/`` nie ausliefert. Die Download-Views prüfen Organisation, Sichtbarkeit (private
Aufgaben, nicht-öffentliche TOPs, fremde Tickets) und Rechte.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.work.faction.models import FactionAgendaItem, FactionAgendaItemAttachment, FactionMeeting
from apps.work.meetings.models import AgendaSupplementaryDocument
from apps.work.motions.models import OrganizationLetterhead
from apps.work.support.models import SupportTicket, SupportTicketAttachment, SupportTicketMessage
from apps.work.tasks.models import Task, TaskAttachment
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

INHALT = b"%PDF-1.4 vertraulich"


@pytest.fixture(autouse=True)
def media_root(settings: Any, tmp_path: Any) -> Any:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _konto(email: str, **felder: Any) -> Any:
    return UserFactory(email=email, **felder)  # type: ignore[no-untyped-call]


def _organisation(name: str, slug: str) -> Any:
    return OrganizationFactory(name=name, slug=slug)  # type: ignore[no-untyped-call]


@pytest.fixture
def fremde_org(db: Any) -> Any:
    return _organisation("Andere Fraktion", "andere-fraktion")


@pytest.fixture
def fremdes_mitglied(fremde_org: Any, make_member: Any) -> Any:
    return make_member(
        fremde_org,
        ["tasks.view", "faction.view_public", "meetings.prepare", "support.view", "motions.view"],
        email="fremd@example.org",
    )


def _datei(name: str = "Strategiepapier.pdf") -> ContentFile[bytes]:
    return ContentFile(INHALT, name=name)


def _inhalt(response: Any) -> bytes:
    return b"".join(response.streaming_content)


# ---------------------------------------------------------------------------
# /media/ liefert Work-Anhänge nie aus
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "prefix",
    [
        "tasks/attachments/",
        "faction/attachments/",
        "meetings/documents/",
        "support/attachments/",
        "motions/letterheads/",
    ],
)
def test_media_liefert_work_anhaenge_nicht_aus(prefix: str, media_root: Any, client_for: Any) -> None:
    ziel = media_root / prefix / "2026" / "09"
    ziel.mkdir(parents=True)
    (ziel / "Strategiepapier.pdf").write_bytes(INHALT)
    ohne_mitgliedschaft = _konto("konto-ohne-org@example.org")

    response = client_for(ohne_mitgliedschaft).get(f"/media/{prefix}2026/09/Strategiepapier.pdf")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Aufgaben
# ---------------------------------------------------------------------------


@pytest.fixture
def aufgaben(org: Any, make_member: Any) -> dict[str, Any]:
    autorin = make_member(org, ["tasks.view"], email="autorin@example.org")
    kollege = make_member(org, ["tasks.view"], email="kollege@example.org")
    task = Task.objects.create(organization=org, title="Private Aufgabe", created_by=autorin, visibility="private")
    anhang = TaskAttachment.objects.create(
        task=task, file=_datei(), filename="Strategiepapier.pdf", mime_type="application/pdf", uploaded_by=autorin
    )
    return {"autorin": autorin, "kollege": kollege, "task": task, "anhang": anhang}


def _task_url(org: Any, task: Any, anhang: Any) -> str:
    return reverse(
        "work:task_attachment_download",
        kwargs={"org_slug": org.slug, "task_id": task.id, "attachment_id": anhang.id},
    )


@pytest.mark.django_db
def test_aufgaben_anhang_nur_mit_zugriff_auf_die_aufgabe(org: Any, aufgaben: dict[str, Any], client_for: Any) -> None:
    url = _task_url(org, aufgaben["task"], aufgaben["anhang"])

    erlaubt = client_for(aufgaben["autorin"].user).get(url)
    assert erlaubt.status_code == 200
    assert _inhalt(erlaubt) == INHALT
    assert erlaubt["Cache-Control"] == "private, no-store"
    assert erlaubt["X-Content-Type-Options"] == "nosniff"
    assert "Strategiepapier.pdf" in erlaubt["Content-Disposition"]
    # Der Speicherpfad verrät den Originalnamen nicht mehr
    assert "Strategiepapier" not in aufgaben["anhang"].file.name

    assert client_for(aufgaben["kollege"].user).get(url).status_code in (403, 404)


@pytest.mark.django_db
def test_aufgaben_anhang_nicht_fuer_fremde_organisation(
    org: Any, fremde_org: Any, fremdes_mitglied: Any, aufgaben: dict[str, Any], client_for: Any
) -> None:
    task, anhang = aufgaben["task"], aufgaben["anhang"]
    client = client_for(fremdes_mitglied.user)

    assert client.get(_task_url(org, task, anhang)).status_code == 403
    assert client.get(_task_url(fremde_org, task, anhang)).status_code == 404


@pytest.mark.django_db
def test_aufgaben_panel_verlinkt_die_geschuetzte_view(org: Any, aufgaben: dict[str, Any], client_for: Any) -> None:
    task = aufgaben["task"]
    url = reverse("work:task_panel", kwargs={"org_slug": org.slug, "task_id": task.id})

    html = client_for(aufgaben["autorin"].user).get(url).content.decode()

    assert _task_url(org, task, aufgaben["anhang"]) in html
    assert "/media/tasks/" not in html


# ---------------------------------------------------------------------------
# Fraktionssitzungen
# ---------------------------------------------------------------------------


@pytest.fixture
def sitzung(org: Any, make_member: Any) -> dict[str, Any]:
    vorsitz = make_member(
        org, ["faction.view_public", "faction.view_non_public", "faction.manage"], email="v@example.org"
    )
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    mitglied = make_member(org, ["faction.view_public"], email="nicht-vereidigt@example.org")
    meeting = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() + timedelta(days=2), created_by=vorsitz
    )
    top = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Personalfragen", visibility="internal")
    anhang = FactionAgendaItemAttachment.objects.create(
        agenda_item=top, file=_datei(), filename="Strategiepapier.pdf", mime_type="application/pdf", uploaded_by=vorsitz
    )
    return {"vorsitz": vorsitz, "mitglied": mitglied, "meeting": meeting, "top": top, "anhang": anhang}


def _faction_url(org: Any, s: dict[str, Any]) -> str:
    return reverse(
        "work:faction_attachment_download",
        kwargs={
            "org_slug": org.slug,
            "meeting_id": s["meeting"].id,
            "item_id": s["top"].id,
            "attachment_id": s["anhang"].id,
        },
    )


@pytest.mark.django_db
def test_top_anhang_eines_nichtoeffentlichen_tops_nur_fuer_vereidigte(
    org: Any, sitzung: dict[str, Any], client_for: Any
) -> None:
    url = _faction_url(org, sitzung)

    erlaubt = client_for(sitzung["vorsitz"].user).get(url)
    assert erlaubt.status_code == 200
    assert _inhalt(erlaubt) == INHALT

    assert client_for(sitzung["mitglied"].user).get(url).status_code == 403


@pytest.mark.django_db
def test_top_anhang_nicht_fuer_fremde_organisation(
    org: Any, sitzung: dict[str, Any], fremdes_mitglied: Any, client_for: Any
) -> None:
    assert client_for(fremdes_mitglied.user).get(_faction_url(org, sitzung)).status_code == 403


@pytest.mark.django_db
def test_top_panel_verlinkt_die_geschuetzte_view(org: Any, sitzung: dict[str, Any], client_for: Any) -> None:
    url = reverse(
        "work:faction_item_panel",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung["meeting"].id, "item_id": sitzung["top"].id},
    )

    html = client_for(sitzung["vorsitz"].user).get(url).content.decode()

    assert _faction_url(org, sitzung) in html
    assert "/media/faction/" not in html


# ---------------------------------------------------------------------------
# Sitzungsvorbereitung (ergänzende Dokumente)
# ---------------------------------------------------------------------------


@pytest.fixture
def vorbereitung(org: Any, make_member: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=timezone.now()
    )
    top = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )
    mitglied = make_member(org, ["meetings.view", "meetings.prepare"], email="vorbereitung@example.org")
    ohne_recht = make_member(org, ["meetings.view"], email="nur-lesen@example.org")
    doc = AgendaSupplementaryDocument.objects.create(
        organization=org,
        added_by=mitglied,
        agenda_item=top,
        document_type="file",
        title="Stellungnahme",
        file=_datei(),
        filename="Strategiepapier.pdf",
        mime_type="application/pdf",
    )
    return {"mitglied": mitglied, "ohne_recht": ohne_recht, "doc": doc}


def _doc_url(org: Any, doc: Any) -> str:
    return reverse("work:meeting_document_download", kwargs={"org_slug": org.slug, "doc_id": doc.id})


@pytest.mark.django_db
def test_vorbereitungs_dokument_nur_mit_vorbereitungsrecht(
    org: Any, vorbereitung: dict[str, Any], client_for: Any
) -> None:
    doc = vorbereitung["doc"]
    url = _doc_url(org, doc)

    erlaubt = client_for(vorbereitung["mitglied"].user).get(url)
    assert erlaubt.status_code == 200
    assert _inhalt(erlaubt) == INHALT
    assert erlaubt["Content-Disposition"].startswith("attachment")
    # Die Vorschau zeigt PDF eingebettet
    vorschau = client_for(vorbereitung["mitglied"].user).get(url + "?vorschau=1")
    assert vorschau["Content-Disposition"].startswith("inline")
    assert vorschau["Content-Type"] == "application/pdf"

    assert client_for(vorbereitung["ohne_recht"].user).get(url).status_code == 403
    assert doc.display_url == url
    assert "/media/" not in doc.display_url


@pytest.mark.django_db
def test_vorbereitungs_dokument_nicht_fuer_fremde_organisation(
    org: Any, fremde_org: Any, fremdes_mitglied: Any, vorbereitung: dict[str, Any], client_for: Any
) -> None:
    client = client_for(fremdes_mitglied.user)
    doc = vorbereitung["doc"]

    assert client.get(_doc_url(org, doc)).status_code == 403
    assert client.get(_doc_url(fremde_org, doc)).status_code == 404


# ---------------------------------------------------------------------------
# Support-Tickets
# ---------------------------------------------------------------------------


@pytest.fixture
def ticket(org: Any, make_member: Any) -> dict[str, Any]:
    erstellerin = make_member(org, ["support.view", "support.create"], email="ticket@example.org")
    kollegin = make_member(org, ["support.view"], email="kollegin@example.org")
    ticket = SupportTicket.objects.create(organization=org, subject="Frage", created_by=erstellerin)
    anhang = SupportTicketAttachment.objects.create(
        ticket=ticket, file=_datei(), filename="Strategiepapier.pdf", mime_type="application/pdf"
    )
    intern = SupportTicketMessage.objects.create(ticket=ticket, is_internal=True)
    interner_anhang = SupportTicketAttachment.objects.create(
        ticket=ticket, message=intern, file=_datei("intern.pdf"), filename="intern.pdf", mime_type="application/pdf"
    )
    return {
        "erstellerin": erstellerin,
        "kollegin": kollegin,
        "ticket": ticket,
        "anhang": anhang,
        "interner_anhang": interner_anhang,
    }


def _support_url(org: Any, ticket: Any, anhang: Any) -> str:
    return reverse(
        "work:support_attachment_download",
        kwargs={"org_slug": org.slug, "ticket_id": ticket.id, "attachment_id": anhang.id},
    )


@pytest.mark.django_db
def test_support_anhang_nur_fuer_ticket_berechtigte(org: Any, ticket: dict[str, Any], client_for: Any) -> None:
    url = _support_url(org, ticket["ticket"], ticket["anhang"])

    erlaubt = client_for(ticket["erstellerin"].user).get(url)
    assert erlaubt.status_code == 200
    assert _inhalt(erlaubt) == INHALT

    assert client_for(ticket["kollegin"].user).get(url).status_code in (403, 404)
    # Anhänge interner Notizen des Supports sieht die Organisation nicht
    intern = _support_url(org, ticket["ticket"], ticket["interner_anhang"])
    assert client_for(ticket["erstellerin"].user).get(intern).status_code == 404


@pytest.mark.django_db
def test_support_team_laedt_anhaenge_im_admin(ticket: dict[str, Any], client_for: Any) -> None:
    url = reverse("admin:work_supportticket_attachment", args=[ticket["ticket"].id, ticket["anhang"].id])
    support = _konto("support@example.org", is_staff=True, is_superuser=True)
    ohne_recht = _konto("staff@example.org", is_staff=True)

    erlaubt = client_for(support).get(url)
    assert erlaubt.status_code == 200
    assert _inhalt(erlaubt) == INHALT
    assert client_for(ohne_recht).get(url).status_code == 403


@pytest.mark.django_db
def test_support_detail_verlinkt_die_geschuetzte_view(org: Any, ticket: dict[str, Any], client_for: Any) -> None:
    url = reverse("work:support_detail", kwargs={"org_slug": org.slug, "ticket_id": ticket["ticket"].id})

    html = client_for(ticket["erstellerin"].user).get(url).content.decode()

    assert _support_url(org, ticket["ticket"], ticket["anhang"]) in html
    assert "/media/support/" not in html


# ---------------------------------------------------------------------------
# Briefköpfe
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_briefkopf_pdf_nur_fuer_mitglieder_mit_dokumentrecht(
    org: Any, make_member: Any, fremdes_mitglied: Any, client_for: Any
) -> None:
    leserin = make_member(org, ["motions.view"], email="leserin@example.org")
    ohne_recht = make_member(org, ["dashboard.view"], email="ohne@example.org")
    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Standard", pdf_file=_datei("kopf.pdf"))
    url = reverse("work:document_letterhead_file", kwargs={"org_slug": org.slug, "letterhead_id": briefkopf.id})

    erlaubt = client_for(leserin.user).get(url)
    assert erlaubt.status_code == 200
    assert _inhalt(erlaubt) == INHALT
    assert client_for(ohne_recht.user).get(url).status_code == 403
    assert client_for(fremdes_mitglied.user).get(url).status_code == 403


@pytest.mark.django_db
def test_konto_ohne_mitgliedschaft_erhaelt_keine_anhaenge(org: Any, aufgaben: dict[str, Any], client_for: Any) -> None:
    konto = _konto("nur-konto@example.org")
    MembershipFactory(user=konto, organization=_organisation("Dritte", "dritte"))  # type: ignore[no-untyped-call]

    response = client_for(konto).get(_task_url(org, aufgaben["task"], aufgaben["anhang"]))

    assert response.status_code == 403
