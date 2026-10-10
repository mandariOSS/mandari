# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatisches Speichern im TOP-Panel der Fraktionssitzung (#854).

Kopf (Titel, Sichtbarkeit) und Beschreibung sind zwei getrennte Formulare mit `action=update`. Das Speichern des
Kopfs sendet keine Beschreibung und darf sie deshalb nicht löschen; das gilt erst recht, seit gescheiterte
Formulare von selbst erneut senden (nach Anmeldung, bei Fokus, wieder online).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionMeeting

BESCHREIBUNG = "Antrag der Verwaltung, Vorlage 2027/0815"


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.view_public", "faction.manage"], email="vorsitz@example.org")


@pytest.fixture
def top(org: Any, vorsitz: Any) -> FactionAgendaItem:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=2),
        status="planned",
        created_by=vorsitz,
    )
    item = FactionAgendaItem(meeting=meeting, number="1", title="Haushalt 2027", visibility="public")
    item.save()
    cast(Any, item).set_description_encrypted(BESCHREIBUNG)
    item.save()
    return item


def _url(org: Any, top: FactionAgendaItem) -> str:
    return reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": org.slug, "meeting_id": top.meeting_id, "item_id": top.id},
    )


def _beschreibung(top: FactionAgendaItem) -> str:
    top.refresh_from_db()
    return cast(Any, top).get_description_decrypted() or ""


@pytest.mark.django_db
def test_titel_speichern_behaelt_die_beschreibung(
    org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any
) -> None:
    antwort = client_for(vorsitz.user).post(
        _url(org, top), {"action": "update", "title": "Haushalt 2027 (Entwurf)", "visibility": "public"}
    )

    assert antwort.status_code == 204
    assert _beschreibung(top) == BESCHREIBUNG
    assert top.title == "Haushalt 2027 (Entwurf)"


@pytest.mark.django_db
def test_beschreibung_speichern_aendert_nur_die_beschreibung(
    org: Any, vorsitz: Any, top: FactionAgendaItem, client_for: Any
) -> None:
    client = client_for(vorsitz.user)

    client.post(_url(org, top), {"action": "update", "description": "Neue Fassung"})
    assert _beschreibung(top) == "Neue Fassung"
    assert top.title == "Haushalt 2027"

    # Ausdrücklich geleert: weg
    client.post(_url(org, top), {"action": "update", "description": "  "})
    assert _beschreibung(top) == ""
