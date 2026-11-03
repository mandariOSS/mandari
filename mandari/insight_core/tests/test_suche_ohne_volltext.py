# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die Suche lädt keine Volltexte aus dem Suchindex.

Trifft eine Datei nur über ihren Namen, gibt es kein Textfragment der Hervorhebung. Die Treffer enthalten dann weder
den Volltext (``_source`` ohne ``text_content`` bzw. ``file_contents_preview``) noch eine Kopie davon in
``_formatted``; Ausschnitte kommen nur aus der Hervorhebung, sonst aus dem kurzen ``text_preview``. Das gilt für die
Trefferliste (Kopfzeilensuche, KI-Werkzeuge) und für die Suche in Work.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from django.urls import reverse

from insight_core.models import OParlBody, OParlSource
from insight_core.services import search_service
from insight_core.services.search_service import ElasticsearchService

WORT = "Bordstein "
VOLLTEXT = WORT * 30_000
VORSCHAU = VOLLTEXT[:500] + "..."


class _Indizes:
    def exists(self, index: str) -> bool:
        return True


class _Suchindex:
    """Elasticsearch-Ersatz mit einer Datei, die nur über ihren Namen trifft; beachtet ``_source`` wie der Index."""

    def __init__(self) -> None:
        self.aufrufe: list[dict[str, Any]] = []
        self.indices = _Indizes()

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.aufrufe.append(body)
        if index != "files":
            return {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}, "max_score": None}}
        quelle: dict[str, Any] = {
            "id": "f1",
            "type": "file",
            "name": "Haushaltsplan 2027",
            "file_name": "haushalt.pdf",
            "paper_id": None,
            "text_content": VOLLTEXT,
            "text_preview": VORSCHAU,
        }
        auswahl = body.get("_source", True)
        if isinstance(auswahl, dict):
            quelle = {k: v for k, v in quelle.items() if k not in auswahl.get("excludes", [])}
        elif isinstance(auswahl, list):
            quelle = {k: v for k, v in quelle.items() if k in auswahl}
        hit: dict[str, Any] = {"_id": "f1", "_score": 3.0}
        if auswahl is not False:
            hit["_source"] = quelle
        if "highlight" in body:
            # Nur der Name trifft: kein Fragment aus dem Text
            hit["highlight"] = {"name": [f"{search_service.HIGHLIGHT_PRE}Haushalt{search_service.HIGHLIGHT_POST}splan"]}
        return {"hits": {"hits": [hit], "total": {"value": 1, "relation": "eq"}, "max_score": 3.0}}

    def count(self, index: str, query: dict[str, Any], **_: Any) -> dict[str, int]:
        return {"count": 1}


def _dienst() -> tuple[Any, _Suchindex]:
    dienst = cast(Any, ElasticsearchService).__new__(ElasticsearchService)
    dienst.client = _Suchindex()
    return dienst, dienst.client


def test_namenstreffer_ohne_textfragment_liefert_keinen_volltext() -> None:
    dienst, client = _dienst()

    ergebnis = dienst.search_all("Haushalt", index_names=["files"])

    (treffer,) = ergebnis["results"]
    assert "text_content" not in treffer
    assert "text_content" not in treffer.get("_formatted", {})
    assert treffer["text_preview"] == VORSCHAU
    assert len(json.dumps(ergebnis, default=str)) < 10_000
    # Auch das Laden der Seite fragt den Volltext nicht ab
    mit_hervorhebung = [body for body in client.aufrufe if "highlight" in body]
    assert mit_hervorhebung and all("text_content" in body["_source"]["excludes"] for body in mit_hervorhebung)


def test_nur_fragmente_aus_der_hervorhebung_auch_wenn_die_quelle_den_text_enthaelt() -> None:
    treffer = ElasticsearchService._to_result(
        {
            "_id": "f1",
            "_score": 1.0,
            "_source": {"id": "f1", "name": "Plan", "text_content": VOLLTEXT, "text_preview": VORSCHAU},
            "highlight": {"name": ["<mark>Plan</mark>"], "text_content": []},
        },
        "files",
    )

    assert "text_content" not in treffer and "text_content" not in treffer["_formatted"]
    assert treffer["_formatted"]["name"] == "<mark>Plan</mark>"


@pytest.mark.django_db
def test_work_suche_zeigt_den_kurzen_anfang_statt_des_volltexts(
    org: Any, make_member: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_service, "ElasticsearchService", lambda: _dienst()[0])
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    org.body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt")
    org.save(update_fields=["body"])
    mitglied = make_member(org, ["ris.view"], email="suche-volltext@example.org")

    antwort = client_for(mitglied.user).get(
        reverse("work:ris_search", kwargs={"org_slug": org.slug}), {"q": "Haushalt"}
    )

    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Haushaltsplan 2027" in html and WORT.strip() in html
    assert html.count(WORT.strip()) <= len(VORSCHAU) // len(WORT) + 1
