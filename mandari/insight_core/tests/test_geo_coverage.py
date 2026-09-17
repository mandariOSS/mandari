# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kommunen ohne OSM-Zuordnung: Service, Command und Admin-Filter (Issue #54)."""

from __future__ import annotations

from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from insight_core.models import OParlBody, OParlSource, Street
from insight_core.services.geo_coverage import geo_status_for_bodies

pytestmark = pytest.mark.django_db


@pytest.fixture
def unmapped_body(geo_body: OParlBody) -> OParlBody:
    return OParlBody.objects.create(
        external_id="https://ris.ohne-osm.example/oparl/bodies/1",
        source=OParlSource.objects.get(),
        name="Ohne OSM",
        slug="ohne-osm",
    )


def test_geo_status_lists_gaps(
    geo_body: OParlBody, unmapped_body: OParlBody, make_street: Callable[..., Street]
) -> None:
    make_street(geo_body, "Hafenweg", 51.94, 7.63)
    statuses = {s.body.slug: s for s in geo_status_for_bodies(only_gaps=False)}

    assert statuses["beispielstadt"].missing == ["Adressen"]
    assert statuses["ohne-osm"].missing == ["osm_relation_id", "AGS", "Bounding-Box", "Straßenverzeichnis", "Adressen"]
    assert statuses["ohne-osm"].street_count == 0

    gaps_only = [s.body.slug for s in geo_status_for_bodies()]
    assert gaps_only == ["beispielstadt", "ohne-osm"]


def test_command_output(geo_body: OParlBody, unmapped_body: OParlBody) -> None:
    out = StringIO()
    call_command("check_body_geodata", stdout=out)
    text = out.getvalue()
    assert "Ohne OSM" in text
    assert "osm_relation_id, AGS" in text
    assert "2 Kommune(n) mit Lücken" in text


def test_command_all_complete(db: Any) -> None:
    out = StringIO()
    call_command("check_body_geodata", stdout=out)
    assert "vollständig" in out.getvalue()


def test_admin_filter_without_osm(geo_body: OParlBody, unmapped_body: OParlBody) -> None:
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)

    response = client.get(reverse("admin:insight_core_oparlbody_changelist"), {"geo": "ohne_osm"})
    assert response.status_code == 200
    html = response.content.decode()
    assert "Ohne OSM" in html
    assert 'value="beispielstadt"' not in html and "Beispielstadt</a>" not in html
