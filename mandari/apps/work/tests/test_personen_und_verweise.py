# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Formularangaben bleiben in der eigenen Organisation und im eigenen Recht.

- Sprecher:innen, Zuständige und Freigaben verweisen nur auf aktive Mitglieder der eigenen Organisation.
- Aufgaben ändert ``tasks.manage`` nur, soweit die Aufgabe sichtbar ist; der Status ist geprüft.
- Starten und Beenden einer Fraktionssitzung verlangt ``faction.start`` – auch über die Statusauswahl.
- Verweise (TOP-Links, Vorbereitungs-Links) nur als http(s)-Adresse.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import OrganizationFactory
from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionProtocolEntry
from apps.work.meetings import services as meeting_services
from apps.work.meetings.services import PreparationError
from apps.work.tasks.models import Task, TaskShare
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource


@pytest.fixture
def fremd(db: Any, make_member: Any) -> Any:
    andere = OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")  # type: ignore[no-untyped-call]
    return make_member(andere, ["dashboard.view"], email="fremd@example.org")


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(
        org, ["faction.view_public", "faction.manage", "protocols.create", "tasks.view"], email="vorsitz@example.org"
    )


@pytest.fixture
def sitzung(org: Any, vorsitz: Any) -> dict[str, Any]:
    meeting = FactionMeeting.objects.create(
        organization=org, title="Sitzung", start=timezone.now(), status="ongoing", created_by=vorsitz
    )
    top = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt", visibility="public")
    return {"meeting": meeting, "top": top}


def _panel_action(org: Any, sitzung: dict[str, Any]) -> str:
    return reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung["meeting"].id, "item_id": sitzung["top"].id},
    )


@pytest.mark.django_db
def test_protokoll_personen_nur_aus_der_eigenen_organisation(
    org: Any, vorsitz: Any, fremd: Any, sitzung: dict[str, Any], client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    client.post(
        _panel_action(org, sitzung),
        {"action": "add_entry", "entry_type": "speech", "content": "Rede", "speaker": str(fremd.id)},
    )
    client.post(
        reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung["meeting"].id}),
        {
            "action": "add_entry",
            "entry_type": "action",
            "content": "Aufgabe",
            "agenda_item_id": str(sitzung["top"].id),
            "action_assignee": str(fremd.id),
        },
    )
    client.post(
        _panel_action(org, sitzung), {"action": "create_task", "title": "Nachfassen", "assigned_to": str(fremd.id)}
    )

    eintraege = FactionProtocolEntry.objects.filter(meeting=sitzung["meeting"])
    assert eintraege.count() == 2
    assert not eintraege.filter(speaker=fremd).exists()
    assert not eintraege.filter(action_assignee=fremd).exists()
    assert not Task.objects.filter(assigned_to=fremd).exists()


@pytest.mark.django_db
def test_aufgaben_verwaltung_nur_fuer_sichtbare_aufgaben(org: Any, make_member: Any, client_for: Any) -> None:
    autorin = make_member(org, ["tasks.view"], email="autorin@example.org")
    verwaltung = make_member(org, ["tasks.view", "tasks.manage"], email="verwaltung@example.org")
    privat = Task.objects.create(organization=org, title="Privat", created_by=autorin, visibility="private")
    sichtbar = Task.objects.create(organization=org, title="Für alle", created_by=autorin, visibility="organization")
    url = reverse("work:tasks_api", kwargs={"org_slug": org.slug})
    client = client_for(verwaltung.user)

    verboten = client.post(url, {"action": "update_status", "task_id": str(privat.id), "status": "done"})
    ungueltig = client.post(url, {"action": "update_status", "task_id": str(sichtbar.id), "status": "<b>x</b>"})

    privat.refresh_from_db()
    sichtbar.refresh_from_db()
    assert verboten.status_code == 403
    assert privat.status == "todo"
    assert ungueltig.status_code == 400
    assert sichtbar.status == "todo"


@pytest.mark.django_db
def test_aufgaben_freigabe_nur_an_eigene_mitglieder(org: Any, make_member: Any, fremd: Any, client_for: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.manage"], email="autorin@example.org")
    kollegin = make_member(org, ["tasks.view"], email="kollegin@example.org")
    task = Task.objects.create(organization=org, title="Geteilt", created_by=autorin, visibility="private")

    client_for(autorin.user).post(
        reverse("work:task_share", kwargs={"org_slug": org.slug, "task_id": task.id}),
        {"visibility": "shared", "share_with[]": [str(fremd.id), str(kollegin.id), "keine-uuid"]},
    )

    geteilt = set(TaskShare.objects.filter(task=task).values_list("membership_id", flat=True))
    assert geteilt == {kollegin.id}


@pytest.mark.django_db
def test_sitzung_starten_nur_mit_startrecht(org: Any, make_member: Any, client_for: Any) -> None:
    ersteller = make_member(org, ["faction.view_public", "faction.create"], email="ersteller@example.org")
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Sitzung",
        start=timezone.now() + timedelta(days=1),
        status="planned",
        created_by=ersteller,
    )
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    client = client_for(ersteller.user)

    client.post(url, {"action": "update_status", "status": "ongoing"})
    meeting.refresh_from_db()
    assert meeting.status == "planned"

    client.post(url, {"action": "update", "title": "Sitzung", "status": "completed"})
    meeting.refresh_from_db()
    assert meeting.status == "planned"

    client.post(url, {"action": "update_status", "status": "cancelled"})
    meeting.refresh_from_db()
    assert meeting.status == "cancelled"


@pytest.mark.django_db
@pytest.mark.parametrize("adresse", ["javascript:alert(1)", " JaVa\tScript:alert(1)", "data:text/html,x", "//ohne"])
def test_verweise_nur_als_http_adresse(
    org: Any, vorsitz: Any, sitzung: dict[str, Any], make_member: Any, client_for: Any, adresse: str
) -> None:
    sitzung["meeting"].status = "planned"
    sitzung["meeting"].save()
    response = client_for(vorsitz.user).post(
        _panel_action(org, sitzung), {"action": "add_link", "link_label": "Link", "link_url": adresse}
    )
    sitzung["top"].refresh_from_db()
    assert response.status_code == 400
    assert not sitzung["top"].reference_links

    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    ris = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/1", body=body, name="Rat")
    ris_top = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=ris, number="1", name="Haushalt", order=1
    )
    mitglied = make_member(org, ["meetings.prepare"], email="vorbereitung@example.org")
    with pytest.raises(PreparationError):
        meeting_services.add_document_link(org, ris, ris_top, mitglied, {"title": "Link", "url": adresse})
