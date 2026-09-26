# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Treffer der RIS-Suche werden maskiert ausgegeben; nur die Such-Markierung bleibt HTML.

Namen und OCR-Volltexte kommen aus fremden Ratsinformationssystemen und PDFs. Elasticsearch
liefert die Hervorhebung als Rohtext mit eingefügten ``<mark>``-Tags – ohne Maskierung.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.urls import reverse

from insight_core.models import OParlBody, OParlSource
from insight_core.services import search_service
from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE

BOESER_NAME = f'<img src=x onerror="alert(1)">{HIGHLIGHT_PRE}Radweg{HIGHLIGHT_POST}'
BOESER_TEXT = f"…<script>alert(2)</script> der {HIGHLIGHT_PRE}Radweg{HIGHLIGHT_POST} wird…"


@pytest.fixture
def fake_es(monkeypatch: Any) -> None:
    class FakeService:
        def search_all(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "results": [
                    {
                        "id": str(uuid.uuid4()),
                        "type": "paper",
                        "name": '<img src=x onerror="alert(1)">Radweg',
                        "_formatted": {"name": BOESER_NAME},
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "type": "file",
                        "name": "Anlage",
                        "file_name": "anlage.pdf",
                        "paper_id": None,
                        "paper_name": "",
                        "meeting_date": "",
                        "_formatted": {"text_content": BOESER_TEXT},
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "type": "person",
                        "name": "<b onmouseover=alert(3)>Person</b>",
                        # Ohne Treffer im Namen kopiert der Suchdienst die Rohfelder
                        "_formatted": {"name": "<b onmouseover=alert(3)>Person</b>", "text_content": ""},
                    },
                ],
                "total": 3,
                "page": 1,
                "pages": 1,
            }

    monkeypatch.setattr(search_service, "ElasticsearchService", FakeService)


@pytest.mark.django_db
def test_suchtreffer_sind_maskiert_nur_markierung_bleibt(
    org: Any, make_member: Any, client_for: Any, fake_es: None
) -> None:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    member = make_member(org, ["ris.view"], email="suche@example.org")

    response = client_for(member.user).get(reverse("work:ris_search", kwargs={"org_slug": org.slug}), {"q": "Radweg"})

    assert response.status_code == 200
    html = response.content.decode()
    assert "<img src=x" not in html
    assert "<script>alert(2)" not in html
    assert "<b onmouseover" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert f"{HIGHLIGHT_PRE}Radweg{HIGHLIGHT_POST}" in html
