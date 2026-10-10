# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenführung der Dokumentkette (#919) mit der Obergrenze des Dokument-Caches (#961): die Schnittstellen.

- ``evicted`` ist kein Zustand, aus dem der Abruf (``hub.ris.abruf``) von selbst beansprucht; ausdrücklich holen
  verdrängte Dokumente nur die Vorschau, ``cache_files --verdraengte`` und die Admin-Aktion von Hand.
- Der Live-Abruf eines verdrängten Dokuments setzt dieselben Zustände wie bei ``none`` (Tabelle der Dokumentkette).
- Ein bestätigtes Fehlen der Kopie setzt ``evicted`` nie auf ``none``; der Löschabgleich holt einen unveränderten
  Inhalt nicht zurück, und Befehle lassen verdrängte Dokumente stehen.
- Mit Objektspeicher verlässt nur ein sicher vorhandener Inhalt die Platte; eine Störung verdrängt nichts.
- Geschützt sind Dateien in der Texterkennung (auch eben beansprucht oder eingereiht), laufende Abrufe sowie
  verweigernde und nicht aktive Quellen.
- ``cache_files`` lädt über den einen Abrufweg nur bis zur Grenze, auch bei der Ablage für alle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.events.models import Task
from hub.ris import abruf
from insight_core.models import OParlBody, OParlFile, OParlFileBlob, OParlSource
from insight_core.services import (
    file_cache,
    file_cache_limit,
    file_reconcile,
    file_store,
    object_storage,
    safe_fetch,
    text_extraction_job,
)

pytestmark = pytest.mark.django_db

GROESSE = 1000
ENDPUNKT = "http://objektspeicher.test"
BUCKET = "mandari-test"


@pytest.fixture(autouse=True)
def ablage(tmp_path: Path, settings: Any) -> Iterator[Path]:
    root = tmp_path / "ablage"
    root.mkdir()
    settings.OPARL_FILES_ROOT = root
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.OBJ_ENABLED = False
    settings.FILE_CACHE_MAX_TOTAL_GB = 0
    settings.FILE_CACHE_EVICT_TARGET_PERCENT = 90
    cache.clear()
    yield root
    cache.clear()


def _inhalt(name: str) -> bytes:
    """Eindeutiger Inhalt fester Größe."""
    kopf = f"%PDF-1.4 {name} ".encode()
    return kopf + b"x" * (GROESSE - len(kopf))


@pytest.fixture
def quelle(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Quell-RIS auf Transportebene: robots.txt fehlt (alles erlaubt), sonst je Adresse ein eindeutiger Inhalt
    (``inhalte``) bzw. ``antwort`` (Response oder Ausnahme) für alle.
    """
    zustand: dict[str, Any] = {"abrufe": [], "antwort": None, "inhalte": {}}

    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        zustand["abrufe"].append(str(request.url))
        antwort = zustand["antwort"]
        if isinstance(antwort, Exception):
            raise antwort
        if antwort is not None:
            return antwort  # type: ignore[no-any-return]
        inhalt = zustand["inhalte"].get(str(request.url)) or _inhalt(request.url.path)
        return httpx.Response(200, content=inhalt, headers={"content-type": "application/pdf"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
    return zustand


def _body(*, gelistet: bool = True, sync_config: dict[str, Any] | None = None) -> OParlBody:
    kennung = uuid.uuid4().hex[:8]
    source = OParlSource.objects.create(
        name=f"Quelle {kennung}", url=f"https://ris.example.org/{kennung}/system", sync_config=sync_config or {}
    )
    return OParlBody.objects.create(
        source=source, external_id=f"https://ris.example.org/{kennung}/body", name="Beispielstadt", is_listed=gelistet
    )


@pytest.fixture
def body() -> OParlBody:
    return _body()


def _datei(body: OParlBody, name: str, *, alter_tage: int, **felder: Any) -> OParlFile:
    werte: dict[str, Any] = {
        "external_id": f"https://ris.example.org/files/{name}-{uuid.uuid4().hex[:8]}",
        "body": body,
        "name": name,
        "file_name": f"{name}.pdf",
        "mime_type": "application/pdf",
        "download_url": f"https://ris.example.org/dokumente/{name}-{uuid.uuid4().hex[:8]}.pdf",
        "file_date": timezone.now() - timedelta(days=alter_tage),
        "text_content": f"Text von {name}",
        "text_extraction_status": "completed",
    }
    werte.update(felder)
    return OParlFile.objects.create(**werte)


def _abgelegt(body: OParlBody, name: str, *, alter_tage: int, inhalt: bytes | None = None, **felder: Any) -> OParlFile:
    """Datei ablegen; die Kopie ist älter als die Schonfrist für frische Kopien."""
    datei = _datei(body, name, alter_tage=alter_tage, **felder)
    file_cache.store_bytes(datei, inhalt if inhalt is not None else _inhalt(name))
    OParlFile.objects.filter(pk=datei.pk).update(local_cached_at=timezone.now() - timedelta(days=2))
    datei.refresh_from_db()
    return datei


def _verdraengt(body: OParlBody, name: str, **felder: Any) -> OParlFile:
    """Abgelegt und von der Obergrenze verdrängt (ohne Objektspeicher: ``evicted``)."""
    datei = _abgelegt(body, name, alter_tage=3000, **felder)
    file_cache_limit.enforce(max_bytes=1)
    datei.refresh_from_db()
    assert (datei.local_status, datei.blob_id) == ("evicted", None)
    return datei


def _status(*dateien: OParlFile) -> list[str]:
    return [OParlFile.objects.get(pk=datei.pk).local_status for datei in dateien]


def _pending_ids(**kwargs: Any) -> set[Any]:
    return set(abruf.pending_queryset(**kwargs).values_list("pk", flat=True))


# =============================================================================
# evicted ist kein Zustand, aus dem der Abruf von selbst beansprucht
# =============================================================================


class TestVerdraengtNurAusdruecklich:
    def test_abruf_beansprucht_verdraengte_nicht(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        datei = _verdraengt(body, "a")

        assert abruf.claim(datei.pk) is None
        assert abruf.abrufen(datei) == abruf.SKIPPED
        assert datei.pk not in _pending_ids()
        assert abruf.counts()["queued"] == 0, "Kennzahl der wartenden Abrufe zählt Verdrängte nicht"
        assert not file_cache.cache_pending(limit=10, sleep=0), "cache_files lädt Verdrängte nicht von selbst"
        assert quelle["abrufe"] == []
        assert _status(datei) == ["evicted"]

    def test_cache_files_verdraengte_und_admin_aktion_holen_sie(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        a = _verdraengt(body, "a")
        b = _verdraengt(_body(), "b")
        quelle["inhalte"][str(a.download_url)] = _inhalt("a")
        assert {a.pk, b.pk} <= _pending_ids(retry_evicted=True)

        ergebnis = file_cache.cache_pending(body, limit=10, retry_evicted=True, sleep=0)
        assert ergebnis == {"ok": 1}
        # Von Hand (Admin-Aktion „Lokal zwischenspeichern“) wie jeder andere Zustand außer ok
        b.refresh_from_db()
        assert abruf.abrufen(b, force=True) == abruf.OK
        for datei in (a, b):
            datei.refresh_from_db()
            assert datei.local_status == "ok" and datei.blob_id
            assert (datei.text_content, datei.text_extraction_status) == (f"Text von {datei.name}", "completed")
        assert len(quelle["abrufe"]) == 2

    def test_cache_files_befehl_verdraengte(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        datei = _verdraengt(body, "a")
        call_command("cache_files", "--trotz-zeitplan", "--sleep", "0", stdout=StringIO())
        assert _status(datei) == ["evicted"]
        out = StringIO()
        call_command("cache_files", "--verdraengte", "--trotz-zeitplan", "--sleep", "0", stdout=out)
        assert _status(datei) == ["ok"]
        assert "verdrängt=0" in out.getvalue()


# =============================================================================
# Live-Abruf der Vorschau: Tabelle wie bei none
# =============================================================================


class TestVorschauVerdraengt:
    @pytest.mark.parametrize(
        ("erfasst_vor_tagen", "antwort", "zustand", "code"),
        [
            (0, httpx.Response(404), "retry", "nicht_gefunden"),
            (30, httpx.Response(404), "missing", "nicht_gefunden"),
            (30, httpx.Response(503), "retry", "http_5xx"),
            (30, httpx.ReadTimeout("zu langsam"), "retry", "timeout"),
        ],
    )
    def test_fehlschlag_wie_bei_none(
        self,
        body: OParlBody,
        quelle: dict[str, Any],
        erfasst_vor_tagen: int,
        antwort: Any,
        zustand: str,
        code: str,
    ) -> None:
        datei = _verdraengt(body, "a")
        OParlFile.objects.filter(pk=datei.pk).update(created_at=timezone.now() - timedelta(days=erfasst_vor_tagen))
        vergleich = _datei(body, "vergleich", alter_tage=1, local_status="none")
        OParlFile.objects.filter(pk=vergleich.pk).update(created_at=timezone.now() - timedelta(days=erfasst_vor_tagen))
        quelle["antwort"] = antwort

        for eintrag in (datei, vergleich):
            Client().get(f"/insight/dokumente/{eintrag.id}/preview/")
        for eintrag in (datei, vergleich):
            eintrag.refresh_from_db()
            assert (eintrag.local_status, eintrag.fetch_error, eintrag.fetch_attempts) == (zustand, code, 1)
        assert datei.text_content == "Text von a", "der Text bleibt"

    def test_write_through_ohne_beanspruchung(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        datei = _verdraengt(body, "a")
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200 and response["X-Mandari-Cache"] == "miss"
        datei.refresh_from_db()
        assert datei.local_status == "ok" and datei.fetch_attempts == 0
        assert datei.local_accessed_at is not None, "die Nutzung zählt für die Rangfolge"


# =============================================================================
# Ein bestätigtes Fehlen setzt evicted nie auf none
# =============================================================================


class TestVerdraengtBleibtVerdraengt:
    def test_bestaetigtes_fehlen(self, body: OParlBody) -> None:
        datei = _verdraengt(body, "a")
        assert file_store.local_copy(datei).missing
        assert not abruf.mark_content_missing(datei)
        assert _status(datei) == ["evicted"]

    def test_loeschabgleich_holt_unveraenderten_inhalt_nicht_zurueck(
        self, body: OParlBody, quelle: dict[str, Any]
    ) -> None:
        datei = _verdraengt(body, "a", inhalt=_inhalt("a"))
        quelle["inhalte"][str(datei.download_url)] = _inhalt("a")
        with abruf.fetch_client() as client:
            assert file_reconcile.verify(datei, client) == file_reconcile.UNCHANGED
        datei.refresh_from_db()
        assert (datei.local_status, datei.blob_id) == ("evicted", None)
        assert datei.text_content == "Text von a"

    def test_loeschabgleich_legt_geaenderten_inhalt_ab(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        # Der Text muss neu erkannt werden, und die Erkennung liest nur aus der Ablage
        datei = _verdraengt(body, "a", inhalt=_inhalt("a"))
        quelle["inhalte"][str(datei.download_url)] = _inhalt("a, neue Fassung")
        with abruf.fetch_client() as client:
            assert file_reconcile.verify(datei, client) == file_reconcile.CHANGED
        datei.refresh_from_db()
        assert datei.local_status == "ok" and datei.blob_id
        assert datei.text_extraction_status == "pending"

    def test_dokumentkette_zuruecksetzen_laesst_verdraengte(self, body: OParlBody) -> None:
        datei = _verdraengt(body, "a")
        call_command("dokumentkette", "zuruecksetzen", stdout=StringIO())
        assert _status(datei) == ["evicted"]

    def test_prune_unlisted_laesst_verdraengte(self, body: OParlBody) -> None:
        datei = _verdraengt(body, "a")
        OParlBody.objects.filter(pk=body.pk).update(is_listed=False)
        call_command("prune_file_cache", "--unlisted", stdout=StringIO())
        assert _status(datei) == ["evicted"], "als none lüde cache_files sie nach, sobald die Kommune wieder ablegt"


# =============================================================================
# Schutz beim Verdrängen
# =============================================================================


class TestSchutz:
    def test_laufender_abruf_haelt_den_inhalt(self, body: OParlBody) -> None:
        geteilt = _inhalt("geteilt")
        a = _abgelegt(body, "a", alter_tage=3000, inhalt=geteilt)
        b = _abgelegt(body, "b", alter_tage=3000, inhalt=geteilt)
        # b wird gerade neu abgerufen (etwa nach einem bestätigten Fehlen der Kopie)
        OParlFile.objects.filter(pk=b.pk).update(local_status="fetching", fetch_next_at=timezone.now() + timedelta(1))

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 0 and ergebnis.protected == {"abruf": GROESSE}
        assert _status(a, b) == ["ok", "fetching"]

    def test_laufender_abruf_unter_der_sperre(self, body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        apply = file_cache_limit._apply

        def abruf_beginnt(run: Any, items: Any) -> bool:
            OParlFile.objects.filter(pk=datei.pk).update(local_status="fetching")
            return apply(run, items)

        monkeypatch.setattr(file_cache_limit, "_apply", abruf_beginnt)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 0 and ergebnis.skipped == {"abruf": 1}
        assert _status(datei) == ["fetching"]

    def test_eingereihte_texterkennung(self, body: OParlBody) -> None:
        # Erledigter Text, aber ein Auftrag file.extract_text wartet (etwa eine Neuerkennung)
        datei = _abgelegt(body, "a", alter_tage=3000)
        Task.objects.create(
            queue=text_extraction_job.TASK_QUEUE,
            task_path=text_extraction_job.TASK_PATH,
            args={"args": [str(datei.pk)], "kwargs": {}},
        )
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 0 and ergebnis.skipped == {"texterkennung": 1}
        assert _status(datei) == ["ok"]

    def test_verweigerte_abrufe_schuetzen_die_quelle(self, body: OParlBody) -> None:
        geschuetzt = _abgelegt(body, "a", alter_tage=3000)
        _datei(body, "verweigert", alter_tage=1, local_status="refused", fetch_error="html")
        frei = _abgelegt(_body(), "frei", alter_tage=3000)

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.protected == {"nicht_abrufbar": GROESSE}
        assert _status(geschuetzt, frei) == ["ok", "evicted"]

    def test_nicht_aktive_quelle_ist_geschuetzt(self, body: OParlBody) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        OParlSource.objects.filter(pk=body.source_id).update(is_active=False)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.protected == {"nicht_abrufbar": GROESSE}
        assert _status(datei) == ["ok"]


# =============================================================================
# Mit Objektspeicher: nur sicher vorhandene Inhalte, eine Störung verdrängt nichts
# =============================================================================


@pytest.fixture
def objektspeicher(ablage: Path, settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPUNKT)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    settings.OBJ_ENABLED = True
    settings.OBJ_ENDPOINT = ENDPUNKT
    settings.OBJ_BUCKET = BUCKET
    settings.OBJ_KEY = "test"
    settings.OBJ_SECRET = "test"
    settings.OBJ_REGION = ""
    object_storage._client.cache_clear()
    with moto.mock_aws():
        object_storage.client().create_bucket(Bucket=BUCKET)
        yield object_storage.client()
    object_storage._client.cache_clear()


def _hochgeladen(body: OParlBody, name: str) -> OParlFile:
    datei = _abgelegt(body, name, alter_tage=3000)
    assert file_store.upload_pending() == {"uploaded": 1}
    return OParlFile.objects.get(pk=datei.pk)


class TestMitObjektspeicher:
    def test_stoerung_verdraengt_nicht(
        self, body: OParlBody, objektspeicher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datei = _hochgeladen(body, "a")
        sha256 = str(datei.blob_id)

        def gestoert(**kwargs: Any) -> Any:
            raise EndpointConnectionError(endpoint_url=ENDPUNKT)

        monkeypatch.setattr(objektspeicher, "head_object", gestoert)
        # Nicht vorhanden ist nicht gestört: exists meldet die Störung, remote_size lässt sie durch
        assert object_storage.exists(sha256) == object_storage.DISTURBED
        with pytest.raises(EndpointConnectionError):
            object_storage.remote_size(sha256)

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 0 and ergebnis.skipped == {"fehler": 1}
        assert Path(datei.local_path or "").exists()
        assert OParlFileBlob.objects.get(pk=sha256).remote_at is not None, "eine Störung ist kein Fehlen"
        assert _status(datei) == ["ok"]

    def test_bestaetigt_fehlend_bleibt_die_kopie(self, body: OParlBody, objektspeicher: Any) -> None:
        datei = _hochgeladen(body, "a")
        sha256 = str(datei.blob_id)
        objektspeicher.delete_object(Bucket=BUCKET, Key=object_storage.key_for(sha256))
        assert object_storage.exists(sha256) == object_storage.MISSING
        assert object_storage.remote_size(sha256) is None

        # Ohne --pruefe-objektspeicher: Der echte Lauf prüft immer
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 0 and ergebnis.verified == {"fehlt": 1}
        assert Path(datei.local_path or "").exists()
        assert OParlFileBlob.objects.get(pk=sha256).remote_at is None, "lädt --hochladen erneut hoch"

    def test_eben_beanspruchte_texterkennung_behaelt_die_kopie(
        self, body: OParlBody, objektspeicher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datei = _hochgeladen(body, "a")
        pruefen = file_cache_limit._remote_state

        def erkennung_beansprucht(sha256: str, expected: int | None) -> str:
            # Zwischen Planung und Löschen beansprucht die Texterkennung die Datei (processing)
            OParlFile.objects.filter(pk=datei.pk).update(text_extraction_status="processing")
            return pruefen(sha256, expected)

        monkeypatch.setattr(file_cache_limit, "_remote_state", erkennung_beansprucht)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 0 and ergebnis.skipped == {"texterkennung": 1}
        assert Path(datei.local_path or "").exists()

    def test_sicher_vorhanden_geht(self, body: OParlBody, objektspeicher: Any) -> None:
        datei = _hochgeladen(body, "a")
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 1 and ergebnis.verified == {"vorhanden": 1}
        assert not Path(datei.local_path or "").exists()
        assert _status(datei) == ["ok"], "mit Objektspeicher bleibt die Datenbank unverändert"
        assert file_store.local_copy(OParlFile.objects.get(pk=datei.pk)).present


# =============================================================================
# Begrenzung beim Schreiben: der eine Abrufweg
# =============================================================================


@pytest.mark.parametrize("runner", ["ingestor", "worker"])
def test_nachladen_haelt_die_grenze(settings: Any, quelle: dict[str, Any], runner: str) -> None:
    settings.TEXT_EXTRACTION_RUNNER = runner
    if runner == "worker":
        # Ablage für alle: ausgeblendete Kommune ab ihrem Stichtag
        body = _body(gelistet=False, sync_config={"document_since": (timezone.now() - timedelta(days=1)).isoformat()})
    else:
        body = _body()
    offen = [_datei(body, f"offen{i}", alter_tage=i, local_status="none") for i in range(5)]
    settings.FILE_CACHE_MAX_TOTAL_GB = 2500 / 1024**3

    assert file_cache.cache_pending(limit=10, sleep=0) == {"ok": 3, "limit": 1}
    assert _status(*offen) == ["ok", "ok", "ok", "none", "none"], "neueste zuerst, bis die Grenze erreicht ist"
    assert len(quelle["abrufe"]) == 3
    # Über der Grenze lädt der nächste Lauf nichts; verdrängt wird nur im stündlichen Aufräumen
    assert file_cache.cache_pending(limit=10, sleep=0) == {"limit": 1}
    assert len(quelle["abrufe"]) == 3
