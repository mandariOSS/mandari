# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemessene Dateigröße und Zugriffsprotokoll der Dokumentablage (Issue #786).

``size`` ist die Angabe der Quelle und fehlt bei manchen Quellen; die gemessene Größe unserer Kopie
steht in ``local_size``. Jeder Abruf über die Dateivorschau zählt genau einmal, ohne Personenbezug.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.db import DatabaseError
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlBody, OParlFile, OParlFileAccessDay, OParlSource
from insight_core.services import file_access, file_cache

pytestmark = pytest.mark.django_db


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt", is_listed=True
    )


@pytest.fixture
def ablage(tmp_path: Path, settings: Any) -> Path:
    root = tmp_path / "ablage"
    settings.OPARL_FILES_ROOT = root
    settings.FILE_CACHE_MIN_FREE_GB = 0
    return root


def _datei(body: OParlBody, name: str = "vorlage.pdf", **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"https://ris.fremd.example/oparl/file/{name}",
        body=body,
        name=name,
        file_name=name,
        mime_type="application/pdf",
        download_url=f"https://ris.fremd.example/files/{name}",
        **felder,
    )


def _zaehler() -> dict[tuple[str, str], int]:
    return {(z.outcome, z.age_class): z.count for z in OParlFileAccessDay.objects.all()}


def _quelle_liefert(monkeypatch: Any, status: int = 200, inhalt: bytes = b"%PDF-1.4 live") -> None:
    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=inhalt, headers={"content-type": "application/pdf"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr("insight_core.services.safe_fetch._resolve", lambda host: ["93.184.215.14"])


class TestGemesseneGroesse:
    def test_ablegen_setzt_die_gemessene_groesse(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body)
        file_cache.store_bytes(datei, b"x" * 1234, content_type="application/pdf")
        datei.refresh_from_db()
        assert datei.local_size == 1234

    def test_statistik_zaehlt_die_kopie_auch_ohne_angabe_der_quelle(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body)
        file_cache.store_bytes(datei, b"x" * 2048, content_type="application/pdf")
        # Der Abgleich hat die Angabe der Quelle (leer) übernommen
        OParlFile.objects.filter(pk=datei.pk).update(size=None)
        stats = file_cache.cache_stats()
        assert stats["cached_bytes"] == 2048
        assert stats["per_body"][0]["cached_bytes"] == 2048
        assert stats["without_size"] == 0

    def test_groessen_nachtragen(self, body: OParlBody, tmp_path: Path) -> None:
        vorhanden = tmp_path / "a.pdf"
        vorhanden.write_bytes(b"y" * 777)
        mit_kopie = _datei(body, "a.pdf", local_path=str(vorhanden), local_status="ok")
        ohne_kopie = _datei(body, "b.pdf", local_path=str(tmp_path / "fehlt.pdf"), local_status="ok")
        assert file_cache.cache_stats()["without_size"] == 2

        call_command("cache_files", "--sizes")
        mit_kopie.refresh_from_db()
        ohne_kopie.refresh_from_db()
        assert mit_kopie.local_size == 777
        assert ohne_kopie.local_size is None
        # Wiederholbar, ohne etwas zu verändern
        assert file_cache.backfill_sizes() == {"missing": 1}


class TestZugriffsprotokoll:
    def test_treffer_zaehlen_je_tag_und_altersklasse(self, body: OParlBody, tmp_path: Path) -> None:
        pfad = tmp_path / "a.pdf"
        pfad.write_bytes(b"%PDF-1.4 test")
        datei = _datei(
            body, local_path=str(pfad), local_status="ok", local_size=13, file_date=timezone.now() - timedelta(days=3)
        )
        client = Client()
        for _ in range(2):
            assert client.get(f"/insight/dokumente/{datei.id}/preview/").status_code == 200
        zeile = OParlFileAccessDay.objects.get()
        assert (zeile.outcome, zeile.age_class, zeile.count, zeile.bytes) == ("hit", "d30", 2, 26)
        assert zeile.body_id == body.pk
        assert zeile.day == timezone.localdate()

    def test_abruf_bei_der_quelle_und_fehler(self, body: OParlBody, ablage: Path, monkeypatch: Any) -> None:
        alt = _datei(body, "alt.pdf", file_date=timezone.now() - timedelta(days=800))
        _quelle_liefert(monkeypatch)
        assert Client().get(f"/insight/dokumente/{alt.id}/preview/")["X-Mandari-Cache"] == "miss"

        weg = _datei(body, "weg.pdf")
        _quelle_liefert(monkeypatch, status=404)
        Client().get(f"/insight/dokumente/{weg.id}/preview/")
        assert _zaehler() == {("miss", "y3"): 1, ("failed", "d30"): 1}

    def test_zurueckgenommenes_zaehlt_als_gesperrt(self, body: OParlBody) -> None:
        datei = OParlFile.objects.create(
            external_id="https://mandari.example/session/stadt/api/oparl/file/1",
            body=body,
            name="Anlage",
            deleted=True,
        )
        assert datei.withdrawn_by_publisher
        Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert list(OParlFileAccessDay.objects.values_list("outcome", flat=True)) == ["blocked"]

    def test_fehler_beim_zaehlen_verhindert_die_auslieferung_nicht(
        self, body: OParlBody, tmp_path: Path, monkeypatch: Any
    ) -> None:
        pfad = tmp_path / "a.pdf"
        pfad.write_bytes(b"%PDF-1.4 test")
        datei = _datei(body, local_path=str(pfad), local_status="ok")

        def kaputt(*args: Any, **kwargs: Any) -> Any:
            raise DatabaseError("Tabelle fehlt")

        monkeypatch.setattr(OParlFileAccessDay.objects, "filter", kaputt)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        assert response["X-Mandari-Cache"] == "hit"

    def test_zusammenfassung_mit_trefferquote(self, body: OParlBody) -> None:
        heute = timezone.localdate()
        OParlFileAccessDay.objects.create(day=heute, body=body, outcome="hit", age_class="d30", count=9, bytes=900)
        OParlFileAccessDay.objects.create(day=heute, body=body, outcome="miss", age_class="older", count=1, bytes=5)
        OParlFileAccessDay.objects.create(
            day=heute - timedelta(days=40), body=body, outcome="miss", age_class="older", count=50
        )
        summary = file_access.summary(30)
        assert summary["hit_rate"] == 90.0
        assert summary["by_outcome"]["hit"] == {"count": 9, "bytes": 900}
        assert summary["by_age"] == {"d30": 9, "older": 1}

    @pytest.mark.parametrize(
        ("tage", "klasse"), [(0, "d30"), (29, "d30"), (31, "d365"), (364, "d365"), (400, "y3"), (1200, "older")]
    )
    def test_altersklassen(self, tage: int, klasse: str) -> None:
        jetzt = timezone.now()
        datei = OParlFile(file_date=jetzt - timedelta(days=tage))
        assert file_access.age_class(datei, now=jetzt) == klasse

    def test_ohne_datum(self) -> None:
        assert file_access.age_class(OParlFile(), now=timezone.now()) == "unknown"
