# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ein Protokolleintrag „Beschluss“ überschreibt keine erfasste Abstimmung (Issue #458).

Der Eintrag setzte ``has_decision`` und die Stimmen am TOP aus dem Formular – das keine Stimmen sendet,
also 0/0/0. Tagesordnung und öffentliche Protokollseite lasen diese Kopie, die Niederschrift fiel auf
sie zurück. Jetzt lesen alle Anzeigen ausschließlich das erfasste Ergebnis (``FactionDecision``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionDecision, FactionMeeting
from insight_core.models import OParlBody, OParlSource


@pytest.fixture
def protokoll(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.view_public", "protocols.create"], email="protokoll@example.org")


@pytest.fixture
def sitzung(org: Any, protokoll: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(hours=1),
        status="ongoing",
        created_by=protokoll,
    )


def _aktion(client: Any, org: Any, sitzung: FactionMeeting, **daten: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.id})
    return client.post(url, daten, HTTP_HX_REQUEST="true")


@pytest.mark.django_db
def test_beschluss_eintrag_ueberschreibt_erfasste_abstimmung_nicht(
    org: Any, protokoll: Any, sitzung: FactionMeeting, client_for: Any
) -> None:
    top = FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public")
    client = client_for(protokoll.user)
    antwort = _aktion(
        client,
        org,
        sitzung,
        action="record_decision",
        agenda_item_id=str(top.id),
        votes_yes="7",
        votes_no="2",
        votes_abstain="1",
        result="accepted",
    )
    assert antwort.status_code == 200

    antwort = _aktion(
        client,
        org,
        sitzung,
        action="add_entry",
        entry_type="decision",
        agenda_item_id=str(top.id),
        content="Angenommen",
    )
    assert antwort.status_code == 200

    top.refresh_from_db()
    assert (top.votes_for, top.votes_against, top.votes_abstain) == (7, 2, 1)
    entscheidung = FactionDecision.objects.get(agenda_item=top)
    assert (entscheidung.votes_yes, entscheidung.votes_no, entscheidung.votes_abstain) == (7, 2, 1)
    assert top.protocol_entries.filter(entry_type="decision").exists()


@pytest.mark.django_db
def test_beschluss_eintrag_ohne_abstimmung_zeigt_keine_stimmen_in_der_tagesordnung(
    org: Any, protokoll: Any, sitzung: FactionMeeting, client_for: Any
) -> None:
    top = FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public")
    client = client_for(protokoll.user)
    antwort = _aktion(
        client, org, sitzung, action="add_entry", entry_type="decision", agenda_item_id=str(top.id), content="Vertagt"
    )

    assert antwort.status_code == 200
    assert "0/0/0" not in antwort.content.decode()
    top.refresh_from_db()
    assert top.recorded_decision is None


def _oeffentlich(org: Any, sitzung: FactionMeeting) -> str:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/1", source=source, name="Stadt Test", slug="stadt-test"
    )
    org.body = body
    org.publish_protocols = True
    org.save(update_fields=["body", "publish_protocols"])
    FactionMeeting.objects.filter(pk=sitzung.pk).update(status="completed", protocol_status="approved")
    return reverse("insight_core:public_protocol_detail", kwargs={"body_slug": body.slug, "meeting_id": sitzung.id})


@pytest.mark.django_db
def test_oeffentliches_protokoll_liest_das_erfasste_ergebnis(org: Any, sitzung: FactionMeeting, client: Any) -> None:
    # Bestand: Kopie am TOP aus einem Beschluss-Eintrag (0/0/0) ohne erfasste Abstimmung …
    FactionAgendaItem.objects.create(
        meeting=sitzung, number="1", title="Ohne Abstimmung", visibility="public", has_decision=True
    )
    # … und eine erfasste Abstimmung, deren Kopie ein Beschluss-Eintrag überschrieben hat
    mit = FactionAgendaItem.objects.create(
        meeting=sitzung, number="2", title="Mit Abstimmung", visibility="public", has_decision=True
    )
    FactionDecision.objects.create(agenda_item=mit, votes_yes=7, votes_no=2, votes_abstain=1, result="accepted")

    html = client.get(_oeffentlich(org, sitzung)).content.decode()

    assert html.count("Abstimmungsergebnis") == 1
    assert "7 Ja" in html
    assert "0 Ja" not in html


@pytest.mark.django_db
def test_niederschrift_zeigt_ohne_erfasste_abstimmung_keine_stimmen(sitzung: FactionMeeting) -> None:
    top = FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", has_decision=True)
    top.entries_list = []  # type: ignore[attr-defined]
    top.decision_obj = top.recorded_decision  # type: ignore[attr-defined]

    html = render_to_string("work/faction/pdf/partial_protocol_top.html", {"item": top})

    assert "Ja:" not in html
