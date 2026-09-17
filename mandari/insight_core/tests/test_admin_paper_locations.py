# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-Korrektur-Workflow: Verortung bestätigen/entfernen mit einem Klick, Herkunft sichtbar (Issue #54)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from insight_core.models import OParlBody, OParlPaper, PaperLocation
from insight_core.services.paper_locations import sync_paper_locations
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client_(db: Any) -> Client:
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def located_paper(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> OParlPaper:
    paper = make_paper(
        geo_body,
        locations=[
            {"lat": CENTER_LAT, "lon": CENTER_LON, "name": "Prinzipalmarkt 1", "source": "oparl", "confidence": 1.0},
            {
                "lat": CENTER_LAT + 0.01,
                "lon": CENTER_LON,
                "name": "Hafenweg",
                "source": "street_match",
                "confidence": 0.8,
            },
        ],
    )
    sync_paper_locations(paper)
    return paper


def test_changelist_shows_source_and_row_actions(admin_client_: Client, located_paper: OParlPaper) -> None:
    response = admin_client_.get(reverse("admin:insight_core_paperlocation_changelist"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "OParl-Ort" in html
    assert "Straße (Straßenverzeichnis)" in html
    row = PaperLocation.objects.get(paper=located_paper, name="Hafenweg")
    assert reverse("admin:insight_core_paperlocation_remove_row", args=[row.pk]) in html
    assert reverse("admin:insight_core_paperlocation_confirm_row", args=[row.pk]) in html


def test_remove_row_action_suppresses_location(admin_client_: Client, located_paper: OParlPaper) -> None:
    row = PaperLocation.objects.get(paper=located_paper, name="Hafenweg")
    response = admin_client_.get(reverse("admin:insight_core_paperlocation_remove_row", args=[row.pk]))
    assert response.status_code == 302

    row.refresh_from_db()
    located_paper.refresh_from_db()
    assert row.status == PaperLocation.STATUS_REMOVED
    assert located_paper.locations is not None
    assert [loc["name"] for loc in located_paper.locations] == ["Prinzipalmarkt 1"]

    # Automatischer Lauf liefert den Punkt erneut → bleibt gesperrt
    located_paper.locations.append(
        {"lat": CENTER_LAT + 0.01, "lon": CENTER_LON, "name": "Hafenweg", "source": "street_match"}
    )
    assert sync_paper_locations(located_paper).suppressed == 1


def test_confirm_detail_action(admin_client_: Client, located_paper: OParlPaper) -> None:
    row = PaperLocation.objects.get(paper=located_paper, name="Prinzipalmarkt 1")
    response = admin_client_.get(reverse("admin:insight_core_paperlocation_confirm_detail", args=[row.pk]))
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.status == PaperLocation.STATUS_CONFIRMED
    assert row.reviewed_at is not None


def test_bulk_actions(admin_client_: Client, located_paper: OParlPaper) -> None:
    ids = list(PaperLocation.objects.filter(paper=located_paper).values_list("pk", flat=True))
    response = admin_client_.post(
        reverse("admin:insight_core_paperlocation_changelist"),
        {"action": "remove_selected_locations", "_selected_action": [str(pk) for pk in ids]},
    )
    assert response.status_code == 302
    assert PaperLocation.objects.filter(paper=located_paper, status=PaperLocation.STATUS_REMOVED).count() == 2
    located_paper.refresh_from_db()
    assert located_paper.locations is None


def test_paper_change_page_lists_locations_inline(admin_client_: Client, located_paper: OParlPaper) -> None:
    response = admin_client_.get(reverse("admin:insight_core_oparlpaper_change", args=[located_paper.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Prinzipalmarkt 1" in html
    assert "Hafenweg" in html
