# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO-Datenexport: Startdaten per json_script statt Inline-Skript (#172).

Die Seite liefert die Exportliste im selben Format wie die Statusabfrage, damit die
Alpine-Komponente `dataExport` (frontend/alpine/profile.ts) beide gleich behandelt.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from django.utils import timezone

from apps.work.organization.models import DataExport

pytestmark = pytest.mark.django_db


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="export@example.org")


def _json_script(html: str, element_id: str) -> Any:
    treffer = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', html, re.S)
    assert treffer, f"json_script {element_id} fehlt"
    return json.loads(treffer.group(1))


def test_exporte_als_json_script(org: Any, mitglied: Any, client_for: Any) -> None:
    fertig = DataExport.objects.create(
        organization=org, membership=mitglied, status="completed", export_format="pdf", file_size=2048
    )
    # Wie im Auftrag: "in Arbeit" immer mit Startzeit (ohne gilt der Export als abgebrochen)
    laufend = DataExport.objects.create(
        organization=org, membership=mitglied, status="processing", started_at=timezone.now()
    )

    client = client_for(mitglied.user)
    html = client.get(f"/work/{org.slug}/profile/data/").content.decode()

    daten = {eintrag["id"]: eintrag for eintrag in _json_script(html, "data-exports")}
    assert set(daten) == {str(fertig.id), str(laufend.id)}
    assert daten[str(fertig.id)]["download_url"] == f"/work/{org.slug}/profile/data/exports/{fertig.id}/download/"
    assert daten[str(laufend.id)]["is_in_progress"] is True
    assert 'x-data="dataExport"' in html
    assert 'data-active="true"' in html
    assert "function dataExport" not in html

    # Statusabfrage und Startdaten teilen sich das Format
    status = client.get(f"/work/{org.slug}/profile/data/exports/{fertig.id}/status/").json()
    assert status == daten[str(fertig.id)]


def test_ohne_exporte_leere_liste(org: Any, mitglied: Any, client_for: Any) -> None:
    html = client_for(mitglied.user).get(f"/work/{org.slug}/profile/data/").content.decode()
    assert _json_script(html, "data-exports") == []
    assert 'data-active="false"' in html
