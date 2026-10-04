# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auftrag ``file.extract_text`` und Zeitplan ``texterkennung_einplanen`` (Issue #530).

Standard bleibt der OCR-Worker des Ingestors (``TEXT_EXTRACTION_RUNNER=ingestor``); mit ``worker`` beansprucht
der Zeitplan wartende Dateien und reiht je Datei einen Auftrag in die Warteschlange ``ocr`` ein. Abruf und
Texterkennung sind ersetzt (kein Netz, keine Werkzeuge).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone
from mandari_dokumente import ExtractionResult, OcrMemoryLimitError

from apps.events.models import Task, TaskStatus
from insight_core.background_tasks import file_extract_text
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import document_extraction, text_extraction_job
from insight_core.services.document_extraction import DownloadedFile, RobotsBlockedError, RobotsUnreachableError

_nummer = count()
WORKER = override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_EXTRACTION_QUEUE_DEPTH=2)


@pytest.fixture
def body(db: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    # Nicht gelistet: keine Dokumentablage, der Auftrag lädt selbst
    return OParlBody.objects.create(
        external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Musterstadt", is_listed=False
    )


def _datei(body: OParlBody, **felder: Any) -> OParlFile:
    werte: dict[str, Any] = {
        "external_id": f"https://ris.example.org/oparl/file/{next(_nummer)}",
        "body": body,
        "download_url": "https://ris.example.org/dokumente/anlage.pdf",
        "mime_type": "application/pdf",
        "file_name": "anlage.pdf",
        **felder,
    }
    return OParlFile.objects.create(**werte)


def _geladen(tmp_path: Path, inhalt: bytes = b"%PDF-1.4 Anlage") -> DownloadedFile:
    pfad = tmp_path / f"{next(_nummer)}.part"
    pfad.write_bytes(inhalt)
    return DownloadedFile(path=pfad, size=len(inhalt), sha256=hashlib.sha256(inhalt).hexdigest(), content_type="")


def _auftraege() -> list[Task]:
    return list(Task.objects.filter(task_path=text_extraction_job.TASK_PATH))


def test_auftrag_in_der_warteschlange_ocr() -> None:
    assert file_extract_text.queue_name == "ocr"
    assert f"{file_extract_text.func.__module__}.{file_extract_text.func.__name__}" == text_extraction_job.TASK_PATH


@pytest.mark.django_db
def test_standard_ingestor_plant_nichts(body: OParlBody) -> None:
    datei = _datei(body)
    assert text_extraction_job.plan() == 0
    datei.refresh_from_db()
    assert datei.text_extraction_status == "pending"
    assert _auftraege() == []


@pytest.mark.django_db
@WORKER
def test_plan_beansprucht_und_reiht_begrenzt_ein(body: OParlBody) -> None:
    dateien = [_datei(body) for _ in range(3)]
    _datei(body, deleted=True)
    _datei(body, download_url=None)
    _datei(body, size=900 * 1024 * 1024)
    # Quelle mit abgeschaltetem Dateiabruf (Zugangsprüfung vor den Dokumenten)
    gesperrte_quelle = OParlSource.objects.create(
        name="Gesperrt", url="https://ris2.example.org/oparl/system", sync_config={"file_downloads": False}
    )
    gesperrt = OParlBody.objects.create(
        external_id="https://ris2.example.org/oparl/body/1", source=gesperrte_quelle, name="Gesperrt", is_listed=False
    )
    _datei(gesperrt)

    assert text_extraction_job.plan() == 2

    auftraege = _auftraege()
    assert {a.queue for a in auftraege} == {"ocr"} and {a.status for a in auftraege} == {TaskStatus.WARTEND}
    beansprucht = {str(d.pk) for d in OParlFile.objects.filter(text_extraction_status="processing")}
    assert beansprucht == {a.args["args"][0] for a in auftraege}
    assert len(beansprucht) == 2 and beansprucht <= {str(d.pk) for d in dateien}
    assert all(
        d.text_extraction_started_at is not None for d in OParlFile.objects.filter(text_extraction_status="processing")
    )
    # Rückstau voll: kein weiterer Auftrag
    assert text_extraction_job.plan() == 0
    # Nach dem Abarbeiten kommt die dritte Datei dran, die der gesperrten Quelle nie
    Task.objects.filter(task_path=text_extraction_job.TASK_PATH).update(status=TaskStatus.ERLEDIGT)
    assert text_extraction_job.plan() == 1
    assert OParlFile.objects.filter(body=gesperrt, text_extraction_status="pending").count() == 1


@pytest.mark.django_db
@WORKER
def test_plan_stellt_haengende_zurueck_und_gibt_auf(body: OParlBody) -> None:
    alt = timezone.now() - timedelta(hours=2)
    aufgeben = _datei(
        body, text_extraction_status="processing", text_extraction_attempts=3, text_extraction_started_at=alt
    )
    zurueck = _datei(
        body, text_extraction_status="processing", text_extraction_attempts=1, text_extraction_started_at=alt
    )

    assert text_extraction_job.release_stale() == (1, 1)

    aufgeben.refresh_from_db()
    zurueck.refresh_from_db()
    assert aufgeben.text_extraction_status == "failed"
    assert (aufgeben.text_extraction_error or "").startswith("Speichergrenze")
    assert (zurueck.text_extraction_status, zurueck.text_extraction_attempts) == ("pending", 1)


@pytest.mark.django_db
def test_auftrag_erkennt_text_und_speichert(body: OParlBody, tmp_path: Path) -> None:
    datei = _datei(body, text_extraction_status="processing")
    geladen = _geladen(tmp_path)
    ergebnis = ExtractionResult("Beschluss zum Radweg", "tesseract", 3)

    with (
        mock.patch.object(document_extraction, "download_to_file", return_value=geladen) as laden,
        mock.patch.object(text_extraction_job, "extract_text", return_value=ergebnis) as erkennen,
    ):
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.ERLEDIGT

    laden.assert_called_once()
    assert erkennen.call_args.args[0] == geladen.path
    datei.refresh_from_db()
    assert datei.text_content == "Beschluss zum Radweg"
    assert (datei.text_extraction_status, datei.text_extraction_method, datei.page_count) == (
        "completed",
        "tesseract",
        3,
    )
    assert (datei.text_extraction_attempts, datei.text_extraction_started_at) == (0, None)
    assert datei.sha256_hash == geladen.sha256 and datei.text_extracted_at is not None
    assert not geladen.path.exists(), "temporäre Datei aufgeräumt"
    # Wiederholt zugestellt: erledigte Datei wird übersprungen
    assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.NICHT_ZU_TUN


@pytest.mark.django_db
def test_vorhandene_kopie_statt_download(body: OParlBody, tmp_path: Path) -> None:
    kopie = tmp_path / "kopie.pdf"
    kopie.write_bytes(b"%PDF-1.4")
    datei = _datei(body, local_path=str(kopie), sha256_hash="a" * 64)

    with (
        mock.patch.object(document_extraction, "download_to_file", side_effect=AssertionError("kein Download")),
        mock.patch.object(text_extraction_job, "extract_text", return_value=ExtractionResult("Text", "pypdf", 1)),
    ):
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.ERLEDIGT
    assert kopie.exists()


@pytest.mark.django_db
def test_speichergrenze_und_abruffehler(body: OParlBody, tmp_path: Path) -> None:
    grenze = _datei(body)
    with (
        mock.patch.object(document_extraction, "download_to_file", return_value=_geladen(tmp_path)),
        mock.patch.object(
            text_extraction_job, "extract_text", side_effect=OcrMemoryLimitError("Speichergrenze: 3 von 3 Seiten")
        ),
    ):
        assert text_extraction_job.extract_file(str(grenze.pk)) == text_extraction_job.GESCHEITERT
    grenze.refresh_from_db()
    assert (grenze.text_extraction_status, grenze.text_extraction_error) == ("failed", "Speichergrenze: 3 von 3 Seiten")

    gesperrt = _datei(body)
    with mock.patch.object(
        document_extraction, "download_to_file", side_effect=RobotsBlockedError("robots.txt sperrt")
    ):
        assert text_extraction_job.extract_file(str(gesperrt.pk)) == text_extraction_job.UEBERSPRUNGEN
    gesperrt.refresh_from_db()
    assert gesperrt.text_extraction_status == "skipped"

    stoerung = _datei(body)
    with mock.patch.object(document_extraction, "download_to_file", side_effect=RobotsUnreachableError("HTTP 503")):
        assert text_extraction_job.extract_file(str(stoerung.pk)) == text_extraction_job.ZURUECKGESTELLT
    stoerung.refresh_from_db()
    assert (stoerung.text_extraction_status, stoerung.text_extraction_attempts) == ("pending", 0)
