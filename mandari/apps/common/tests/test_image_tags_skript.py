# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/check_image_tags.py`` (Issue #698): Der Release-Workflow prüft damit anonym, ob jedes Tag für alle
drei Images abrufbar ist. Hier ohne Netz gegen eine nachgebaute Registry."""

from __future__ import annotations

import importlib.util
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_image_tags.py"


def _lade_skript() -> Any:
    spec = importlib.util.spec_from_file_location("check_image_tags", SKRIPT)
    assert spec and spec.loader
    modul = importlib.util.module_from_spec(spec)
    sys.modules["check_image_tags"] = modul
    spec.loader.exec_module(modul)
    return modul


class _Registry:
    """Antwortet auf HEAD-Anfragen nach Manifesten mit festen Statuscodes; merkt sich die Anfragen."""

    def __init__(self, status: dict[str, int], standard: int = 200) -> None:
        self.status, self.standard = status, standard
        self.anfragen: list[urllib.request.Request] = []

    def __call__(self, anfrage: urllib.request.Request) -> int:
        self.anfragen.append(anfrage)
        pfad = anfrage.full_url.split("/v2/", 1)[1]
        repository, tag = pfad.split("/manifests/")
        return self.status.get(f"{repository.split('/')[-1]}:{tag}", self.standard)


def _token(registry: str, repository: str) -> str:
    return f"token-{repository}"


def test_alle_vorhanden() -> None:
    modul = _lade_skript()
    registry = _Registry({})

    ergebnis = modul.pruefe(["v0.11.0", "latest"], token_holen=_token, oeffner=registry, pause=0)

    assert set(ergebnis) == {f"{i}:{t}" for i in ("mandari", "ingestor", "website") for t in ("v0.11.0", "latest")}
    assert set(ergebnis.values()) == {modul.VORHANDEN}
    anfrage = registry.anfragen[0]
    assert anfrage.get_method() == "HEAD"
    assert anfrage.full_url == "https://ghcr.io/v2/mandarioss/mandari/manifests/v0.11.0"
    assert anfrage.get_header("Authorization") == "Bearer token-mandarioss/mandari"
    assert "application/vnd.oci.image.index.v1+json" in (anfrage.get_header("Accept") or "")


def test_fehlendes_website_tag_wird_gemeldet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """So sah es nach dem Release v0.11.0 aus: mandari und ingestor mit Versions-Tag, die Website ohne."""
    modul = _lade_skript()
    registry = _Registry({"website:v0.11.0": 404})
    monkeypatch.setattr(modul, "_http_status", registry)
    monkeypatch.setattr(modul, "_anonymes_token", _token)

    assert modul.main(["v0.11.0,latest"]) == 1

    ausgabe = capsys.readouterr()
    assert "website:v0.11.0" in ausgabe.out and modul.FEHLT in ausgabe.out
    assert "Nicht abrufbar: website:v0.11.0" in ausgabe.err


def test_unklare_antwort_wird_wiederholt_und_gilt_nicht_als_vorhanden(monkeypatch: pytest.MonkeyPatch) -> None:
    modul = _lade_skript()
    registry = _Registry({"mandari:dev": 503})
    monkeypatch.setattr(modul.time, "sleep", lambda _s: None)

    ergebnis = modul.pruefe(["dev"], images=["mandari"], token_holen=_token, oeffner=registry, versuche=3)

    assert ergebnis["mandari:dev"].startswith(modul.UNKLAR)
    assert len(registry.anfragen) == 3


def test_auswahl_der_images(monkeypatch: pytest.MonkeyPatch) -> None:
    modul = _lade_skript()
    registry = _Registry({"website:dev": 404})
    monkeypatch.setattr(modul, "_http_status", registry)
    monkeypatch.setattr(modul, "_anonymes_token", _token)

    assert modul.main(["--images", "mandari,ingestor", "dev"]) == 0
    assert all("/website/" not in anfrage.full_url for anfrage in registry.anfragen)
