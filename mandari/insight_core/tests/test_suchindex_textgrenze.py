# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchindex mit begrenztem Speicher, Django-Seite.

Der Volltext je Datei kommt gekürzt und ohne Steuerzeichen in den Index (derselbe Wert wie im Ingestor). Die
Vorschau eines Vorgangs lädt von seinen Dateien nur den Anfang, und der Neuaufbau (``reindex_elasticsearch``)
lädt Dateitexte in kleinen Abschnitten, schon in der Datenbank gekürzt, und teilt seine Bulk-Anfragen nach Bytes.
Vorher lud der Neuaufbau 500 Dateien samt vollem Text auf einmal und schickte sie in einer Anfrage.
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

from insight_core import signals
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource
from insight_core.services.search_documents import INDEX_TEXT_MAX_CHARS, file_to_doc, paper_to_doc

INGESTOR_BUILDERS = Path(__file__).resolve().parents[3] / "ingestor" / "src" / "indexing" / "document_builders.py"
#: Bytegrenze je Bulk-Anfrage (wie im Ingestor)
GRENZE = 8 * 1024 * 1024


@pytest.fixture
def body(db: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="Beispielstadt", url="https://ris.example/oparl/system")
    return OParlBody.objects.create(source=quelle, external_id="https://ris.example/oparl/body/1", name="Beispielstadt")


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


def _texte_geladen(abfragen: CaptureQueriesContext) -> list[str]:
    return [_auswahl(q["sql"]) for q in abfragen.captured_queries if "text_content" in _auswahl(q["sql"])]


# --- Grenze je Dokument ---------------------------------------------------------------------------


def _datei_ohne_bestand(text_content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        body_id=None,
        name="Anlage",
        file_name="anlage.txt",
        mime_type="text/plain",
        access_url="",
        text_content=text_content,
        paper_id=None,
        paper=None,
        meeting_id=None,
        oparl_modified=None,
    )


KONTEXT = {"organization_names": []}


def test_volltext_je_datei_gekuerzt() -> None:
    assert INDEX_TEXT_MAX_CHARS == 5_000_000
    doc = file_to_doc(_datei_ohne_bestand("ä" * (INDEX_TEXT_MAX_CHARS + 10)), context_info=KONTEXT)
    assert len(doc["text_content"]) == INDEX_TEXT_MAX_CHARS
    assert doc["text_preview"] == "ä" * 500 + "..."


def test_steuerzeichen_vergroessern_das_dokument_nicht() -> None:
    doc = file_to_doc(_datei_ohne_bestand("A\x00B\x01C\tD\nE\rF\x1f" + "\x01😀" * 1000), context_info=KONTEXT)
    assert doc["text_content"] == "ABC\tD\nE\rF" + "😀" * 1000
    # Höchstens vier Bytes je Zeichen statt sechs je maskiertem Steuerzeichen
    assert len(json.dumps(doc["text_content"], ensure_ascii=False).encode()) < 4 * 2000 + 100


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


# --- Vorschau eines Vorgangs ----------------------------------------------------------------------


@pytest.mark.django_db
def test_vorschau_eines_vorgangs_laedt_nur_den_anfang(body: OParlBody) -> None:
    vorgang = OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body)
    _datei(body, "A" * 6000 + " Ende", paper=vorgang, file_name="a.pdf")
    _datei(body, "  kurz  ", paper=vorgang, file_name="b.pdf")

    with CaptureQueriesContext(connection) as abfragen:
        doc = paper_to_doc(OParlPaper.objects.get(pk=vorgang.pk))

    assert sorted(doc["file_names"]) == ["a.pdf", "b.pdf"]
    vorschau = doc["file_contents_preview"]
    assert "A" * 5000 in vorschau and "Ende" not in vorschau and "kurz" in vorschau
    texte = _texte_geladen(abfragen)
    assert texte and all("SUBSTR" in auswahl.upper() for auswahl in texte), texte


@pytest.mark.django_db
def test_neue_datei_laedt_fuer_den_vorgang_nur_den_anfang(
    body: OParlBody, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    vorgang = OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body)
    _datei(body, "B" * 6000, paper=vorgang, file_name="b.pdf")
    settings.ELASTICSEARCH_AUTO_INDEX = True
    indexiert: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(signals, "_index_document", lambda index, _id, doc: indexiert.append((index, doc)))

    with CaptureQueriesContext(connection) as abfragen:
        _datei(body, "C" * 6000, paper=vorgang, file_name="c.pdf")

    vorschau = next(doc for index, doc in indexiert if index == "papers")["file_contents_preview"]
    assert vorschau.count("B") == 5000 and vorschau.count("C") == 5000
    texte = [auswahl for auswahl in _texte_geladen(abfragen) if auswahl.upper().startswith("SELECT")]
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


@pytest.fixture
def knoten(monkeypatch: pytest.MonkeyPatch) -> type[_Knoten]:
    echt = elasticsearch.Elasticsearch
    monkeypatch.setattr(elasticsearch, "Elasticsearch", lambda url, **kw: echt(url, node_class=_Knoten, **kw))
    _Knoten.anfragen = []
    return _Knoten


@pytest.mark.django_db
def test_neuaufbau_teilt_bulk_anfragen_nach_bytes(body: OParlBody, knoten: type[_Knoten]) -> None:
    # Volle Länge: je großes Dokument rund 5 MB, zwei passen nicht in eine Anfrage
    gross = [_datei(body, "x" * (INDEX_TEXT_MAX_CHARS + 1000)).pk for _ in range(3)]
    klein = _datei(body, "Radweg entlang der Bahnstrecke").pk
    ausgabe = io.StringIO()

    with CaptureQueriesContext(connection) as abfragen:
        call_command("reindex_elasticsearch", "--index", "files", "--body", str(body.pk), stdout=ausgabe)

    gesehen: list[str] = []
    for anfrage in _Knoten.anfragen:
        zeilen = [json.loads(zeile) for zeile in anfrage.splitlines() if zeile]
        quellen = zeilen[1::2]
        gesehen.extend(quelle["id"] for quelle in quellen)
        assert sum(quelle["id"] in {str(pk) for pk in gross} for quelle in quellen) <= 1, "große Dokumente einzeln"
        assert len(anfrage) <= GRENZE or len(quellen) == 1
        assert all(len(quelle["text_content"]) <= INDEX_TEXT_MAX_CHARS for quelle in quellen)
    assert sorted(gesehen) == sorted(str(pk) for pk in [*gross, klein])
    assert len(_Knoten.anfragen) >= 3
    assert "files: 4/4 indexed" in ausgabe.getvalue()
    # Texte schon in der Datenbank gekürzt
    texte = [auswahl for auswahl in _texte_geladen(abfragen) if "COUNT(" not in auswahl.upper()]
    assert texte, "Text wird geladen"
    for auswahl in texte:
        assert auswahl.upper().count("TEXT_CONTENT") == 1 and "SUBSTR" in auswahl.upper(), auswahl


@pytest.mark.django_db
def test_neuaufbau_der_vorgaenge_laedt_nur_den_anfang(body: OParlBody, knoten: type[_Knoten]) -> None:
    vorgang = OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body)
    _datei(body, "A" * 6000 + " Ende", paper=vorgang, file_name="a.pdf")

    with CaptureQueriesContext(connection) as abfragen:
        call_command("reindex_elasticsearch", "--index", "papers", "--body", str(body.pk), stdout=io.StringIO())

    zeilen = [json.loads(zeile) for anfrage in _Knoten.anfragen for zeile in anfrage.splitlines() if zeile]
    assert [quelle["file_contents_preview"] for quelle in zeilen[1::2]] == ["A" * 5000]
    texte = _texte_geladen(abfragen)
    assert texte and all("SUBSTR" in auswahl.upper() for auswahl in texte), texte
