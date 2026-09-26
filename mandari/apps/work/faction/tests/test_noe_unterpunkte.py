# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nicht-öffentliche Unterpunkte, Protokolleinträge und Aufgaben bleiben Vereidigten vorbehalten.

- Ein Unterpunkt ist nicht-öffentlich, wenn er selbst oder ein übergeordneter TOP es ist.
- Nicht-Vereidigte sehen NÖ-Unterpunkte weder in der Sitzung, im Panel noch in der
  öffentlichen Niederschrift oder Tagesordnung.
- Über das Panel eines öffentlichen TOPs lässt sich kein Eintrag eines anderen TOPs ändern.
- Aufgaben-Übernahme aus dem Protokoll (Liste und Vorbelegung) ohne NÖ-Einträge für Nicht-Vereidigte.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import services as faction_services
from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionProtocolEntry

GEHEIM = "Personalie Geheimhaltung"


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    member = make_member(
        org,
        ["faction.view_public", "faction.view_non_public", "faction.manage", "protocols.create", "tasks.create"],
        email="vorsitz@example.org",
    )
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


@pytest.fixture
def protokoll(org: Any, make_member: Any) -> Any:
    """Nicht vereidigt, darf aber Protokoll führen und Aufgaben anlegen."""
    return make_member(
        org,
        ["faction.view_public", "protocols.create", "protocols.view_public", "tasks.view", "tasks.create"],
        email="protokoll@example.org",
    )


@pytest.fixture
def sitzung(org: Any, vorsitz: Any) -> dict[str, Any]:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(hours=1),
        status="ongoing",
        created_by=vorsitz,
    )
    oeffentlich = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt", visibility="public")
    geheim = FactionAgendaItem.objects.create(
        meeting=meeting, number="1.1", title=GEHEIM, visibility="internal", parent=oeffentlich
    )
    nichtoeffentlich = FactionAgendaItem.objects.create(
        meeting=meeting, number="N1", title="Vertragssache", visibility="internal"
    )
    erbt = FactionAgendaItem.objects.create(
        meeting=meeting, number="N1.1", title="Erbt NÖ", visibility="public", parent=nichtoeffentlich
    )
    eintrag: Any = FactionProtocolEntry(
        meeting=meeting, agenda_item=geheim, entry_type="action", created_by=vorsitz, order=1
    )
    eintrag.set_content_encrypted("Vertrauliche Aufgabe")
    eintrag.save()
    return {
        "meeting": meeting,
        "oeffentlich": oeffentlich,
        "geheim": geheim,
        "erbt": erbt,
        "eintrag": eintrag,
    }


@pytest.mark.django_db
def test_unterpunkte_erben_den_nichtoeffentlichen_teil(protokoll: Any, sitzung: dict[str, Any]) -> None:
    from apps.work.faction.visibility import can_view_item

    assert not can_view_item(sitzung["geheim"], protokoll)
    assert not can_view_item(sitzung["erbt"], protokoll)
    assert can_view_item(sitzung["oeffentlich"], protokoll)


@pytest.mark.django_db
def test_sitzung_und_panel_zeigen_keine_noe_unterpunkte(
    org: Any, protokoll: Any, vorsitz: Any, sitzung: dict[str, Any], client_for: Any
) -> None:
    meeting = sitzung["meeting"]
    detail = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    panel = reverse(
        "work:faction_item_panel",
        kwargs={"org_slug": org.slug, "meeting_id": meeting.id, "item_id": sitzung["oeffentlich"].id},
    )

    for url in (detail, panel):
        assert GEHEIM not in client_for(protokoll.user).get(url).content.decode(), url
    assert GEHEIM in client_for(vorsitz.user).get(detail).content.decode()


@pytest.mark.django_db
def test_oeffentliche_niederschrift_und_tagesordnung_ohne_noe_unterpunkte(
    monkeypatch: pytest.MonkeyPatch, vorsitz: Any, sitzung: dict[str, Any]
) -> None:
    monkeypatch.setattr(faction_services, "html_to_pdf", lambda html: html.encode())
    meeting = sitzung["meeting"]

    oeffentlich = faction_services.build_faction_protocol_pdf(meeting, internal=False).decode()
    intern = faction_services.build_faction_protocol_pdf(meeting, internal=True).decode()
    tagesordnung = faction_services.FactionMeetingEmailService().build_agenda_pdf(meeting, include_internal=False)

    assert GEHEIM not in oeffentlich
    assert "Vertrauliche Aufgabe" not in oeffentlich
    assert GEHEIM in intern
    assert GEHEIM not in tagesordnung.decode()


@pytest.mark.django_db
def test_panel_aendert_keine_eintraege_anderer_tops(
    org: Any, protokoll: Any, sitzung: dict[str, Any], client_for: Any
) -> None:
    meeting = sitzung["meeting"]
    url = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": org.slug, "meeting_id": meeting.id, "item_id": sitzung["oeffentlich"].id},
    )

    response = client_for(protokoll.user).post(
        url, {"action": "edit_entry", "entry_id": str(sitzung["eintrag"].id), "content": "Überschrieben"}
    )

    sitzung["eintrag"].refresh_from_db()
    assert response.status_code in (403, 404)
    assert cast(Any, sitzung["eintrag"]).get_content_decrypted() == "Vertrauliche Aufgabe"


@pytest.mark.django_db
def test_aufgaben_aus_dem_protokoll_ohne_noe_eintraege(
    org: Any, protokoll: Any, vorsitz: Any, sitzung: dict[str, Any], client_for: Any
) -> None:
    eintrag = sitzung["eintrag"]
    liste = client_for(protokoll.user).get(reverse("work:tasks_import", kwargs={"org_slug": org.slug})).json()
    vorbelegt = client_for(protokoll.user).get(
        reverse("work:task_create", kwargs={"org_slug": org.slug}), {"from_protocol": str(eintrag.id)}
    )

    assert str(eintrag.id) not in [item["id"] for item in liste["items"]]
    assert "Vertrauliche Aufgabe" not in vorbelegt.content.decode()
    liste_vorsitz = client_for(vorsitz.user).get(reverse("work:tasks_import", kwargs={"org_slug": org.slug})).json()
    assert str(eintrag.id) in [item["id"] for item in liste_vorsitz["items"]]
