# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PDF-Erzeugung liest nichts außerhalb des Dokuments; Bilder werden vorher eingebettet.

- ``html_to_pdf`` übergibt der Bibliothek eine Richtlinie ohne lokale Dateien und ohne Fernabrufe, mit Grenzen für
  Bildgröße und Dauer. Gesperrte Bildquellen fehlen im PDF, die Umwandlung gelingt.
- ``inline_remote_images`` lädt Bilder fremder Server über den abgesicherten Abruf und bettet sie ein; was sich
  nicht laden lässt oder über den Grenzen liegt, verliert seine Adresse.
- ``file_data_uri`` bettet eigene Bilddateien ein (Logo im Briefkopf).
"""

from __future__ import annotations

import base64
import io
from typing import Any

import httpx
import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db.models.fields.files import FieldFile, FileField

from apps.common import pdf
from apps.work import pdf_bilder
from insight_core.services import safe_fetch

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


# --- Richtlinie der Umwandlung -----------------------------------------------------------------------------------


def test_richtlinie_ohne_dateien_und_ohne_fernabruf() -> None:
    richtlinie = pdf._resource_policy()

    assert richtlinie.allow_remote is False
    assert richtlinie.base_dir is None and richtlinie.roots == ()
    assert richtlinie.max_image_pixels is not None and richtlinie.max_image_pixels <= 25_000_000
    assert richtlinie.max_fetch_seconds is not None
    assert richtlinie.max_render_seconds == pdf.MAX_RENDER_SECONDS


def test_umwandlung_bekommt_die_richtlinie(monkeypatch: pytest.MonkeyPatch) -> None:
    from xhtml2pdf import pisa

    gesehen: dict[str, Any] = {}
    echt = pisa.CreatePDF

    def mitschreiben(*args: Any, **kwargs: Any) -> Any:
        gesehen.update(kwargs)
        return echt(*args, **kwargs)

    monkeypatch.setattr(pisa, "CreatePDF", mitschreiben)

    pdf.html_to_pdf("<html><body><p>Text</p></body></html>")

    assert gesehen["resource_policy"].allow_remote is False


def test_gesperrte_bildquellen_fehlen_und_das_pdf_entsteht(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    geheim = tmp_path / "geheim.png"
    geheim.write_bytes(PNG)
    monkeypatch.chdir(tmp_path)
    verbunden: list[Any] = []

    def keine_verbindung(*args: Any, **kwargs: Any) -> None:
        verbunden.append(args)
        pytest.fail("Fernabruf")

    monkeypatch.setattr("socket.create_connection", keine_verbindung)
    html = (
        "<html><body><p>Text</p>"
        '<img src="geheim.png"><img src="http://bilder.example.org/x.png">'
        f'<img src="data:image/png;base64,{base64.b64encode(PNG).decode()}">'
        "</body></html>"
    )

    ergebnis = pdf.html_to_pdf(html)

    assert ergebnis.startswith(b"%PDF") and verbunden == []


# --- Bilder fremder Server ---------------------------------------------------------------------------------------


def _abruf(monkeypatch: pytest.MonkeyPatch, antworten: dict[str, Any]) -> list[str]:
    geladen: list[str] = []

    def download_to(target: io.BytesIO, url: str, **kwargs: Any) -> safe_fetch.Download:
        geladen.append(url)
        antwort = antworten[url]
        if isinstance(antwort, Exception):
            raise antwort
        inhalt, art = antwort
        target.write(inhalt)
        return safe_fetch.Download(content_type=art, size=len(inhalt))

    monkeypatch.setattr(safe_fetch, "download_to", download_to)
    return geladen


def test_bilder_fremder_server_werden_eingebettet(monkeypatch: pytest.MonkeyPatch) -> None:
    geladen = _abruf(monkeypatch, {"https://bilder.example.org/a.png": (PNG, "image/png; charset=binary")})
    html = '<p>Text</p><img alt="A" src="https://bilder.example.org/a.png"><img src="https://bilder.example.org/a.png">'

    ergebnis = pdf_bilder.inline_remote_images(html)

    eingebettet = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    assert ergebnis.count(f'src="{eingebettet}"') == 2 and "https://bilder.example.org" not in ergebnis
    # Dieselbe Adresse wird einmal geladen
    assert geladen == ["https://bilder.example.org/a.png"]


@pytest.mark.parametrize(
    "antwort",
    [
        (b"<html>kein Bild</html>", "text/html"),
        httpx.ConnectError("nicht erreichbar"),
        safe_fetch.BlockedDestinationError("Ziel liegt nicht im öffentlichen Netz"),
        safe_fetch.TooLargeError(),
        safe_fetch.DeadlineExceededError(),
    ],
)
def test_nicht_ladbare_bilder_verlieren_ihre_adresse(monkeypatch: pytest.MonkeyPatch, antwort: Any) -> None:
    _abruf(monkeypatch, {"http://intern.example.org/x.png": antwort})

    ergebnis = pdf_bilder.inline_remote_images('<img src="http://intern.example.org/x.png" alt="x">')

    assert ergebnis == '<img src="" alt="x">'


def test_hoechstens_so_viele_bilder_je_dokument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_bilder, "MAX_REMOTE_IMAGES", 2)
    adressen = [f"https://bilder.example.org/{nummer}.png" for nummer in range(4)]
    geladen = _abruf(monkeypatch, dict.fromkeys(adressen, (PNG, "image/png")))

    ergebnis = pdf_bilder.inline_remote_images("".join(f'<img src="{adresse}">' for adresse in adressen))

    assert geladen == adressen[:2]
    assert ergebnis.count("data:image/png;base64,") == 2 and ergebnis.count('src=""') == 2


def test_text_ohne_fremde_bilder_bleibt_unveraendert(monkeypatch: pytest.MonkeyPatch) -> None:
    geladen = _abruf(monkeypatch, {})
    html = '<p>Text mit <a href="https://example.org/x.png">Link</a></p><img src="data:image/png;base64,AAAA">'

    assert pdf_bilder.inline_remote_images(html) == html and geladen == []


# --- Eigene Dateien ------------------------------------------------------------------------------------------------


def _datei(tmp_path: Any, name: str, inhalt: bytes) -> FieldFile:
    ablage = FileSystemStorage(location=str(tmp_path))
    gespeichert = ablage.save(name, ContentFile(inhalt))
    return FieldFile(None, FileField(storage=ablage), gespeichert)  # type: ignore[arg-type]


def test_logo_wird_eingebettet(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert pdf_bilder.file_data_uri(_datei(tmp_path, "logo.png", PNG)).startswith("data:image/png;base64,")
    # Kein Bild, zu groß oder ohne Datei: kein Logo
    assert pdf_bilder.file_data_uri(_datei(tmp_path, "logo.svg", b"<svg/>")) == ""
    assert pdf_bilder.file_data_uri(None) == ""
    monkeypatch.setattr(pdf_bilder, "MAX_FILE_BYTES", 10)
    assert pdf_bilder.file_data_uri(_datei(tmp_path, "gross.png", PNG)) == ""
