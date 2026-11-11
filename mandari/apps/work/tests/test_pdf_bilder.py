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


def _server(monkeypatch: pytest.MonkeyPatch, antworten: dict[str, Any]) -> list[str]:
    """Abgesicherter Client mit Ersatz-Transport: je Adresse eine Antwort oder ein Fehler; zurück die Abrufe."""
    geladen: list[str] = []

    def antworten_auf(request: httpx.Request) -> httpx.Response:
        geladen.append(str(request.url))
        antwort = antworten[str(request.url)]
        if isinstance(antwort, Exception):
            raise antwort
        inhalt, art = antwort
        # Als Strom wie von einem echten Server (nicht schon vollständig geladen)
        return httpx.Response(200, stream=httpx.ByteStream(inhalt), headers={"Content-Type": art})

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return httpx.Client(transport=httpx.MockTransport(antworten_auf), **kwargs)

    monkeypatch.setattr(safe_fetch, "guarded_client", client)
    return geladen


EINGEBETTET = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"


def test_bilder_fremder_server_werden_eingebettet(monkeypatch: pytest.MonkeyPatch) -> None:
    geladen = _server(monkeypatch, {"https://bilder.example.org/a.png": (PNG, "image/png; charset=binary")})
    html = '<p>Text</p><img alt="A" src="https://bilder.example.org/a.png"><img src="https://bilder.example.org/a.png">'

    ergebnis = pdf_bilder.inline_remote_images(html)

    assert ergebnis.count(f'src="{EINGEBETTET}"') == 2 and "https://bilder.example.org" not in ergebnis
    # Dieselbe Adresse wird einmal geladen
    assert geladen == ["https://bilder.example.org/a.png"]


def test_adresse_mit_mehreren_parametern_wird_unmaskiert_abgerufen(monkeypatch: pytest.MonkeyPatch) -> None:
    geladen = _server(monkeypatch, {"https://bilder.example.org/a.png?a=1&b=2": (PNG, "image/png")})

    ergebnis = pdf_bilder.inline_remote_images('<img src="https://bilder.example.org/a.png?a=1&amp;b=2">')

    assert geladen == ["https://bilder.example.org/a.png?a=1&b=2"]
    assert ergebnis == f'<img src="{EINGEBETTET}">'


@pytest.mark.parametrize(
    "antwort",
    [
        (b"<html>kein Bild</html>", "text/html"),
        httpx.ConnectError("nicht erreichbar"),
        safe_fetch.BlockedDestinationError("Ziel liegt nicht im öffentlichen Netz"),
        httpx.ReadTimeout("zu langsam"),
    ],
)
def test_nicht_ladbare_bilder_verlieren_ihre_adresse(monkeypatch: pytest.MonkeyPatch, antwort: Any) -> None:
    _server(monkeypatch, {"http://intern.example.org/x.png": antwort})

    ergebnis = pdf_bilder.inline_remote_images('<img src="http://intern.example.org/x.png" alt="x">')

    assert ergebnis == '<img src="" alt="x">'


def test_zu_grosses_bild_wird_nicht_eingebettet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_bilder, "MAX_IMAGE_BYTES", 10)
    _server(monkeypatch, {"https://bilder.example.org/gross.png": (PNG, "image/png")})

    assert pdf_bilder.inline_remote_images('<img src="https://bilder.example.org/gross.png">') == '<img src="">'


def test_frist_gilt_auch_bei_troepfelnden_daten(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Rumpf kommt in vielen kleinen Stücken: Nach der Frist bricht der Abruf ab, statt weiterzulesen."""
    uhr = {"jetzt": 1000.0}
    monkeypatch.setattr(pdf_bilder, "_now", lambda: uhr["jetzt"])

    class Troepfelnd(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            for _ in range(10_000):
                uhr["jetzt"] += 1.0
                yield b"x"

    def antworten_auf(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Troepfelnd(), headers={"Content-Type": "image/png"})

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("transport", None)
        return httpx.Client(transport=httpx.MockTransport(antworten_auf), **kwargs)

    monkeypatch.setattr(safe_fetch, "guarded_client", client)

    assert pdf_bilder.inline_remote_images('<img src="https://langsam.example.org/x.png">') == '<img src="">'
    # Abgebrochen kurz nach der Frist je Bild, nicht erst nach allen Stücken
    assert uhr["jetzt"] < 1000.0 + pdf_bilder.IMAGE_SECONDS + 5


def test_export_wartet_hoechstens_bis_zur_frist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hängt der Abruf (Verbindungsaufbau, Antwortkopf, Stillstand), geht es nach der Frist ohne das Bild weiter."""
    import threading
    import time

    monkeypatch.setattr(pdf_bilder, "IMAGE_SECONDS", 0.3)
    freigabe = threading.Event()

    def haengt(url: str, deadline: float) -> str:
        freigabe.wait(10)
        return "data:image/png;base64,AAAA"

    monkeypatch.setattr(pdf_bilder, "_fetch", haengt)
    start = time.monotonic()
    try:
        ergebnis = pdf_bilder.inline_remote_images('<img src="https://haengt.example.org/x.png">')
    finally:
        dauer = time.monotonic() - start
        freigabe.set()

    assert ergebnis == '<img src="">'
    assert dauer < 3


def test_hoechstens_so_viele_bilder_je_dokument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_bilder, "MAX_REMOTE_IMAGES", 2)
    adressen = [f"https://bilder.example.org/{nummer}.png" for nummer in range(4)]
    geladen = _server(monkeypatch, dict.fromkeys(adressen, (PNG, "image/png")))

    ergebnis = pdf_bilder.inline_remote_images("".join(f'<img src="{adresse}">' for adresse in adressen))

    assert geladen == adressen[:2]
    assert ergebnis.count("data:image/png;base64,") == 2 and ergebnis.count('src=""') == 2


def test_wiederholte_adresse_zaehlt_gegen_das_datenbudget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dieselbe Adresse an vielen Stellen: Eingebettet wird nur, bis das Budget des Dokuments aufgebraucht ist."""
    monkeypatch.setattr(pdf_bilder, "MAX_EMBEDDED_BYTES", 2 * len(EINGEBETTET) + 10)
    geladen = _server(monkeypatch, {"https://bilder.example.org/a.png": (PNG, "image/png")})

    ergebnis = pdf_bilder.inline_remote_images('<img src="https://bilder.example.org/a.png">' * 50)

    assert len(geladen) == 1
    assert ergebnis.count(EINGEBETTET) == 2 and ergebnis.count('src=""') == 48


def test_text_ohne_fremde_bilder_bleibt_unveraendert(monkeypatch: pytest.MonkeyPatch) -> None:
    geladen = _server(monkeypatch, {})
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
