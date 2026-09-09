# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unit-Tests für Selectors und Services der RIS-Datenansicht (Issue #160, Service-Layer).

Prüft die Bindung an die Kommunen der Organisation (Mandanten-Isolation),
Listenfilter, Detail-Anreicherungen (Dateien aus raw_json, Beratungsfolge),
Karte/GeoJSON sowie die Suche mit ORM-Fallback.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.work.ris import selectors, services
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)


@pytest.fixture
def source(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")


@pytest.fixture
def body(org: Any, source: OParlSource) -> OParlBody:
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    return body


@pytest.fixture
def foreign_body(source: OParlSource) -> OParlBody:
    """Kommune, die nicht mit der Organisation verknüpft ist (darf nie erscheinen)."""
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/2", source=source, name="Stadt Fremd")
    OParlPaper.objects.create(external_id="https://ris.example.org/paper/fremd", body=body, name="Fremde Vorlage")
    OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/fremd", body=body, name="Fremder Rat")
    OParlOrganization.objects.create(external_id="https://ris.example.org/org/fremd", body=body, name="Fremdes Gremium")
    OParlPerson.objects.create(external_id="https://ris.example.org/person/fremd", body=body, name="Fremde Person")
    return body


@pytest.fixture
def bodies(org: Any, body: OParlBody, foreign_body: OParlBody) -> Any:
    return selectors.bodies_for_organization(org)


def make_paper(body: OParlBody, key: str, **kwargs: Any) -> OParlPaper:
    return OParlPaper.objects.create(external_id=f"https://ris.example.org/paper/{key}", body=body, **kwargs)


def make_meeting(body: OParlBody, key: str, **kwargs: Any) -> OParlMeeting:
    return OParlMeeting.objects.create(external_id=f"https://ris.example.org/meeting/{key}", body=body, **kwargs)


def make_org(body: OParlBody, key: str, **kwargs: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"https://ris.example.org/org/{key}", body=body, **kwargs)


# ---------------------------------------------------------------------------
# Kommunen und Übersicht
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bodies_for_organization_only_linked_bodies(org: Any, body: OParlBody, foreign_body: OParlBody) -> None:
    assert list(selectors.bodies_for_organization(org)) == [body]
    assert selectors.primary_body(org) == body


@pytest.mark.django_db
def test_overview_stats_and_lists_are_isolated(bodies: Any, body: OParlBody) -> None:
    now = timezone.now()
    make_paper(body, "1", name="Alt", date=(now - timedelta(days=400)).date())
    make_paper(body, "2", name="Neu", date=now.date())
    make_meeting(body, "1", name="Kommend", start=now + timedelta(days=3))
    make_meeting(body, "2", name="Abgesagt", start=now + timedelta(days=4), cancelled=True)
    make_meeting(body, "3", name="Vergangen", start=now - timedelta(days=4))
    make_org(body, "1", name="Rat")
    make_org(body, "2", name="Alter Ausschuss", end_date=(now - timedelta(days=10)).date())
    OParlPerson.objects.create(external_id="https://ris.example.org/person/1", body=body, name="Erika")

    stats = selectors.overview_stats(bodies)

    assert stats == {
        "papers_total": 2,
        "papers_this_year": 1,
        "meetings_total": 3,
        "meetings_upcoming": 1,
        "organizations_total": 2,
        "organizations_active": 1,
        "persons_total": 1,
    }
    assert [p.name for p in selectors.recent_papers(bodies)] == ["Neu", "Alt"]
    assert [m.name for m in selectors.upcoming_meetings(bodies)] == ["Kommend"]


# ---------------------------------------------------------------------------
# Sitzungen, Gremien, Personen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_meetings_queryset_modes_and_filters(bodies: Any, body: OParlBody) -> None:
    now = timezone.now()
    rat = make_org(body, "rat", name="Rat")
    upcoming = make_meeting(body, "u", name="Kommend", start=now + timedelta(days=1))
    upcoming.organizations.add(rat)
    past = make_meeting(body, "p", name="Vergangen", start=now - timedelta(days=1))

    assert list(selectors.meetings_queryset(bodies, view_mode="upcoming")) == [upcoming]
    assert list(selectors.meetings_queryset(bodies, view_mode="past")) == [past]
    assert list(selectors.meetings_queryset(bodies, view_mode="all")) == [upcoming, past]
    assert list(selectors.meetings_queryset(bodies, view_mode="all", organization_id=str(rat.id))) == [upcoming]
    assert list(selectors.meetings_queryset(bodies, view_mode="all", year=now.year - 5)) == []
    expected_years = sorted({(now + timedelta(days=1)).year, (now - timedelta(days=1)).year}, reverse=True)
    assert [d.year for d in selectors.meeting_years(bodies)] == expected_years
    assert [o.name for o in selectors.organizations_for_filter(bodies)] == ["Rat"]


@pytest.mark.django_db
def test_agenda_items_with_papers_uses_natural_sort(body: OParlBody) -> None:
    meeting = make_meeting(body, "m", name="Rat")
    for number in ("10", "2", "1"):
        OParlAgendaItem.objects.create(
            external_id=f"https://ris.example.org/agenda/{number}", meeting=meeting, number=number, name=f"TOP {number}"
        )
    paper = make_paper(body, "top2", name="Vorlage zu TOP 2")
    OParlConsultation.objects.create(
        external_id="https://ris.example.org/consultation/1",
        paper=paper,
        agenda_item_external_id="https://ris.example.org/agenda/2",
    )

    items = selectors.agenda_items_with_papers(meeting)

    assert [entry["item"].number for entry in items] == ["1", "2", "10"]
    assert list(items[1]["papers"]) == [paper]


@pytest.mark.django_db
def test_organizations_active_filter_and_counts(bodies: Any, body: OParlBody) -> None:
    now = timezone.now()
    active = make_org(body, "a", name="Hauptausschuss")
    make_meeting(body, "m", name="Sitzung", start=now - timedelta(days=30)).organizations.add(active)
    make_org(body, "b", name="Ohne Sitzungen")
    ended = make_org(body, "c", name="Beendet", end_date=(now - timedelta(days=1)).date())
    make_meeting(body, "m2", name="Alte Sitzung", start=now - timedelta(days=500)).organizations.add(ended)

    annotated = selectors.organizations_with_meeting_info(bodies)
    assert [o.name for o in selectors.filter_organizations(annotated, active_only=True)] == ["Hauptausschuss"]
    assert {o.name for o in selectors.filter_organizations(annotated, active_only=False)} == {
        "Hauptausschuss",
        "Ohne Sitzungen",
        "Beendet",
    }
    assert [o.name for o in selectors.filter_organizations(annotated, search="beend", active_only=False)] == ["Beendet"]
    assert selectors.organization_tab_counts(bodies) == {"active_count": 1, "all_count": 3}
    ranked = [o.name for o in selectors.ranked_organizations(annotated)]
    assert ranked[0] == "Hauptausschuss" and set(ranked[1:]) == {"Beendet", "Ohne Sitzungen"}  # inaktive ans Ende


@pytest.mark.django_db
def test_organization_members_and_meetings(body: OParlBody) -> None:
    now = timezone.now()
    today = now.date()
    org = make_org(body, "rat", name="Rat")
    active_person = OParlPerson.objects.create(
        external_id="https://ris.example.org/person/a", body=body, name="Aktiv", family_name="Aktiv"
    )
    past_person = OParlPerson.objects.create(
        external_id="https://ris.example.org/person/b", body=body, name="Ehemalig", family_name="Ehemalig"
    )
    OParlMembership.objects.create(
        external_id="https://ris.example.org/membership/a", person=active_person, organization=org
    )
    OParlMembership.objects.create(
        external_id="https://ris.example.org/membership/b",
        person=past_person,
        organization=org,
        end_date=today - timedelta(days=1),
    )
    upcoming = make_meeting(body, "u", name="Heute spaet", start=now.replace(hour=23, minute=59))
    past = make_meeting(body, "p", name="Gestern", start=now - timedelta(days=1))
    upcoming.organizations.add(org)
    past.organizations.add(org)

    members = selectors.organization_members(org)
    assert [m.person.name for m in members["active"]] == ["Aktiv"]
    assert [m.person.name for m in members["past"]] == ["Ehemalig"]

    meetings = selectors.organization_meetings(org)
    assert list(meetings["upcoming"]) == [upcoming]
    assert list(meetings["past"]) == [past]

    person_memberships = selectors.person_memberships(past_person)
    assert list(person_memberships["active"]) == [] and person_memberships["past"].count() == 1


@pytest.mark.django_db
def test_persons_queryset_search_and_council_role(bodies: Any, body: OParlBody) -> None:
    rat = make_org(body, "rat", name="Rat")
    erika = OParlPerson.objects.create(
        external_id="https://ris.example.org/person/e", body=body, name="Erika Muster", family_name="Muster"
    )
    OParlPerson.objects.create(
        external_id="https://ris.example.org/person/m", body=body, name="Max Beispiel", family_name="Beispiel"
    )
    OParlMembership.objects.create(
        external_id="https://ris.example.org/membership/e", person=erika, organization=rat, role="Vorsitz"
    )

    persons = list(selectors.persons_queryset(bodies))
    assert [p.family_name for p in persons] == ["Beispiel", "Muster"]
    erika_row = cast(Any, persons[1])  # Annotationen sind dem Typsystem nicht bekannt
    assert erika_row.council_role == "Vorsitz" and erika_row.membership_count == 1

    assert [p.name for p in selectors.persons_queryset(bodies, search="erika")] == ["Erika Muster"]
    assert "Fremde Person" not in {p.name for p in selectors.persons_queryset(bodies)}


# ---------------------------------------------------------------------------
# Vorgänge, Dateien, Karte
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_papers_queryset_filters_and_options(bodies: Any, body: OParlBody) -> None:
    today = timezone.now().date()
    make_paper(body, "1", name="Haushalt 2026", reference="V/001", paper_type="Vorlage", date=today)
    make_paper(
        body, "2", name="Antrag Radweg", reference="A/002", paper_type="Antrag", date=today.replace(year=today.year - 1)
    )

    assert [p.name for p in selectors.papers_queryset(bodies)] == ["Haushalt 2026", "Antrag Radweg"]
    assert [p.name for p in selectors.papers_queryset(bodies, search="v/001")] == ["Haushalt 2026"]
    assert [p.name for p in selectors.papers_queryset(bodies, paper_type="Antrag")] == ["Antrag Radweg"]
    assert [p.name for p in selectors.papers_queryset(bodies, year=today.year)] == ["Haushalt 2026"]
    assert list(selectors.paper_types(bodies)) == ["Antrag", "Vorlage"]
    assert [d.year for d in selectors.paper_years(bodies)] == [today.year, today.year - 1]
    assert list(selectors.search_paper_types(bodies)) == ["Antrag", "Vorlage"]


@pytest.mark.django_db
def test_paper_files_falls_back_to_raw_json(body: OParlBody) -> None:
    paper = make_paper(
        body,
        "raw",
        name="Nur raw_json",
        raw_json={
            "mainFile": {"fileName": "haupt.pdf", "accessUrl": "https://ris.example.org/haupt.pdf"},
            "auxiliaryFile": [{"name": "Anlage 1", "mimeType": "application/pdf"}, "kaputt"],
        },
    )

    files, from_raw = selectors.paper_files(paper)

    assert from_raw is True
    assert isinstance(files, list)
    assert [f["name"] for f in files] == ["haupt.pdf", "Anlage 1"]
    assert files[0]["is_main"] is True and files[1]["is_main"] is False


@pytest.mark.django_db
def test_enriched_consultations_resolves_meeting_and_agenda_item(body: OParlBody) -> None:
    now = timezone.now()
    rat = make_org(body, "rat", name="Rat", short_name="R")
    later = make_meeting(body, "later", name="Rat spaeter", start=now + timedelta(days=10))
    earlier = make_meeting(body, "earlier", name="Ausschuss frueher", start=now)
    later.organizations.add(rat)
    item = OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/x", meeting=later, number="5", name="TOP 5", result="beschlossen"
    )
    paper = make_paper(body, "p", name="Vorlage")
    OParlConsultation.objects.create(
        external_id="https://ris.example.org/consultation/a",
        paper=paper,
        meeting_external_id=later.external_id,
        agenda_item_external_id=item.external_id,
        role="Entscheidung",
        authoritative=True,
    )
    OParlConsultation.objects.create(
        external_id="https://ris.example.org/consultation/b",
        paper=paper,
        meeting_external_id=earlier.external_id,
        role="Vorberatung",
    )
    OParlConsultation.objects.create(external_id="https://ris.example.org/consultation/c", paper=paper)

    entries = selectors.enriched_consultations(paper)

    # Ohne Sitzung wird "jetzt" als Sortierschlüssel genutzt: zwischen heute und in zehn Tagen
    assert [e["meeting_name"] for e in entries] == ["Ausschuss frueher", None, "Rat spaeter"]
    decision = entries[2]
    assert decision["organization_name"] == "Rat"
    assert decision["agenda_number"] == "5" and decision["result"] == "beschlossen"
    assert decision["authoritative"] is True and decision["public"] is True
    assert selectors.enriched_consultations(make_paper(body, "leer")) == []


@pytest.mark.django_db
def test_geojson_features_and_map_config(bodies: Any, body: OParlBody) -> None:
    make_paper(
        body,
        "geo",
        name="Spielplatz",
        reference="V/9",
        locations=[{"lat": 51.96, "lon": 7.62, "name": "Prinzipalmarkt"}, {"name": "ohne Koordinaten"}, "kaputt"],
    )
    make_paper(body, "nogeo", name="Ohne Ort")

    collection = services.geojson_features(bodies)

    assert collection["type"] == "FeatureCollection" and len(collection["features"]) == 1
    feature = collection["features"][0]
    assert feature["geometry"]["coordinates"] == [7.62, 51.96]
    assert feature["properties"]["title"] == "Spielplatz" and feature["properties"]["location_name"] == "Prinzipalmarkt"

    config = services.map_config(body)
    assert config == {"center_lat": 51.5, "center_lng": 7.5, "zoom": 13, "bbox": None}


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_resolve_search_bodies_ignores_unknown_filter(bodies: Any, body: OParlBody, foreign_body: OParlBody) -> None:
    assert services.resolve_search_bodies(bodies, "") == ([str(body.id)], "")
    assert services.resolve_search_bodies(bodies, str(body.id)) == ([str(body.id)], str(body.id))
    assert services.resolve_search_bodies(bodies, str(foreign_body.id)) == ([str(body.id)], "")


def test_search_query_index_names() -> None:
    assert services.SearchQuery(query="x").index_names() is None
    assert services.SearchQuery(result_type="persons").index_names() == ["persons"]
    assert services.SearchQuery(committee="Rat").index_names() == ["papers", "meetings", "files"]
    assert services.SearchQuery(paper_type="Antrag").index_names() == ["papers"]
    assert services.SearchQuery().is_empty and not services.SearchQuery(date_from="2026-01-01").is_empty


@pytest.mark.django_db
def test_search_falls_back_to_orm_when_elasticsearch_fails(
    monkeypatch: Any, bodies: Any, body: OParlBody, foreign_body: OParlBody
) -> None:
    class BrokenService:
        def __init__(self) -> None:
            raise ConnectionError("kein Elasticsearch")

    from insight_core.services import search_service

    monkeypatch.setattr(search_service, "ElasticsearchService", BrokenService)
    now = timezone.now()
    rat = make_org(body, "rat", name="Rat")
    make_paper(body, "1", name="Radweg Nord", date=now.date())
    meeting = make_meeting(body, "1", name="Radweg-Sitzung", start=now)
    meeting.organizations.add(rat)
    OParlPerson.objects.create(external_id="https://ris.example.org/person/r", body=body, name="Rad Fahrer")

    result = services.search(services.SearchQuery(query="Radweg"), [str(body.id)])

    assert result.backend == "orm" and result.total == 2
    assert [p.name for p in result.orm_results["papers"]] == ["Radweg Nord"]
    assert [m.name for m in result.orm_results["meetings"]] == ["Radweg-Sitzung"]
    assert list(result.orm_results["persons"]) == []

    filtered = services.search(services.SearchQuery(query="Rad", committee="Rat"), [str(body.id)])
    assert filtered.backend == "orm" and [m.name for m in filtered.orm_results["meetings"]] == ["Radweg-Sitzung"]
    assert [p.name for p in filtered.orm_results["persons"]] == ["Rad Fahrer"]


@pytest.mark.django_db
def test_search_uses_elasticsearch_result(monkeypatch: Any, body: OParlBody) -> None:
    calls: list[dict[str, Any]] = []

    class FakeService:
        def search_all(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "results": [{"title": "Treffer", "_formatted": {"title": "<em>Treffer</em>"}}],
                "total": 1,
                "page": 2,
                "pages": 3,
            }

    from insight_core.services import search_service

    monkeypatch.setattr(search_service, "ElasticsearchService", FakeService)

    result = services.search(services.SearchQuery(query="x", paper_type="Antrag", page=2), [str(body.id)])

    assert result.backend == "elasticsearch" and (result.total, result.page, result.pages) == (1, 2, 3)
    assert result.es_results[0]["formatted"] == {"title": "<em>Treffer</em>"}
    assert calls[0]["body_ids"] == [str(body.id)] and calls[0]["index_names"] == ["papers"]
    assert calls[0]["page_size"] == services.SEARCH_PAGE_SIZE


# ---------------------------------------------------------------------------
# Beschlusskontrolle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_decisions_are_bound_to_linked_session_tenants(bodies: Any, body: OParlBody, foreign_body: OParlBody) -> None:
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant

    tenant = SessionTenant.objects.create(name="Verwaltung Test", slug="verwaltung-test", oparl_body=body)
    SessionTenant.objects.create(name="Verwaltung Fremd", slug="verwaltung-fremd", oparl_body=foreign_body)
    council = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    now = timezone.now()
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=council, start=now, is_public=True
    )
    SessionAgendaItem.objects.create(
        meeting=meeting,
        number="1",
        name="Radweg bauen",
        vote_result="approved",
        is_public=True,
        implementation_status="open",
    )
    SessionAgendaItem.objects.create(
        meeting=meeting,
        number="2",
        name="Geheim",
        vote_result="approved",
        is_public=False,
        implementation_status="open",
    )
    SessionAgendaItem.objects.create(
        meeting=meeting,
        number="3",
        name="Verschoben",
        vote_result="approved",
        is_public=True,
        implementation_status="done",
        implementation_deadline=(now - timedelta(days=1)).date(),
    )

    tenants = selectors.session_tenants(bodies)
    assert [t.slug for t in tenants] == ["verwaltung-test"]

    items = selectors.decided_items(tenants)
    assert [i.name for i in items] == ["Radweg bauen", "Verschoben"]
    assert selectors.tracking_stats(items, now.date()) == {
        "open": 1,
        "in_progress": 0,
        "done": 1,
        "deferred": 0,
        "overdue": 0,
        "total": 2,
    }
    assert [i.name for i in selectors.filter_decisions(items, query="radweg")] == ["Radweg bauen"]
    assert [i.name for i in selectors.filter_decisions(items, organization_id=str(council.id), year=str(now.year))] == [
        "Radweg bauen",
        "Verschoben",
    ]
    assert [i.name for i in selectors.filter_implementation(items, status="done", today=now.date())] == ["Verschoben"]
    assert [o.name for o in selectors.decision_organizations(tenants)] == ["Rat"]
    assert selectors.decision_years(tenants) == [now.year]
