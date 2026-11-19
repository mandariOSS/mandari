# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Terminumfrage, Ergebnis als interne Sitzung (Issue #1489): Statt eines Termins entsteht eine Sitzung mit
Tagesordnung und Protokoll, angelegt wie beim Planen von Hand; die Antworten stehen schon auf der Anwesenheitsliste.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAttendance, FactionMeeting, FactionMeetingType
from apps.work.notifications.models import Notification, NotificationType
from apps.work.termine import umfrage
from apps.work.termine.models import Termin, Terminumfrage

pytestmark = pytest.mark.django_db

LESEN = ["dashboard.view", "meetings.view", "faction.view_public"]


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def vera(org: Any, make_member: Any) -> Any:
    return make_member(org, [*LESEN, "calendar.create", "faction.create"], first_name="Vera", last_name="Beispiel")


@pytest.fixture
def karim(org: Any, make_member: Any) -> Any:
    return make_member(org, LESEN, first_name="Karim", last_name="Kaya")


@pytest.fixture
def lea(org: Any, make_member: Any) -> Any:
    return make_member(org, LESEN, first_name="Lea", last_name="Lorenz")


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def _umfrage(org: Any, vera: Any, gefragt: list[Any]) -> Terminumfrage:
    tag = timezone.localdate() + timedelta(days=10)
    daten = QueryDict(mutable=True)
    for name, wert in {
        "title": ["Klausur im Frühjahr"],
        "location": ["Rathaus, Raum 2"],
        "description": ["Haushalt und Arbeitsprogramm."],
        "vorschlag_tag": [tag.isoformat(), (tag + timedelta(days=1)).isoformat()],
        "vorschlag_beginn": ["18:00", "18:00"],
        "vorschlag_ende": ["21:00", ""],
        "wen": [umfrage.AUSWAHL],
        "gefragte": [str(person.pk) for person in gefragt],
    }.items():
        daten.setlist(name, wert)
    form = umfrage.UmfrageForm(daten, membership=vera)
    assert form.is_valid(), form.errors
    return form.speichern(org)


def test_ergebnis_wird_interne_sitzung(org: Any, vera: Any, karim: Any, lea: Any, client_for: Any) -> None:
    arbeitskreis = FactionMeetingType.objects.create(organization=org, name="AK Bauen", participants_mode="persons")
    neue = _umfrage(org, vera, [karim, lea])
    erster = neue.vorschlaege.order_by("start")[0]
    assert umfrage.antworten(neue, karim, {f"wahl-{erster.pk}": "ja"}) == 1
    assert umfrage.antworten(neue, lea, {f"wahl-{erster.pk}": "notfalls"}) == 1
    adresse = _url("terminumfrage", org, umfrage_id=neue.pk)

    # Wer Sitzungen anlegen darf, wählt zwischen Termin und interner Sitzung samt Sitzungsart (Standardart zuerst)
    seite = client_for(vera.user).get(adresse).content.decode()
    assert 'data-testid="umfrage-als"' in seite and 'value="sitzung"' in seite
    assert seite.index("Fraktionssitzung") < seite.index("AK Bauen")
    assert 'data-testid="umfrage-als"' not in client_for(karim.user).get(adresse).content.decode()

    antwort = client_for(vera.user).post(
        _url("terminumfrage_festlegen", org, umfrage_id=neue.pk),
        {"vorschlag": str(erster.pk), "als": "sitzung", "meeting_type": str(arbeitskreis.pk)},
    )
    sitzung: Any = FactionMeeting.objects.get(title="Klausur im Frühjahr")
    assert antwort.status_code == 302 and antwort.url == _url("faction_detail", org, meeting_id=sitzung.pk)
    assert not Termin.objects.exists()
    assert (sitzung.meeting_type, sitzung.start, sitzung.end, sitzung.location) == (
        arbeitskreis,
        erster.start,
        erster.end,
        "Rathaus, Raum 2",
    )
    assert sitzung.rsvp_enabled and sitzung.status == "planned" and sitzung.created_by == vera
    # Die Beschreibung bleibt verschlüsselt an der Umfrage (die einer Sitzung ist es nicht)
    assert sitzung.description == ""
    assert neue.get_description_decrypted() == "Haushalt und Arbeitsprogramm."

    # Die Antworten stehen mit ihrem Zeitpunkt auf der Anwesenheitsliste; die Einladung verschickt die Sitzung
    stand = dict(FactionAttendance.objects.filter(meeting=sitzung).values_list("membership_id", "status"))
    assert stand[karim.pk] == "confirmed" and stand[lea.pk] == "tentative" and stand[vera.pk] == "invited"
    beantwortet = erster.antworten.get(membership=karim).updated_at
    assert FactionAttendance.objects.get(meeting=sitzung, membership=karim).responded_at == beantwortet
    assert FactionAttendance.objects.get(meeting=sitzung, membership=vera).responded_at is None
    meldungen = Notification.objects.filter(
        title="Sitzung steht fest", notification_type=NotificationType.TERMIN_EINLADUNG
    )
    assert set(meldungen.values_list("recipient_id", flat=True)) == {karim.pk, lea.pk}

    neue.refresh_from_db()
    assert neue.faction_meeting == sitzung and neue.termin is None and neue.abgeschlossen_am is not None
    seite = client_for(karim.user).get(adresse).content.decode()
    assert "Zur Sitzung" in seite and _url("faction_detail", org, meeting_id=sitzung.pk) in seite


def test_ohne_recht_auf_sitzungen_nur_termin(org: Any, make_member: Any, karim: Any, client_for: Any) -> None:
    nur_termine = make_member(org, [*LESEN, "calendar.create"])
    neue = _umfrage(org, nur_termine, [karim])
    erster = neue.vorschlaege.order_by("start")[0]
    festlegen = _url("terminumfrage_festlegen", org, umfrage_id=neue.pk)
    client = client_for(nur_termine.user)
    assert (
        'data-testid="umfrage-als"' not in client.get(_url("terminumfrage", org, umfrage_id=neue.pk)).content.decode()
    )
    assert client.post(festlegen, {"vorschlag": str(erster.pk), "als": "sitzung"}).status_code == 403
    assert not FactionMeeting.objects.exists() and Terminumfrage.objects.get(pk=neue.pk).abgeschlossen_am is None
    # Ohne Angabe oder mit „termin“ entsteht wie bisher ein Termin
    assert client.post(festlegen, {"vorschlag": str(erster.pk)}).status_code == 302
    assert Termin.objects.count() == 1 and not FactionMeeting.objects.exists()


def test_fremde_sitzungsart_wird_abgelehnt(org: Any, vera: Any, karim: Any, client_for: Any) -> None:
    fremd = FactionMeetingType.objects.create(
        organization=org, name="Vorstand", participants_mode="persons", visibility="participants"
    )
    neue = _umfrage(org, vera, [karim])
    erster = neue.vorschlaege.order_by("start")[0]
    antwort = client_for(vera.user).post(
        _url("terminumfrage_festlegen", org, umfrage_id=neue.pk),
        {"vorschlag": str(erster.pk), "als": "sitzung", "meeting_type": str(fremd.pk)},
    )
    assert antwort.status_code == 302 and antwort.url == _url("terminumfrage", org, umfrage_id=neue.pk)
    assert not FactionMeeting.objects.exists() and Terminumfrage.objects.get(pk=neue.pk).abgeschlossen_am is None
