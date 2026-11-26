# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abruf auf Anforderung (``sync_config["file_downloads"] = "bei_bedarf"``).

Nichts wird vorab geladen. Öffnet eine Person ein Dokument, holt die Vorschau es bei der Quelle, liefert es aus und
legt es ab; weitere Aufrufe kommen aus der Kopie. Den Abruf löst nur eine Person aus: angemeldet oder über den Knopf
„Dokument laden“ (POST mit CSRF-Token). Crawler sehen nur die Seite mit dem Knopf. Die robots.txt gilt für diese
Dateien nicht, weil jeder Abruf auf eine Person zurückgeht.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.test import Client
from mandari_oparl.robots import KIND_API, KIND_FILES

from apps.common.tests.factories import UserFactory
from hub.ris import abruf
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, robots, safe_fetch

pytestmark = pytest.mark.django_db

ORIGINAL = "https://ris.bedarf.example/bi/getfile.asp?id=7&type=do"
BEI_BEDARF = {"file_downloads": "bei_bedarf"}


def _datei(sync_config: dict[str, Any] | None = None, *, listed: bool = False, aktiv: bool = True) -> OParlFile:
    source = OParlSource.objects.create(
        name="Zweckverband", url="https://ris.bedarf.example/bi/", sync_config=sync_config or dict(BEI_BEDARF)
    )
    source.is_active = aktiv
    source.save(update_fields=["is_active"])
    body = OParlBody.objects.create(
        source=source, external_id="https://ris.bedarf.example/bi/body", name="Zweckverband", is_listed=listed
    )
    return OParlFile.objects.create(
        body=body,
        external_id="https://ris.bedarf.example/bi/file/7",
        name="Vorlage",
        file_name="vorlage.pdf",
        mime_type="application/pdf",
        download_url=ORIGINAL,
    )


@pytest.fixture
def quelle(monkeypatch: Any, tmp_path: Path) -> dict[str, Any]:
    """Quell-RIS auf Transportebene: robots.txt sperrt alles, Dateien kommen als kleines PDF."""
    zustand: dict[str, Any] = {"abrufe": []}

    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        zustand["abrufe"].append(str(request.url))
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n", headers={"content-type": "text/plain"})
        return httpx.Response(200, content=b"%PDF-1.4 bei bedarf", headers={"content-type": "application/pdf"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
    return zustand


def _dateiabrufe(zustand: dict[str, Any]) -> list[str]:
    return [url for url in zustand["abrufe"] if not url.endswith("/robots.txt")]


def _adresse(datei: OParlFile) -> str:
    return f"/insight/dokumente/{datei.id}/preview/"


def test_schalter() -> None:
    datei = _datei()
    assert file_cache.downloads_on_demand(datei.body) is True
    # Im Hintergrund gilt die Quelle als abgeschaltet: Cache, Texterkennung, Löschabgleich, Personenfotos
    assert file_cache.downloads_disabled(datei.body) is True
    assert file_cache.downloads_on_demand_in({"file_downloads": False}) is False
    assert file_cache.downloads_on_demand_in({"file_downloads": True}) is False
    assert file_cache.downloads_on_demand_in(None) is False


def test_abgeschaltete_quelle_holt_nichts() -> None:
    assert file_cache.downloads_on_demand(_datei(aktiv=False).body) is False


def test_kein_abruf_im_hintergrund() -> None:
    datei = _datei(listed=True)
    assert abruf.fetch_allowed(datei.body) is False
    assert abruf.stores_file(datei) is False
    assert not OParlFile.objects.filter(abruf.storable_q()).filter(pk=datei.pk).exists()
    assert cast(Any, datei.body).source_id in file_cache.sources_without_downloads()


def test_robots_gilt_nur_fuer_dateien_nicht() -> None:
    entscheidung = robots.check(ORIGINAL, KIND_FILES, sync_config=BEI_BEDARF)
    assert entscheidung.allowed is True
    assert "bei_bedarf" in entscheidung.rule
    assert cast(Any, robots.cached_check(ORIGINAL, KIND_FILES, sync_config=BEI_BEDARF)).allowed is True
    # Die Schnittstelle selbst bleibt bei den Regeln der robots.txt (keine Ausnahme ohne Vermerk)
    assert robots._on_demand_decision(KIND_API, BEI_BEDARF) is None


def test_besucher_sieht_erst_den_knopf(quelle: dict[str, Any]) -> None:
    datei = _datei()
    response = Client().get(_adresse(datei))
    html = response.content.decode()
    assert response.status_code == 200
    assert "Dokument laden" in html
    assert '<form method="post"' in html
    assert 'name="csrfmiddlewaretoken"' in html
    # Link auf das Original bleibt
    assert "Dokument im Ratsinformationssystem öffnen" in html
    assert _dateiabrufe(quelle) == []


def test_knopf_holt_legt_ab_und_liefert(quelle: dict[str, Any]) -> None:
    datei = _datei()
    client = Client(enforce_csrf_checks=True)
    seite = client.get(_adresse(datei))
    token = seite.cookies["csrftoken"].value
    response = client.post(_adresse(datei), {"csrfmiddlewaretoken": token})
    assert response.status_code == 200
    assert b"".join(cast(Any, response).streaming_content) == b"%PDF-1.4 bei bedarf"
    assert _dateiabrufe(quelle) == [ORIGINAL]
    datei.refresh_from_db()
    assert datei.local_status == "ok"
    # Der nächste Aufruf kommt aus der Kopie, ohne Knopf und ohne Anfrage bei der Quelle
    wieder = Client().get(_adresse(datei))
    assert wieder.status_code == 200
    assert wieder["X-Mandari-Cache"] == "hit"
    assert _dateiabrufe(quelle) == [ORIGINAL]


def test_post_ohne_csrf_token_holt_nichts(quelle: dict[str, Any]) -> None:
    datei = _datei()
    response = Client(enforce_csrf_checks=True).post(_adresse(datei))
    assert response.status_code == 403
    assert _dateiabrufe(quelle) == []


def test_angemeldete_person_bekommt_das_dokument_direkt(quelle: dict[str, Any]) -> None:
    datei = _datei()
    client = Client()
    client.force_login(cast(Any, UserFactory)())
    response = client.get(_adresse(datei))
    assert response.status_code == 200
    assert response["X-Mandari-Cache"] == "miss"
    assert _dateiabrufe(quelle) == [ORIGINAL]


def test_ohne_bei_bedarf_bleibt_es_beim_original(quelle: dict[str, Any]) -> None:
    datei = _datei({"file_downloads": False})
    client = Client()
    client.force_login(cast(Any, UserFactory)())
    html = client.get(_adresse(datei)).content.decode()
    assert "Dieses Dokument rufen wir nicht selbst ab" in html
    assert _dateiabrufe(quelle) == []


def test_head_holt_nichts(quelle: dict[str, Any]) -> None:
    # Linkprüfer schicken HEAD: kein Live-Abruf bei der Quelle (wie bisher 405)
    datei = _datei({"file_downloads": True})
    assert Client().head(_adresse(datei)).status_code == 405
    assert _dateiabrufe(quelle) == []


def test_geschonte_quelle_verspricht_kein_nachholen(quelle: dict[str, Any]) -> None:
    datei = _datei()
    source = cast(Any, datei.body).source
    source.consecutive_failures = 99
    source.save(update_fields=["consecutive_failures"])
    client = Client()
    client.force_login(cast(Any, UserFactory)())
    html = client.get(_adresse(datei)).content.decode()
    assert "Bitte versuche es später noch einmal." in html
    assert "automatisch nach" not in html
    assert _dateiabrufe(quelle) == []


def test_zusammenfassung_wartet_nur_auf_geoeffnete_anlagen(settings: Any, monkeypatch: Any) -> None:
    from insight_ai.services import summarizer
    from insight_core.models import OParlPaper

    datei = _datei()
    paper = OParlPaper.objects.create(
        external_id="https://ris.bedarf.example/bi/paper/1", body=cast(Any, datei.body), name="V"
    )
    datei.paper = paper
    datei.text_extraction_status = "pending"
    datei.save(update_fields=["paper", "text_extraction_status"])
    dienst = summarizer.SummaryService.__new__(summarizer.SummaryService)
    monkeypatch.setattr(summarizer, "priority_extraction_enabled", lambda: False)
    # Ohne Worker erkennt niemand den Text dieser Anlagen: kein Dauerhinweis „Texterkennung steht aus“
    settings.TEXT_EXTRACTION_RUNNER = "ingestor"
    assert dienst._text_pending(paper) is False
    # Mit Worker zählen nur geöffnete (abgelegte) Anlagen
    settings.TEXT_EXTRACTION_RUNNER = "worker"
    assert dienst._text_pending(paper) is False
    datei.local_status = "ok"
    datei.save(update_fields=["local_status"])
    assert dienst._text_pending(paper) is True
