# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auslieferung lokaler Dokumentkopien über den Webserver (X-Accel-Redirect, Issue #785).

Django prüft Zugriff und Sperre und antwortet ohne Dateiinhalt; die Bytes liefert Caddy aus der
Ablage (Range, ETag). Die Schutzkopfzeilen müssen in der Antwort von Django stehen, denn Caddy
übernimmt genau diese – sonst liefe eine HTML- oder SVG-Anlage im gemeinsamen Ursprung.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.test import Client, override_settings

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_accel

pytestmark = pytest.mark.django_db


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt"
    )


@pytest.fixture
def ablage(tmp_path: Path, settings: Any) -> Path:
    root = tmp_path / "ablage"
    root.mkdir()
    settings.OPARL_FILES_ROOT = root
    settings.FILE_ACCEL_REDIRECT = True
    return root


def _datei(body: OParlBody, pfad: Path, mime: str, name: str, inhalt: bytes = b"%PDF-1.4 test") -> OParlFile:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(inhalt)
    return OParlFile.objects.create(
        external_id=f"https://ris.fremd.example/oparl/file/{name}",
        body=body,
        name=name,
        file_name=name,
        mime_type=mime,
        download_url=f"https://ris.fremd.example/files/{name}",
        local_path=str(pfad),
        local_status="ok",
    )


def _abrufen(datei: OParlFile, **params: str) -> Any:
    return Client().get(f"/insight/dokumente/{datei.id}/preview/", params)


class TestWeiterleitung:
    def test_pdf_ohne_inhalt_mit_internem_pfad(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body, ablage / "beispielstadt" / "2026" / "a1b2.pdf", "application/pdf", "vorlage.pdf")
        response = _abrufen(datei)
        assert response.status_code == 200
        assert response["X-Accel-Redirect"] == "/_mandari/dateien/beispielstadt/2026/a1b2.pdf"
        assert response.content == b""
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith("inline")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" not in response.get("Content-Security-Policy", "")
        assert response["X-Mandari-Cache"] == "hit"
        assert response["Cache-Control"] == "public, max-age=86400"
        assert "X-Frame-Options" not in response
        # Dokumente gehören nicht in Suchmaschinen (Issue #914); Caddy übernimmt die Kopfzeile
        assert response["X-Robots-Tag"] == "noindex"

    def test_herunterladen(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body, ablage / "beispielstadt" / "2026" / "a1b2.pdf", "application/pdf", "vorlage.pdf")
        response = _abrufen(datei, download="1")
        assert response["X-Accel-Redirect"].startswith("/_mandari/dateien/")
        assert response["Content-Disposition"].startswith("attachment")
        assert 'filename="vorlage.pdf"' in response["Content-Disposition"]

    @pytest.mark.parametrize(
        ("mime", "name", "inhalt"),
        [
            ("text/html", "bericht.html", b"<html><script>alert(document.domain)</script></html>"),
            ("image/svg+xml", "plan.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        ],
    )
    def test_aktive_formate_behalten_die_schutzkopfzeilen(
        self, body: OParlBody, ablage: Path, mime: str, name: str, inhalt: bytes
    ) -> None:
        # Die Datei liegt mit ihrer Endung in der Ablage; ohne die Kopfzeilen von Django würde der
        # Webserver sie nach der Endung als HTML bzw. SVG ausliefern
        datei = _datei(body, ablage / "beispielstadt" / "2026" / name, mime, name, inhalt)
        response = _abrufen(datei)
        assert response["X-Accel-Redirect"] == f"/_mandari/dateien/beispielstadt/2026/{name}"
        assert response["Content-Type"] == "application/octet-stream"
        assert response["Content-Disposition"].startswith("attachment")
        assert response["Content-Security-Policy"] == "sandbox"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_abgeschaltet_liefert_django_selbst(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body, ablage / "beispielstadt" / "2026" / "a1b2.pdf", "application/pdf", "vorlage.pdf")
        with override_settings(FILE_ACCEL_REDIRECT=False):
            response = _abrufen(datei)
        assert "X-Accel-Redirect" not in response
        assert b"".join(response.streaming_content) == b"%PDF-1.4 test"


def test_caddy_uebernimmt_die_kopfzeilen_von_django() -> None:
    """Was Django für Dokumente aus der Ablage setzt, muss Caddy in die Antwort übernehmen (Caddyfile)."""
    caddyfile = (Path(django_settings.BASE_DIR).parent / "Caddyfile").read_text(encoding="utf-8")
    uebernommen = next(z for z in caddyfile.splitlines() if z.strip().startswith("include Content-Type")).split()
    for kopf in ("Content-Type", "Content-Disposition", "X-Content-Type-Options", "Content-Security-Policy"):
        assert kopf in uebernommen, kopf
    assert "X-Robots-Tag" in uebernommen, "sonst stehen Dokumente aus der Ablage ohne noindex im Netz (Issue #914)"


class TestNurUnterhalbDerAblage:
    def test_datei_ausserhalb_der_ablage(self, body: OParlBody, ablage: Path, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path / "anderswo" / "a.pdf", "application/pdf", "a.pdf")
        response = _abrufen(datei)
        assert "X-Accel-Redirect" not in response
        assert b"".join(response.streaming_content) == b"%PDF-1.4 test"

    def test_pfad_mit_ruecksprung(self, ablage: Path, tmp_path: Path) -> None:
        (tmp_path / "geheim.pdf").write_bytes(b"x")
        (ablage / "stadt").mkdir()
        assert file_accel.internal_path(ablage / "stadt" / ".." / ".." / "geheim.pdf") is None

    def test_verweis_nach_aussen(self, ablage: Path, tmp_path: Path) -> None:
        ziel = tmp_path / "geheim.pdf"
        ziel.write_bytes(b"x")
        verweis = ablage / "verweis.pdf"
        try:
            os.symlink(ziel, verweis)
        except (OSError, NotImplementedError):
            pytest.skip("Symbolische Verweise auf diesem System nicht erlaubt")
        assert file_accel.internal_path(verweis) is None

    @pytest.mark.parametrize("name", [".versteckt.pdf", "mit leerzeichen.pdf", "umlaut-ä.pdf", "frage?.pdf"])
    def test_ungewoehnliche_namen_liefert_django(self, ablage: Path, name: str) -> None:
        pfad = ablage / "stadt" / name
        pfad.parent.mkdir()
        try:
            pfad.write_bytes(b"x")
        except OSError:
            pytest.skip("Dateiname auf diesem System nicht zulässig")
        assert file_accel.internal_path(pfad) is None

    def test_fehlende_datei(self, ablage: Path) -> None:
        assert file_accel.internal_path(ablage / "stadt" / "fehlt.pdf") is None

    def test_verzeichnis_statt_datei(self, ablage: Path) -> None:
        (ablage / "stadt").mkdir()
        assert file_accel.internal_path(ablage / "stadt") is None
