# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lesewege der Sitzungsvorbereitung prüfen den heutigen Stand.

- Diskussionsnotizen kannten bis zum Umbau der Sitzungsvorbereitung die Sichtbarkeit „Nur ich“. Solche Zeilen
  gibt es an TOPs ohne Vorlage noch; im Thread und in den Zählern der Seite liest sie nur, wer sie geschrieben hat.
- Der Titel eines als Redebeitrag verknüpften Dokuments erscheint auf der Vorbereitungsseite nur mit aktuellem
  Zugriff, wie in der Schnittstelle des Redebeitrags.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.meetings import selectors, services
from apps.work.meetings.models import AgendaItemNote
from apps.work.meetings.serializers import build_prepare_config
from apps.work.motions.models import Motion
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db

NUR_ICH = "Meine Einschätzung, nicht für die Fraktion"
FUER_ALLE = "Abgestimmte Linie der Fraktion"
DOKUMENT = "Rede zum Nachtragshaushalt"


@pytest.fixture
def meeting(org: Any) -> OParlMeeting:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    return OParlMeeting.objects.create(
        external_id="https://ris.example.org/meeting/1", body=body, name="Rat", start=timezone.now() + timedelta(days=3)
    )


@pytest.fixture
def item(meeting: OParlMeeting) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.view", "meetings.prepare", "motions.view"], email="autorin@example.org")


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.view", "meetings.prepare", "motions.view"], email="kollegin@example.org")


def _notiz(org: Any, item: OParlAgendaItem, author: Any, text: str, visibility: str) -> AgendaItemNote:
    note = AgendaItemNote(organization=org, agenda_item=item, author=author, visibility=visibility)
    cast(Any, note).set_content_encrypted(text)
    note.save()
    return note


def _config(org: Any, meeting: OParlMeeting, member: Any, **kwargs: Any) -> dict[str, Any]:
    """Seitenkonfiguration der Vorbereitung, wie die Ansicht sie für ``member`` baut."""
    preparation = services.ensure_preparation(org, meeting, member)
    data = selectors.load_preparation_data(org, member, meeting)
    return build_prepare_config(
        organization=org,
        meeting=meeting,
        preparation=preparation,
        data=data,
        current_user_name="Test",
        account_id=str(member.user.pk),
        **kwargs,
    )


def _thread(client_for: Any, org: Any, member: Any, meeting: OParlMeeting, item: OParlAgendaItem) -> list[str]:
    url = reverse("work:meeting_notes_api", kwargs={"org_slug": org.slug, "meeting_id": meeting.id, "item_id": item.id})
    response = client_for(member.user).get(url)
    assert response.status_code == 200
    return [note["content"] for note in response.json()["notes"]]


# --- Altnotizen „Nur ich“ ---------------------------------------------------------------------------------------


def test_altnotiz_nur_ich_liest_im_thread_nur_die_autorin(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, autorin: Any, kollegin: Any, client_for: Any
) -> None:
    privat = _notiz(org, item, autorin, NUR_ICH, "private")
    offen = _notiz(org, item, autorin, FUER_ALLE, "organization")

    assert _thread(client_for, org, kollegin, meeting, item) == [FUER_ALLE]
    assert set(_thread(client_for, org, autorin, meeting, item)) == {NUR_ICH, FUER_ALLE}

    assert set(selectors.thread_notes(org, item.id, autorin)) == {privat, offen}
    assert selectors.thread_notes(org, item.id, kollegin) == [offen]
    assert selectors.thread_notes(org, item.id) == [offen], "ohne Mitglied nur, was für die Organisation gilt"


def test_altnotiz_ohne_autorin_bleibt_fuer_alle_verborgen(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, autorin: Any, kollegin: Any, client_for: Any
) -> None:
    """Die Autorin wurde entfernt (Autor geleert): Die Notiz gehört niemandem mehr und erscheint nicht."""
    privat = _notiz(org, item, autorin, NUR_ICH, "private")
    AgendaItemNote.objects.filter(pk=privat.pk).update(author=None)

    assert _thread(client_for, org, kollegin, meeting, item) == []
    assert selectors.thread_notes(org, item.id, kollegin) == []


def test_altnotiz_nur_ich_zaehlt_auf_der_seite_nur_fuer_die_autorin(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, autorin: Any, kollegin: Any
) -> None:
    _notiz(org, item, autorin, NUR_ICH, "private")
    _notiz(org, item, autorin, FUER_ALLE, "organization")

    assert _config(org, meeting, autorin, membership=autorin)["items"][0]["notesCount"] == 2
    assert _config(org, meeting, kollegin, membership=kollegin)["items"][0]["notesCount"] == 1


# --- Dokument als Redebeitrag -----------------------------------------------------------------------------------


def _verweis(org: Any, meeting: OParlMeeting, member: Any, **kwargs: Any) -> Any:
    return _config(org, meeting, member, **kwargs)["items"][0]["speechLinkedDocument"]


def test_titel_des_verknuepften_dokuments_nur_mit_aktuellem_zugriff(
    org: Any, meeting: OParlMeeting, item: OParlAgendaItem, autorin: Any, kollegin: Any, client_for: Any
) -> None:
    dokument = Motion.objects.create(
        organization=org, author=kollegin, title=DOKUMENT, status="review", visibility="organization"
    )
    services.save_speech_note(org, item, autorin, {"linked_document": str(dokument.id)})
    seite = reverse("work:meeting_prepare", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})

    assert DOKUMENT in client_for(autorin.user).get(seite).content.decode()
    assert _verweis(org, meeting, autorin, membership=autorin) == {"id": str(dokument.id), "title": DOKUMENT}

    # Die Kollegin stellt ihr Dokument privat: Die Verknüpfung bleibt, der Titel nicht
    Motion.objects.filter(pk=dokument.pk).update(visibility="private", title=f"{DOKUMENT} (neue Fassung)")

    assert DOKUMENT not in client_for(autorin.user).get(seite).content.decode()
    assert _verweis(org, meeting, autorin, membership=autorin) == {
        "id": str(dokument.id),
        "title": None,
        "access": False,
    }
    # Ohne Angabe des Mitglieds nie ein Titel
    assert _verweis(org, meeting, autorin)["title"] is None
