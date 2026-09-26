# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Geteilte Redebeiträge gehen nur bereinigt an andere Mitglieder.

Der Redetext stammt aus dem Editor (HTML) und wird beim Speichern auf dessen Positivliste
reduziert; auch Altbestand geht über die Redebeitrags-API und die Vorbereitungsseite nur
bereinigt hinaus. Im Browser wird der Text ohne ``innerHTML`` in Klartext umgewandelt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.meetings.models import AgendaSpeechNote
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

BOESE = '<p>Rede</p><img src="x" onerror="alert(1)"><script>alert(2)</script>'
CONFIG_RE = re.compile(r'<script[^>]*id="prepare-config"[^>]*>(.*?)</script>', re.S)
FRONTEND = Path(__file__).resolve().parents[4] / "frontend"


@pytest.fixture
def sitzung(org: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/1", body=body, name="Rat")
    item = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )
    return {"meeting": meeting, "item": item}


@pytest.fixture
def rednerin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.view", "meetings.prepare"], email="rednerin@example.org")


@pytest.fixture
def kollege(org: Any, make_member: Any) -> Any:
    return make_member(org, ["meetings.view", "meetings.prepare"], email="kollege@example.org")


def _speech_url(org: Any, sitzung: dict[str, Any]) -> str:
    return reverse(
        "work:meeting_speech_api",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung["meeting"].id, "item_id": sitzung["item"].id},
    )


def _frei_von_skript(text: str) -> None:
    assert "onerror" not in text
    assert "alert(2)" not in text


@pytest.mark.django_db
def test_redebeitrag_wird_beim_speichern_bereinigt(
    org: Any, sitzung: dict[str, Any], rednerin: Any, client_for: Any
) -> None:
    response = client_for(rednerin.user).post(
        _speech_url(org, sitzung),
        data=json.dumps({"content": BOESE, "is_shared": True}),
        content_type="application/json",
    )

    assert response.status_code == 200
    note = AgendaSpeechNote.objects.get(author=rednerin)
    gespeichert = str(cast(Any, note).get_content_decrypted())
    _frei_von_skript(gespeichert)
    assert "<p>Rede</p>" in gespeichert


@pytest.fixture
def altbestand(org: Any, sitzung: dict[str, Any], rednerin: Any) -> AgendaSpeechNote:
    note = AgendaSpeechNote(organization=org, author=rednerin, agenda_item=sitzung["item"], is_shared=True)
    cast(Any, note).set_content_encrypted(BOESE)
    note.save()
    return note


@pytest.mark.django_db
def test_geteilter_redebeitrag_geht_bereinigt_an_andere(
    org: Any, sitzung: dict[str, Any], kollege: Any, altbestand: AgendaSpeechNote, client_for: Any
) -> None:
    response = client_for(kollege.user).get(_speech_url(org, sitzung))

    assert response.status_code == 200
    geteilt = response.json()["shared"]
    assert len(geteilt) == 1
    _frei_von_skript(geteilt[0]["content"])
    assert "<p>Rede</p>" in geteilt[0]["content"]


@pytest.mark.django_db
def test_vorbereitungsseite_liefert_geteilte_redebeitraege_bereinigt(
    org: Any, sitzung: dict[str, Any], kollege: Any, altbestand: AgendaSpeechNote, client_for: Any
) -> None:
    html = client_for(kollege.user).get(f"/work/{org.slug}/meetings/{sitzung['meeting'].id}/prepare/").content.decode()

    match = CONFIG_RE.search(html)
    assert match
    _frei_von_skript(match.group(1))


def test_klartext_im_browser_ohne_innerhtml() -> None:
    quelle = (FRONTEND / "alpine" / "prepare-meeting.ts").read_text(encoding="utf-8")
    strip_html = quelle.split("stripHtml(html", 1)[1].split("\n    },", 1)[0]

    assert ".innerHTML" not in strip_html
    assert "new DOMParser()" in strip_html


def test_versionsvergleich_parst_ohne_innerhtml() -> None:
    quelle = (FRONTEND / "editor" / "diff.ts").read_text(encoding="utf-8")
    html_to_blocks = quelle.split("function htmlToBlocks", 1)[1].split("\n}\n", 1)[0]

    assert ".innerHTML" not in html_to_blocks
    assert "new DOMParser()" in html_to_blocks
    assert "escapeHtml(cls)" in html_to_blocks
