# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Standard-Tagesordnung je Organisation (Issue #872).

- Jede neue Sitzung erhält die Standard-TOPs nach dem Genehmigungs-TOP, von Hand angelegt wie aus der Reihe;
  öffentliche und nicht-öffentliche Punkte getrennt nummeriert.
- Bestehende Sitzungen bleiben unverändert, abgesagte Termine der Reihe erhalten keine Tagesordnung.
- Im Dialog „Neue Sitzung“ abwählbar; Datum, Ort, Videolink und Standardpunkte in einem Schritt.
- Pflege in den Fraktions-Einstellungen; nicht-öffentliche Standard-TOPs nur für Vereidigte.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import OrganizationFactory
from apps.work.faction.agenda import apply_standard_agenda
from apps.work.faction.generation import generate_meetings_for_schedule
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
    FactionStandardAgendaItem,
)

VORSITZ_RECHTE = ["faction.view_public", "faction.view_non_public", "faction.create", "faction.manage"]


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    member = make_member(org, VORSITZ_RECHTE, email="vorsitz@example.org")
    member.is_sworn_in = True
    member.save(update_fields=["is_sworn_in"])
    return member


@pytest.fixture
def standard(org: Any) -> list[FactionStandardAgendaItem]:
    return [
        FactionStandardAgendaItem.objects.create(organization=org, title="Beschlüsse", order=1),
        FactionStandardAgendaItem.objects.create(organization=org, title="Personalien", visibility="internal", order=2),
        FactionStandardAgendaItem.objects.create(organization=org, title="Termine", order=3),
    ]


def _tagesordnung(meeting: FactionMeeting) -> list[tuple[str, str, str]]:
    return [
        (item.number, item.title, item.visibility)
        for item in meeting.agenda_items.filter(parent__isnull=True).order_by("order")
    ]


def _anlegen(client: Any, org: Any, **extra: str) -> FactionMeeting:
    data = {"title": "Fraktionssitzung", "start_date": "2030-03-04", "start_time": "18:00", **extra}
    response = client.post(reverse("work:faction", kwargs={"org_slug": org.slug}), data)
    assert response.status_code == 302
    return FactionMeeting.objects.get(organization=org, title=data["title"])


@pytest.mark.django_db
def test_neue_sitzung_erhaelt_standard_tops_nach_dem_genehmigungs_top(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    meeting = _anlegen(client_for(vorsitz.user), org)

    assert _tagesordnung(meeting) == [
        ("1", "Tagesordnung festlegen", "public"),
        ("2", "Beschlüsse", "public"),
        ("NÖ 1", "Personalien", "internal"),
        ("3", "Termine", "public"),
    ]
    assert meeting.agenda_items.get(title="Beschlüsse").standard_item == standard[0]
    assert meeting.agenda_items.get(is_approval_item=True).standard_item is None


@pytest.mark.django_db
def test_planen_in_einem_schritt_mit_ort_videolink_und_standardpunkten(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    meeting = _anlegen(
        client_for(vorsitz.user),
        org,
        location="Fraktionsbüro",
        video_link="https://meet.example.org/fraktion",
        standard_agenda_offered="1",
        standard_agenda="on",
    )

    assert meeting.location == "Fraktionsbüro"
    assert meeting.video_link == "https://meet.example.org/fraktion"
    assert meeting.is_virtual
    assert meeting.agenda_items.filter(standard_item__isnull=False).count() == 3


@pytest.mark.django_db
def test_videolink_nur_als_web_adresse(org: Any, vorsitz: Any, client_for: Any) -> None:
    response = client_for(vorsitz.user).post(
        reverse("work:faction", kwargs={"org_slug": org.slug}),
        {"title": "Sitzung", "start_date": "2030-03-04", "start_time": "18:00", "video_link": "javascript:alert(1)"},
    )

    assert response.status_code == 302
    assert not FactionMeeting.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_standardpunkte_im_dialog_abwaehlbar(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    meeting = _anlegen(client_for(vorsitz.user), org, standard_agenda_offered="1")

    assert _tagesordnung(meeting) == [("1", "Tagesordnung festlegen", "public")]


@pytest.mark.django_db
def test_ohne_standard_tagesordnung_bleibt_alles_wie_bisher(org: Any, vorsitz: Any, client_for: Any) -> None:
    meeting = _anlegen(client_for(vorsitz.user), org)

    assert _tagesordnung(meeting) == [("1", "Tagesordnung festlegen", "public")]


@pytest.mark.django_db
def test_ohne_genehmigungs_top_beginnt_die_nummerierung_bei_eins(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    org.settings = {"faction": {"auto_create_approval_item": False}}
    org.save(update_fields=["settings"])

    meeting = _anlegen(client_for(vorsitz.user), org)

    assert _tagesordnung(meeting) == [
        ("1", "Beschlüsse", "public"),
        ("NÖ 1", "Personalien", "internal"),
        ("2", "Termine", "public"),
    ]


@pytest.mark.django_db
def test_sitzungsreihe_erzeugt_sitzungen_mit_standard_tops(org: Any, standard: list[FactionStandardAgendaItem]) -> None:
    now = timezone.now()
    schedule = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0)
    )
    ausfall = timezone.localtime(now).date() + timedelta(days=(0 - timezone.localtime(now).weekday()) % 7 + 7)
    FactionMeetingException.objects.create(
        schedule=schedule, original_date=ausfall, exception_type="cancelled", reason="Sommerpause"
    )

    stats = generate_meetings_for_schedule(schedule, now=now)

    assert stats["created"] >= 2
    assert stats["cancelled"] == 1
    for meeting in FactionMeeting.objects.filter(schedule=schedule, status="planned"):
        titel = [title for _number, title, _visibility in _tagesordnung(meeting)]
        assert titel[1:] == ["Beschlüsse", "Personalien", "Termine"], meeting.scheduled_date
    abgesagt = FactionMeeting.objects.get(schedule=schedule, scheduled_date=ausfall)
    assert not abgesagt.agenda_items.exists()


@pytest.mark.django_db
def test_bestehende_sitzungen_bleiben_unveraendert(org: Any, vorsitz: Any, client_for: Any) -> None:
    bestehend = FactionMeeting.objects.create(
        organization=org, title="Alt", start=timezone.now() + timedelta(days=2), status="planned"
    )
    FactionAgendaItem.objects.create(meeting=bestehend, number="1", title="Haushalt", order=1)
    url = reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})

    client_for(vorsitz.user).post(url, {"section": "add_standard_item", "title": "Termine"})

    assert FactionStandardAgendaItem.objects.filter(organization=org, title="Termine").exists()
    assert _tagesordnung(bestehend) == [("1", "Haushalt", "public")]


@pytest.mark.django_db
def test_uebernahme_ist_idempotent_und_laesst_abgesagte_aus(
    org: Any, standard: list[FactionStandardAgendaItem]
) -> None:
    meeting = FactionMeeting.objects.create(
        organization=org, title="Sitzung", start=timezone.now() + timedelta(days=2), status="planned"
    )
    abgesagt = FactionMeeting.objects.create(
        organization=org, title="Entfällt", start=timezone.now() + timedelta(days=9), status="cancelled"
    )

    assert apply_standard_agenda(meeting) == 3
    meeting.agenda_items.filter(title="Termine").delete()
    assert apply_standard_agenda(meeting) == 0
    assert apply_standard_agenda(abgesagt) == 0

    assert [title for _n, title, _v in _tagesordnung(meeting)] == ["Beschlüsse", "Personalien"]
    assert not abgesagt.agenda_items.exists()


@pytest.mark.django_db
def test_entfernter_standard_top_bleibt_in_angelegten_sitzungen(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    meeting = _anlegen(client_for(vorsitz.user), org)
    url = reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})

    client_for(vorsitz.user).post(url, {"section": "delete_standard_item", "item_id": str(standard[0].id)})

    assert not FactionStandardAgendaItem.objects.filter(id=standard[0].id).exists()
    item = meeting.agenda_items.get(title="Beschlüsse")
    assert item.standard_item is None


@pytest.mark.django_db
def test_einstellungen_anlegen_verschieben_und_vorschlag_uebernehmen(org: Any, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    url = reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})

    client.post(url, {"section": "add_standard_suggestion"})
    titel = list(FactionStandardAgendaItem.objects.filter(organization=org).values_list("title", flat=True))
    assert titel == ["Beschlüsse", "Politische Arbeit", "Termine", "Presse und Social Media", "Sonstiges"]

    termine = FactionStandardAgendaItem.objects.get(organization=org, title="Termine")
    client.post(url, {"section": "move_standard_item", "item_id": str(termine.id), "direction": "up"})
    client.post(url, {"section": "add_standard_item", "title": "Personalien", "visibility": "internal"})
    client.post(url, {"section": "add_standard_suggestion"})

    reihenfolge = list(FactionStandardAgendaItem.objects.filter(organization=org).values_list("title", flat=True))
    assert reihenfolge == [
        "Beschlüsse",
        "Termine",
        "Politische Arbeit",
        "Presse und Social Media",
        "Sonstiges",
        "Personalien",
    ]

    html = client.get(url).content.decode()
    assert "Standard-Tagesordnung" in html
    assert "NÖ 1" in html and "Personalien" in html


@pytest.mark.django_db
def test_nicht_oeffentliche_standard_tops_nur_fuer_vereidigte(
    org: Any, make_member: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    unvereidigt = make_member(org, VORSITZ_RECHTE, email="unvereidigt@example.org")
    client = client_for(unvereidigt.user)
    url = reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})

    html = client.get(url).content.decode()
    client.post(url, {"section": "add_standard_item", "title": "Vertragssache", "visibility": "internal"})
    client.post(url, {"section": "delete_standard_item", "item_id": str(standard[1].id)})
    liste = client.get(reverse("work:faction", kwargs={"org_slug": org.slug})).content.decode()

    assert "Personalien" not in html
    assert "1 nicht-öffentlicher Punkt" in html
    assert "Personalien" not in liste
    assert not FactionStandardAgendaItem.objects.filter(title="Vertragssache").exists()
    assert FactionStandardAgendaItem.objects.filter(id=standard[1].id).exists()


@pytest.mark.django_db
def test_fremde_standard_tops_lassen_sich_nicht_aendern(org: Any, vorsitz: Any, client_for: Any) -> None:
    andere = cast(Any, OrganizationFactory)(slug="andere-fraktion")
    fremd = FactionStandardAgendaItem.objects.create(organization=andere, title="Fremd", order=1)
    url = reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})
    client = client_for(vorsitz.user)

    client.post(url, {"section": "delete_standard_item", "item_id": str(fremd.id)})
    client.post(url, {"section": "move_standard_item", "item_id": str(fremd.id), "direction": "up"})
    client.post(url, {"section": "delete_standard_item", "item_id": "kein-uuid"})

    fremd.refresh_from_db()
    assert fremd.order == 1


@pytest.mark.django_db
def test_dialog_zeigt_die_tagesordnung_der_neuen_sitzung(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    html = client_for(vorsitz.user).get(reverse("work:faction", kwargs={"org_slug": org.slug})).content.decode()

    assert 'name="standard_agenda_offered"' in html
    assert 'name="standard_agenda"' in html
    assert "Beschlüsse" in html and "Personalien" in html and "Termine" in html
    assert 'name="video_link"' in html


@pytest.mark.django_db
def test_vorschau_nennt_den_ersten_top_wie_die_anlage(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    client = client_for(vorsitz.user)
    liste = reverse("work:faction", kwargs={"org_slug": org.slug})

    ohne_vorsitzung = client.get(liste).context["standard_agenda_preview"]["approval_title"]
    FactionMeeting.objects.create(
        organization=org, title="Letzte", start=timezone.now() - timedelta(days=7), status="completed"
    )
    mit_vorsitzung = client.get(liste).context["standard_agenda_preview"]["approval_title"]

    assert ohne_vorsitzung == "Tagesordnung festlegen"
    assert mit_vorsitzung == "Tagesordnung festlegen und letztes Protokoll genehmigen"
    assert _anlegen(client, org).agenda_items.get(is_approval_item=True).title == mit_vorsitzung


@pytest.mark.django_db
def test_einstellungen_zeigen_keine_rohen_platzhalter(
    org: Any, vorsitz: Any, standard: list[FactionStandardAgendaItem], client_for: Any
) -> None:
    org.settings = {"faction": {"first_agenda_title_with_previous": "TO und Protokoll vom {datum_letzte_sitzung}"}}
    org.save(update_fields=["settings"])

    html = (
        client_for(vorsitz.user)
        .get(reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug}))
        .content.decode()
    )

    # Die Karte der Standard-Tagesordnung (die Vorlage selbst steht weiter im Eingabefeld der Titelvorlage)
    karte = html[html.index("Diese Punkte erhält jede neu angelegte") : html.index("Neuer Standardpunkt")]
    assert "{datum_letzte_sitzung}" not in karte
    assert "Tagesordnung festlegen und letztes Protokoll genehmigen" in karte


@pytest.mark.django_db
def test_hinweis_dass_vorausgeplante_termine_unveraendert_bleiben(org: Any, vorsitz: Any, client_for: Any) -> None:
    url = reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})

    antwort = client_for(vorsitz.user).post(url, {"section": "add_standard_item", "title": "Termine"}, follow=True)

    html = antwort.content.decode()
    assert "vorausgeplante Termine der Sitzungsreihen, bleiben unverändert" in html
    assert "vorausgeplante Termine einer Sitzungsreihe, bleiben unverändert" in html


@pytest.mark.django_db
def test_zu_langer_videolink_wird_abgewiesen(org: Any, vorsitz: Any, client_for: Any) -> None:
    zu_lang = "https://meet.example.org/" + "x" * 200
    client = client_for(vorsitz.user)

    antwort = client.post(
        reverse("work:faction", kwargs={"org_slug": org.slug}),
        {"title": "Sitzung", "start_date": "2030-03-04", "start_time": "18:00", "video_link": zu_lang},
        follow=True,
    )

    assert "Der Videolink darf höchstens 200 Zeichen lang sein." in antwort.content.decode()
    assert not FactionMeeting.objects.filter(organization=org).exists()
    assert 'maxlength="200"' in client.get(reverse("work:faction", kwargs={"org_slug": org.slug})).content.decode()


@pytest.mark.django_db
def test_zu_langer_videolink_beim_bearbeiten_wird_abgewiesen(org: Any, vorsitz: Any, client_for: Any) -> None:
    meeting = _anlegen(client_for(vorsitz.user), org, video_link="https://meet.example.org/fraktion")
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})

    client_for(vorsitz.user).post(
        url,
        {
            "action": "update",
            "title": "Umbenannt",
            "is_virtual": "on",
            "video_link": "https://meet.example.org/" + "x" * 200,
        },
    )

    meeting.refresh_from_db()
    assert meeting.title == "Fraktionssitzung"
    assert meeting.video_link == "https://meet.example.org/fraktion"
