"""
Gestreamter Download mit Größengrenze und Ablage nach SHA-256 im Ingestor (Issue #788).

Der Ingestor hält Dateien nie vollständig im Arbeitsspeicher: Er lädt in eine temporäre Datei, hasht
dabei und bricht ab, sobald die Grenze überschritten ist. Mit eingerichteter Ablage legt er die Datei
gleich unter ihrem SHA-256 ab (ein Abruf je Datei für Text und Ablage).
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.config import settings
from src.extraction import extractor as extractor_modul
from src.extraction.extractor import FileTooLargeError, TextExtractor

INHALT = b"%PDF-1.4\n" + b"Ratsbeschluss " * 2000


def _transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[int]:
    gesendet: list[int] = []
    echter_client = httpx.AsyncClient

    def mitzaehlen(request: httpx.Request) -> httpx.Response:
        response: httpx.Response = handler(request)
        gesendet.append(1)
        return response

    def client_mit_transport(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return echter_client(transport=httpx.MockTransport(mitzaehlen), **kwargs)

    monkeypatch.setattr("src.extraction.extractor.httpx.AsyncClient", client_mit_transport)
    return gesendet


class _Storage:
    def __init__(self, gelistet: bool = True) -> None:
        self.gelistet = gelistet
        self.texte: list[dict[str, Any]] = []
        self.abgelegt: list[tuple[Any, str, int, Path, Path]] = []

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def update_file_text(self, **werte: Any) -> None:
        self.texte.append(werte)

    async def body_stores_files(self, body_id: Any) -> bool:
        return self.gelistet

    async def attach_file_blob(self, file_id: Any, sha256: str, size: int, source: Path, target: Path) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        self.abgelegt.append((file_id, sha256, size, source, target))
        return True


@pytest.fixture
def ablage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "oparl_files_root", str(tmp_path))
    monkeypatch.setattr(settings, "file_store_layout", "sha256")
    monkeypatch.setattr(settings, "file_cache_min_free_gb", 0)
    return tmp_path


def _datei(**felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {
        "id": uuid.uuid4(),
        "body_id": uuid.uuid4(),
        "download_url": "https://ris.example.org/files/a.pdf",
        "access_url": None,
        "mime_type": "application/pdf",
        "file_name": "a.pdf",
    }
    werte.update(felder)
    return SimpleNamespace(**werte)


@pytest.mark.asyncio
async def test_gestreamt_mit_hash(monkeypatch: pytest.MonkeyPatch, ablage: Path) -> None:
    _transport(monkeypatch, lambda request: httpx.Response(200, content=INHALT))
    geladen = await TextExtractor(_Storage())._download_to_file("https://ris.example.org/files/a.pdf")
    try:
        assert geladen.sha256 == hashlib.sha256(INHALT).hexdigest()
        assert geladen.size == len(INHALT)
        assert geladen.path.read_bytes() == INHALT
        # Im selben Dateisystem wie die Ablage: Ablegen ist ein Umbenennen
        assert geladen.path.parent == ablage / "sha256" / "tmp"
    finally:
        geladen.discard()


@pytest.mark.asyncio
async def test_groessengrenze_greift_waehrend_des_downloads(monkeypatch: pytest.MonkeyPatch, ablage: Path) -> None:
    # Ohne Content-Length: die Grenze muss beim Lesen greifen, nicht erst danach
    def ohne_laenge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(INHALT))

    _transport(monkeypatch, ohne_laenge)
    extractor = TextExtractor(_Storage())
    extractor.max_size_bytes = 1000
    with pytest.raises(FileTooLargeError):
        await extractor._download_to_file("https://ris.example.org/files/a.pdf")
    assert list((ablage / "sha256" / "tmp").iterdir()) == [], "Teil-Download wird entfernt"


@pytest.mark.asyncio
async def test_zu_grosse_datei_wird_uebersprungen(monkeypatch: pytest.MonkeyPatch, ablage: Path) -> None:
    _transport(monkeypatch, lambda request: httpx.Response(200, content=INHALT))
    storage = _Storage()
    extractor = TextExtractor(storage)
    extractor.max_size_bytes = 1000
    assert not await extractor._process_file(_datei())
    assert storage.texte[0]["status"] == "skipped"
    assert storage.abgelegt == []


@pytest.mark.asyncio
async def test_text_und_ablage_aus_einem_abruf(monkeypatch: pytest.MonkeyPatch, ablage: Path) -> None:
    gesendet = _transport(monkeypatch, lambda request: httpx.Response(200, content=INHALT))
    monkeypatch.setattr(extractor_modul, "_extract_text_from_pdf", lambda source, name: ("Beschluss", 1, "pypdf"))
    storage = _Storage()
    datei = _datei()
    assert await TextExtractor(storage)._process_file(datei)

    assert len(gesendet) == 1, "ein Abruf für Text und Ablage"
    sha256 = hashlib.sha256(INHALT).hexdigest()
    assert [(a[0], a[1], a[2]) for a in storage.abgelegt] == [(datei.id, sha256, len(INHALT))]
    ziel = storage.abgelegt[0][4]
    assert ziel == ablage / "sha256" / sha256[:2] / sha256
    assert ziel.read_bytes() == INHALT
    assert storage.texte[-1]["status"] == "completed" and storage.texte[-1]["sha256_hash"] == sha256
    assert list((ablage / "sha256" / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_ausgeblendete_kommune_und_hinweisseiten_werden_nicht_abgelegt(
    monkeypatch: pytest.MonkeyPatch, ablage: Path
) -> None:
    monkeypatch.setattr(extractor_modul, "_extract_text_from_pdf", lambda source, name: ("", None, "none"))
    _transport(monkeypatch, lambda request: httpx.Response(200, content=INHALT))
    storage = _Storage(gelistet=False)
    await TextExtractor(storage)._process_file(_datei())
    assert storage.abgelegt == []

    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"<!doctype html><html>Bitte warten</html>"))
    storage = _Storage()
    await TextExtractor(storage)._process_file(_datei(mime_type=""))
    assert storage.abgelegt == []
    assert list((ablage / "sha256" / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_ohne_ablage_im_temp_verzeichnis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oparl_files_root", "")
    monkeypatch.setattr(extractor_modul, "_extract_text_from_pdf", lambda source, name: ("Text", 1, "pypdf"))
    _transport(monkeypatch, lambda request: httpx.Response(200, content=INHALT))
    storage = _Storage()
    assert await TextExtractor(storage)._process_file(_datei())
    assert storage.abgelegt == []
