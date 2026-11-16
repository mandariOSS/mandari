# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Seite eines genehmigten Fraktionsprotokolls: nur der öffentliche Teil.

Ein Unterpunkt gehört zum nichtöffentlichen Teil, wenn er selbst oder ein übergeordneter Tagesordnungspunkt es
ist (``apps.work.faction.visibility``). Das gilt auch für die Seite ohne Anmeldung: Sie zeigt von ihm weder Titel
noch Beschreibung noch Protokolleinträge, auch wenn an seiner eigenen Zeile „öffentlich“ steht.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionProtocolEntry
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db

OEFFENTLICH = "Haushalt 2027"
UNTERPUNKT = "Zuschuss Sportverein"
NICHTOEFFENTLICH = "Vertragssache Grundstück"
ERBT = "Angebot der Bietergemeinschaft"
ERBT_TIEF = "Preisspiegel der Angebote"
BESCHREIBUNG = "Kaufpreis und Vertragspartner"
EINTRAG = "Die Fraktion lehnt das Angebot ab"
EINTRAG_OEFFENTLICH = "Der Zuschuss wird befürwortet"


def _eintrag(meeting: FactionMeeting, item: FactionAgendaItem, text: str, order: int) -> None:
    eintrag: Any = FactionProtocolEntry(meeting=meeting, agenda_item=item, entry_type="note", order=order)
    eintrag.set_content_encrypted(text)
    eintrag.save()


@pytest.fixture
def seite(org: Any) -> str:
    """Genehmigtes Protokoll einer veröffentlichenden Fraktion; liefert die Adresse der öffentlichen Seite."""
    quelle = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/1", source=quelle, name="Stadt Test", slug="stadt-test"
    )
    org.body = body
    org.publish_protocols = True
    org.save(update_fields=["body", "publish_protocols"])
    meeting = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() - timedelta(days=1), status="planned"
    )
    FactionMeeting.objects.filter(pk=meeting.pk).update(status="completed", protocol_status="approved")

    oeffentlich = FactionAgendaItem.objects.create(
        meeting=meeting, number="1", title=OEFFENTLICH, visibility="public", order=1
    )
    unterpunkt = FactionAgendaItem.objects.create(
        meeting=meeting, number="1.1", title=UNTERPUNKT, visibility="public", parent=oeffentlich, order=2
    )
    intern = FactionAgendaItem.objects.create(
        meeting=meeting, number="N1", title=NICHTOEFFENTLICH, visibility="internal", order=3
    )
    # Die Zeile sagt „öffentlich“, der übergeordnete TOP ist nichtöffentlich: etwa nach dem Verschieben des TOPs
    erbt: Any = FactionAgendaItem(
        meeting=meeting, number="N1.1", title=ERBT, visibility="public", parent=intern, order=4
    )
    erbt.set_description_encrypted(BESCHREIBUNG)
    erbt.save()
    erbt_tief = FactionAgendaItem.objects.create(
        meeting=meeting, number="N1.1.1", title=ERBT_TIEF, visibility="public", parent=erbt, order=5
    )
    _eintrag(meeting, cast(FactionAgendaItem, erbt), EINTRAG, 1)
    _eintrag(meeting, erbt_tief, EINTRAG, 2)
    _eintrag(meeting, unterpunkt, EINTRAG_OEFFENTLICH, 3)
    return reverse("insight_core:public_protocol_detail", kwargs={"body_slug": body.slug, "meeting_id": meeting.pk})


def test_unterpunkte_eines_nichtoeffentlichen_tops_fehlen(client: Client, seite: str) -> None:
    antwort = client.get(seite)

    assert antwort.status_code == 200
    html = antwort.content.decode()
    for text in (NICHTOEFFENTLICH, ERBT, ERBT_TIEF, BESCHREIBUNG, EINTRAG):
        assert text not in html, text


def test_oeffentliche_tops_und_ihre_unterpunkte_bleiben(client: Client, seite: str) -> None:
    html = client.get(seite).content.decode()

    for text in (OEFFENTLICH, UNTERPUNKT, EINTRAG_OEFFENTLICH):
        assert text in html, text
