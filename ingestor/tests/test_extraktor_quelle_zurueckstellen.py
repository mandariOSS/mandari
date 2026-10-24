"""
Quelle nicht erreichbar (Issue #1121): Verbindungsfehler, Zeitüberschreitungen, 408, 429 und 5xx beim Download
stellen die ganze Quelle mit wachsendem Abstand zurück; die Datei bleibt ``pending`` statt ``failed``.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mandari_oparl.robots import RETRY_UNREACHABLE_SECONDS

from src.extraction.extractor import (
    MAX_DEFER_SECONDS,
    MAX_TRANSIENT_FAILURES,
    DownloadedFile,
    SourceBackoff,
    TextExtractor,
    is_transient,
    retry_after_seconds,
)

BODY = uuid.UUID(int=1)
ANFRAGE = httpx.Request("GET", "https://rat.example.de/dokumente/a.pdf")


class _Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


class _Speicher:
    def __init__(self, dateien: list[Any] | None = None) -> None:
        self.dateien = dateien or []
        self.updates: list[dict[str, Any]] = []

    async def get_pending_files(
        self, *, body_id: Any, batch_size: int, max_size_bytes: int | None = None, retried: bool | None = None
    ) -> list[Any]:
        passend = [d for d in self.dateien if retried is None or (d.text_extraction_attempts > 0) == retried]
        auswahl = passend[:batch_size]
        for datei in auswahl:
            self.dateien.remove(datei)
        return auswahl

    async def mark_extraction_started(self, file_id: Any) -> int | None:
        return 1

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def update_file_text(self, **werte: Any) -> None:
        self.updates.append(werte)


def _datei(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=name,
        body_id=BODY,
        download_url=f"https://rat.example.de/dokumente/{name}.pdf",
        access_url=None,
        mime_type="application/pdf",
        file_name=f"{name}.pdf",
        text_extraction_attempts=0,
    )


def _status(code: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    antwort = httpx.Response(code, headers=headers, request=ANFRAGE)
    return httpx.HTTPStatusError(f"{code}", request=ANFRAGE, response=antwort)


def _download(tmp_path: Path, ergebnisse: list[BaseException | None], geladen: list[str]) -> Any:
    """Download-Ersatz: je Aufruf das nächste Ergebnis (Fehler oder geladene Datei)."""

    async def download(url: str, *_args: Any) -> DownloadedFile:
        geladen.append(url.rsplit("/", 1)[-1].removesuffix(".pdf"))
        fehler = ergebnisse.pop(0) if ergebnisse else None
        if fehler is not None:
            raise fehler
        pfad = tmp_path / f"{uuid.uuid4().hex}.part"
        pfad.write_bytes(b"%PDF-1.4")
        return DownloadedFile(path=pfad, size=8, sha256="0" * 64, head=b"%PDF-1.4")

    return download


@pytest.fixture
def ohne_texterkennung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TextExtractor, "_extract_text", staticmethod(lambda *a: ("Beschluss", 1, "pypdf")))


@pytest.mark.parametrize(
    ("fehler", "voruebergehend"),
    [
        (httpx.ConnectError("All connection attempts failed"), True),
        (httpx.ReadError("Connection reset by peer"), True),
        (httpx.ReadTimeout(""), True),
        (httpx.RemoteProtocolError("Server disconnected"), True),
        (_status(429), True),
        (_status(503), True),
        (_status(504), True),
        (_status(403), False),
        (_status(404), False),
        (httpx.UnsupportedProtocol("ftp"), False),
        (ValueError("kaputt"), False),
    ],
)
def test_voruebergehende_fehler(fehler: BaseException, voruebergehend: bool) -> None:
    assert is_transient(fehler) is voruebergehend


async def test_verbindungsfehler_stellt_quelle_zurueck_und_laesst_datei_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ohne_texterkennung: None
) -> None:
    speicher = _Speicher([_datei(f"d{nummer}") for nummer in range(6)])
    uhr = _Uhr()
    extractor = TextExtractor(speicher, clock=uhr)
    extractor.concurrency = 1
    geladen: list[str] = []
    monkeypatch.setattr(
        extractor,
        "_download_to_file",
        _download(tmp_path, [httpx.ConnectError("All connection attempts failed")], geladen),
    )

    assert await extractor.extract_pending_files(BODY) == 0
    # Nur die erste Datei fragt an; die zweite derselben Portion bleibt unangefragt, weitere werden nicht beansprucht
    assert geladen == ["d0"]
    assert speicher.updates == [
        {"file_id": "d0", "status": "pending", "undo_attempt": True},
        {"file_id": "d1", "status": "pending", "reset_attempts": False},
    ]
    assert len(speicher.dateien) == 4
    assert extractor._deferred(BODY)

    # Auch ein neuer Extraktor am selben Speicher (Sync und Scraper je Kommune) hält sich daran
    assert await TextExtractor(speicher, clock=uhr).extract_pending_files(BODY) == 0
    assert geladen == ["d0"]

    # Nach Ablauf geht es weiter
    uhr.jetzt += RETRY_UNREACHABLE_SECONDS
    assert await extractor.extract_pending_files(BODY) == 4
    assert geladen == ["d0", "d2", "d3", "d4", "d5"]


async def test_abstand_waechst_und_beginnt_nach_erfolg_von_vorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ohne_texterkennung: None
) -> None:
    speicher = _Speicher()
    uhr = _Uhr()
    extractor = TextExtractor(speicher, clock=uhr)
    abgewiesen = httpx.ConnectError("abgewiesen")
    ergebnisse: list[BaseException | None] = [abgewiesen, abgewiesen, None, abgewiesen]
    monkeypatch.setattr(extractor, "_download_to_file", _download(tmp_path, ergebnisse, []))

    def frist() -> float:
        return extractor._backoff.until[BODY] - uhr.jetzt

    assert await extractor._process_file(_datei("a")) is False
    assert frist() == RETRY_UNREACHABLE_SECONDS
    uhr.jetzt += frist()
    assert await extractor._process_file(_datei("b")) is False
    assert frist() == 2 * RETRY_UNREACHABLE_SECONDS
    uhr.jetzt += frist()
    assert await extractor._process_file(_datei("c")) is True
    assert await extractor._process_file(_datei("d")) is False
    assert frist() == RETRY_UNREACHABLE_SECONDS


async def test_abstand_hoechstens_sechs_stunden_und_retry_after_gilt_mindestens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speicher = _Speicher()
    uhr = _Uhr()
    extractor = TextExtractor(speicher, clock=uhr)
    ergebnisse: list[BaseException | None] = [_status(503, {"Retry-After": "7200"})] + [_status(502)] * 20
    monkeypatch.setattr(extractor, "_download_to_file", _download(tmp_path, ergebnisse, []))

    assert await extractor._process_file(_datei("a")) is False
    assert extractor._backoff.until[BODY] - uhr.jetzt == 7200
    for nummer in range(12):
        uhr.jetzt = extractor._backoff.until[BODY]
        await extractor._process_file(_datei(f"n{nummer}"))
    assert extractor._backoff.until[BODY] - uhr.jetzt == MAX_DEFER_SECONDS


async def test_dieselbe_datei_scheitert_nach_drei_fehlschlaegen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speicher = _Speicher()
    uhr = _Uhr()
    extractor = TextExtractor(speicher, clock=uhr)
    ergebnisse: list[BaseException | None] = [httpx.ReadTimeout("")] * MAX_TRANSIENT_FAILURES
    monkeypatch.setattr(extractor, "_download_to_file", _download(tmp_path, ergebnisse, []))

    for _ in range(MAX_TRANSIENT_FAILURES):
        uhr.jetzt += MAX_DEFER_SECONDS
        assert await extractor._process_file(_datei("a")) is False
    assert [update["status"] for update in speicher.updates] == ["pending"] * (MAX_TRANSIENT_FAILURES - 1) + ["failed"]
    assert speicher.updates[-1]["error"] == "Download failed: ReadTimeout"
    assert "a" not in extractor._backoff.file_failures
    # Der Fehlschlag, mit dem die Datei aufgegeben wird, stellt die Quelle nicht zurück: Weiter mit der nächsten
    assert not extractor._deferred(BODY)


async def test_parallele_fehlschlaege_einer_runde_zaehlen_einmal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fällt die Quelle aus, scheitern die gleichzeitig laufenden Dateien an ihr, nicht an sich selbst."""
    speicher = _Speicher([_datei(f"d{nummer}") for nummer in range(4)])
    uhr = _Uhr()
    extractor = TextExtractor(speicher, clock=uhr)
    extractor.concurrency = 4

    async def download(url: str, *_args: Any) -> DownloadedFile:
        await asyncio.sleep(0.01)
        raise httpx.ConnectError("abgewiesen")

    monkeypatch.setattr(extractor, "_download_to_file", download)
    for _ in range(MAX_TRANSIENT_FAILURES + 2):
        speicher.dateien = [_datei(f"d{nummer}") for nummer in range(4)]
        assert await extractor.extract_pending_files(BODY) == 0
        uhr.jetzt = extractor._backoff.until[BODY]

    # Je Runde ein gezählter Fehlschlag: höchstens eine Datei aufgegeben, nicht alle vier
    gescheitert = [u["file_id"] for u in speicher.updates if u["status"] == "failed"]
    assert len(gescheitert) <= 1
    assert sum(extractor._backoff.file_failures.values()) <= MAX_TRANSIENT_FAILURES


def test_robots_frist_kuerzt_keine_laengere_zurueckstellung() -> None:
    backoff = SourceBackoff()
    assert backoff.defer(BODY, 0.0, minimum=7200) == 7200
    backoff.hold(BODY, RETRY_UNREACHABLE_SECONDS)
    assert backoff.until[BODY] == 7200
    backoff.hold(BODY, 9000)
    assert backoff.until[BODY] == 9000


@pytest.mark.parametrize(("wert", "sekunden"), [("120", 120.0), ("", 0.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 0.0)])
def test_retry_after(wert: str, sekunden: float) -> None:
    assert retry_after_seconds(_status(503, {"Retry-After": wert})) == sekunden


def test_retry_after_mit_sonderziffern() -> None:
    antwort = httpx.Response(503, headers=[(b"Retry-After", "²".encode())], request=ANFRAGE)
    assert retry_after_seconds(httpx.HTTPStatusError("503", request=ANFRAGE, response=antwort)) == 0.0


async def test_dateifehler_wie_bisher_gescheitert_ohne_zurueckstellen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speicher = _Speicher()
    extractor = TextExtractor(speicher, clock=_Uhr())
    monkeypatch.setattr(extractor, "_download_to_file", _download(tmp_path, [_status(404)], []))

    assert await extractor._process_file(_datei("a")) is False
    assert speicher.updates == [{"file_id": "a", "status": "failed", "error": "Download failed: 404"}]
    assert not extractor._deferred(BODY)
