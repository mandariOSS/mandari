# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Dokument und Briefkopf-Formular: Startdaten per json_script statt Inline-Skript (#172).

Die Alpine-Komponenten `createMotion` und `letterheadForm` (frontend/alpine/) lesen ihre
Startwerte aus `<script type="application/json">`; Texte mit Anführungszeichen oder
Zeilenumbrüchen kommen dabei unverändert an (vorher per escapejs in JavaScript-Literale
eingebettet).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from apps.work.motions.models import MotionTemplate, MotionType, OrganizationLetterhead

pytestmark = pytest.mark.django_db


@pytest.fixture
def verwaltung(org: Any, make_member: Any) -> Any:
    return make_member(org, ["motions.create", "motions.view", "organization.edit"], email="doku@example.org")


def _json_script(html: str, element_id: str) -> Any:
    treffer = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', html, re.S)
    assert treffer, f"json_script {element_id} fehlt"
    return json.loads(treffer.group(1))


def test_neues_dokument_vorlagen_als_json(org: Any, verwaltung: Any, client_for: Any) -> None:
    antrag = MotionType.objects.create(organization=org, name="Antrag", slug="antrag", is_default=True)
    anfrage = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
    standard = MotionTemplate.objects.create(
        organization=org,
        motion_type=antrag,
        letterhead=briefkopf,
        name='Antrag "Standard"',
        description="Zeile 1\nZeile 2",
        is_default=True,
    )
    frei = MotionTemplate.objects.create(organization=org, name="Freitext")
    MotionTemplate.objects.create(organization=org, motion_type=anfrage, name="Inaktiv", is_active=False)

    html = client_for(verwaltung.user).get(f"/work/{org.slug}/documents/create/").content.decode()

    optionen = {option["id"]: option for option in _json_script(html, "motion-template-options")}
    assert set(optionen) == {str(standard.id), str(frei.id)}
    assert optionen[str(standard.id)] == {
        "id": str(standard.id),
        "name": 'Antrag "Standard"',
        "description": "Zeile 1\nZeile 2",
        "type_name": "Antrag",
        "letterhead_name": "Fraktion",
        "motion_type_id": str(antrag.id),
        "is_default": True,
    }
    assert optionen[str(frei.id)]["motion_type_id"] is None
    assert f'data-default-type="{antrag.id}"' in html
    assert 'x-data="createMotion"' in html
    assert "function createMotion" not in html


def test_briefkopf_neu_mit_vorbelegung(org: Any, verwaltung: Any, client_for: Any) -> None:
    org.address = "Rathausplatz 1\n12345 Beispielstadt"
    org.save(update_fields=["address"])

    html = client_for(verwaltung.user).get(f"/work/{org.slug}/organization/documents/letterheads/create/")
    daten = _json_script(html.content.decode(), "letterhead-form-data")

    assert daten["kind"] == "generated"
    assert daten["header_logo_enabled"] is True
    assert daten["accent_color_enabled"] is True
    assert daten["address_block"] == f"{org.name}\nRathausplatz 1\n12345 Beispielstadt"
    assert "function letterheadForm" not in html.content.decode()


def test_briefkopf_bearbeiten_mit_gespeicherten_werten(org: Any, verwaltung: Any, client_for: Any) -> None:
    briefkopf = OrganizationLetterhead.objects.create(
        organization=org,
        name="Fraktion",
        kind="generated",
        header_logo_enabled=False,
        accent_color_enabled=True,
        sender_line="Fraktion 'Mitte' · Rathaus",
        address_block="Fraktion\nRathaus",
        footer_text="kontakt@example.org",
    )
    html = (
        client_for(verwaltung.user)
        .get(f"/work/{org.slug}/organization/documents/letterheads/{briefkopf.id}/")
        .content.decode()
    )

    assert _json_script(html, "letterhead-form-data") == {
        "kind": "generated",
        "header_logo_enabled": False,
        "accent_color_enabled": True,
        "sender_line": "Fraktion 'Mitte' · Rathaus",
        "address_block": "Fraktion\nRathaus",
        "footer_text": "kontakt@example.org",
    }
    assert 'data-preview-url="' in html
