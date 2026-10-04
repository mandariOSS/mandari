# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statische Dateien: Vite-Bundles unter ihrem Vite-Namen, gehashte Dateien dauerhaft cachebar (mandari/static_files.py).

Vorher lud der Browser die Chunks ``json-script-…js`` und ``csrf-…js`` doppelt (Vorab-Link unter dem Namen des
Manifest-Storage, Import unter dem Vite-Namen), und WhiteNoise lieferte die Vite-Namen mit ``max-age=60`` aus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.test import RequestFactory, override_settings
from whitenoise.middleware import WhiteNoiseMiddleware

from mandari.static_files import ManifestStaticFilesStorage, immutable_file_test, is_vite_asset

DAUERHAFT = "max-age=315360000, public, immutable"
KURZ = "max-age=60, public"


@pytest.mark.parametrize(
    ("name", "erwartet"),
    [
        ("dist/assets/csrf-PtHGiBr-.js", True),
        ("dist/assets/main-BXRI7s5k.js", True),
        ("dist/assets/pdf.worker.min-Dswkl-cV.mjs", True),
        ("dist/assets/zap--lKHGnjw.js", True),
        ("dist/manifest.json", False),
        ("dist/assets/main.js", False),
        ("dist/assets/main-BXRI7s5k.67da78e2dddf.js", False),
        ("js/alpine-collapse.js", False),
        ("vendor/leaflet/leaflet-src.js", False),
    ],
)
def test_vite_namen(name: str, erwartet: bool) -> None:
    assert is_vite_asset(name) is erwartet


@pytest.fixture
def static_root(tmp_path: Path) -> Any:
    dateien = {
        "dist/assets/main-AbCd_f12.js": "export {}",
        "dist/assets/csrf-PtHGiBr-.js": "export {}",
        "dist/manifest.json": "{}",
        "css/app.css": "body{}",
        "css/app.0123456789ab.css": "body{}",
    }
    for name, inhalt in dateien.items():
        pfad = tmp_path / name
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(inhalt, encoding="utf-8")
    manifest = {"paths": {"css/app.css": "css/app.0123456789ab.css"}, "version": "1.1", "hash": "0"}
    (tmp_path / "staticfiles.json").write_text(json.dumps(manifest), encoding="utf-8")
    storages = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "mandari.static_files.ManifestStaticFilesStorage"},
    }
    with override_settings(
        DEBUG=False,
        STATIC_ROOT=str(tmp_path),
        STATIC_URL="/static/",
        STORAGES=storages,
        WHITENOISE_AUTOREFRESH=False,
        WHITENOISE_USE_FINDERS=False,
    ):
        yield tmp_path


def test_einstellung_gesetzt() -> None:
    from django.conf import settings

    assert settings.WHITENOISE_IMMUTABLE_FILE_TEST is immutable_file_test


def test_storage_adressiert_vite_dateien_ohne_zweiten_hash(static_root: Path) -> None:
    storage = ManifestStaticFilesStorage(location=str(static_root), base_url="/static/")

    # Einstieg und Vorab-Links ({% vite_asset %}) zeigen auf dieselbe Adresse wie die Importe in den Bundles
    assert storage.url("dist/assets/main-AbCd_f12.js") == "/static/dist/assets/main-AbCd_f12.js"
    assert storage.url("dist/assets/csrf-PtHGiBr-.js") == "/static/dist/assets/csrf-PtHGiBr-.js"
    # Alles andere weiter mit dem Hash des Manifest-Storage
    assert storage.url("css/app.css") == "/static/css/app.0123456789ab.css"


@pytest.mark.parametrize(
    ("url", "cache_control"),
    [
        ("/static/dist/assets/main-AbCd_f12.js", DAUERHAFT),
        ("/static/dist/assets/csrf-PtHGiBr-.js", DAUERHAFT),
        ("/static/css/app.0123456789ab.css", DAUERHAFT),
        ("/static/css/app.css", KURZ),
        ("/static/dist/manifest.json", KURZ),
    ],
)
def test_cache_header_von_whitenoise(static_root: Path, url: str, cache_control: str) -> None:
    middleware = WhiteNoiseMiddleware(lambda request: None)

    antwort = middleware(RequestFactory().get(url))

    assert antwort is not None and antwort.status_code == 200, url
    assert antwort["Cache-Control"] == cache_control
