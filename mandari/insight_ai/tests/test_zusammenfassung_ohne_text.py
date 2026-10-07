# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassung ohne gespeicherten Text (Issue #919, ADR Dokumentkette, Abschnitt 9).

- Der Dienst lädt und erkennt nichts selbst. Steht die Erkennung einer Anlage aus, zeigt die Vorgangsseite einen
  Hinweis (kein „kein Text“, das sich die Ansicht sechs Stunden merkte).
- Mit Vorrang eingeplant wird die Erkennung nur, wenn Aufträge im Journal landen und der Worker den Text erkennt
  (``TASKS_BACKEND=journal``, ``TEXT_EXTRACTION_RUNNER=worker``); sonst nie, schon gar nicht in der Anfrage.
- Ein wiederholter Klick reiht nichts doppelt ein; höchstens ``MAX_PRIORITY_EXTRACTIONS`` Anlagen je Anfrage.
- Kommt während der Erstellung neuer Text hinzu, wird das Ergebnis nicht gespeichert.

Nur erfundene Namen.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from itertools import count
from typing import Any

import pytest
from django.tasks import task_backends
from django.test import Client
from django.utils import timezone

from apps.events.models import Task, TaskStatus
from apps.events.tasks_backend import JournalBackend
from insight_ai.services import summarizer
from insight_ai.services.summarizer import (
    EXTRACTION_PRIORITY,
    MAX_PRIORITY_EXTRACTIONS,
    NoTextContentError,
    SummaryService,
    TextPendingError,
)
from insight_core.background_tasks import file_extract_text
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource
from insight_core.services import summary_guard

pytestmark = pytest.mark.django_db

_nummer = count()


@dataclass
class _Antwort:
    content: str
    input_tokens: int = 1
    output_tokens: int = 1


class _Anbieter:
    def __init__(self) -> None:
        self.aufrufe = 0

    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
        self.aufrufe += 1
        return _Antwort("Kurzfassung")


@pytest.fixture
def anbieter(monkeypatch: pytest.MonkeyPatch) -> _Anbieter:
    neu = _Anbieter()
    monkeypatch.setattr("insight_ai.services.summarizer.NebiusProvider", lambda: neu)
    return neu


@pytest.fixture
def journal(settings: Any) -> Iterator[JournalBackend]:
    """Aufträge landen im Journal, der Worker erkennt den Text."""
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.TEXT_EXTRACTION_RUNNER = "worker"
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    yield backend


@pytest.fixture
def body() -> OParlBody:
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    return OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt")


def _vorgang(body: OParlBody) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"https://ris.example.org/oparl/paper/{next(_nummer)}", body=body, name="Vorlage"
    )


def _anlage(paper: OParlPaper, status: str = "pending", **felder: Any) -> OParlFile:
    nummer = next(_nummer)
    werte: dict[str, Any] = {
        "external_id": f"https://ris.example.org/oparl/file/{nummer}",
        "body": paper.body,
        "paper": paper,
        "name": f"Anlage {nummer}",
        "download_url": f"https://ris.example.org/dokumente/{nummer}.pdf",
        "mime_type": "application/pdf",
        "text_extraction_status": status,
        **felder,
    }
    return OParlFile.objects.create(**werte)


def _vorrang() -> list[tuple[str, int]]:
    """Eingereihte Aufträge file.extract_text als (Datei, Priorität)."""
    return [
        (str(zeile.args["args"][0]), zeile.priority)
        for zeile in Task.objects.filter(task_path=file_extract_text.module_path, status=TaskStatus.WARTEND)
    ]


def test_wartende_erkennung_gibt_hinweis_statt_kein_text(body: OParlBody, anbieter: _Anbieter) -> None:
    paper = _vorgang(body)
    _anlage(paper, "pending")
    with pytest.raises(TextPendingError):
        SummaryService().generate_summary(paper)
    assert anbieter.aufrufe == 0


def test_ohne_ausstehende_erkennung_bleibt_es_bei_kein_text(body: OParlBody, anbieter: _Anbieter) -> None:
    """Gescheitert, übersprungen, ohne Adresse: Da kommt kein Text mehr, auch nicht durch einen Download."""
    paper = _vorgang(body)
    _anlage(paper, "failed")
    _anlage(paper, "skipped")
    _anlage(paper, "pending", download_url=None, access_url=None)
    with pytest.raises(NoTextContentError):
        SummaryService().generate_summary(paper)
    assert not _vorrang()


def test_quelle_ohne_dateiabruf_gibt_kein_text(body: OParlBody, anbieter: _Anbieter, journal: JournalBackend) -> None:
    """Dokumente nur hinter einer Prüfung für Menschen: Die Erkennung kommt nie, also kein Dauerhinweis."""
    from insight_core.services import file_cache

    assert summarizer.FILE_DOWNLOADS_KEY == file_cache.FILE_DOWNLOADS_KEY
    assert body.source is not None
    body.source.sync_config = {file_cache.FILE_DOWNLOADS_KEY: False}
    body.source.save(update_fields=["sync_config"])
    paper = _vorgang(body)
    _anlage(paper, "pending")
    with pytest.raises(NoTextContentError):
        SummaryService().generate_summary(OParlPaper.objects.select_related("body__source").get(pk=paper.pk))
    assert not _vorrang()


def test_mit_journal_und_worker_mit_vorrang_eingeplant(
    body: OParlBody, anbieter: _Anbieter, journal: JournalBackend
) -> None:
    paper = _vorgang(body)
    # Ohne abgelegten Inhalt endete der Auftrag ohne Wirkung (er liest nur aus der Ablage): kein Platz dafür
    _anlage(paper, "pending")
    wartend = [_anlage(paper, "pending", local_status="ok") for _ in range(MAX_PRIORITY_EXTRACTIONS + 1)]
    _anlage(paper, "processing", local_status="ok")  # schon beansprucht
    with pytest.raises(TextPendingError):
        SummaryService().generate_summary(paper)
    erwartet = sorted((str(datei.pk), EXTRACTION_PRIORITY) for datei in wartend[:MAX_PRIORITY_EXTRACTIONS])
    assert sorted(_vorrang()) == erwartet
    assert EXTRACTION_PRIORITY > 0, "vor den Aufträgen aus Zeitplan und Ereignissen (Priorität 0)"
    # Der Auftrag liegt in der Warteschlange ocr, nichts wurde in der Anfrage erkannt
    assert set(Task.objects.values_list("queue", flat=True)) == {"ocr"}
    assert set(OParlFile.objects.values_list("text_extraction_status", flat=True)) == {"pending", "processing"}


def test_wiederholter_klick_reiht_nicht_doppelt_ein(
    body: OParlBody, anbieter: _Anbieter, journal: JournalBackend
) -> None:
    paper = _vorgang(body)
    datei = _anlage(paper, "pending", local_status="ok")
    # Ein gewöhnlicher Auftrag aus dem Zeitplan wartet schon: Der mit Vorrang überholt ihn
    file_extract_text.enqueue(str(datei.pk))
    for _ in range(3):
        with pytest.raises(TextPendingError):
            SummaryService().generate_summary(paper)
    assert sorted(_vorrang()) == [(str(datei.pk), 0), (str(datei.pk), EXTRACTION_PRIORITY)]


@pytest.mark.parametrize(
    ("backend", "runner"),
    [
        # Sofort ausführendes Backend: Der Auftrag liefe in der Webanfrage
        ("django.tasks.backends.immediate.ImmediateBackend", "worker"),
        # Der OCR-Worker des Ingestors erkennt den Text; ein Auftrag wäre ein zweiter Weg
        ("apps.events.tasks_backend.JournalBackend", "ingestor"),
    ],
)
def test_sonst_nur_hinweis(
    body: OParlBody, anbieter: _Anbieter, settings: Any, monkeypatch: pytest.MonkeyPatch, backend: str, runner: str
) -> None:
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": backend}}
    settings.TEXT_EXTRACTION_RUNNER = runner

    erkannt: list[Any] = []

    def keine_erkennung(file_id: Any, *_a: Any, **_kw: Any) -> str:
        erkannt.append(file_id)
        return "erledigt"

    # Der Auftrag file.extract_text ruft hub.ris.erkennung.erkennen (Issue #919)
    monkeypatch.setattr("hub.ris.erkennung.erkennen", keine_erkennung)
    paper = _vorgang(body)
    _anlage(paper, "pending", local_status="ok")
    with pytest.raises(TextPendingError):
        SummaryService().generate_summary(paper)
    assert erkannt == [], "keine Texterkennung in der Anfrage"
    assert not Task.objects.exists()
    assert not summarizer.priority_extraction_enabled()


def test_fehler_beim_einplanen_aendert_den_hinweis_nicht(
    body: OParlBody, anbieter: _Anbieter, journal: JournalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def kaputt(_ids: Any) -> int:
        raise RuntimeError("Journal nicht erreichbar")

    monkeypatch.setattr(summarizer, "plan_priority_extraction", kaputt)
    paper = _vorgang(body)
    _anlage(paper, "pending")
    with pytest.raises(TextPendingError):
        SummaryService().generate_summary(paper)


def test_neuer_text_waehrend_der_erstellung_wird_nicht_gespeichert(body: OParlBody) -> None:
    paper = _vorgang(body)
    alt = _anlage(paper, "completed", text_content="Alter Text", text_extracted_at=timezone.now() - timedelta(days=1))

    class _MitNeuemText(_Anbieter):
        def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
            OParlFile.objects.filter(pk=alt.pk).update(text_content="Neuer Text", text_extracted_at=timezone.now())
            return super().chat_completion(messages, **kwargs)

    assert SummaryService(provider=_MitNeuemText()).generate_summary(paper) == "Kurzfassung"
    paper.refresh_from_db()
    assert paper.summary is None

    # Ohne neuen Text wird gespeichert
    assert SummaryService(provider=_Anbieter()).generate_summary(paper) == "Kurzfassung"
    paper.refresh_from_db()
    assert paper.summary == "Kurzfassung"


def test_vorgangsseite_zeigt_hinweis_und_merkt_sich_kein_kein_text(body: OParlBody, anbieter: _Anbieter) -> None:
    paper = _vorgang(body)
    _anlage(paper, "pending")
    antwort = Client().post(f"/insight/vorgaenge/{paper.pk}/zusammenfassung/")
    inhalt = antwort.content.decode()
    assert antwort.status_code == 200
    assert "werden gerade erkannt" in inhalt
    assert "Erneut versuchen" in inhalt
    assert not summary_guard.has_no_text(paper.pk)
    assert anbieter.aufrufe == 0
