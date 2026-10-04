# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zurückgezogene und gelöschte RIS-Objekte in Work (Issue #524, ADR ``docs/adr/20260929-fremdschluessel-ris-bestand.md``).

- Verweise aus Work auf den RIS-Bestand sind nie ``CASCADE``: Wird eine Sitzung hart gelöscht, verschwinden Vorbereitung,
  Notizen und Positionen nicht still, die Löschung bricht ab.
- Eine markierte Sitzung bleibt in Work erreichbar; die Seite sagt „Zurückgezogen“ bzw. „In der Quelle gelöscht“, die
  Arbeitsdaten bleiben.
"""

import json
import re
from typing import Any

import pytest
from django.db import models
from django.db.models import ProtectedError

from apps.work.meetings.models import AgendaItemPosition, AgendaSpeechNote, MeetingPreparation
from insight_core.models import (
    REASON_NOT_PUBLIC,
    REASON_WITHDRAWN,
    OParlAgendaItem,
    OParlBody,
    OParlMeeting,
    OParlSource,
)

CONFIG_RE = re.compile(r'<script[^>]*id="prepare-config"[^>]*>(.*?)</script>', re.S)

pytestmark = pytest.mark.django_db


@pytest.fixture
def meeting(org: Any) -> OParlMeeting:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/1", body=body, name="Rat")
    OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )
    return meeting


def _vorbereitung(org: Any, meeting: OParlMeeting, client: Any) -> tuple[str, dict[str, Any]]:
    antwort = client.get(f"/work/{org.slug}/meetings/{meeting.id}/prepare/")
    assert antwort.status_code == 200
    html = antwort.content.decode()
    treffer = CONFIG_RE.search(html)
    assert treffer
    return html, json.loads(treffer.group(1))


def test_harte_loeschung_der_sitzung_bricht_bei_arbeitsdaten_ab(org: Any, meeting: OParlMeeting) -> None:
    MeetingPreparation.objects.create(organization=org, meeting=meeting)
    punkt = meeting.agenda_items.get()
    AgendaItemPosition.objects.create(organization=org, agenda_item=punkt, position="for")

    with pytest.raises(ProtectedError):
        meeting.delete()
    with pytest.raises(ProtectedError):
        punkt.delete()

    assert MeetingPreparation.objects.filter(meeting=meeting).exists()
    assert AgendaItemPosition.objects.filter(agenda_item=punkt).exists()


def test_redebeitrag_behaelt_sich_ohne_sitzungsbezug() -> None:
    """Der Sitzungsbezug eines Redebeitrags ist optional (``SET_NULL``), der TOP-Bezug geschützt."""
    sitzung = AgendaSpeechNote._meta.get_field("meeting").remote_field
    punkt = AgendaSpeechNote._meta.get_field("agenda_item").remote_field
    assert sitzung is not None and sitzung.on_delete is models.SET_NULL
    assert punkt is not None and punkt.on_delete is models.PROTECT


@pytest.mark.parametrize(
    ("grund", "titel", "satz"),
    [
        (None, "In der Quelle gelöscht", "im Ratsinformationssystem gelöscht"),
        (REASON_WITHDRAWN, "Zurückgezogen", "Die Verwaltung hat diese Sitzung zurückgezogen."),
        (REASON_NOT_PUBLIC, "Zurückgezogen", "Die Verwaltung hat diese Sitzung zurückgezogen."),
    ],
)
def test_markierte_sitzung_zeigt_den_hinweis_und_behaelt_die_vorbereitung(
    org: Any, meeting: OParlMeeting, make_member: Any, client_for: Any, grund: str | None, titel: str, satz: str
) -> None:
    member = make_member(org, ["meetings.prepare", "meetings.view"], email="vorbereiter@example.org")
    client = client_for(member.user)
    punkt = meeting.agenda_items.get()
    AgendaItemPosition.objects.create(organization=org, agenda_item=punkt, position="against")
    html, _ = _vorbereitung(org, meeting, client)
    assert "In der Quelle gelöscht" not in html and "Zurückgezogen" not in html

    if grund is None:
        meeting.mark_deleted()
        punkt.mark_deleted()
    else:
        meeting.mark_deleted(reason=grund)
        punkt.mark_deleted(reason=grund)

    html, config = _vorbereitung(org, meeting, client)
    assert titel in html and satz in html
    assert "Vorbereitung, Notizen und Positionen bleiben erhalten." in html
    assert config["items"][0]["withdrawn"] == titel
    assert config["items"][0]["position"] == "against"

    detail = client.get(f"/work/{org.slug}/meetings/{meeting.id}/")
    assert detail.status_code == 200
    assert titel in detail.content.decode()


def test_bestehender_top_hat_keinen_hinweis(org: Any, meeting: OParlMeeting, make_member: Any, client_for: Any) -> None:
    member = make_member(org, ["meetings.prepare"], email="vorbereiter@example.org")
    _, config = _vorbereitung(org, meeting, client_for(member.user))
    assert config["items"][0]["withdrawn"] == ""
