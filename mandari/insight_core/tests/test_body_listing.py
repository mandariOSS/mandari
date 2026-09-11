# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nicht gelistete Kommunen (OParlBody.is_listed=False, z. B. die Demo-Kommune).

Sie bleiben per direkter URL erreichbar, erscheinen aber nicht in Kommunenauswahl,
Übersichten, Sitemaps, Statistiken und öffentlichen Listen.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.core.cache import cache
from django.http import HttpRequest
from django.test import Client

from insight_core.models import OParlBody, OParlPaper, OParlSource
from insight_core.views._helpers import get_active_body


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


@pytest.fixture
def source(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")


@pytest.fixture
def listed_bodies(source: OParlSource) -> list[OParlBody]:
    return [
        OParlBody.objects.create(external_id=f"https://ris.example.org/body/{key}", source=source, name=name, slug=key)
        for key, name in (("aachen", "Stadt Aachen"), ("bonn", "Stadt Bonn"))
    ]


@pytest.fixture
def hidden_body(source: OParlSource) -> OParlBody:
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/body/demo",
        source=source,
        name="Stadt Abc (Demo)",
        slug="abc-demo",
        is_listed=False,
    )
    OParlPaper.objects.create(external_id="https://ris.example.org/paper/demo", body=body, name="Demo-Vorlage")
    return body


def test_listed_excludes_hidden_and_deleted(listed_bodies: list[OParlBody], hidden_body: OParlBody) -> None:
    deleted = listed_bodies[1]
    deleted.deleted = True
    deleted.save(update_fields=["deleted"])

    assert list(OParlBody.objects.listed()) == [listed_bodies[0]]


def test_new_body_is_listed_by_default(source: OParlSource) -> None:
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/neu", source=source, name="Neu")
    body.refresh_from_db()
    assert body.is_listed is True


def test_selection_page_and_switcher_hide_body(listed_bodies: list[OParlBody], hidden_body: OParlBody) -> None:
    response = Client().get("/insight/")

    assert response.status_code == 200
    assert hidden_body not in response.context["select_bodies"]
    assert hidden_body not in response.context["available_bodies"]
    assert response.context["stats"]["bodies"] == 2
    assert response.context["stats"]["papers"] == 0
    assert "Stadt Abc (Demo)" not in response.content.decode()


def test_hidden_body_stays_reachable_by_url(listed_bodies: list[OParlBody], hidden_body: OParlBody) -> None:
    client = Client()
    response = client.get(f"/insight/kommune/{hidden_body.id}/", follow=True)

    assert response.status_code == 200
    assert client.session["active_body_id"] == str(hidden_body.id)
    assert response.context["active_body"] == hidden_body


def test_fallback_never_picks_hidden_body(source: OParlSource, hidden_body: OParlBody) -> None:
    visible = OParlBody.objects.create(external_id="https://ris.example.org/body/z", source=source, name="Stadt Z")

    class _Request:
        session: dict[str, str] = {"active_body_id": "all"}

    assert get_active_body(cast(HttpRequest, _Request())) == visible


def test_sitemaps_skip_hidden_body(listed_bodies: list[OParlBody], hidden_body: OParlBody) -> None:
    client = Client()
    index = client.get("/sitemap-insight-index.xml").content.decode()

    assert "sitemap-insight-aachen.xml" in index
    assert "abc-demo" not in index
    assert client.get("/sitemap-insight-abc-demo.xml").status_code == 404


def test_public_stats_api_hides_body(listed_bodies: list[OParlBody], hidden_body: OParlBody) -> None:
    client = Client()
    names = [b["name"] for b in client.get("/api/stats/bodies/").json()["bodies"]]

    assert names == ["Stadt Aachen", "Stadt Bonn"]
    stats = client.get("/api/stats/").json()
    assert stats["bodies"] == 2
    assert stats["papers"] == 0
