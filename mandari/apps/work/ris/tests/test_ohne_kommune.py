# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Seiten einer Organisation ohne verknüpfte Kommune.

Solange einer Organisation keine Kommune zugeordnet ist, zeigt jede RIS-Seite einen Hinweis statt
eines Serverfehlers – auch die Detailseiten, die über eine alte oder geratene Adresse erreicht werden.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.urls import reverse

HINWEIS = "Noch keine Kommune verknüpft"

SEITEN = [
    ("work:ris_overview", {}, {}),
    ("work:ris_search", {}, {}),
    ("work:ris_search", {}, {"q": "Radweg"}),
    ("work:ris_papers", {}, {}),
    ("work:ris_papers", {}, {"q": "Radweg", "year": "2026"}),
    ("work:ris_paper_detail", {"paper_id": uuid.uuid4()}, {}),
    ("work:ris_meetings", {}, {}),
    ("work:ris_meetings", {}, {"view": "past", "year": "2025"}),
    ("work:ris_meeting_detail", {"meeting_id": uuid.uuid4()}, {}),
    ("work:ris_organizations", {}, {}),
    ("work:ris_organization_detail", {"org_id": uuid.uuid4()}, {}),
    ("work:ris_persons", {}, {}),
    ("work:ris_person_detail", {"person_id": uuid.uuid4()}, {}),
    ("work:ris_files", {}, {}),
    ("work:ris_decisions", {}, {}),
    ("work:ris_map", {}, {}),
]


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    assert org.body_id is None
    assert not org.bodies.exists()
    return make_member(org, ["ris.view"], email="ohne-kommune@example.org")


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "kwargs", "params"), SEITEN, ids=[f"{s[0]}{'?' if s[2] else ''}" for s in SEITEN])
def test_ris_seite_ohne_kommune_zeigt_hinweis(
    org: Any, mitglied: Any, client_for: Any, name: str, kwargs: dict, params: dict
) -> None:
    url = reverse(name, kwargs={"org_slug": org.slug, **kwargs})

    response = client_for(mitglied.user).get(url, params)

    assert response.status_code == 200
    assert HINWEIS in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "kwargs", "status", "mit_hinweis"),
    [
        ("work:meetings", {}, 200, False),
        ("work:meetings_calendar", {}, 200, False),
        ("work:meeting_detail", {"meeting_id": uuid.uuid4()}, 200, True),
        ("work:meeting_prepare", {"meeting_id": uuid.uuid4()}, 200, True),
        ("work:meeting_summary", {"meeting_id": uuid.uuid4()}, 200, False),
        # Vollbildseite ohne Navigation: ohne Kommune gibt es die Sitzung schlicht nicht
        ("work:meeting_teleprompter", {"meeting_id": uuid.uuid4(), "item_id": uuid.uuid4()}, 404, False),
    ],
    ids=lambda wert: wert if isinstance(wert, str) else "",
)
def test_sitzungsvorbereitung_ohne_kommune_ohne_serverfehler(
    org: Any, make_member: Any, client_for: Any, name: str, kwargs: dict, status: int, mit_hinweis: bool
) -> None:
    mitglied = make_member(org, ["meetings.view", "meetings.prepare"], email="vorbereitung@example.org")
    url = reverse(name, kwargs={"org_slug": org.slug, **kwargs})

    response = client_for(mitglied.user).get(url)

    assert response.status_code == status
    if mit_hinweis:
        assert HINWEIS in response.content.decode()


@pytest.mark.django_db
def test_kartendaten_ohne_kommune_liefern_leere_sammlung(org: Any, mitglied: Any, client_for: Any) -> None:
    url = reverse("work:ris_map_data", kwargs={"org_slug": org.slug})

    response = client_for(mitglied.user).get(url)

    assert response.status_code == 200
    assert response.json() == {"type": "FeatureCollection", "features": []}


@pytest.mark.django_db
def test_hinweis_verlinkt_support_nur_mit_berechtigung(org: Any, make_member: Any, client_for: Any) -> None:
    support = reverse("work:support_create", kwargs={"org_slug": org.slug})
    url = reverse("work:ris_meeting_detail", kwargs={"org_slug": org.slug, "meeting_id": uuid.uuid4()})

    darf_anfragen = make_member(org, ["ris.view", "support.create"], email="support-ohne-kommune@example.org")
    nur_lesen = make_member(org, ["ris.view"], email="leser-ohne-kommune@example.org")

    assert support in client_for(darf_anfragen.user).get(url).content.decode()
    html = client_for(nur_lesen.user).get(url).content.decode()
    assert support not in html
    assert HINWEIS in html
