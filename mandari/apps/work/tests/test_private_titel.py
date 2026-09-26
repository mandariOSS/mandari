# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Titel privater Dokumente und Aufgaben erscheinen nur bei Berechtigten.

- TOP-Panel: Verknüpfen und Anzeigen von Dokumenten nur im Rahmen von ``Motion.visible_to``;
  Aufgaben am TOP nur im Rahmen der Aufgaben-Sichtbarkeit.
- Aufgabenformular: Vorbelegung „Aufgabe aus Dokument“ nur für sichtbare Dokumente.
- Dokument-Editor: verknüpfte Aufgaben nur, soweit sichtbar; Gäste sehen keine Aufgaben.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.motions.models import Motion, MotionShare
from apps.work.tasks.models import Task

PRIVATER_TITEL = "Entwurf Kandidatenliste"
PRIVATE_AUFGABE = "Gegenkandidatur vorbereiten"


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.view", "motions.edit", "tasks.view", "tasks.create"], email="autorin@example.org")


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(
        org,
        ["faction.view_public", "faction.manage", "motions.view", "tasks.view", "tasks.create"],
        email="vorsitz@example.org",
    )


@pytest.fixture
def privat(org: Any, autorin: Any) -> Motion:
    return Motion.objects.create(organization=org, author=autorin, title=PRIVATER_TITEL, visibility="private")


@pytest.fixture
def top(org: Any, vorsitz: Any) -> FactionAgendaItem:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Sitzung",
        start=timezone.now() + timedelta(days=1),
        status="planned",
        created_by=vorsitz,
    )
    return FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Wahlen", visibility="public")


def _panel(org: Any, top: FactionAgendaItem, suffix: str = "panel") -> str:
    name = "work:faction_item_panel" if suffix == "panel" else "work:faction_item_panel_action"
    return reverse(name, kwargs={"org_slug": org.slug, "meeting_id": top.meeting_id, "item_id": top.id})


@pytest.mark.django_db
def test_top_panel_verknuepft_und_zeigt_keine_privaten_dokumente(
    org: Any, vorsitz: Any, autorin: Any, privat: Motion, top: FactionAgendaItem, client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    response = client.post(_panel(org, top, "action"), {"action": "link_motion", "motion_id": str(privat.id)})

    assert response.status_code in (403, 404)
    assert not top.related_motions.filter(id=privat.id).exists()

    # Bestand: früher verknüpft – der Titel erscheint trotzdem nicht
    top.related_motions.add(privat)
    Task.objects.create(
        organization=org,
        title=PRIVATE_AUFGABE,
        created_by=autorin,
        visibility="private",
        related_faction_agenda_item=top,
    )
    html = client.get(_panel(org, top)).content.decode()
    assert PRIVATER_TITEL not in html
    assert PRIVATE_AUFGABE not in html


@pytest.mark.django_db
def test_aufgabenformular_belegt_keine_privaten_dokumente_vor(
    org: Any, vorsitz: Any, privat: Motion, client_for: Any
) -> None:
    url = reverse("work:task_create", kwargs={"org_slug": org.slug})

    html = client_for(vorsitz.user).get(url, {"related_motion": str(privat.id)}).content.decode()
    client_for(vorsitz.user).post(
        url,
        {
            "title": "Neue Aufgabe",
            "related_motion": str(privat.id),
            "status": "todo",
            "priority": "medium",
            "visibility": "organization",
        },
    )

    assert PRIVATER_TITEL not in html
    assert not Task.objects.filter(related_motion=privat).exists()


@pytest.mark.django_db
def test_editor_zeigt_nur_sichtbare_aufgaben_und_gaesten_keine(
    org: Any, autorin: Any, make_member: Any, client_for: Any
) -> None:
    dokument = Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="organization")
    kollege = make_member(org, ["motions.view", "tasks.view"], email="kollege@example.org")
    Task.objects.create(
        organization=org, title=PRIVATE_AUFGABE, created_by=autorin, visibility="private", related_motion=dokument
    )
    Task.objects.create(
        organization=org,
        title="Öffentliche Aufgabe",
        created_by=autorin,
        visibility="organization",
        related_motion=dokument,
    )
    konto: Any = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    MotionShare.objects.create(motion=dokument, scope="user", user=konto, level="view", created_by=autorin.user)
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": dokument.id})

    kollege_html = client_for(kollege.user).get(url).content.decode()
    gast_html = client_for(konto).get(url).content.decode()

    assert PRIVATE_AUFGABE not in kollege_html
    assert "Öffentliche Aufgabe" in kollege_html
    assert PRIVATE_AUFGABE not in gast_html
    assert "Öffentliche Aufgabe" not in gast_html
