# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auftrag ``file.extract_text`` und Zeitplan ``texterkennung_einplanen`` (Issues #530, #919).

Standard bleibt der OCR-Worker des Ingestors (``TEXT_EXTRACTION_RUNNER=ingestor``). Mit ``worker`` reiht der Zeitplan
je Datei mit abgelegtem Inhalt einen Auftrag in die Warteschlange ``ocr`` ein, ohne zu beanspruchen; der Auftrag
beansprucht beim Start und liest nur aus der Ablage (``hub/ris/erkennung.py``, ADR Dokumentkette Abschnitte 1, 2
und 4). Die Texterkennung selbst ist ersetzt; ein Abruf bei der Quelle ließe den Test scheitern.
"""

from __future__ import annotations

import errno
import os
import time
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import InterfaceError, OperationalError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from mandari_dokumente import ExtractionResult, OcrMemoryLimitError

from apps.events.models import Event, Task, TaskStatus
from hub.ris import abruf, erkennung
from insight_core.background_tasks import file_extract_text
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import document_extraction, file_cache, text_extraction_job
from insight_core.services.text_extraction_health import extraction_health

_nummer = count()
PDF = b"%PDF-1.4 Anlage zur Vorlage"
WORKER = override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_EXTRACTION_QUEUE_DEPTH=2)


@pytest.fixture(autouse=True)
def _ablage(tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Ablage nach SHA-256 im Testordner; jeder Abruf bei der Quelle wäre ein Fehler."""
    settings.OPARL_FILES_ROOT = str(tmp_path / "ablage")
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.OBJ_ENABLED = False

    def kein_abruf(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("die Texterkennung ruft nie bei der Quelle ab")

    monkeypatch.setattr(abruf, "abrufen", kein_abruf)
    monkeypatch.setattr(abruf, "stream", kein_abruf)
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def body(db: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Musterstadt", is_listed=False
    )


def _datei(body: OParlBody, inhalt: bytes | None = PDF, **felder: Any) -> OParlFile:
    """Datei, mit ``inhalt`` abgelegt (``local_status = ok``), sonst ohne Kopie."""
    werte: dict[str, Any] = {
        "external_id": f"https://ris.example.org/oparl/file/{next(_nummer)}",
        "body": body,
        "download_url": "https://ris.example.org/dokumente/anlage.pdf",
        "mime_type": "application/pdf",
        "file_name": "anlage.pdf",
    }
    status = felder.pop("text_extraction_status", None)
    werte.update(felder)
    datei = OParlFile.objects.create(**werte)
    if inhalt is not None:
        file_cache.store_bytes(datei, inhalt + str(datei.pk).encode())
    if status is not None:
        OParlFile.objects.filter(pk=datei.pk).update(text_extraction_status=status)
    datei.refresh_from_db()
    return datei


def _auftraege() -> list[Task]:
    return list(Task.objects.filter(task_path=text_extraction_job.TASK_PATH))


def _eingereiht() -> set[str]:
    return {a.args["args"][0] for a in _auftraege() if a.status == TaskStatus.WARTEND}


def _abarbeiten() -> set[str]:
    """Wartende Aufträge ausführen wie der Runner und als erledigt markieren; Rückgabe: ihre Dateien."""
    dateien = set()
    with mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("Text", "pypdf", 1)):
        for auftrag in Task.objects.filter(task_path=text_extraction_job.TASK_PATH, status=TaskStatus.WARTEND):
            assert erkennung.erkennen(auftrag.args["args"][0]) == erkennung.ERLEDIGT
            Task.objects.filter(pk=auftrag.pk).update(status=TaskStatus.ERLEDIGT)
            dateien.add(auftrag.args["args"][0])
    return dateien


def test_auftrag_in_der_warteschlange_ocr_als_duenne_huelle(monkeypatch: pytest.MonkeyPatch) -> None:
    assert file_extract_text.queue_name == "ocr"
    assert f"{file_extract_text.func.__module__}.{file_extract_text.func.__name__}" == text_extraction_job.TASK_PATH
    aufrufe: list[str] = []

    def erkennen(file_id: str) -> str:
        aufrufe.append(file_id)
        return erkennung.ERLEDIGT

    monkeypatch.setattr(erkennung, "erkennen", erkennen)
    file_extract_text.func("f-1")
    assert aufrufe == ["f-1"], "der registrierte Pfad ruft die Logik in hub.ris.erkennung auf"


@pytest.mark.django_db
def test_standard_ingestor_plant_nichts(body: OParlBody) -> None:
    datei = _datei(body)
    assert erkennung.einplanen() == 0
    datei.refresh_from_db()
    assert datei.text_extraction_status == "pending"
    assert _auftraege() == []


# =============================================================================
# Zeitplan: einreihen ohne Beanspruchung (ADR Abschnitt 2)
# =============================================================================


@pytest.mark.django_db
@WORKER
def test_einplanen_beansprucht_nicht_und_reiht_begrenzt_ein(body: OParlBody) -> None:
    abgelegt = [_datei(body) for _ in range(3)]
    # Quelle mit abgeschaltetem Dateiabruf: Der Inhalt liegt schon in der Ablage, erkannt wird ohne die Quelle
    ohne_abruf = OParlBody.objects.create(
        external_id="https://ris2.example.org/oparl/body/1",
        source=OParlSource.objects.create(
            name="Ohne Abruf", url="https://ris2.example.org/oparl/system", sync_config={"file_downloads": False}
        ),
        name="Beispiel",
    )
    abgelegt.append(_datei(ohne_abruf))
    nie = [
        _datei(body, deleted=True),
        _datei(body, source_missing_since=timezone.now()),
        _datei(body, content_purged_at=timezone.now()),
        _datei(body, inhalt=None),  # noch nicht abgelegt: zuerst der Abruf
    ]

    assert erkennung.einplanen() == 2
    assert {a.queue for a in _auftraege()} == {"ocr"}
    assert set(OParlFile.objects.values_list("text_extraction_status", flat=True)) == {"pending"}, "nicht beansprucht"
    # Rückstau voll: kein weiterer Auftrag
    assert erkennung.einplanen() == 0
    erster = _abarbeiten()
    assert erkennung.einplanen() == 2
    assert erster | _abarbeiten() == {str(d.pk) for d in abgelegt}
    assert erkennung.einplanen() == 0
    assert not {str(d.pk) for d in nie} & {a.args["args"][0] for a in _auftraege()}
    assert set(
        OParlFile.objects.filter(pk__in=[d.pk for d in nie]).values_list("text_extraction_status", flat=True)
    ) == {"pending"}


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_EXTRACTION_QUEUE_DEPTH=5)
def test_kein_zweiter_auftrag_fuer_eine_wartende_datei(body: OParlBody) -> None:
    datei = _datei(body)
    # Auch ein für später neu eingeplanter Auftrag (Störung der Ablage) gilt als wartend
    erkennung._enqueue([datei.pk], run_after=timezone.now() + erkennung.STORAGE_RETRY)
    assert erkennung.einplanen() == 0
    assert len(_auftraege()) == 1


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_EXTRACTION_QUEUE_DEPTH=1)
def test_wartende_vor_der_neuerkennung(body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
    import mandari_dokumente

    monkeypatch.setattr(mandari_dokumente, "EXTRACTION_VERSION", 2)
    veraltet = _datei(body, text_extraction_status="completed", text_content="Alter Text")
    wartend = _datei(body)
    OParlFile.objects.filter(pk=wartend.pk).update(created_at=timezone.now() - timedelta(days=30))

    assert erkennung.einplanen() == 1
    assert _eingereiht() == {str(wartend.pk)}
    Task.objects.update(status=TaskStatus.ERLEDIGT)
    OParlFile.objects.filter(pk=wartend.pk).update(text_extraction_status="completed", text_extraction_version="2")
    assert erkennung.einplanen() == 1
    assert _eingereiht() == {str(veraltet.pk)}
    veraltet.refresh_from_db()
    assert (veraltet.text_extraction_status, veraltet.text_content) == ("completed", "Alter Text")


@pytest.mark.django_db
@WORKER
def test_zu_grosse_abrufe_werden_uebersprungen(body: OParlBody) -> None:
    datei = _datei(body, inhalt=None, local_status="too_large")
    erkennung.einplanen()
    datei.refresh_from_db()
    assert datei.text_extraction_status == "skipped"
    assert (datei.text_extraction_error or "").startswith("File too large")


@pytest.mark.django_db
@WORKER
def test_liegen_gebliebene_beanspruchungen_werden_frei(body: OParlBody) -> None:
    alt = timezone.now() - timedelta(hours=2)
    haengend: dict[str, Any] = {"text_extraction_status": "processing", "text_extraction_started_at": alt}
    aufgeben = _datei(body, text_extraction_attempts=3, **haengend)
    zurueck = _datei(body, text_extraction_attempts=1, **haengend)
    # Neuerkennung mit vorhandenem Text: der Text bleibt erledigt
    neu_zurueck = _datei(body, text_extraction_attempts=1, text_content="Alter Text", **haengend)
    neu_aufgeben = _datei(body, text_extraction_attempts=3, text_content="Alter Text", **haengend)
    mit_auftrag = _datei(body, text_extraction_attempts=1, **haengend)
    erkennung._enqueue([mit_auftrag.pk])

    assert erkennung.release_stale() == (2, 2)

    for datei in (aufgeben, zurueck, neu_zurueck, neu_aufgeben, mit_auftrag):
        datei.refresh_from_db()
    assert aufgeben.text_extraction_status == "failed"
    assert (aufgeben.text_extraction_error or "").startswith("Speichergrenze")
    assert aufgeben.text_extracted_at is not None, "Zeitpunkt der Aufgabe für die Prüfung texterkennung"
    assert (zurueck.text_extraction_status, zurueck.text_extraction_attempts) == ("pending", 1)
    assert (neu_zurueck.text_extraction_status, neu_zurueck.text_content) == ("completed", "Alter Text")
    assert (neu_aufgeben.text_extraction_status, neu_aufgeben.text_content) == ("completed", "Alter Text")
    assert neu_aufgeben.text_extraction_version == str(erkennung.current_version()), "sonst liefe sie immer wieder"
    assert mit_auftrag.text_extraction_status == "processing", "eingereiht: kein Abbruch"
    assert extraction_health().haengend == 0


@pytest.mark.django_db
@WORKER
def test_freigeben_fragt_wartende_auftraege_nur_bei_liegen_gebliebenen_ab(
    body: OParlBody, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keine Liste aller wartenden Aufträge in den UPDATEs (alle zwei Minuten), ohne Liegengebliebene kein Journal."""
    wartende = [_datei(body) for _ in range(3)]
    erkennung._enqueue([d.pk for d in wartende])
    abfrage = mock.Mock(wraps=text_extraction_job.queued_file_ids)
    monkeypatch.setattr(erkennung, "queued_file_ids", abfrage)
    assert erkennung.release_stale() == (0, 0)
    assert abfrage.call_count == 0

    alt = timezone.now() - timedelta(hours=2)
    haengend = _datei(
        body, text_extraction_status="processing", text_extraction_started_at=alt, text_extraction_attempts=1
    )
    with CaptureQueriesContext(connection) as sql:
        assert erkennung.release_stale() == (1, 0)
    assert abfrage.call_count == 1
    updates = [q["sql"] for q in sql.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")]
    assert updates
    for datei in wartende:
        assert not any(datei.pk.hex in u or str(datei.pk) in u for u in updates), "wartende nicht in den UPDATEs"
    haengend.refresh_from_db()
    assert haengend.text_extraction_status == "pending"


# =============================================================================
# Auftrag: beanspruchen, aus der Ablage lesen, bedingt speichern
# =============================================================================


@pytest.mark.django_db
def test_auftrag_erkennt_aus_der_ablage(body: OParlBody, settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = True
    datei = _datei(body)
    ergebnis = ExtractionResult("Beschluss zum Radweg", "tesseract", 3)

    with mock.patch.object(erkennung, "extract_text", return_value=ergebnis) as erkennen:
        assert text_extraction_job.extract_file(str(datei.pk)) == erkennung.ERLEDIGT

    pfad = erkennen.call_args.args[0]
    assert Path(str(datei.local_path)) == pfad and pfad.read_bytes().startswith(PDF)
    datei.refresh_from_db()
    assert (datei.text_content, datei.text_extraction_status, datei.text_extraction_method, datei.page_count) == (
        "Beschluss zum Radweg",
        "completed",
        "tesseract",
        3,
    )
    assert (datei.text_extraction_attempts, datei.text_extraction_started_at) == (0, None)
    assert datei.text_source_sha256 == datei.sha256_hash and datei.text_extraction_version == "1"
    (ereignis,) = Event.objects.filter(type="ris.file.text_extracted")
    assert ereignis.payload == {
        "file": str(datei.pk),
        "method": "tesseract",
        "characters": 20,
        "sha256": datei.sha256_hash,
    }
    assert (ereignis.visibility, ereignis.tenant_ref) == ("intern", f"source:{body.source_id}")
    # Wiederholt zugestellt: erledigte Datei wird nicht noch einmal beansprucht
    assert erkennung.erkennen(str(datei.pk)) == erkennung.NICHT_ZU_TUN


@pytest.mark.django_db
def test_ohne_text_kein_ereignis(body: OParlBody, settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = True
    datei = _datei(body)
    with mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("", "none", 1)):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.OHNE_TEXT
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_method, datei.text_content) == (
        "completed",
        "none",
        None,
    )
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_suchindex_folgt_dem_ergebnis_erst_nach_dem_commit(
    body: OParlBody, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    """
    Das Ergebnis wird bedingt (UPDATE) mit seinem Ereignis in einer Transaktion gespeichert; der Suchindex (Signal
    ``index_file``) folgt erst nach dem Commit.
    """
    from insight_core import signals

    indexiert: list[str] = []
    monkeypatch.setattr(signals, "_index_document", lambda index, doc_id, doc: indexiert.append(f"{index}/{doc_id}"))
    datei = _datei(body)
    with (
        mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("Beschluss zum Radweg", "pypdf", 1)),
        django_capture_on_commit_callbacks(execute=False) as nach_dem_commit,
    ):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT
        assert indexiert == [], "nicht in der Transaktion"

    for rueckruf in nach_dem_commit:
        rueckruf()
    assert f"files/{datei.pk}" in indexiert


@pytest.mark.django_db
def test_scheitert_nur_das_ereignis_bleibt_das_ergebnis(body: OParlBody, settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = True
    datei = _datei(body)
    with (
        mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("Beschluss", "pypdf", 1)),
        mock.patch("hub.ris.text_extraction.publish", side_effect=RuntimeError("Journal nicht erreichbar")),
    ):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_content) == ("completed", "Beschluss")
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_nur_abgelegte_und_nicht_ausgeschlossene_werden_beansprucht(body: OParlBody) -> None:
    dateien = [
        _datei(body, inhalt=None),
        _datei(body, deleted=True),
        _datei(body, source_missing_since=timezone.now()),
        _datei(body, content_purged_at=timezone.now()),
        _datei(body, text_extraction_status="failed"),
    ]
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung")):
        for datei in dateien:
            assert erkennung.erkennen(str(datei.pk)) == erkennung.NICHT_ZU_TUN
    assert set(OParlFile.objects.values_list("text_extraction_status", flat=True)) == {"pending", "failed"}


@pytest.mark.django_db
def test_zwei_auftraege_einer_beansprucht(body: OParlBody) -> None:
    datei = _datei(body)
    erste = erkennung.beanspruchen(datei.pk)
    assert erste is not None and erste.vorher == "pending"
    assert erkennung.beanspruchen(datei.pk) is None
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine zweite Erkennung")):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.NICHT_ZU_TUN
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("processing", 1)


@pytest.mark.django_db
def test_speichergrenze_und_unlesbarer_inhalt(body: OParlBody) -> None:
    grenze = _datei(body)
    with mock.patch.object(
        erkennung, "extract_text", side_effect=OcrMemoryLimitError("Speichergrenze: 3 von 3 Seiten")
    ):
        assert erkennung.erkennen(str(grenze.pk)) == erkennung.GESCHEITERT
    grenze.refresh_from_db()
    assert (grenze.text_extraction_status, grenze.text_extraction_error) == ("failed", "Speichergrenze: 3 von 3 Seiten")

    kaputt = _datei(body)
    with mock.patch.object(erkennung, "extract_text", side_effect=ValueError("kein PDF")):
        assert erkennung.erkennen(str(kaputt.pk)) == erkennung.GESCHEITERT
    kaputt.refresh_from_db()
    assert (kaputt.text_extraction_status, kaputt.text_extraction_error) == ("failed", "Extraction failed (ValueError)")
    assert kaputt.text_extraction_attempts == 0


@pytest.mark.django_db
@pytest.mark.parametrize("fehler", [OperationalError("Verbindung verloren"), InterfaceError("Verbindung zu")])
def test_gestoerte_datenbank_beim_speichern_ist_kein_erkennungsfehler(body: OParlBody, fehler: Exception) -> None:
    """Eine Störung der Umgebung: Stand davor, kein Versuch gezählt, später erneut – nicht ``failed``."""
    datei = _datei(body, text_extraction_attempts=1)
    with (
        mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("Text", "pypdf", 1)),
        mock.patch.object(erkennung, "_speichern", side_effect=fehler),
    ):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ZURUECKGESTELLT
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("pending", 1)
    assert not datei.text_extraction_error
    (auftrag,) = _auftraege()
    assert auftrag.run_after is not None, "derselbe Auftrag später erneut"

    # Bleibt die Datenbank gestört, bleibt die Beanspruchung bis zur Zeitgrenze (release_stale)
    Task.objects.all().delete()
    with (
        mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("Text", "pypdf", 1)),
        mock.patch.object(erkennung, "_speichern", side_effect=fehler),
        mock.patch.object(erkennung, "freigeben", side_effect=fehler),
    ):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ZURUECKGESTELLT
    datei.refresh_from_db()
    assert datei.text_extraction_status == "processing"


@pytest.mark.django_db
def test_kein_platz_fuer_zwischendateien_ist_kein_erkennungsfehler(body: OParlBody) -> None:
    """Volles Temp-Verzeichnis (ENOSPC): Störung, später erneut; andere ``OSError`` der Bibliothek bleiben ``failed``."""
    voll = _datei(body, text_extraction_attempts=1)
    with mock.patch.object(erkennung, "extract_text", side_effect=OSError(errno.ENOSPC, "No space left on device")):
        assert erkennung.erkennen(str(voll.pk)) == erkennung.ZURUECKGESTELLT
    voll.refresh_from_db()
    assert (voll.text_extraction_status, voll.text_extraction_attempts) == ("pending", 1)
    assert not voll.text_extraction_error
    (auftrag,) = _auftraege()
    assert auftrag.run_after is not None, "derselbe Auftrag später erneut"

    # Ohne Fehlernummer meldet etwa die Bildbibliothek einen unlesbaren Inhalt: bleibt ein Erkennungsfehler
    unlesbar = _datei(body)
    with mock.patch.object(erkennung, "extract_text", side_effect=OSError("cannot identify image file")):
        assert erkennung.erkennen(str(unlesbar.pk)) == erkennung.GESCHEITERT
    unlesbar.refresh_from_db()
    assert (unlesbar.text_extraction_status, unlesbar.text_extraction_error) == (
        "failed",
        "Extraction failed (OSError)",
    )


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_MAX_SIZE_MB=0)
def test_zu_gross_und_bilder_werden_uebersprungen(body: OParlBody) -> None:
    gross = _datei(body)
    bild = _datei(body, mime_type="image/png")
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung")):
        assert erkennung.erkennen(str(gross.pk)) == erkennung.UEBERSPRUNGEN
        assert erkennung.erkennen(str(bild.pk)) == erkennung.UEBERSPRUNGEN
    gross.refresh_from_db()
    bild.refresh_from_db()
    assert gross.text_extraction_status == "skipped" and (gross.text_extraction_error or "").startswith(
        "File too large"
    )
    assert (bild.text_extraction_status, bild.text_extraction_error) == ("skipped", "Unsupported MIME type: image/png")


@pytest.mark.django_db
def test_verlorener_inhalt_geht_zurueck_an_den_abruf(body: OParlBody) -> None:
    """Weder lokal noch im Objektspeicher (bestätigt): ``local_status = none``, Erkennung wartet, kein Quellabruf."""
    datei = _datei(body)
    Path(str(datei.local_path)).unlink()
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung")):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.OHNE_INHALT
    datei.refresh_from_db()
    assert (datei.local_status, datei.text_extraction_status, datei.text_extraction_attempts) == ("none", "pending", 0)


@pytest.mark.django_db
def test_nicht_eingehaengte_ablage_stellt_zurueck_und_plant_spaeter(body: OParlBody, settings: Any) -> None:
    datei = _datei(body, text_extraction_attempts=1)
    # Kopie nicht lesbar, weil die Ablage nicht eingehängt ist: eine Störung, kein bestätigtes Fehlen
    Path(str(datei.local_path)).unlink()
    settings.OPARL_FILES_ROOT = str(Path(settings.OPARL_FILES_ROOT).parent / "nicht-eingehaengt")
    vorher = timezone.now()
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine Erkennung")):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ZURUECKGESTELLT
    datei.refresh_from_db()
    assert (datei.local_status, datei.text_extraction_status) == ("ok", "pending"), "gestört ist nicht fehlend"
    assert datei.text_extraction_attempts == 1, "kein Versuch gezählt, frühere Abbrüche bleiben"
    (auftrag,) = _auftraege()
    assert auftrag.args["args"] == [str(datei.pk)]
    assert auftrag.run_after is not None and auftrag.run_after >= vorher + erkennung.STORAGE_RETRY


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
