# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentkette, Teil A (Issue #919): Vorschau, Löschabgleich, Auftrag und Bestand nutzen den einen Weg zur Quelle
(``hub.ris.abruf``) und dieselben Zustände des Abrufs.
"""

from __future__ import annotations

import importlib
import shutil
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from django.utils import timezone

from hub.ris import abruf
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import file_cache, file_reconcile, file_store, safe_fetch, text_extraction_job

PDF = b"%PDF-1.4 Vorlage " + b"y" * 64
WORKER = override_settings(TEXT_EXTRACTION_RUNNER="worker")
MIGRATION = importlib.import_module("insight_core.migrations.0059_dokumentkette_abruf")
VORHER = ("insight_core", "0058_chatnutzung_verbrauch")
NACHHER = ("insight_core", "0059_dokumentkette_abruf")


@pytest.fixture(autouse=True)
def _ablage(tmp_path: Path, settings: Any) -> Any:
    settings.OPARL_FILES_ROOT = str(tmp_path / "ablage")
    settings.FILE_CACHE_MIN_FREE_GB = 0
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def quelle(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Quell-RIS auf Transportebene; ``antwort``: Response, Ausnahme oder ``None`` (ein kleines PDF)."""
    zustand: dict[str, Any] = {"abrufe": [], "antwort": None}

    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        zustand["abrufe"].append(str(request.url))
        antwort = zustand["antwort"]
        if antwort is None:
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        if isinstance(antwort, Exception):
            raise antwort
        return cast(httpx.Response, antwort)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
    return zustand


def _body(*, gelistet: bool = True, sync_config: dict[str, Any] | None = None) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {uuid.uuid4().hex[:6]}",
        url=f"https://ris.example.org/{uuid.uuid4().hex[:6]}/system",
        sync_config=sync_config or {},
    )
    return OParlBody.objects.create(
        source=source, external_id=f"https://ris.example.org/bodies/{uuid.uuid4()}", name="Beispiel", is_listed=gelistet
    )


def _datei(body: OParlBody, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        name="Vorlage",
        file_name="vorlage.pdf",
        mime_type="application/pdf",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.pdf",
        **felder,
    )


def _vorschau(datei: OParlFile) -> Any:
    return Client().get(f"/insight/dokumente/{datei.id}/preview/")


# =============================================================================
# Vorschau
# =============================================================================


@pytest.mark.django_db
def test_vorschau_waehrend_eines_abrufs_wird_geladen(quelle: dict[str, Any]) -> None:
    datei = _datei(_body())
    assert abruf.claim(datei.pk) is not None
    response = _vorschau(datei)
    assert response.status_code == 503
    assert response["Retry-After"] == "15"
    assert "wird geladen" in response.content.decode()
    assert quelle["abrufe"] == [], "kein paralleler Abruf bei der Quelle"


@pytest.mark.django_db
def test_vorschau_404_einer_neuen_datei_wird_wiederholt_nicht_fehlend(quelle: dict[str, Any]) -> None:
    datei = _datei(_body())
    quelle["antwort"] = httpx.Response(404)
    response = _vorschau(datei)
    assert "Datei nicht gefunden" in response.content.decode()
    datei.refresh_from_db()
    assert (datei.local_status, datei.fetch_error, datei.fetch_attempts) == ("retry", "nicht_gefunden", 1)

    # Weitere Aufrufe zählen nicht als weitere Fehlschläge, solange die Wiederholung nicht fällig ist
    _vorschau(datei)
    datei.refresh_from_db()
    assert datei.fetch_attempts == 1


@pytest.mark.django_db
def test_vorschau_vermerkt_die_robots_txt_wie_der_abruf(
    quelle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from insight_core.services import robots

    gesperrt = _datei(_body())
    monkeypatch.setattr(robots, "_fetch", lambda url, *a, **k: (200, b"User-agent: *\nDisallow: /dokumente/\n"))
    _vorschau(gesperrt)
    gesperrt.refresh_from_db()
    assert (gesperrt.local_status, gesperrt.fetch_error) == ("refused", "robots")

    cache.clear()
    stoerung = _datei(_body())
    monkeypatch.setattr(robots, "_fetch", lambda url, *a, **k: (503, b""))
    assert _vorschau(stoerung).status_code == 503
    stoerung.refresh_from_db()
    assert (stoerung.local_status, stoerung.fetch_error) == ("retry", "robots_unerreichbar")
    assert quelle["abrufe"] == []


@pytest.mark.django_db
def test_vorschau_laedt_nicht_in_der_anfrage_hoch(quelle: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Besucher wartet nicht auf den Objektspeicher; den Upload holt der Zeitplan nach."""

    def kein_upload(_sha256: str) -> bool:
        raise AssertionError("kein Upload in der Web-Anfrage")

    monkeypatch.setattr(file_store, "upload_now", kein_upload)
    datei = _datei(_body())
    response = _vorschau(datei)
    assert response.status_code == 200 and response["X-Mandari-Cache"] == "miss"
    datei.refresh_from_db()
    assert datei.local_status == "ok"


@pytest.mark.django_db
def test_vorschau_legt_nach_der_regel_der_ablage_ab(quelle: dict[str, Any]) -> None:
    pilot = _datei(_body(gelistet=False, sync_config={"document_since": timezone.now().isoformat()}))
    pilot.created_at = timezone.now() + timedelta(seconds=1)
    OParlFile.objects.filter(pk=pilot.pk).update(created_at=pilot.created_at)

    response = _vorschau(pilot)
    assert response.status_code == 200 and response["X-Mandari-Cache"] == "miss"
    pilot.refresh_from_db()
    assert pilot.local_status == "none", "ohne TEXT_EXTRACTION_RUNNER=worker nur gelistete Kommunen"

    with WORKER:
        response = _vorschau(pilot)
        assert b"".join(response.streaming_content) == PDF
    pilot.refresh_from_db()
    assert pilot.local_status == "ok", "Ablage für alle Quellen mit erlaubtem Abruf ab dem Stichtag"


@pytest.mark.django_db
def test_vorschau_holt_eine_verlorene_kopie_wieder(quelle: dict[str, Any]) -> None:
    datei = _datei(_body())
    pfad = file_cache.store_bytes(datei, PDF)
    pfad.unlink()
    datei.refresh_from_db()
    assert file_store.local_copy(datei).missing

    response = _vorschau(datei)
    assert response.status_code == 200 and response["X-Mandari-Cache"] == "miss"
    datei.refresh_from_db()
    assert datei.local_status == "ok" and pfad.is_file()


@pytest.mark.django_db
def test_nicht_eingehaengte_ablage_ist_gestoert_nicht_fehlend(quelle: dict[str, Any]) -> None:
    """Leerer Einhängepunkt (Ablage nicht eingehängt): kein Fehlen, keine Abrufwelle bei den Quellen."""
    datei = _datei(_body())
    file_cache.store_bytes(datei, PDF)
    datei.refresh_from_db()
    shutil.rmtree(file_store.blob_root())

    assert file_store.local_copy(datei).disturbed
    response = _vorschau(datei)
    assert response.status_code == 503
    assert quelle["abrufe"] == []
    datei.refresh_from_db()
    assert datei.local_status == "ok"


# =============================================================================
# Löschabgleich
# =============================================================================


@pytest.mark.django_db
def test_stichprobe_prueft_auch_nie_abgelegte_404_dateien() -> None:
    body = _body()
    fehlend = _datei(body, local_status="missing")
    assert fehlend in file_reconcile.sample_queryset(body)


@pytest.mark.django_db
def test_abgleich_legt_einen_wieder_gelieferten_inhalt_ab(quelle: dict[str, Any]) -> None:
    datei = _datei(_body(), local_status="missing")
    with file_reconcile.fetch_client() as client:
        assert file_reconcile.verify(datei, client) == file_reconcile.UNCHANGED
    datei.refresh_from_db()
    assert datei.local_status == "ok" and Path(datei.local_path or "").read_bytes() == PDF
    assert len([url for url in quelle["abrufe"] if not url.endswith("/robots.txt")]) == 1, "ein Quellabruf"


@pytest.mark.django_db
def test_abgleich_ueberspringt_laufende_abrufe(quelle: dict[str, Any]) -> None:
    body = _body()
    datei = _datei(body, local_status="ok", text_extraction_status="completed")
    assert OParlFile.objects.filter(pk=datei.pk).update(
        local_status="fetching", fetch_next_at=timezone.now() + timedelta(minutes=5)
    )
    datei.refresh_from_db()
    ergebnis = file_reconcile.sample_heads(body, interval=0)
    assert ergebnis.get("skipped") == 1
    assert quelle["abrufe"] == []


@pytest.mark.django_db
def test_abgleich_ruft_gesperrte_dateien_ausdruecklich_ab(quelle: dict[str, Any]) -> None:
    datei = _datei(_body(), source_missing_since=timezone.now() - timedelta(days=2))
    assert abruf.is_excluded(datei)
    with file_reconcile.fetch_client() as client:
        file_reconcile.verify(datei, client)
    datei.refresh_from_db()
    assert datei.source_missing_since is None, "liefert die Quelle wieder, wird entsperrt"


# =============================================================================
# Auftrag file.extract_text (Übergang bis Teil B)
# =============================================================================


@pytest.mark.django_db
def test_auftrag_ruft_bei_gestoerter_ablage_nicht_bei_der_quelle_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    datei = _datei(_body(), text_extraction_status="pending")
    monkeypatch.setattr(file_cache, "fetch_and_cache", lambda *a, **k: "storage_error")

    def kein_download(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("kein Abruf bei der Quelle")

    monkeypatch.setattr(abruf, "download_to_file", kein_download)
    with WORKER:
        assert text_extraction_job.extract_file(str(datei.pk)) == text_extraction_job.ZURUECKGESTELLT
    datei.refresh_from_db()
    assert datei.text_extraction_status == "pending"


# =============================================================================
# Bestand: Statistik, Aufräumen, Freigabe nach robots_override
# =============================================================================


@pytest.mark.django_db
def test_statistik_kennt_die_neuen_zustaende() -> None:
    body = _body()
    for zustand in ("retry", "refused", "fetching", "none"):
        _datei(body, local_status=zustand)
    _datei(_body(gelistet=False))
    stats = file_cache.cache_stats()
    assert (stats["retry"], stats["refused"], stats["fetching"], stats["pending"]) == (1, 1, 1, 1)
    assert stats["not_stored"] == 1
    out = StringIO()
    call_command("cache_files", stats=True, stdout=out)
    assert "verweigert=1" in out.getvalue()


@pytest.mark.django_db
def test_aufraeumen_laesst_ablegende_ausgeblendete_kommunen_stehen() -> None:
    pilot = _body(gelistet=False, sync_config={"document_since": timezone.now().isoformat()})
    datei = _datei(pilot)
    file_cache.store_bytes(datei, PDF)
    with WORKER:
        call_command("prune_file_cache", unlisted=True, stdout=StringIO())
    datei.refresh_from_db()
    assert datei.local_status == "ok"
    call_command("prune_file_cache", unlisted=True, stdout=StringIO())
    datei.refresh_from_db()
    assert datei.local_status == "none", "ohne Ablage für alle wie bisher"


# =============================================================================
# Datenmigration (insight_core 0059)
# =============================================================================


@pytest.mark.django_db(transaction=True)
def test_datenmigration_verweigert_und_zurueck() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        historisch = MigrationExecutor(connection).loader.project_state([VORHER]).apps
        quelle_alt = historisch.get_model("insight_core", "OParlSource").objects.create(
            name="Alt", url="https://ris.example.org/alt/system"
        )
        body_alt = historisch.get_model("insight_core", "OParlBody").objects.create(
            source=quelle_alt, external_id="https://ris.example.org/alt/body", name="Alt"
        )
        datei_alt = historisch.get_model("insight_core", "OParlFile")

        def anlegen(status: str, fehler: str) -> Any:
            return datei_alt.objects.create(
                body=body_alt,
                external_id=f"https://ris.example.org/alt/{uuid.uuid4()}",
                local_status=status,
                local_error=fehler,
            ).pk

        robots_pk = anlegen("error", "robots.txt sperrt /dokumente/ (Disallow: /dokumente/)")
        html_pk = anlegen("error", MIGRATION.HTML_TEXT)
        sonst_pk = anlegen("error", "HTTP 403")
        ok_pk = anlegen("ok", "")
        # Von der Obergrenze verdrängt (0056, #961): bleibt in beide Richtungen verdrängt
        verdraengt_pk = anlegen("evicted", "")

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])
        stand = dict(OParlFile.objects.values_list("pk", "local_status"))
        assert stand[robots_pk] == stand[html_pk] == "refused"
        assert (stand[sonst_pk], stand[ok_pk], stand[verdraengt_pk]) == ("error", "ok", "evicted")
        assert OParlFile.objects.get(pk=robots_pk).fetch_error == "robots"
        assert OParlFile.objects.get(pk=html_pk).local_error == MIGRATION.HTML_TEXT, "Fehlertext bleibt"

        executor = MigrationExecutor(connection)
        executor.migrate([VORHER])
        zurueck = dict(
            MigrationExecutor(connection)
            .loader.project_state([VORHER])
            .apps.get_model("insight_core", "OParlFile")
            .objects.values_list("pk", "local_status")
        )
        assert zurueck[robots_pk] == zurueck[html_pk] == zurueck[sonst_pk] == "error"
        assert zurueck[verdraengt_pk] == "evicted"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
