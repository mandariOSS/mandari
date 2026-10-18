# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fitnessfunktion „insight_ai lädt keine Dateien“ (Issue #919, ADR Dokumentkette, Abschnitt 7 und „Prüfung“).

Dateien holt nur der Abrufweg, Text erkennt nur der Auftrag ``file.extract_text``. Die KI-Funktionen lesen den
gespeicherten Text. Geprüft wird zweifach:

- **Im Quelltext:** Kein Modul von ``insight_ai`` (außer Tests) importiert die Download-, Ablage- oder
  Erkennungsmodule, auch nicht innerhalb von Funktionen; HTTP-Bibliotheken nur die Anbieter der KI
  (``insight_ai.providers``).
- **Im Ablauf:** Eine Zusammenfassung für einen Vorgang ohne gespeicherten Text öffnet keine Verbindung.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import insight_ai

#: Module, die Dateien laden, ablegen oder Text erkennen (Präfixe; Untermodule zählen mit)
VERBOTEN = (
    "hub.ris.abruf",
    "hub.ris.erkennung",
    "insight_core.services.document_extraction",
    "insight_core.services.file_cache",
    "insight_core.services.file_store",
    "insight_core.services.file_reconcile",
    "insight_core.services.object_storage",
    "insight_core.services.robots",
    "insight_core.services.safe_fetch",
    "insight_core.services.text_extraction_job",
    "mandari_dokumente",
    "mandari_oparl.robots",
)
#: HTTP-Bibliotheken: nur für die Anbieter der KI
HTTP = ("aiohttp", "httpx", "requests", "urllib.request", "urllib3")
ANBIETER = "insight_ai.providers"


def _module() -> list[tuple[str, Path]]:
    basis = Path(insight_ai.__file__).parent
    module = []
    for pfad in sorted(basis.rglob("*.py")):
        teile = pfad.relative_to(basis.parent).with_suffix("").parts
        if "tests" in teile:
            continue
        name = ".".join(teile[:-1] if teile[-1] == "__init__" else teile)
        module.append((name, pfad))
    return module


def _importe(modul: str, pfad: Path) -> set[str]:
    """Alle importierten Module (absolut), auch in Funktionen; ``from x import y`` zählt als ``x`` und ``x.y``."""
    paket = modul if pfad.name == "__init__.py" else modul.rpartition(".")[0]
    gefunden: set[str] = set()
    for knoten in ast.walk(ast.parse(pfad.read_text(encoding="utf-8"))):
        if isinstance(knoten, ast.Import):
            gefunden.update(alias.name for alias in knoten.names)
        elif isinstance(knoten, ast.ImportFrom):
            if knoten.level:
                basis = paket.split(".")
                basis = basis[: len(basis) - (knoten.level - 1)]
                ziel = ".".join([*basis, knoten.module] if knoten.module else basis)
            else:
                ziel = knoten.module or ""
            gefunden.add(ziel)
            gefunden.update(f"{ziel}.{alias.name}" for alias in knoten.names)
    return gefunden


def _trifft(name: str, praefixe: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(f"{p}.") for p in praefixe)


def test_quelltext_importiert_keine_download_oder_erkennungsmodule() -> None:
    module = _module()
    assert any(name == "insight_ai.services.summarizer" for name, _ in module), "Module nicht gefunden"
    verstoesse = []
    for name, pfad in module:
        for ziel in sorted(_importe(name, pfad)):
            if _trifft(ziel, VERBOTEN):
                verstoesse.append(f"{name} → {ziel}")
            elif _trifft(ziel, HTTP) and not _trifft(name, (ANBIETER,)):
                verstoesse.append(f"{name} → {ziel} (HTTP nur in {ANBIETER})")
    assert not verstoesse, "insight_ai lädt keine Dateien:\n" + "\n".join(verstoesse)


def test_pruefung_erkennt_importe_in_funktionen(tmp_path: Path) -> None:
    """Die Prüfung selbst: Ein Import im Funktionsrumpf, relativ oder absolut, wird gefunden."""
    quelle = tmp_path / "beispiel.py"
    quelle.write_text(
        "def f():\n"
        "    from insight_core.services.document_extraction import download_and_extract\n"
        "    from insight_core.services import file_cache\n"
        "    import httpx\n",
        encoding="utf-8",
    )
    importe = _importe("insight_ai.services.beispiel", quelle)
    assert _trifft("insight_core.services.document_extraction", VERBOTEN)
    assert "insight_core.services.document_extraction" in importe
    assert "insight_core.services.file_cache" in importe
    assert "httpx" in importe


@pytest.mark.django_db
def test_zusammenfassung_ohne_text_oeffnet_keine_verbindung(monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_ai.services.summarizer import NoTextContentError, SummaryError, SummaryService
    from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource

    verbindungen: list[Any] = []

    def kein_netz(self: socket.socket, adresse: Any) -> None:
        verbindungen.append(adresse)
        raise OSError("Netz gesperrt")

    def keine_aufloesung(host: Any, *_args: Any, **_kwargs: Any) -> Any:
        verbindungen.append(host)
        raise socket.gaierror("Netz gesperrt")

    monkeypatch.setattr(socket.socket, "connect", kein_netz)
    monkeypatch.setattr(socket.socket, "connect_ex", kein_netz)
    monkeypatch.setattr(socket, "getaddrinfo", keine_aufloesung)
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt")
    paper = OParlPaper.objects.create(external_id="https://ris.example.org/oparl/paper/1", body=body, name="Vorlage")
    for nummer, status in enumerate(("pending", "failed", "skipped")):
        OParlFile.objects.create(
            external_id=f"https://ris.example.org/oparl/file/{nummer}",
            body=body,
            paper=paper,
            download_url=f"https://ris.example.org/dokumente/{nummer}.pdf",
            mime_type="application/pdf",
            text_extraction_status=status,
        )
    anbieter = mock.Mock()
    anbieter.is_available.return_value = True

    with pytest.raises(SummaryError) as fehler:
        SummaryService(provider=anbieter).generate_summary(paper)

    assert not isinstance(fehler.value, NoTextContentError), "die Erkennung steht aus: Hinweis, nicht „kein Text“"
    assert verbindungen == []
    anbieter.chat_completion.assert_not_called()
    assert set(OParlFile.objects.values_list("text_content", flat=True)) == {None}
