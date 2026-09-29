# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Aufgabenimport bietet übernommene Protokolleinträge nicht erneut an (Issue #459).

Der Filter „noch nicht importiert“ verglich Eintrags-IDs mit Sitzungs-IDs und traf nie; übernommene
Einträge standen weiter in der Liste, ein erneuter Import legte doppelte Aufgaben an. Jetzt merkt sich
die Aufgabe ihren Herkunftseintrag; Aufgaben von vor dieser Kennung erkennt der Abgleich von Sitzung
und Titel.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionMeeting, FactionProtocolEntry
from apps.work.tasks import selectors, services
from apps.work.tasks.models import Task


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(
        org, ["tasks.view", "tasks.create", "faction.view_public", "protocols.create"], email="m@example.org"
    )


@pytest.fixture
def sitzung(org: Any, mitglied: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now(), created_by=mitglied
    )


def _eintrag(sitzung: FactionMeeting, inhalt: str) -> FactionProtocolEntry:
    eintrag = cast(Any, FactionProtocolEntry)(meeting=sitzung, entry_type="action")
    eintrag.set_content_encrypted(inhalt)
    eintrag.save()
    return cast(FactionProtocolEntry, eintrag)


@pytest.mark.django_db
def test_importierter_eintrag_wird_nicht_erneut_angeboten(org: Any, mitglied: Any, sitzung: FactionMeeting) -> None:
    eintrag = _eintrag(sitzung, "Pressemitteilung schreiben")
    anderer = _eintrag(sitzung, "Saal buchen")

    assert services.import_protocol_entries(org, mitglied, [str(eintrag.id)]) == 1

    assert selectors.open_protocol_action_items(org, mitglied) == [anderer]
    assert Task.objects.get(related_protocol_entry=eintrag).title == "Pressemitteilung schreiben"
    # Erneutes Absenden (Doppelklick, veraltete Liste) legt keine zweite Aufgabe an
    assert services.import_protocol_entries(org, mitglied, [str(eintrag.id)]) == 0
    assert Task.objects.filter(related_faction_meeting=sitzung).count() == 1


@pytest.mark.django_db
def test_eintrag_aus_genehmigtem_protokoll(org: Any, mitglied: Any, sitzung: FactionMeeting) -> None:
    """Genehmigte Protokolle sind gesperrt – der Eintrag lässt sich nicht als erledigt markieren."""
    eintrag = _eintrag(sitzung, "Antwort an die Verwaltung")
    FactionMeeting.objects.filter(pk=sitzung.pk).update(protocol_approved=True, protocol_status="approved")

    assert services.import_protocol_entries(org, mitglied, [str(eintrag.id)]) == 1
    assert selectors.open_protocol_action_items(org, mitglied) == []


@pytest.mark.django_db
def test_altbestand_ohne_herkunft_wird_erkannt(org: Any, mitglied: Any, sitzung: FactionMeeting) -> None:
    uebernommen = _eintrag(sitzung, "Flyer drucken")
    offen = _eintrag(sitzung, "Flyer verteilen")
    # Aufgabe aus einem Import vor der Herkunftskennung: nur Sitzung und Titel
    Task.objects.create(organization=org, title="Flyer drucken", created_by=mitglied, related_faction_meeting=sitzung)

    assert selectors.open_protocol_action_items(org, mitglied) == [offen]
    assert services.import_protocol_entries(org, mitglied, [str(uebernommen.id)]) == 0


@pytest.mark.django_db
def test_uebernahme_in_der_sitzung_speichert_die_herkunft(
    org: Any, mitglied: Any, sitzung: FactionMeeting, client_for: Any
) -> None:
    eintrag = _eintrag(sitzung, "Einladung verschicken")
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.id})

    client_for(mitglied.user).post(url, {"action": "create_task", "entry_id": str(eintrag.id)})

    assert Task.objects.filter(related_protocol_entry=eintrag).count() == 1
    assert selectors.open_protocol_action_items(org, mitglied) == []
