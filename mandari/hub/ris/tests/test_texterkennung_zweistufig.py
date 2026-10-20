# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung in zwei Stufen (Issue #1107).

- Bibliothek: Die Textebene reicht bei getippten PDFs; gescannte und (mit ``ocr_mixed_pages``) gemischte PDFs brauchen
  die Erkennung aus dem Bild, die dann nur die eingescannten Seiten liest. Ohne ``ocr_mixed_pages`` (Ingestor) bleibt
  es beim bisherigen Verhalten. Die erste Stufe läuft in einem eigenen Prozess; scheitert er, übernimmt die zweite.
- Auftrag ``file.read_text_layer``: Ein PDF mit Textebene wird in der ersten Stufe fertig und erreicht ``ocr`` nie,
  gescannte und gemischte PDFs landen in ``ocr``; ist ``ocr`` voll, plant der Zeitplan sie später ein.
- Zwei Worker auf derselben Warteschlange bearbeiten keine Datei doppelt.
- Das Einplanen wechselt zwischen den Quellen ab; ohne Worker für ``text`` plant es wie bisher alles in ``ocr`` ein,
  auch Dateien mit wartendem Auftrag in ``text``, und schließt verwaiste Aufträge dort.
- Ein Erkennungsserver ohne die gemeinsame Ablage (``FILE_STORE_SHARED=false``) bestätigt kein Fehlen eines Inhalts.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import uuid
from collections import Counter, deque
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone
from mandari_dokumente import (
    LAYER_ERROR,
    LAYER_MEMORY,
    LAYER_NEEDS_OCR,
    LAYER_OK,
    LAYER_TIMEOUT,
    ExtractionConfig,
    ExtractionResult,
    OcrMemoryLimitError,
    TextLayerOutcome,
    extract_text,
    extract_text_layer,
    extract_text_layer_isolated,
    ocr,
    read_text_layer,
)
from mandari_dokumente import texterkennung as bibliothek

from apps.events import presence
from apps.events.models import Event, Task, TaskStatus
from apps.events.tests.hilfen import nur_postgres
from hub.ris import abruf, erkennung
from insight_core.models import OParlBody, OParlFile, OParlFileBlob, OParlSource
from insight_core.services import file_cache, file_store, object_storage, text_extraction_job

GETIPPT = "Antrag der Fraktion zum Radweg an der Promenade"
ZWEI_STUFEN = override_settings(
    TEXT_EXTRACTION_RUNNER="worker", TEXT_LAYER_QUEUE_DEPTH=10, TEXT_EXTRACTION_QUEUE_DEPTH=5
)
GEMISCHT = ExtractionConfig(ocr_mixed_pages=True)


def pdf(seiten: Sequence[tuple[str, bool]]) -> bytes:
    """Minimales PDF: je Seite (Text der Textebene, mit Bild?). Ein Bild ohne Text ist eine eingescannte Seite."""
    objekte: list[bytes] = [b"", b""]  # 1 Katalog, 2 Seitenbaum (unten gefüllt)
    objekte.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    schrift = len(objekte)
    objekte.append(
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
        b"/Length 1 >>\nstream\n\x00\nendstream"
    )
    bild = len(objekte)
    kinder: list[int] = []
    for text, mit_bild in seiten:
        teile = (["q 595 0 0 842 0 0 cm /Im1 Do Q"] if mit_bild else []) + (
            [f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET"] if text else []
        )
        inhalt = " ".join(teile).encode()
        objekte.append(b"<< /Length %d >>\nstream\n" % len(inhalt) + inhalt + b"\nendstream")
        inhalt_nr = len(objekte)
        ressourcen = f"/Font << /F1 {schrift} 0 R >>" + (f" /XObject << /Im1 {bild} 0 R >>" if mit_bild else "")
        objekte.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << {ressourcen} >> "
                f"/Contents {inhalt_nr} 0 R >>"
            ).encode()
        )
        kinder.append(len(objekte))
    objekte[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objekte[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kinder)}] /Count {len(kinder)} >>".encode()
    aus = bytearray(b"%PDF-1.4\n")
    versatz: list[int] = []
    for nummer, rumpf in enumerate(objekte, start=1):
        versatz.append(len(aus))
        aus += f"{nummer} 0 obj\n".encode() + rumpf + b"\nendobj\n"
    xref = len(aus)
    aus += f"xref\n0 {len(objekte) + 1}\n".encode() + b"0000000000 65535 f \n"
    for wert in versatz:
        aus += f"{wert:010d} 00000 n \n".encode()
    aus += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(aus)


TEXT_PDF = pdf([(GETIPPT, False), ("Begruendung: Der Radweg ist zu schmal und gefaehrlich", False)])
SCAN_PDF = pdf([("", True), ("", True)])
#: Getipptes Anschreiben, dahinter eine eingescannte Anlage mit aufgestempeltem Kopf (weniger als 20 Zeichen)
GEMISCHT_PDF = pdf([(GETIPPT, False), ("Anlage 1", True)])
#: Getippt mit einer Leerseite ohne Bild: braucht keine Erkennung
LEERSEITE_PDF = pdf([(GETIPPT, False), ("", False)])


def _ocr_liefert(monkeypatch: pytest.MonkeyPatch, text: str) -> list[list[str]]:
    """Tesseract und pdftoppm ersetzt; Rückgabe: die Aufrufe."""
    aufrufe: list[list[str]] = []

    def runner(
        command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        aufrufe.append(list(command))
        if command[0] == "pdftoppm":
            Path(command[-1]).with_suffix(".pgm").write_bytes(b"P5\n1 1\n255\n\x00")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 0, text.encode(), b"")

    monkeypatch.setattr(ocr, "tools_available", lambda: True)
    monkeypatch.setattr(ocr, "run_limited", runner)
    return aufrufe


# =============================================================================
# Bibliothek: wann reicht die Textebene?
# =============================================================================


def test_getipptes_pdf_reicht_ohne_erkennung() -> None:
    ebene = read_text_layer(TEXT_PDF, max_pages=100)
    assert ebene.page_count == 2 and ebene.scan_pages == ()
    assert ebene.sufficient(mixed_pages=True)
    ergebnis = extract_text_layer(TEXT_PDF, "application/pdf", "antrag.pdf", GEMISCHT)
    assert ergebnis is not None
    assert (ergebnis.method, ergebnis.page_count) == ("pypdf", 2)
    assert GETIPPT in ergebnis.text


def test_scan_und_gemischtes_pdf_brauchen_die_erkennung() -> None:
    assert extract_text_layer(SCAN_PDF, "application/pdf", "scan.pdf", GEMISCHT) is None
    ebene = read_text_layer(GEMISCHT_PDF, max_pages=100)
    assert ebene.has_text and ebene.scan_pages == (2,)
    assert extract_text_layer(GEMISCHT_PDF, "application/pdf", "anlage.pdf", GEMISCHT) is None
    # Ohne ocr_mixed_pages (Ingestor, bisheriges Verhalten) reicht jeder Text
    bisher = extract_text_layer(GEMISCHT_PDF, "application/pdf", "anlage.pdf", ExtractionConfig())
    assert bisher is not None and bisher.method == "pypdf"


def test_leerseite_ohne_bild_braucht_keine_erkennung() -> None:
    assert extract_text_layer(LEERSEITE_PDF, "application/pdf", "antrag.pdf", GEMISCHT) is not None
    # Nur Seiten bis zur Seitengrenze der Erkennung zählen
    assert read_text_layer(GEMISCHT_PDF, max_pages=1).scan_pages == ()


def test_andere_dateien_erkennt_die_erste_stufe_vollstaendig() -> None:
    ergebnis = extract_text_layer(b"Niederschrift der Sitzung", "text/plain", "protokoll.txt", GEMISCHT)
    assert ergebnis is not None and (ergebnis.text, ergebnis.method) == ("Niederschrift der Sitzung", "text")


def test_gemischtes_pdf_erkennt_nur_die_eingescannten_seiten(monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe = _ocr_liefert(monkeypatch, "Stellungnahme des Bezirks")
    ergebnis = extract_text(GEMISCHT_PDF, "application/pdf", "anlage.pdf", GEMISCHT)

    assert ergebnis.method == "tesseract"
    assert ergebnis.text.index(GETIPPT) < ergebnis.text.index("Stellungnahme des Bezirks"), "Reihenfolge der Seiten"
    gerendert = [befehl for befehl in aufrufe if befehl[0] == "pdftoppm"]
    assert len(gerendert) == 1 and gerendert[0][1:5] == ["-f", "2", "-l", "2"], "nur die eingescannte Seite"


def test_ohne_gemischte_seiten_bleibt_die_kette_wie_bisher(monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe = _ocr_liefert(monkeypatch, "darf nicht vorkommen")
    ergebnis = extract_text(GEMISCHT_PDF, "application/pdf", "anlage.pdf")
    assert (ergebnis.method, aufrufe) == ("pypdf", [])
    # Ein Scan ganz ohne Textebene geht wie bisher an Tesseract
    _ocr_liefert(monkeypatch, "Gescannter Beschluss")
    assert extract_text(SCAN_PDF, "application/pdf", "scan.pdf").text == "Gescannter Beschluss\n\nGescannter Beschluss"


def test_gemischtes_pdf_an_der_speichergrenze_behaelt_die_textebene(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bibliothek, "ocr_pdf", mock.Mock(side_effect=OcrMemoryLimitError("Speichergrenze: 1 von 1")))
    ergebnis = extract_text(GEMISCHT_PDF, "application/pdf", "anlage.pdf", GEMISCHT)
    assert ergebnis.method == "pypdf" and GETIPPT in ergebnis.text
    assert ergebnis.notes and "Speichergrenze" in ergebnis.notes[0]


# =============================================================================
# Bibliothek: erste Stufe in einem eigenen Prozess
# =============================================================================


def test_eigener_prozess_liest_die_textebene(tmp_path: Path) -> None:
    getippt, scan = tmp_path / "antrag.pdf", tmp_path / "scan.pdf"
    getippt.write_bytes(TEXT_PDF)
    scan.write_bytes(SCAN_PDF)

    ausgang = extract_text_layer_isolated(getippt, "application/pdf", "antrag.pdf", GEMISCHT)
    assert ausgang.reason == LAYER_OK and ausgang.result is not None
    assert GETIPPT in ausgang.result.text and ausgang.result.page_count == 2
    assert extract_text_layer_isolated(scan, "application/pdf", "scan.pdf", GEMISCHT) == TextLayerOutcome(
        None, LAYER_NEEDS_OCR
    )


@pytest.mark.parametrize(
    ("ende", "grund"),
    [
        (subprocess.TimeoutExpired("python", 120), LAYER_TIMEOUT),
        (subprocess.CompletedProcess([], 3, b"", b"MemoryError"), LAYER_MEMORY),
        (subprocess.CompletedProcess([], -9, b"", b""), LAYER_MEMORY),
        (subprocess.CompletedProcess([], 1, b"", b"Traceback"), LAYER_ERROR),
        (subprocess.CompletedProcess([], 0, b"kein JSON", b""), LAYER_ERROR),
    ],
)
def test_scheitert_der_prozess_uebernimmt_die_zweite_stufe(tmp_path: Path, ende: Any, grund: str) -> None:
    pfad = tmp_path / "antrag.pdf"
    pfad.write_bytes(TEXT_PDF)
    gesehen: list[tuple[list[str], float, int]] = []

    def runner(command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]) -> Any:
        gesehen.append((list(command), timeout, memory_limit_mb))
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(Path(bibliothek.__file__).resolve().parent.parent)
        if isinstance(ende, BaseException):
            raise ende
        return ende

    grenzen = bibliothek.IsolationLimits(memory_limit_mb=384, timeout=60)
    ausgang = extract_text_layer_isolated(pfad, "application/pdf", "antrag.pdf", GEMISCHT, grenzen, runner)
    assert ausgang == TextLayerOutcome(None, grund)
    befehl, zeit, speicher = gesehen[0]
    assert befehl[1:4] == ["-m", "mandari_dokumente", "textebene"] and "--gemischt" in befehl
    assert (zeit, speicher) == (60, 384)


@pytest.mark.skipif(os.name != "posix" or not Path("/bin/sh").exists(), reason="Grenzen nur unter Linux")
def test_prozess_mit_speichergrenze_und_niedriger_prioritaet() -> None:
    skript = "import os, resource; print(resource.getrlimit(resource.RLIMIT_AS)[0], os.nice(0))"
    lauf = bibliothek.run_isolated([sys.executable, "-c", skript], 30, 384, dict(os.environ))
    grenze, prioritaet = lauf.stdout.decode().split()
    assert int(grenze) == 384 * 1024 * 1024
    assert int(prioritaet) >= 10


# =============================================================================
# Auftrag file.read_text_layer und Zeitplan
# =============================================================================


@pytest.fixture(autouse=True)
def _ablage(tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.OPARL_FILES_ROOT = str(tmp_path / "ablage")
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.OBJ_ENABLED = False

    def kein_abruf(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("die Texterkennung ruft nie bei der Quelle ab")

    monkeypatch.setattr(abruf, "abrufen", kein_abruf)
    monkeypatch.setattr(abruf, "stream", kein_abruf)


@pytest.fixture
def im_prozess(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Erste Stufe ohne eigenen Prozess (schneller); Rückgabe: die gelesenen Dateinamen."""
    gelesen: list[str] = []

    def lesen(pfad: Path, mime: str, name: str, config: ExtractionConfig, *_a: Any, **_k: Any) -> TextLayerOutcome:
        gelesen.append(name)
        ergebnis = extract_text_layer(pfad, mime, name, config)
        return TextLayerOutcome(ergebnis, LAYER_OK if ergebnis is not None else LAYER_NEEDS_OCR)

    monkeypatch.setattr(erkennung, "extract_text_layer_isolated", lesen)
    return gelesen


@pytest.fixture
def worker_fuer_text(db: Any) -> None:
    """Ein lebender Worker bedient text (wie der Dienst worker in docker-compose.yml)."""
    presence.announce("haupt", ["sequencer", "dispatch", "tasks", "scheduler"], ["default", "mail", "index", "text"])
    presence.announce("schwer", ["tasks"], ["ocr", "ai"])


def _quelle(name: str = "Quelle") -> OParlBody:
    kennung = uuid.uuid4().hex[:8]
    source = OParlSource.objects.create(name=f"{name} {kennung}", url=f"https://{kennung}.example.org/oparl/system")
    return OParlBody.objects.create(
        source=source, external_id=f"https://{kennung}.example.org/oparl/body/1", name=name, is_listed=False
    )


def _datei(body: OParlBody, inhalt: bytes, name: str = "anlage.pdf") -> OParlFile:
    datei = OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        name="Anlage",
        file_name=name,
        mime_type="application/pdf",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.pdf",
    )
    file_cache.store_bytes(datei, inhalt)
    datei.refresh_from_db()
    return datei


def _wartend(queue: str) -> list[str]:
    return [
        t.args["args"][0]
        for t in Task.objects.filter(
            queue=queue, task_path=text_extraction_job.STAGES[queue], status=TaskStatus.WARTEND
        )
    ]


@pytest.mark.django_db
@ZWEI_STUFEN
def test_pdf_mit_textebene_wird_in_der_ersten_stufe_fertig(worker_fuer_text: None, settings: Any) -> None:
    """Ende zu Ende mit eigenem Prozess: eingeplant in text, erledigt, ocr nie erreicht."""
    settings.INGESTOR_EVENTS_ENABLED = True
    datei = _datei(_quelle(), TEXT_PDF)

    assert erkennung.einplanen() == 1
    assert _wartend("text") == [str(datei.pk)] and _wartend("ocr") == []
    with mock.patch.object(erkennung, "extract_text", side_effect=AssertionError("keine zweite Stufe")):
        assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.ERLEDIGT

    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_method, datei.page_count) == ("completed", "pypdf", 2)
    assert GETIPPT in (datei.text_content or "")
    assert datei.text_source_sha256 == datei.sha256_hash == hashlib.sha256(TEXT_PDF).hexdigest()
    assert (datei.text_extraction_attempts, datei.text_ocr_needed_sha256) == (0, None)
    (ereignis,) = Event.objects.filter(type="ris.file.text_extracted")
    assert ereignis.payload["method"] == "pypdf" and ereignis.payload["sha256"] == datei.sha256_hash
    Task.objects.update(status=TaskStatus.ERLEDIGT)
    assert erkennung.einplanen() == 0, "erledigt: kein Auftrag in ocr"
    assert not Task.objects.filter(queue="ocr").exists()
    assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.NICHT_ZU_TUN, "wiederholt zugestellt"


@pytest.mark.django_db
@ZWEI_STUFEN
@pytest.mark.parametrize("inhalt", [SCAN_PDF, GEMISCHT_PDF], ids=["gescannt", "gemischt"])
def test_gescanntes_und_gemischtes_pdf_landen_in_ocr(
    worker_fuer_text: None, im_prozess: list[str], inhalt: bytes
) -> None:
    datei = _datei(_quelle(), inhalt)
    assert erkennung.einplanen() == 1 and _wartend("text") == [str(datei.pk)]

    assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.ZUR_TEXTERKENNUNG
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("pending", 0), "freigegeben"
    assert datei.text_ocr_needed_sha256 == datei.sha256_hash, "vermerkt"
    assert _wartend("ocr") == [str(datei.pk)]
    assert erkennung.counts()["awaiting_ocr"] == 1

    # Der Zeitplan plant weder die erste Stufe erneut noch einen zweiten Auftrag in ocr ein
    Task.objects.filter(queue="text").update(status=TaskStatus.ERLEDIGT)
    assert erkennung.einplanen() == 0
    # Die erste Stufe beansprucht die vermerkte Datei nicht noch einmal
    assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.NICHT_ZU_TUN
    assert im_prozess == ["anlage.pdf"]

    with mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult("Gescannt", "tesseract", 2)) as e:
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT
    assert e.call_args.args[3].ocr_mixed_pages, "die zweite Stufe erkennt eingescannte Seiten"
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_method) == ("completed", "tesseract")


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_LAYER_QUEUE_DEPTH=10, TEXT_EXTRACTION_QUEUE_DEPTH=1)
def test_ist_ocr_voll_plant_der_zeitplan_vermerkte_spaeter_ein(worker_fuer_text: None, im_prozess: list[str]) -> None:
    body = _quelle()
    erster, zweiter = _datei(body, SCAN_PDF), _datei(body, SCAN_PDF + b"\n% zweite Fassung")
    assert erkennung.einplanen() == 2

    assert erkennung.textebene_lesen(str(erster.pk)) == erkennung.ZUR_TEXTERKENNUNG
    assert erkennung.textebene_lesen(str(zweiter.pk)) == erkennung.ZUR_TEXTERKENNUNG
    assert _wartend("ocr") == [str(erster.pk)], "ocr voll: der zweite wartet mit Vermerk"
    Task.objects.filter(queue="text").update(status=TaskStatus.ERLEDIGT)
    assert erkennung.einplanen() == 0

    Task.objects.filter(queue="ocr").update(status=TaskStatus.ERLEDIGT)
    assert erkennung.einplanen() == 1
    assert _wartend("ocr") == [str(zweiter.pk)] and _wartend("text") == []


@pytest.mark.django_db
@ZWEI_STUFEN
def test_ohne_worker_fuer_text_plant_der_zeitplan_alles_in_ocr() -> None:
    """Rückwärtskompatibel: Fehlt ein Worker für text, bleibt nichts liegen; ocr erkennt mit der vollständigen Kette."""
    presence.announce("alt", ["sequencer", "dispatch", "tasks", "scheduler"], ["default", "mail", "index", "adapter"])
    presence.announce("schwer", ["tasks"], ["ocr", "ai"])
    body = _quelle()
    dateien = {str(_datei(body, inhalt).pk) for inhalt in (TEXT_PDF, SCAN_PDF)}

    assert erkennung.einplanen() == 2
    assert set(_wartend("ocr")) == dateien and _wartend("text") == []


def _text_aus_queues() -> None:
    """Rückweg: Der Dienst worker bedient text nicht mehr (``text`` aus ``--queues``)."""
    presence.announce("haupt", ["sequencer", "dispatch", "tasks", "scheduler"], ["default", "mail", "index"])
    assert not erkennung.text_stufe_aktiv()


@pytest.mark.django_db
@ZWEI_STUFEN
def test_ohne_worker_fuer_text_gehen_wartende_dateien_nach_ocr(worker_fuer_text: None) -> None:
    """
    Ein wartender Auftrag in text hält seine Datei nicht fest, wenn kein Worker text mehr bedient (Rückweg, längerer
    Ausfall): Sie geht nach ocr. Seit 15 Minuten fällige Aufträge in text schließt der Zeitplan (rueckstau).
    """
    datei = _datei(_quelle(), TEXT_PDF)
    assert erkennung.einplanen() == 1 and _wartend("text") == [str(datei.pk)]

    _text_aus_queues()
    assert erkennung.einplanen() == 1
    assert _wartend("ocr") == [str(datei.pk)], "nicht hinter dem wartenden Auftrag in text eingesperrt"
    assert _wartend("text") == [str(datei.pk)], "noch keine 15 Minuten fällig (etwa ein Neustart des Workers)"
    assert erkennung.einplanen() == 0, "kein zweiter Auftrag in ocr"

    Task.objects.filter(queue="text").update(run_after=timezone.now() - timedelta(minutes=16))
    assert erkennung.einplanen() == 0
    (geschlossen,) = Task.objects.filter(queue="text")
    assert (geschlossen.status, geschlossen.result_code) == (TaskStatus.ERLEDIGT, erkennung.OHNE_WORKER)
    assert _wartend("ocr") == [str(datei.pk)]
    with mock.patch.object(erkennung, "extract_text", return_value=ExtractionResult(GETIPPT, "pypdf", 2)):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT


@pytest.mark.django_db
@ZWEI_STUFEN
def test_ohne_worker_fuer_text_gibt_die_zeitgrenze_die_datei_frei(worker_fuer_text: None) -> None:
    """In der ersten Stufe abgebrochen, ihr Auftrag wartet erneut: Ohne Worker für text gibt die Zeitgrenze sie frei."""
    datei = _datei(_quelle(), TEXT_PDF)
    assert erkennung.einplanen() == 1
    OParlFile.objects.filter(pk=datei.pk).update(
        text_extraction_status="processing",
        text_extraction_started_at=timezone.now() - timedelta(hours=2),
        text_extraction_attempts=1,
    )
    assert erkennung.release_stale() == (0, 0), "mit Worker für text übernimmt der wartende Auftrag"

    _text_aus_queues()
    assert erkennung.einplanen() == 1
    datei.refresh_from_db()
    assert datei.text_extraction_status == "pending"
    assert _wartend("ocr") == [str(datei.pk)]


@pytest.mark.django_db
@ZWEI_STUFEN
def test_ohne_gemeinsame_ablage_ist_ein_fehlender_inhalt_gestoert(
    worker_fuer_text: None, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Eigener Erkennungsserver (``FILE_STORE_SHARED=false``): Was weder lokal noch im Objektspeicher liegt, kann auf dem
    Anwendungsserver liegen (noch nicht hochgeladen, altes Layout). Kein bestätigtes Fehlen, kein neuer Abruf.
    """
    datei = _datei(_quelle(), SCAN_PDF)
    # Liegt nur auf dem Anwendungsserver und ist noch nicht hochgeladen (remote_at leer)
    Path(str(datei.local_path)).unlink()
    assert file_store.local_copy(datei).missing, "mit der gemeinsamen Ablage bestätigt fehlend"

    settings.FILE_STORE_SHARED = False
    assert file_store.local_copy(datei).disturbed
    assert erkennung.erkennen(str(datei.pk)) == erkennung.ZURUECKGESTELLT
    datei.refresh_from_db()
    assert (datei.local_status, datei.text_extraction_status) == ("ok", "pending"), "kein Abruf bei der Quelle"
    assert _wartend("ocr") == [str(datei.pk)]

    # Laut Datenbank im Objektspeicher, dort aber nicht gefunden: ebenso gestört (lokal beim Anwendungsserver möglich)
    OParlFileBlob.objects.filter(pk=datei.blob_id).update(remote_at=timezone.now())
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    monkeypatch.setattr(file_store, "fetch_remote", lambda *_a, **_k: file_store.Copy(file_store.MISSING))
    assert file_store.local_copy(datei).disturbed
    settings.FILE_STORE_SHARED = True
    assert file_store.local_copy(datei).missing


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_RUNNER="ingestor")
def test_mit_dem_ingestor_aendert_sich_nichts(worker_fuer_text: None) -> None:
    _datei(_quelle(), TEXT_PDF)
    assert erkennung.einplanen() == 0
    assert not Task.objects.exists()


@pytest.mark.django_db
@ZWEI_STUFEN
def test_unerwarteter_fehler_der_ersten_stufe_geht_an_ocr(
    worker_fuer_text: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    datei = _datei(_quelle(), TEXT_PDF)
    monkeypatch.setattr(erkennung, "extract_text_layer_isolated", mock.Mock(side_effect=RuntimeError("kaputt")))
    assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.ZUR_TEXTERKENNUNG
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_ocr_needed_sha256) == ("pending", datei.sha256_hash)
    assert _wartend("ocr") == [str(datei.pk)]


@pytest.mark.django_db
@ZWEI_STUFEN
def test_gestoerte_ablage_plant_die_erste_stufe_spaeter(worker_fuer_text: None, settings: Any) -> None:
    datei = _datei(_quelle(), TEXT_PDF)
    # Kopie nicht lesbar, weil die Ablage nicht eingehängt ist: eine Störung, kein bestätigtes Fehlen
    Path(str(datei.local_path)).unlink()
    settings.OPARL_FILES_ROOT = str(Path(settings.OPARL_FILES_ROOT).parent / "nicht-eingehaengt")
    with mock.patch.object(erkennung, "extract_text_layer_isolated", side_effect=AssertionError("nicht lesen")):
        assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.ZURUECKGESTELLT
    datei.refresh_from_db()
    assert (datei.text_extraction_status, datei.text_extraction_attempts) == ("pending", 0)
    (auftrag,) = Task.objects.all()
    assert (auftrag.queue, auftrag.task_path) == ("text", text_extraction_job.TEXT_LAYER_TASK_PATH)
    assert auftrag.run_after > timezone.now()


# =============================================================================
# Zwei Worker auf derselben Warteschlange
# =============================================================================


@pytest.mark.django_db
@ZWEI_STUFEN
def test_zwei_worker_bearbeiten_keine_datei_doppelt(worker_fuer_text: None, im_prozess: list[str]) -> None:
    """
    Zwei Aufträge für dieselbe Datei (doppelt eingereiht, etwa nach einer Störung) in zwei Workern: Während der eine die
    Textebene liest, findet der andere die Datei beansprucht und endet ohne Wirkung. In beiden Stufen.
    """
    datei = _datei(_quelle(), TEXT_PDF)
    zweiter: list[str] = []
    lesen: Callable[..., TextLayerOutcome] = getattr(erkennung, "extract_text_layer_isolated")  # noqa: B009 - Ersatz

    def waehrenddessen(*args: Any, **kwargs: Any) -> TextLayerOutcome:
        zweiter.append(erkennung.textebene_lesen(str(datei.pk)))
        return lesen(*args, **kwargs)

    with mock.patch.object(erkennung, "extract_text_layer_isolated", side_effect=waehrenddessen):
        assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.ERLEDIGT
    assert zweiter == [erkennung.NICHT_ZU_TUN]
    assert im_prozess == ["anlage.pdf"], "genau einmal gelesen"

    scan = _datei(_quelle(), SCAN_PDF)
    assert erkennung.textebene_lesen(str(scan.pk)) == erkennung.ZUR_TEXTERKENNUNG
    erkannt: list[str] = []

    def erkennen_waehrenddessen(*_a: Any, **_k: Any) -> ExtractionResult:
        erkannt.append(erkennung.erkennen(str(scan.pk)))
        return ExtractionResult("Gescannt", "tesseract", 2)

    with mock.patch.object(erkennung, "extract_text", side_effect=erkennen_waehrenddessen) as zweite_stufe:
        assert erkennung.erkennen(str(scan.pk)) == erkennung.ERLEDIGT
    assert erkannt == [erkennung.NICHT_ZU_TUN] and zweite_stufe.call_count == 1


@pytest.mark.django_db(transaction=True)
@ZWEI_STUFEN
def test_zwei_worker_nebenlaeufig(im_prozess: list[str]) -> None:
    """Wie oben, aber wirklich gleichzeitig in eigenen Verbindungen (nur PostgreSQL: SELECT … FOR UPDATE SKIP LOCKED)."""
    nur_postgres()
    datei = _datei(_quelle(), TEXT_PDF)
    start = threading.Barrier(4)
    ergebnisse: list[str] = []
    fehler: list[BaseException] = []

    def worker() -> None:
        from django.db import connection

        try:
            start.wait(30)
            ergebnisse.append(erkennung.textebene_lesen(str(datei.pk)))
        except BaseException as exc:  # noqa: BLE001 - im Test sammeln
            fehler.append(exc)
        finally:
            connection.close()

    faeden = [threading.Thread(target=worker) for _ in range(4)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join(60)
    assert not fehler
    assert Counter(ergebnisse) == {erkennung.ERLEDIGT: 1, erkennung.NICHT_ZU_TUN: 3}
    assert im_prozess == ["anlage.pdf"]


# =============================================================================
# Reihum über die Quellen
# =============================================================================


@pytest.mark.django_db
@override_settings(TEXT_EXTRACTION_RUNNER="worker", TEXT_LAYER_QUEUE_DEPTH=4)
def test_einplanen_wechselt_zwischen_den_quellen_ab(worker_fuer_text: None) -> None:
    gross, mittel, klein = _quelle("Groß"), _quelle("Mittel"), _quelle("Klein")
    for nummer in range(6):
        _datei(gross, TEXT_PDF + b"\n%% g%d" % nummer)
    for nummer in range(2):
        _datei(mittel, TEXT_PDF + b"\n%% m%d" % nummer)
    kleine = _datei(klein, TEXT_PDF + b"\n% k")
    # Die Dateien der großen Quelle sind die neuesten: Früher gingen alle Plätze an sie
    OParlFile.objects.filter(body__in=[mittel, klein]).update(created_at=timezone.now() - timedelta(days=300))

    assert erkennung.einplanen() == 4
    je_quelle = Counter(OParlFile.objects.filter(pk__in=_wartend("text")).values_list("body__source__name", flat=True))
    assert set(je_quelle) == {gross.source.name, mittel.source.name, klein.source.name}
    assert max(je_quelle.values()) == 2
    assert str(kleine.pk) in _wartend("text")


def test_reihum_beruecksichtigt_wartende_auftraege() -> None:
    reihen = {"A": deque(["a1", "a2", "a3"]), "B": deque(["b1", "b2"])}
    # A hat schon drei Aufträge in der Warteschlange: zuerst B, danach wieder A
    assert erkennung._reihum(reihen, 3, Counter({"A": 3})) == ["b1", "b2", "a1"]
    reihen = {"A": deque(["a1", "a2", "a3"]), "B": deque(["b1"])}
    assert sorted(erkennung._reihum(reihen, 2, Counter())) == ["a1", "b1"], "je Quelle einer"
    assert erkennung._reihum({"A": deque(["a1"])}, 5, Counter()) == ["a1"]
