# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kalender als eigene Ansicht (Issue #1548): eigener Bereich mit eigener Adresse, Sitzungen eigener und weiterer
Gremien getrennt, Sitzungen einer Reihe zu ihrer festen Zeit, Sitzungen nach ihrem Gremium benannt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work import rahmen
from apps.work.faction.models import FactionMeeting, FactionMeetingSchedule
from apps.work.meetings import kalender
from insight_core.models import (
    OParlBody,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"
LESEN = ["dashboard.view", "meetings.view", "faction.view_public", "faction.manage"]


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(source=source, external_id=f"{RIS}/body/1", name="Musterstadt")


@pytest.fixture
def org(org: Any, body: OParlBody) -> Any:
    org.work_new_design = True
    org.body = body
    org.save(update_fields=["work_new_design", "body"])
    return org


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, LESEN, first_name="Vera", last_name="Beispiel")


def _gremium(body: OParlBody, nr: int, name: str) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{RIS}/organization/{nr}", body=body, name=name)


def _sitzung(
    body: OParlBody, nr: int, name: str | None, gremium: OParlOrganization | None, start: datetime, **felder: Any
) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{nr}",
        body=body,
        name=name,
        start=start,
        raw_json={"organization": [gremium.external_id]} if gremium else {},
        **felder,
    )
    if gremium:
        sitzung.organizations.add(gremium)
    return sitzung


def _eintraege(client: Any, org: Any, arten: str | None = None) -> list[dict[str, Any]]:
    jetzt = timezone.now()
    parameter = {"start": (jetzt - timedelta(days=10)).isoformat(), "end": (jetzt + timedelta(days=30)).isoformat()}
    if arten is not None:
        parameter["arten"] = arten
    adresse = reverse("work:meetings_calendar_events", kwargs={"org_slug": org.slug})
    antwort: list[dict[str, Any]] = client.get(adresse, parameter).json()
    return antwort


# ---- Name einer Sitzung: zuerst das Gremium ---------------------------------------------------------------------


def test_sitzung_heisst_nach_ihrem_gremium_auch_mit_laufender_nummer(body: OParlBody) -> None:
    start = datetime(2027, 4, 6, 15, 0, tzinfo=UTC)
    nord = _gremium(body, 1, "Bezirksvertretung Nord")
    marketing = _gremium(body, 2, "Betriebsausschuss Marketing")

    assert _sitzung(body, 1, "8. Sitzung", nord, start).bezeichnung() == "Bezirksvertretung Nord"
    assert _sitzung(body, 2, "40.Sitzung", nord, start).bezeichnung() == "Bezirksvertretung Nord"
    # Steht im RIS etwas anderes als der Name (eine Notiz), kommt es hinter das Gremium
    notiz = _sitzung(body, 3, "Mail Herr Muster vom 28.10.2026", marketing, start)
    assert notiz.bezeichnung() == "Betriebsausschuss Marketing: Mail Herr Muster vom 28.10.2026"
    # Nennt der eigene Name das Gremium schon, steht er allein; ein Wortteil zählt nicht
    rat = _gremium(body, 3, "Rat")
    assert _sitzung(body, 6, "Sitzung Bezirksvertretung Nord", nord, start).bezeichnung() == (
        "Sitzung Bezirksvertretung Nord"
    )
    assert _sitzung(body, 7, "Sondersitzung Beirat", rat, start).bezeichnung() == "Rat: Sondersitzung Beirat"
    # Ohne Gremium bleibt der eigene Name, auch die laufende Nummer
    assert _sitzung(body, 4, "9. Sitzung", None, start).bezeichnung() == "9. Sitzung"
    # Der Name aus dem öffentlichen Kalender der Kommune zählt als Gremium (Issue #1505)
    verborgen = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/5",
        body=body,
        name="7. Sitzung",
        start=start,
        raw_json={"organization": [f"{RIS}/organization/152"], "mandari:gremiumWeb": {"name": "AG Internationales"}},
    )
    assert verborgen.bezeichnung() == "AG Internationales" == verborgen.get_display_name()


# ---- Eigene und weitere Gremien ---------------------------------------------------------------------------------


def test_ohne_eigene_gremien_eine_art_fuer_alle_sitzungen(
    org: Any, body: OParlBody, mitglied: Any, client_for: Any
) -> None:
    _sitzung(body, 1, "Sitzung", _gremium(body, 1, "Rat"), timezone.now() + timedelta(days=2))

    assert kalender.eigene_gremien(org, mitglied) == set()
    arten = kalender.arten_fuer(mitglied, mit_eigenen_gremien=False)
    assert [art.key for art in arten][:2] == ["sitzung", "fraktion"]
    assert arten[0].label == "Sitzungen der Gremien"

    eintraege = _eintraege(client_for(mitglied.user), org)
    assert [(e["title"], e["extendedProps"]["art"]) for e in eintraege] == [("Rat", "sitzung")]
    html = client_for(mitglied.user).get(reverse("work:meetings_calendar", kwargs={"org_slug": org.slug})).content
    assert "Sitzungen der Gremien" in html.decode() and 'id="kalender-art-gremium"' not in html.decode()


def test_eigene_und_weitere_gremien_getrennt(
    org: Any, body: OParlBody, mitglied: Any, make_member: Any, client_for: Any
) -> None:
    rat = _gremium(body, 1, "Rat")
    kultur = _gremium(body, 2, "Kulturausschuss")
    sport = _gremium(body, 3, "Sportausschuss")
    hiltrup = _gremium(body, 4, "Bezirksvertretung Hiltrup")
    bald = timezone.now() + timedelta(days=2)
    for nr, gremium in enumerate((rat, kultur, sport, hiltrup), start=1):
        _sitzung(body, nr, f"{nr}. Sitzung", gremium, bald + timedelta(hours=nr))
    # Ohne zugeordnetes Gremium, aber mit dessen Adresse im Rohdatensatz
    OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/9",
        body=body,
        name="Sitzung",
        start=bald + timedelta(hours=9),
        raw_json={"organization": [sport.external_id]},
    )

    # Zugewiesen an ein anderes Mitglied, Person im RIS mit laufender Mitgliedschaft, selbst gefolgt
    andere = make_member(org, ["meetings.view"])
    andere.oparl_committees.add(rat)
    person = OParlPerson.objects.create(external_id=f"{RIS}/person/1", body=body, name="Anna Rat")
    OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=kultur)
    OParlMembership.objects.create(
        external_id=f"{RIS}/membership/2", person=person, organization=hiltrup, end_date=date(2020, 1, 1)
    )
    andere.oparl_person = person
    andere.save(update_fields=["oparl_person"])
    mitglied.followed_organizations.add(sport)
    # Gäste zählen nicht
    gast = make_member(org, [])
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    gast.oparl_committees.add(hiltrup)

    assert kalender.eigene_gremien(org, mitglied) == {rat.pk, kultur.pk, sport.pk}

    client = client_for(mitglied.user)
    je_titel = {e["title"]: e for e in _eintraege(client, org)}
    assert {titel: e["extendedProps"]["art"] for titel, e in je_titel.items()} == {
        "Rat": "sitzung",
        "Kulturausschuss": "sitzung",
        "Sportausschuss": "sitzung",
        "Bezirksvertretung Hiltrup": "gremium",
    }
    assert je_titel["Bezirksvertretung Hiltrup"]["classNames"] == ["termin-gremium"]
    assert je_titel["Rat"]["extendedProps"]["fullTitle"] == "Rat"
    # Nur weitere Gremien gewählt
    nur_weitere = _eintraege(client, org, "gremium")
    assert [e["title"] for e in nur_weitere] == ["Bezirksvertretung Hiltrup"]

    html = client.get(reverse("work:meetings_calendar", kwargs={"org_slug": org.slug})).content.decode()
    assert "Sitzungen eigener Gremien" in html and "Sitzungen weiterer Gremien" in html
    assert 'data-arten="sitzung,gremium,fraktion,' in html


# ---- Sitzungsreihe zur festen Zeit ------------------------------------------------------------------------------


def test_sitzung_einer_reihe_steht_zur_festen_zeit(org: Any, mitglied: Any, client_for: Any) -> None:
    reihe = FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0), duration_minutes=150
    )
    beginn = timezone.now() + timedelta(days=1)
    spaet_beendet = FactionMeeting.objects.create(
        organization=org,
        title="Wöchentliche Fraktionssitzung",
        schedule=reihe,
        start=beginn,
        # „Sitzung beenden“ erst am nächsten Tag
        end=beginn + timedelta(hours=7),
        status="completed",
        created_by=mitglied,
    )
    geplant = FactionMeeting.objects.create(
        organization=org,
        title="Mit eigenem Ende",
        schedule=reihe,
        start=beginn + timedelta(days=7),
        end=beginn + timedelta(days=7, hours=1),
        created_by=mitglied,
    )
    einzeln = FactionMeeting.objects.create(
        organization=org,
        title="Klausur",
        start=beginn + timedelta(days=3),
        end=beginn + timedelta(days=3, hours=9),
        status="completed",
        created_by=mitglied,
    )

    je_id = {e["id"]: e for e in _eintraege(client_for(mitglied.user), org, "fraktion")}
    eintrag = je_id[f"fraktion-{spaet_beendet.pk}"]
    assert eintrag["start"] == beginn.isoformat()
    assert eintrag["end"] == (beginn + timedelta(minutes=150)).isoformat()
    # Ein geplantes Ende einer einzelnen Sitzung der Reihe bleibt, ebenso das Ende einer Sitzung ohne Reihe
    assert je_id[f"fraktion-{geplant.pk}"]["end"] == (beginn + timedelta(days=7, hours=1)).isoformat()
    assert je_id[f"fraktion-{einzeln.pk}"]["end"] == (beginn + timedelta(days=3, hours=9)).isoformat()


# ---- Eigene Ansicht ---------------------------------------------------------------------------------------------


def test_kalender_ist_ein_eigener_bereich(org: Any, mitglied: Any, make_member: Any, client_for: Any) -> None:
    adresse = reverse("work:meetings_calendar", kwargs={"org_slug": org.slug})
    assert adresse == f"/work/{org.slug}/kalender/"
    assert rahmen.einordnen("meetings_calendar") == ("kalender", None, None)
    assert rahmen.einordnen("terminumfrage_neu") == ("kalender", None, "Neue Terminumfrage")

    client = client_for(mitglied.user)
    # Die bisherige Adresse leitet weiter, mit ihren Parametern
    bisher = client.get(f"/work/{org.slug}/meetings/calendar/?ansicht=bisher")
    assert bisher.status_code == 302 and bisher.headers["Location"] == f"{adresse}?ansicht=bisher"

    html = client.get(adresse).content.decode()
    leiste = html[html.index('aria-label="Bereiche"') : html.index("</aside>")]
    eintrag = leiste[leiste.index(f'href="{adresse}"') :]
    assert 'aria-current="page"' in eintrag[: eintrag.index("</a>")] and "Kalender" in eintrag[: eintrag.index("</a>")]
    # Kein Rückweg zu „Meine Sitzungen“ mehr
    assert "Meine Sitzungen</a>" not in html

    # „Termin abstimmen“ ist ein Knopf mit Symbol, kein Textlink
    planer = make_member(org, [*LESEN, "calendar.create"])
    html = client_for(planer.user).get(adresse).content.decode()
    umfrage = reverse("work:terminumfrage_neu", kwargs={"org_slug": org.slug})
    kopf = html[html.index("<h1") :]
    knopf = kopf[kopf.index(f'<a href="{umfrage}"') :]
    knopf = knopf[: knopf.index("</a>")]
    assert "Termin abstimmen" in knopf and 'data-lucide="calendar-search"' in knopf and "border" in knopf
