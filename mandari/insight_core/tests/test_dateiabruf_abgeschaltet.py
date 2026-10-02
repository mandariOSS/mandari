# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dateiabruf je Quelle abschaltbar (``sync_config["file_downloads"] = false``).

Manche Ratsinformationssysteme geben Dokumente nur nach einer Zugangsprüfung für Menschen heraus
(z. B. ALTCHA vor den Anlagen, während die OParl-Schnittstelle frei ist). Cache, Vorschau und
Textextraktion fragen solche Quellen nicht an; die Vorschau verweist auf das Original.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache

pytestmark = pytest.mark.django_db

GESPERRT = "https://ris.gesperrt.example/public/doc?DOCTYP=130&DOLFDNR=1&OTYP=41"


def _datei(sync_config: dict[str, Any], nummer: int = 1, **felder: Any) -> OParlFile:
    source = OParlSource.objects.create(
        name=f"Quelle {nummer}", url=f"https://ris{nummer}.example/oparl/system", sync_config=sync_config
    )
    body = OParlBody.objects.create(
        source=source, external_id=f"https://ris{nummer}.example/oparl/bodies/1", name=f"Stadt {nummer}"
    )
    daten: dict[str, Any] = {
        "body": body,
        "external_id": f"https://ris{nummer}.example/oparl/files/1",
        "name": "Vorlage",
        "file_name": "vorlage.pdf",
        "mime_type": "pdf",
        "access_url": GESPERRT,
    }
    daten.update(felder)
    return OParlFile.objects.create(**daten)


def _kein_abruf(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Quelle darf nicht angefragt werden: {request.url}")


def test_schalter_nur_bei_ausdruecklichem_false() -> None:
    assert file_cache.downloads_disabled(_datei({"file_downloads": False}).body) is True
    assert file_cache.downloads_disabled(_datei({"file_downloads": True}, 2).body) is False
    assert file_cache.downloads_disabled(_datei({}, 3).body) is False
    assert file_cache.downloads_disabled(_datei({"file_downloads": "false"}, 4).body) is False
    assert file_cache.downloads_disabled(None) is False


def test_cache_fragt_die_quelle_nicht_an(tmp_path: Path, monkeypatch: Any) -> None:
    datei = _datei({"file_downloads": False})
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(_kein_abruf))
    assert file_cache.fetch_and_cache(datei, client=client) == "paused"
    datei.refresh_from_db()
    assert datei.local_status == "none", "bleibt offen und wird nachgeholt, sobald der Schalter fällt"


def test_warteschlange_und_statistik(tmp_path: Path, monkeypatch: Any) -> None:
    gesperrt = _datei({"file_downloads": False})
    offen = _datei({}, 2)
    monkeypatch.setattr(file_cache, "disk_free_bytes", lambda: 10**15)
    ids = set(file_cache.pending_queryset().values_list("id", flat=True))
    assert offen.id in ids and gesperrt.id not in ids
    assert file_cache.cache_stats()["paused"] == 1


def test_vorschau_verweist_auf_das_original() -> None:
    datei = _datei({"file_downloads": False})
    response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "Zugangsprüfung" in html
    assert 'href="https://ris.gesperrt.example/public/doc?DOCTYP=130&amp;DOLFDNR=1&amp;OTYP=41"' in html
    assert 'rel="noopener noreferrer"' in html


def test_vorschau_verlinkt_nur_http_adressen() -> None:
    datei = _datei({"file_downloads": False}, access_url="javascript:alert(1)")
    html = Client().get(f"/insight/dokumente/{datei.id}/preview/").content.decode()
    assert "javascript:" not in html
    assert "direkt im Ratsinformationssystem" in html


def test_lokale_kopie_wird_weiter_ausgeliefert(tmp_path: Path) -> None:
    datei = _datei({"file_downloads": False})
    pfad = tmp_path / f"{datei.id}.pdf"
    pfad.write_bytes(b"%PDF-1.4 test")
    datei.local_path = str(pfad)
    datei.local_status = "ok"
    datei.save(update_fields=["local_path", "local_status"])
    response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
    assert response.status_code == 200
    assert response["X-Mandari-Cache"] == "hit"
