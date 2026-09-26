# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einbettung fremder Daten und Dateivorschau im Bürgerportal.

Namen, Titel und Anlagen stammen aus fremden Ratsinformationssystemen. Sie dürfen im
gemeinsamen Ursprung von Insight, Work und Session nie als Skript oder aktives Dokument
laufen: JSON in ``<script>``-Elementen wird maskiert, Werte für Skripte kommen nicht als
Quelltext in den Code, und die Dateivorschau zeigt nur passive Formate im Browser an.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.test import Client

from insight_core.models import (
    LocationMapping,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.services import file_cache

pytestmark = pytest.mark.django_db

AUSBRUCH = "Radweg </script><script>alert(document.domain)</script>"
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt"
    )


def _json_ld(html: str) -> list[Any]:
    return [
        json.loads(inhalt)
        for attribute, inhalt in SCRIPT_RE.findall(html)
        if "application/ld+json" in attribute.lower()
    ]


def _ausfuehrbare_skripte(html: str) -> str:
    """Inhalt aller Skripte, die der Browser ausführt (ohne JSON-Datenblöcke)."""
    return "\n".join(inhalt for attribute, inhalt in SCRIPT_RE.findall(html) if "json" not in attribute.lower())


# =============================================================================
# JSON-LD und Daten in Skripten
# =============================================================================


class TestJsonLd:
    def test_vorgangsname_bricht_nicht_aus(self, body: OParlBody) -> None:
        paper = OParlPaper.objects.create(
            external_id="https://ris.fremd.example/oparl/paper/1", body=body, name=AUSBRUCH, reference="V/1"
        )
        html = Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()
        assert "</script><script>alert(document.domain)" not in html
        # Der Inhalt bleibt als Daten erhalten: Suchmaschinen lesen denselben Namen
        assert _json_ld(html)[0]["name"] == AUSBRUCH

    @pytest.mark.parametrize("art", ["person", "organization"])
    def test_personen_und_gremien(self, body: OParlBody, art: str) -> None:
        if art == "person":
            person = OParlPerson.objects.create(
                external_id="https://ris.fremd.example/oparl/person/1", body=body, name=AUSBRUCH, family_name="X"
            )
            url = f"/insight/personen/{person.id}/"
        else:
            gremium = OParlOrganization.objects.create(
                external_id="https://ris.fremd.example/oparl/organization/1", body=body, name=AUSBRUCH
            )
            url = f"/insight/gremien/{gremium.id}/"
        html = Client().get(url).content.decode()
        assert "</script><script>alert(document.domain)" not in html
        assert _json_ld(html)[0]["name"] == AUSBRUCH

    def test_zeilentrenner_und_kaufmanns_und(self) -> None:
        from insight_core.seo import script_json

        text = script_json({"name": "A & B   <b>"})
        assert "<" not in text and ">" not in text and "&" not in text
        assert " " not in text
        assert json.loads(text) == {"name": "A & B   <b>"}

    def test_admin_diagramm_mit_kommunennamen(self, body: OParlBody, rf: Any) -> None:
        from insight_core.admin_dashboard import dashboard_callback

        body.name = AUSBRUCH
        body.save()
        OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/2", body=body, name="X")
        context = cast(Any, dashboard_callback)(rf.get("/admin/"), {})
        labels = str(context["papers_by_body_labels"])
        assert "</script>" not in labels
        assert json.loads(labels) == [AUSBRUCH]


class TestSitzungsortInDerKarte:
    def test_ortsname_nicht_als_quelltext_im_skript(self, body: OParlBody) -> None:
        ort = "Rathaus ${alert(1)} <img src=x onerror=alert(2)>"
        LocationMapping.objects.create(body=body, location_name=ort, latitude="51.96", longitude="7.62")
        meeting = OParlMeeting.objects.create(
            external_id="https://ris.fremd.example/oparl/meeting/1",
            body=body,
            name="Rat",
            location_name=ort,
            location_address="Platz 1 `${alert(3)}`",
        )
        html = Client().get(f"/insight/termine/{meeting.id}/").content.decode()
        skripte = _ausfuehrbare_skripte(html)
        assert "L.map(" in skripte, "Karte fehlt – Testaufbau prüfen"
        assert "alert(1)" not in skripte
        assert "alert(3)" not in skripte
        # Die Angaben selbst erscheinen weiter, maskiert als Text
        assert "Rathaus ${alert(1)} &lt;img" in html


# =============================================================================
# Dateivorschau
# =============================================================================


def _datei(body: OParlBody, mime: str, name: str, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"https://ris.fremd.example/oparl/file/{name}",
        body=body,
        name=name,
        file_name=name,
        mime_type=mime,
        download_url=f"https://ris.fremd.example/files/{name}",
        **felder,
    )


def _lokal(tmp_path: Path, datei: OParlFile, inhalt: bytes) -> OParlFile:
    pfad = tmp_path / f"{datei.id}.bin"
    pfad.write_bytes(inhalt)
    datei.local_path = str(pfad)
    datei.local_status = "ok"
    datei.save(update_fields=["local_path", "local_status"])
    return datei


def _abgesichert(response: Any) -> None:
    assert response["Content-Type"].split(";")[0] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")
    assert "sandbox" in response.get("Content-Security-Policy", "")
    assert response["X-Content-Type-Options"] == "nosniff"


class TestDateivorschau:
    @pytest.mark.parametrize(
        ("mime", "name", "inhalt"),
        [
            ("text/html", "bericht.html", b"<html><script>alert(document.domain)</script></html>"),
            ("image/svg+xml", "plan.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
            ("application/xhtml+xml", "seite.xhtml", b"<html xmlns='http://www.w3.org/1999/xhtml'/>"),
            ("text/xml", "daten.xml", b"<?xml version='1.0'?><x/>"),
        ],
    )
    def test_aktive_formate_aus_dem_cache_nur_als_download(
        self, body: OParlBody, tmp_path: Path, mime: str, name: str, inhalt: bytes
    ) -> None:
        datei = _lokal(tmp_path, _datei(body, mime, name), inhalt)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        _abgesichert(response)

    def test_aktive_formate_beim_liveabruf_nur_als_download(
        self, body: OParlBody, tmp_path: Path, monkeypatch: Any
    ) -> None:
        datei = _datei(body, "text/html", "bericht.html")
        _quelle_liefert(monkeypatch, tmp_path, b"<!doctype html><script>alert(document.domain)</script>", "text/html")
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        _abgesichert(response)

    def test_svg_ohne_mime_angabe_beim_liveabruf(self, body: OParlBody, tmp_path: Path, monkeypatch: Any) -> None:
        datei = _datei(body, "", "plan")
        _quelle_liefert(monkeypatch, tmp_path, b"<svg><script>alert(1)</script></svg>", "image/svg+xml")
        _abgesichert(Client().get(f"/insight/dokumente/{datei.id}/preview/"))

    def test_pdf_bleibt_im_iframe_anzeigbar(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _lokal(tmp_path, _datei(body, "application/pdf", "vorlage.pdf"), b"%PDF-1.4 test")
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith("inline")
        # Eine Sandbox würde den PDF-Betrachter der Browser abschalten
        assert "sandbox" not in response.get("Content-Security-Policy", "")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "X-Frame-Options" not in response

    def test_pdf_herunterladen(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _lokal(tmp_path, _datei(body, "application/pdf", "vorlage.pdf"), b"%PDF-1.4 test")
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/?download=1")
        assert response["Content-Disposition"].startswith("attachment")
        assert 'filename="vorlage.pdf"' in response["Content-Disposition"]

    def test_bilder_inline_mit_sandbox(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _lokal(tmp_path, _datei(body, "image/png", "foto.png"), b"\x89PNG\r\n\x1a\n")
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response["Content-Type"] == "image/png"
        assert response["Content-Disposition"].startswith("inline")
        assert "sandbox" in response["Content-Security-Policy"]

    @pytest.mark.parametrize("weg", ["cache", "live"])
    @pytest.mark.parametrize(
        ("name", "erwartet"),
        [
            ('Bericht "Teil 1"\r\nX-Evil: 1;.pdf', 'filename="Bericht Teil 1 X-Evil 1.pdf"'),
            ("Übersicht Straßen.pdf", "filename*=utf-8''%C3%9Cbersicht%20Stra%C3%9Fen.pdf"),
            ("../../etc/passwd", 'filename="passwd"'),
        ],
    )
    def test_dateiname_aus_dem_ris_wird_bereinigt(
        self, body: OParlBody, tmp_path: Path, monkeypatch: Any, weg: str, name: str, erwartet: str
    ) -> None:
        datei = _datei(body, "application/pdf", "b.pdf")
        if weg == "cache":
            _lokal(tmp_path, datei, b"%PDF-1.4 test")
        else:
            _quelle_liefert(monkeypatch, tmp_path, b"%PDF-1.4 test", "application/pdf")
        datei.file_name = name
        datei.save(update_fields=["file_name"])
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/?download=1")
        assert response.status_code == 200
        kopf = response["Content-Disposition"]
        assert kopf.startswith("attachment")
        assert erwartet in kopf
        assert not any(zeichen in kopf for zeichen in "\r\n")


def _quelle_liefert(monkeypatch: Any, tmp_path: Path, inhalt: bytes, content_type: str) -> None:
    """Das Quell-RIS antwortet mit ``inhalt``; der Dokument-Cache schreibt nach ``tmp_path``."""
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)

    def upstream(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200, content=inhalt, headers={"content-type": content_type}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("insight_core.views.files.httpx.get", upstream)
