# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorbereitungs-APIs arbeiten nur mit Tagesordnungspunkten und Vorlagen der eigenen Körperschaften.

Notizen, Redebeiträge, Anlagen und Vorlagen-Kommentare zu RIS-Daten einer fremden Kommune
ergeben 404 und legen nichts an.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.urls import reverse

from apps.work.meetings.models import AgendaPrivateNote, AgendaSpeechNote, PaperComment
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlPaper, OParlSource


@pytest.fixture
def ris(org: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    eigene = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt A")
    fremde = OParlBody.objects.create(external_id="https://ris.example.org/body/2", source=source, name="Stadt B")
    org.body = eigene
    org.save(update_fields=["body"])
    fremde_sitzung = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/2", body=fremde)
    fremder_top = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/2", meeting=fremde_sitzung, number="1", name="Fremd", order=1
    )
    fremde_vorlage = OParlPaper.objects.create(external_id="https://ris.example.org/paper/2", body=fremde, name="V")
    return {"meeting": fremde_sitzung, "top": fremder_top, "paper": fremde_vorlage}


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.view", "meetings.prepare"], email="vorbereitung@example.org")


def _post(client: Any, url: str, daten: dict[str, Any]) -> Any:
    return client.post(url, data=json.dumps(daten), content_type="application/json")


@pytest.mark.django_db
def test_notiz_redebeitrag_und_kommentar_nur_fuer_eigene_ris_daten(
    org: Any, mitglied: Any, ris: dict[str, Any], client_for: Any
) -> None:
    client = client_for(mitglied.user)
    kwargs = {"org_slug": org.slug, "meeting_id": ris["meeting"].id, "item_id": ris["top"].id}

    notiz = _post(client, reverse("work:meeting_private_note_api", kwargs=kwargs), {"content": "Notiz"})
    rede = _post(client, reverse("work:meeting_speech_api", kwargs=kwargs), {"content": "<p>Rede</p>"})
    lesen = client.get(reverse("work:meeting_speech_api", kwargs=kwargs))
    kommentar = _post(
        client,
        reverse("work:paper_comments_api", kwargs={"org_slug": org.slug, "paper_id": ris["paper"].id}),
        {"content": "Kommentar", "visibility": "organization"},
    )

    assert [notiz.status_code, rede.status_code, lesen.status_code, kommentar.status_code] == [404, 404, 404, 404]
    assert not AgendaPrivateNote.objects.filter(agenda_item=ris["top"]).exists()
    assert not AgendaSpeechNote.objects.filter(agenda_item=ris["top"]).exists()
    assert not PaperComment.objects.filter(paper=ris["paper"]).exists()
