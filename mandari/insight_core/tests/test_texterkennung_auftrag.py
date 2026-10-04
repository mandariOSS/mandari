# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auftrag ``file.extract_text`` und Zeitplan ``texterkennung_einplanen`` (Issue #530).

Standard bleibt der OCR-Worker des Ingestors (``TEXT_EXTRACTION_RUNNER=ingestor``); mit ``worker`` beansprucht
der Zeitplan wartende Dateien und reiht je Datei einen Auftrag in die Warteschlange ``ocr`` ein. Abruf und
Texterkennung sind ersetzt (kein Netz, keine Werkzeuge).
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from mandari_dokumente import ExtractionResult, OcrMemoryLimitError

from apps.events.models import Event, Task, TaskStatus
from insight_core.background_tasks import file_extract_text
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import document_extraction, file_cache, text_extraction_job
from insight_core.services.document_extraction import DownloadedFile, RobotsBlockedError, RobotsUnreachableError
from insight_core.services.text_extraction_health import extraction_health

_nummer = count()
WORKER = override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_EXTRACTION_QUEUE_DEPTH=2)


@pytest.fixture(autouse=True)
def _ohne_zurueckgestellte_kommunen() -> Any:
    """Zurückgestellte Kommunen liegen im Zwischenspeicher; jeder Test beginnt ohne."""
    cache.clear()
    yield
    cache.clear()


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
    # Zeitpunkt der Aufgabe für die Prüfung „texterkennung“ (aufgegeben in 24 h)
    assert aufgeben.text_extracted_at is not None
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
def test_auftrag_meldet_erkannten_text_intern(body: OParlBody, tmp_path: Path, settings: Any) -> None:
    """``ris.file.text_extracted`` wie im Ingestor (Issue #821): nur mit Text, intern, ohne den Text selbst."""
    settings.INGESTOR_EVENTS_ENABLED = True
    mit_text, ohne_text = (
        _datei(body, text_extraction_status="processing"),
        _datei(body, text_extraction_status="processing"),
    )
    ergebnisse = [ExtractionResult("Beschluss zum Radweg", "tesseract", 3), ExtractionResult("", "none", 1)]

    with (
        mock.patch.object(document_extraction, "download_to_file", side_effect=lambda *a, **k: _geladen(tmp_path)),
        mock.patch.object(text_extraction_job, "extract_text", side_effect=ergebnisse),
    ):
        assert text_extraction_job.extract_file(str(mit_text.pk)) == text_extraction_job.ERLEDIGT
        assert text_extraction_job.extract_file(str(ohne_text.pk)) == text_extraction_job.OHNE_TEXT

    (ereignis,) = Event.objects.filter(type="ris.file.text_extracted")
    assert (ereignis.aggregate_type, ereignis.aggregate_id, ereignis.body_id) == ("File", mit_text.pk, body.pk)
    assert (ereignis.visibility, ereignis.tenant_ref) == ("intern", f"source:{body.source_id}")
    assert ereignis.payload == {"file": str(mit_text.pk), "method": "tesseract", "characters": 20}


@pytest.mark.django_db
def test_ohne_schalter_oder_bei_fehler_bleibt_das_ergebnis_ohne_ereignis(
    body: OParlBody, tmp_path: Path, settings: Any
) -> None:
    datei = _datei(body, text_extraction_status="processing")
    ergebnis = ExtractionResult("Beschluss zum Radweg", "pypdf", 1)
    with (
        mock.patch.object(document_extraction, "download_to_file", side_effect=lambda *a, **k: _geladen(tmp_path)),
        mock.patch.object(text_extraction_job, "extract_text", return_value=ergebnis),
    ):
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.ERLEDIGT
        assert not Event.objects.exists()  # INGESTOR_EVENTS_ENABLED aus

        settings.INGESTOR_EVENTS_ENABLED = True
        OParlFile.objects.filter(pk=datei.pk).update(text_extraction_status="processing")
        with mock.patch("hub.ris.text_extraction.publish", side_effect=RuntimeError("Journal nicht erreichbar")):
            assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.ERLEDIGT

    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_content) == ("completed", "Beschluss zum Radweg")
    assert not Event.objects.exists()


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


def _kommune(nummer: int, **quelle: Any) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {nummer}", url=f"https://ris{nummer}.example.org/oparl/system", **quelle
    )
    return OParlBody.objects.create(
        external_id=f"https://ris{nummer}.example.org/oparl/body/1", source=source, name="Musterstadt", is_listed=False
    )


def _abarbeiten() -> list[str]:
    """Wartende Aufträge ausführen wie der Runner und als erledigt markieren; Rückgabe: Ergebniscodes."""
    ergebnisse = []
    for auftrag in Task.objects.filter(task_path=text_extraction_job.TASK_PATH, status=TaskStatus.WARTEND):
        ergebnisse.append(text_extraction_job.extract_file(auftrag.args["args"][0]))
        Task.objects.filter(pk=auftrag.pk).update(status=TaskStatus.ERLEDIGT)
    return ergebnisse


@pytest.mark.django_db
@WORKER
def test_zurueckgestellte_aeltere_dateien_blockieren_die_warteschlange_nicht(tmp_path: Path) -> None:
    """
    Die zwei ältesten Dateien stammen aus einer Quelle mit unlesbarer robots.txt (HTTP 503) und werden bei jedem
    Versuch zurückgestellt. Die neuere Datei einer anderen Quelle kommt trotzdem dran (Tiefe 2).
    """
    gestoert = _kommune(1)
    erreichbar = _kommune(2)
    jetzt = timezone.now()
    alt1 = _datei(gestoert, download_url="https://ris1.example.org/dokumente/a.pdf")
    alt2 = _datei(gestoert, download_url="https://ris1.example.org/dokumente/b.pdf")
    neu = _datei(erreichbar, download_url="https://ris2.example.org/dokumente/c.pdf")
    for datei, alter in ((alt1, 3), (alt2, 2), (neu, 1)):
        OParlFile.objects.filter(pk=datei.pk).update(created_at=jetzt - timedelta(hours=alter))

    def laden(url: str, **_kwargs: Any) -> DownloadedFile:
        if "ris1." in url:
            raise RobotsUnreachableError("robots.txt: HTTP 503")
        return _geladen(tmp_path)

    with (
        mock.patch.object(document_extraction, "download_to_file", side_effect=laden),
        mock.patch.object(text_extraction_job, "extract_text", return_value=ExtractionResult("Text", "pypdf", 1)),
    ):
        for _ in range(3):
            text_extraction_job.plan()
            _abarbeiten()

    neu.refresh_from_db()
    assert neu.text_extraction_status == "completed"
    for datei in (alt1, alt2):
        datei.refresh_from_db()
        # Zurückgestellt ohne Abbruch; die Kommune ruht bis zum nächsten Versuch der robots.txt
        assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("pending", 0)
    assert str(gestoert.pk) in text_extraction_job.deferred_bodies()


@pytest.mark.django_db
@WORKER
def test_quelle_in_schonung_wird_nicht_beansprucht() -> None:
    schonung = _kommune(1, consecutive_failures=5)
    erreichbar = _kommune(2)
    jetzt = timezone.now()
    alt = _datei(schonung)
    neu = _datei(erreichbar)
    OParlFile.objects.filter(pk=alt.pk).update(created_at=jetzt - timedelta(hours=2))
    OParlFile.objects.filter(pk=neu.pk).update(created_at=jetzt - timedelta(hours=1))

    assert text_extraction_job.plan() == 1
    assert {a.args["args"][0] for a in _auftraege()} == {str(neu.pk)}
    alt.refresh_from_db()
    assert alt.text_extraction_status == "pending"


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_EXTRACTION_QUEUE_DEPTH=5)
def test_eingereihte_dateien_gelten_nicht_als_abgebrochen(body: OParlBody) -> None:
    """
    Bei Parallelität 1 und bis zu 30 min je Auftrag warten eingereihte Dateien oft länger als die Zeitgrenze.
    Solange ihr Auftrag wartet, stellt niemand sie zurück, die Prüfung zählt sie nicht als hängend, und es
    entsteht kein zweiter Auftrag für dieselbe Datei.
    """
    _datei(body)
    _datei(body)
    assert text_extraction_job.plan(now=timezone.now() - timedelta(hours=2)) == 2

    assert text_extraction_job.release_stale() == (0, 0)
    assert OParlFile.objects.filter(text_extraction_status="processing").count() == 2
    assert extraction_health().haengend == 0
    # Platz in der Warteschlange wäre frei, aber kein zweiter Auftrag für dieselben Dateien
    assert text_extraction_job.plan() == 0
    assert len(_auftraege()) == 2

    # Hat der Auftrag begonnen und ist der Worker dabei gestorben, gilt wieder die Zeitgrenze
    Task.objects.filter(task_path=text_extraction_job.TASK_PATH).update(status=TaskStatus.LAEUFT)
    assert extraction_health().haengend == 2
    assert text_extraction_job.release_stale() == (2, 0)


@pytest.mark.django_db
def test_zurueckstellen_behaelt_fruehere_abbrueche(body: OParlBody) -> None:
    datei = _datei(body, text_extraction_status="processing", text_extraction_attempts=1)

    with mock.patch.object(document_extraction, "download_to_file", side_effect=RobotsUnreachableError("HTTP 503")):
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.ZURUECKGESTELLT

    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("pending", 1)
    assert datei.text_extraction_started_at is None
    assert str(body.pk) in text_extraction_job.deferred_bodies()


@pytest.mark.django_db
def test_fehler_beim_ablegen_beendet_die_datei(body: OParlBody) -> None:
    """Fehler außerhalb von Abruf und Erkennung lassen die Datei nicht in processing (sonst zählten Wiederholungen)."""
    OParlBody.objects.filter(pk=body.pk).update(is_listed=True)
    datei = _datei(body)

    with (
        mock.patch.object(file_cache, "fetch_and_cache", side_effect=OSError("Datenträger voll")),
        mock.patch.object(text_extraction_job, "extract_text", side_effect=AssertionError("keine Erkennung")),
    ):
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.GESCHEITERT

    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("failed", 0)
    assert datei.text_extraction_error == "Extraction failed (OSError)"


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_MAX_SIZE_MB=1)
def test_zu_grosse_kopie_aus_der_ablage_wird_uebersprungen(body: OParlBody, tmp_path: Path) -> None:
    """Die Ablage nimmt größere Dateien (FILE_CACHE_MAX_MB) als die Texterkennung (TEXT_EXTRACTION_MAX_SIZE_MB)."""
    kopie = tmp_path / "plan.pdf"
    kopie.write_bytes(b"%PDF-1.4" + b"0" * (1024 * 1024))
    datei = _datei(body, local_path=str(kopie), sha256_hash="b" * 64)

    with mock.patch.object(text_extraction_job, "extract_text", side_effect=AssertionError("keine Erkennung")):
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.UEBERSPRUNGEN

    datei.refresh_from_db()
    assert datei.text_extraction_status == "skipped"
    assert (datei.text_extraction_error or "").startswith("File too large")


def test_liegengebliebene_temporaere_dateien_werden_geloescht(tmp_path: Path) -> None:
    alt = time.time() - 3 * 3600
    reste = [tmp_path / "texterkennung-abc.bin", tmp_path / "texterkennung-def.part"]
    for pfad in reste:
        pfad.write_bytes(b"Anlage")
        os.utime(pfad, (alt, alt))
    seiten = tmp_path / "ocr-seite-xyz"
    seiten.mkdir()
    (seiten / "seite-1.pgm").write_bytes(b"P5")
    os.utime(seiten, (alt, alt))
    laufend = tmp_path / "texterkennung-laufend.pdf"
    laufend.write_bytes(b"%PDF")
    fremd = tmp_path / "anderes.bin"
    fremd.write_bytes(b"x")
    os.utime(fremd, (alt, alt))

    assert document_extraction.purge_leftover_temp_files(directory=tmp_path) == 3

    assert not any(p.exists() for p in [*reste, seiten])
    assert laufend.exists() and fremd.exists()
