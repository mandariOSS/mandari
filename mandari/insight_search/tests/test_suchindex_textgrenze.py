# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchindex mit begrenztem Speicher, Django-Seite.

Der Volltext je Datei kommt gekürzt in den Index (derselbe Wert wie im Ingestor). Die Projektion lädt ihn schon
gekürzt aus der Datenbank, die Vorschau eines Vorgangs nur den Anfang seiner Dateien, und der Neuaufbau
(``reindex_elasticsearch``) nutzt dieselbe Projektion und teilt seine Bulk-Anfragen nach Bytes. Vorher lud der
Neuaufbau 500 Dateien samt vollem Text auf einmal und schickte sie in einer Anfrage.
"""

from __future__ import annotations

import ast
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import elasticsearch
import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from elastic_transport import ApiResponseMeta, BaseNode, HttpHeaders
from elastic_transport._node._base import NodeApiResponse

from insight_core.models import OParlBody, OParlFile, OParlPaper
from insight_core.services.search_documents import INDEX_TEXT_MAX_CHARS, file_to_doc, paper_to_doc
from insight_core.services.search_projection import iter_documents
from insight_search.abonnement import BULK_BYTES

INGESTOR_BUILDERS = Path(__file__).resolve().parents[3] / "ingestor" / "src" / "indexing" / "document_builders.py"


def _datei(body: OParlBody, text_content: str, paper: OParlPaper | None = None, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"https://ris.example/file/{uuid.uuid4()}",
        body=body,
        paper=paper,
        name="Anlage",
        file_name=felder.pop("file_name", "anlage.txt"),
        mime_type="text/plain",
        text_content=text_content,
        text_extraction_status="completed",
        **felder,
    )


def _auswahl(sql: str) -> str:
    return sql.split(" FROM ")[0]


# --- Grenze je Dokument ---------------------------------------------------------------------------


def test_volltext_je_datei_gekuerzt() -> None:
    datei = SimpleNamespace(
        id=uuid.uuid4(),
        body_id=None,
        name="Anlage",
        file_name="anlage.txt",
        mime_type="text/plain",
        access_url="",
        text_content="ä" * (INDEX_TEXT_MAX_CHARS + 10),
        paper_id=None,
        paper=None,
        meeting_id=None,
        oparl_modified=None,
    )
    doc = file_to_doc(datei, context_info={})
    assert len(doc["text_content"]) == INDEX_TEXT_MAX_CHARS
    assert doc["text_preview"] == "ä" * 500 + "..."


def test_grenze_wie_im_ingestor() -> None:
    if not INGESTOR_BUILDERS.exists():
        pytest.skip("Quelltext des Ingestors nicht vorhanden")
    for knoten in ast.walk(ast.parse(INGESTOR_BUILDERS.read_text(encoding="utf-8"))):
        if isinstance(knoten, ast.Assign) and any(
            isinstance(ziel, ast.Name) and ziel.id == "FILE_TEXT_MAX_CHARS" for ziel in knoten.targets
        ):
            assert ast.literal_eval(knoten.value) == INDEX_TEXT_MAX_CHARS
            return
    pytest.fail("FILE_TEXT_MAX_CHARS fehlt im Ingestor (src/indexing/document_builders.py)")


# --- Laden aus der Datenbank ----------------------------------------------------------------------


@pytest.mark.django_db
def test_projektion_laedt_dateitexte_gekuerzt(kommune: Any) -> None:
    datei = _datei(kommune("Beispielstadt"), "x" * (INDEX_TEXT_MAX_CHARS + 5000))

    with CaptureQueriesContext(connection) as abfragen:
        dokumente = dict(iter_documents("files", [datei.pk]))

    assert len(dokumente[datei.pk]["text_content"]) == INDEX_TEXT_MAX_CHARS  # type: ignore[index]
    texte = [_auswahl(q["sql"]) for q in abfragen.captured_queries if "text_content" in _auswahl(q["sql"])]
    assert texte, "Text wird geladen"
    for auswahl in texte:
        assert auswahl.upper().count("TEXT_CONTENT") == 1 and "SUBSTR" in auswahl.upper(), auswahl


@pytest.mark.django_db
def test_vorschau_eines_vorgangs_laedt_nur_den_anfang(kommune: Any) -> None:
    body = kommune("Beispielstadt")
    vorgang = OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body)
    _datei(body, "A" * 6000 + " Ende", paper=vorgang, file_name="a.pdf")
    _datei(body, "  kurz  ", paper=vorgang, file_name="b.pdf")

    with CaptureQueriesContext(connection) as abfragen:
        doc = paper_to_doc(OParlPaper.objects.get(pk=vorgang.pk))

    assert sorted(doc["file_names"]) == ["a.pdf", "b.pdf"]
    vorschau = doc["file_contents_preview"]
    assert "A" * 5000 in vorschau and "Ende" not in vorschau and "kurz" in vorschau
    texte = [_auswahl(q["sql"]) for q in abfragen.captured_queries if "text_content" in _auswahl(q["sql"])]
    assert texte and all("SUBSTR" in auswahl.upper() for auswahl in texte), texte


# --- Neuaufbau ------------------------------------------------------------------------------------


class _Knoten(BaseNode):
    """Elasticsearch-Knoten im Speicher: beantwortet Ping und Bulk und merkt sich jede Bulk-Anfrage."""

    anfragen: ClassVar[list[bytes]] = []

    def perform_request(
        self,
        method: str,
        target: str,
        body: bytes | None = None,
        headers: HttpHeaders | None = None,
        request_timeout: Any = None,
    ) -> NodeApiResponse:
        kopf = HttpHeaders({"x-elastic-product": "Elasticsearch", "content-type": "application/json"})
        meta = ApiResponseMeta(status=200, http_version="1.1", headers=kopf, duration=0.0, node=self.config)
        if not target.startswith("/_bulk"):
            return NodeApiResponse(meta, b"{}")
        assert body is not None
        _Knoten.anfragen.append(body)
        aktionen = [json.loads(zeile) for zeile in body.splitlines()[::2] if zeile]
        items = [{art: {"_id": werte["_id"], "status": 201}} for aktion in aktionen for art, werte in aktion.items()]
        return NodeApiResponse(meta, json.dumps({"errors": False, "items": items}).encode())


@pytest.mark.django_db
def test_neuaufbau_teilt_bulk_anfragen_nach_bytes(kommune: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    echt = elasticsearch.Elasticsearch
    monkeypatch.setattr(elasticsearch, "Elasticsearch", lambda url, **kw: echt(url, node_class=_Knoten, **kw))
    _Knoten.anfragen = []
    body = kommune("Beispielstadt")
    # Steuerzeichen stehen in JSON sechsfach maskiert: je großes Dokument rund 6 MB
    gross = [_datei(body, "\x01" * (INDEX_TEXT_MAX_CHARS + 1000)).pk for _ in range(3)]
    klein = _datei(body, "Radweg entlang der Bahnstrecke").pk

    call_command("reindex_elasticsearch", "--index", "files", "--body", str(body.pk), stdout=io.StringIO())

    gesehen: list[str] = []
    for anfrage in _Knoten.anfragen:
        zeilen = [json.loads(zeile) for zeile in anfrage.splitlines() if zeile]
        quellen = zeilen[1::2]
        gesehen.extend(quelle["id"] for quelle in quellen)
        assert sum(quelle["id"] in {str(pk) for pk in gross} for quelle in quellen) <= 1, "große Dokumente einzeln"
        assert len(anfrage) <= BULK_BYTES or len(quellen) == 1
        assert all(len(quelle["text_content"]) <= INDEX_TEXT_MAX_CHARS for quelle in quellen)
    assert sorted(gesehen) == sorted(str(pk) for pk in [*gross, klein])
    assert len(_Knoten.anfragen) >= 3
