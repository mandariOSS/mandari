# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentablage nach SHA-256 mit Referenzzählung und vorbereiteter Objektspeicher (Issue #788).

Gleiche Dateien liegen nur einmal in der Ablage; ein Inhalt verschwindet erst, wenn ihn keine Datei mehr
braucht. Der Objektspeicher ist standardmäßig aus; die Tests nutzen moto als lokalen S3-Ersatz.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import Iterator
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlBody, OParlFile, OParlFileBlob, OParlSource
from insight_core.services import file_accel, file_cache, file_reconcile, file_store, object_storage

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4 Anlage zur Vorlage"
ANDERS = b"%PDF-1.4 andere Anlage"
ENDPUNKT = "http://objektspeicher.test"
BUCKET = "mandari-test"


@pytest.fixture
def ablage(tmp_path: Path, settings: Any) -> Path:
    root = tmp_path / "ablage"
    settings.OPARL_FILES_ROOT = root
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.OBJ_ENABLED = False
    return root


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt", is_listed=True
    )


def _datei(body: OParlBody, name: str = "a.pdf", **felder: Any) -> OParlFile:
    werte: dict[str, Any] = {
        "external_id": f"https://ris.fremd.example/oparl/file/{name}",
        "body": body,
        "name": name,
        "file_name": name,
        "mime_type": "application/pdf",
        "download_url": f"https://ris.fremd.example/files/{name}",
    }
    werte.update(felder)
    return OParlFile.objects.create(**werte)


def _sha(inhalt: bytes) -> str:
    return hashlib.sha256(inhalt).hexdigest()


def _blob(inhalt: bytes) -> OParlFileBlob:
    return OParlFileBlob.objects.get(pk=_sha(inhalt))


class TestAblageNachHash:
    def test_layout_ohne_endung(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body, "plan.svg", mime_type="image/svg+xml")
        pfad = file_cache.store_bytes(datei, PDF)
        sha = _sha(PDF)
        assert pfad == ablage / "sha256" / sha[:2] / sha
        datei.refresh_from_db()
        assert (datei.blob_id, datei.local_path, datei.local_size, datei.sha256_hash) == (sha, str(pfad), len(PDF), sha)
        assert _blob(PDF).ref_count == 1
        assert list((ablage / "sha256" / "tmp").iterdir()) == []

    def test_gleiche_dateien_liegen_einmal(self, body: OParlBody, ablage: Path) -> None:
        erste, zweite = _datei(body, "a.pdf"), _datei(body, "b.pdf")
        assert file_cache.store_bytes(erste, PDF) == file_cache.store_bytes(zweite, PDF)
        assert _blob(PDF).ref_count == 2
        # Erneut ablegen zählt nicht doppelt
        file_cache.store_bytes(erste, PDF)
        assert _blob(PDF).ref_count == 2
        assert len([p for p in (ablage / "sha256").rglob("*") if p.is_file()]) == 1

    def test_neue_fassung_gibt_den_alten_inhalt_frei(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body)
        alt = file_cache.store_bytes(datei, PDF)
        neu = file_cache.store_bytes(datei, ANDERS)
        alter_inhalt = _blob(PDF)
        assert alter_inhalt.ref_count == 0 and alter_inhalt.orphaned_at is not None
        assert _blob(ANDERS).ref_count == 1
        assert alt.exists(), "erst das Aufräumen löscht"

        assert file_store.cleanup_orphans(min_age=timedelta(0)) == {"deleted": 1}
        assert not alt.exists() and neu.exists()
        assert not OParlFileBlob.objects.filter(pk=_sha(PDF)).exists()

    def test_geteilter_inhalt_bleibt_beim_loeschen_nach_frist(self, body: OParlBody, ablage: Path) -> None:
        bleibt = _datei(body, "bleibt.pdf")
        weg = _datei(body, "weg.pdf")
        pfad = file_cache.store_bytes(bleibt, PDF)
        file_cache.store_bytes(weg, PDF)
        OParlFile.objects.filter(pk=weg.pk).update(deleted=True, deleted_at=timezone.now() - timedelta(days=31))

        file_reconcile.purge_expired()
        file_store.cleanup_orphans(min_age=timedelta(0))
        weg.refresh_from_db()
        assert (weg.blob_id, weg.local_path, weg.local_status) == (None, None, "none")
        assert _blob(PDF).ref_count == 1
        assert pfad.exists()
        assert Client().get(f"/insight/dokumente/{bleibt.id}/preview/").status_code == 200

    def test_purge_deleted_loescht_keinen_geteilten_inhalt(self, body: OParlBody, ablage: Path) -> None:
        bleibt, weg = _datei(body, "bleibt.pdf"), _datei(body, "weg.pdf")
        pfad = file_cache.store_bytes(bleibt, PDF)
        file_cache.store_bytes(weg, PDF)
        OParlFile.objects.filter(pk=weg.pk).update(deleted=True, deleted_at=timezone.now())
        call_command("purge_deleted", "--ids", str(weg.pk), "--yes", stdout=StringIO())
        assert not OParlFile.objects.filter(pk=weg.pk).exists()
        assert pfad.exists()
        assert _blob(PDF).ref_count == 1

    def test_ausgeblendete_kommune_gibt_referenzen_frei(self, body: OParlBody, ablage: Path) -> None:
        source = OParlSource.objects.create(name="Pilot", url="https://pilot.example/oparl/system")
        pilot = OParlBody.objects.create(
            external_id="https://pilot.example/oparl/body/1", source=source, name="Pilot", is_listed=False
        )
        gelistet, ausgeblendet = _datei(body, "a.pdf"), _datei(pilot, "p.pdf")
        file_cache.store_bytes(gelistet, PDF)
        file_cache.store_bytes(ausgeblendet, ANDERS)
        call_command("prune_file_cache", unlisted=True, stdout=StringIO())
        assert _blob(ANDERS).ref_count == 0
        assert _blob(PDF).ref_count == 1

    def test_referenzen_neu_berechnen(self, body: OParlBody, ablage: Path) -> None:
        file_cache.store_bytes(_datei(body), PDF)
        OParlFileBlob.objects.filter(pk=_sha(PDF)).update(ref_count=7)
        assert file_store.repair_refcounts() == {"fixed": 1}
        assert _blob(PDF).ref_count == 1

    def test_falsche_zaehlung_loescht_nichts(self, body: OParlBody, ablage: Path) -> None:
        pfad = file_cache.store_bytes(_datei(body), PDF)
        OParlFileBlob.objects.filter(pk=_sha(PDF)).update(ref_count=0, orphaned_at=timezone.now() - timedelta(hours=1))
        assert file_store.cleanup_orphans() == {"repaired": 1}
        assert pfad.exists()

    def test_auslieferung_ueber_den_webserver(self, body: OParlBody, ablage: Path, settings: Any) -> None:
        settings.FILE_ACCEL_REDIRECT = True
        datei = _datei(body)
        file_cache.store_bytes(datei, PDF)
        sha = _sha(PDF)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response["X-Accel-Redirect"] == f"{file_accel.ACCEL_PREFIX}sha256/{sha[:2]}/{sha}"
        assert response["Content-Type"] == "application/pdf"

    @pytest.mark.skipif(os.name == "nt", reason="Unix-Rechte")
    def test_abgelegte_inhalte_sind_lesbar(self, body: OParlBody, ablage: Path) -> None:
        pfad = file_cache.store_bytes(_datei(body), PDF)
        assert stat.S_IMODE(pfad.stat().st_mode) == file_store.FILE_MODE

    def test_geteilter_pfad_ohne_referenz_bleibt_beim_ablegen(
        self, body: OParlBody, ablage: Path, django_capture_on_commit_callbacks: Any
    ) -> None:
        geteilt = file_cache.store_bytes(_datei(body, "a.pdf"), PDF)
        # Fehlende Referenz (Absturz, alter Code): die Datei zeigt auf einen Inhalt unter sha256/
        ohne_referenz = _datei(body, "b.pdf", local_path=str(geteilt), local_status="ok")
        with django_capture_on_commit_callbacks(execute=True):
            file_cache.store_bytes(ohne_referenz, ANDERS)
        assert geteilt.exists()
        assert _blob(PDF).ref_count == 1

    def test_statistik_zaehlt_geteilte_inhalte_einmal(self, body: OParlBody, ablage: Path) -> None:
        file_cache.store_bytes(_datei(body, "a.pdf"), PDF)
        file_cache.store_bytes(_datei(body, "b.pdf"), PDF)
        stats = file_cache.cache_stats()
        assert stats["cached_bytes"] == 2 * len(PDF)
        assert stats["stored_bytes"] == len(PDF)
        out = StringIO()
        call_command("cache_files", "--stats", stdout=out)
        assert "je Datei gezählt" in out.getvalue()

    def test_loeschabgleich_raeumt_verwaiste_inhalte_auf(self, body: OParlBody, ablage: Path) -> None:
        datei = _datei(body)
        alt = file_cache.store_bytes(datei, PDF)
        file_cache.store_bytes(datei, ANDERS)
        OParlFileBlob.objects.filter(pk=_sha(PDF)).update(orphaned_at=timezone.now() - timedelta(hours=1))
        out = StringIO()
        call_command("loeschabgleich", "--nur-loeschen", stdout=out)
        assert "Verwaiste Inhalte: deleted=1" in out.getvalue()
        assert not alt.exists()

    def test_bisheriges_layout_abschaltbar(self, body: OParlBody, ablage: Path, settings: Any) -> None:
        settings.FILE_STORE_LAYOUT = "kommune"
        datei = _datei(body)
        pfad = file_cache.store_bytes(datei, PDF)
        assert "sha256" not in pfad.parts
        datei.refresh_from_db()
        assert datei.blob_id is None


class TestUmstellen:
    def test_kopien_im_alten_layout_wandern_in_die_ablage(
        self, body: OParlBody, ablage: Path, django_capture_on_commit_callbacks: Any
    ) -> None:
        dateien = []
        for name, inhalt in (("a.pdf", PDF), ("b.pdf", PDF), ("c.pdf", ANDERS)):
            alt = ablage / "beispielstadt" / "2026" / f"{name}.pdf"
            alt.parent.mkdir(parents=True, exist_ok=True)
            alt.write_bytes(inhalt)
            dateien.append((_datei(body, name, local_path=str(alt), local_status="ok"), alt))

        # Doppelte Kopien verwirft die Ablage erst nach dem Commit
        with django_capture_on_commit_callbacks(execute=True):
            assert file_store.migrate_legacy() == {"moved": 3}
        for datei, alt in dateien:
            datei.refresh_from_db()
            assert not alt.exists()
            assert Path(datei.local_path or "").is_file()
            assert datei.blob_id
        assert _blob(PDF).ref_count == 2 and _blob(ANDERS).ref_count == 1
        # Wiederholbar
        assert file_store.migrate_legacy() == {}

        out = StringIO()
        call_command("dokumentablage", stdout=out)
        assert "noch im alten Layout 0" in out.getvalue()


class TestCacheHoltNichtNach:
    def test_dateien_in_der_texterkennung_laedt_der_ingestor(
        self, body: OParlBody, ablage: Path, settings: Any
    ) -> None:
        settings.INGESTOR_STORES_FILES = True
        frisch = _datei(body, "frisch.pdf", text_extraction_status="pending")
        haengt = _datei(body, "haengt.pdf", text_extraction_status="pending")
        vor_zwei_tagen = timezone.now() - timedelta(days=2)
        OParlFile.objects.filter(pk=haengt.pk).update(created_at=vor_zwei_tagen, updated_at=vor_zwei_tagen)
        fertig = _datei(body, "fertig.pdf", text_extraction_status="completed")
        offen = set(file_cache.pending_queryset().values_list("pk", flat=True))
        assert offen == {haengt.pk, fertig.pk}
        assert frisch.pk not in offen
        # Legt der Ingestor nichts ab, holt der Cache wie bisher alles
        settings.INGESTOR_STORES_FILES = False
        assert frisch.pk in set(file_cache.pending_queryset().values_list("pk", flat=True))

    def test_wieder_freigegebene_altdatei_laedt_der_ingestor(
        self, body: OParlBody, ablage: Path, settings: Any
    ) -> None:
        """Eine ältere Datei, die nach dem Löschabgleich wieder auf „pending“ geht, holt nur der Ingestor."""
        settings.INGESTOR_STORES_FILES = True
        alt = timezone.now() - timedelta(days=200)
        datei = _datei(body, "alt.pdf", text_extraction_status="skipped", content_purged_at=alt)
        OParlFile.objects.filter(pk=datei.pk).update(created_at=alt, updated_at=alt)
        assert datei.pk in set(file_cache.pending_queryset().values_list("pk", flat=True))

        assert file_reconcile.restore_reappeared() == 1
        datei.refresh_from_db()
        assert datei.text_extraction_status == "pending"
        assert datei.pk not in set(file_cache.pending_queryset().values_list("pk", flat=True))


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


class TestObjektspeicher:
    def test_standard_aus(self, settings: Any) -> None:
        settings.OBJ_ENABLED = False
        assert not object_storage.enabled()
        settings.OBJ_ENABLED = True
        settings.OBJ_ENDPOINT = ""
        assert not object_storage.enabled(), "ohne Zugangsdaten bleibt er aus"

    def test_region_aus_dem_endpunkt(self, settings: Any) -> None:
        settings.OBJ_REGION = ""
        settings.OBJ_ENDPOINT = "https://nbg1.objekte.example"
        assert object_storage.region() == "nbg1"
        settings.OBJ_ENDPOINT = "https://s3.objekte.example"
        assert object_storage.region() == object_storage.DEFAULT_REGION
        settings.OBJ_REGION = "eu-central"
        assert object_storage.region() == "eu-central"

    def test_hochladen_verdraengen_und_zurueckholen(self, body: OParlBody, objektspeicher: Any) -> None:
        datei = _datei(body)
        pfad = file_cache.store_bytes(datei, PDF)
        assert file_store.upload_pending() == {"uploaded": 1}
        sha = _sha(PDF)
        assert _blob(PDF).remote_at is not None
        objekt = objektspeicher.get_object(Bucket=BUCKET, Key=f"sha256/{sha[:2]}/{sha}")
        assert objekt["Body"].read() == PDF

        # Zwischenspeicher voll: verdrängt wird nur, was im Objektspeicher liegt
        nur_lokal = _datei(body, "b.pdf")
        lokal_pfad = file_cache.store_bytes(nur_lokal, ANDERS)
        ergebnis = file_store.evict_local(max_bytes=0)
        assert ergebnis["evicted"] == 1
        assert not pfad.exists() and lokal_pfad.exists()

        # Vorschau holt den Inhalt zurück (Hash geprüft) und liefert ihn aus
        datei.refresh_from_db()
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        assert b"".join(cast(Any, response).streaming_content) == PDF
        assert pfad.exists()

    def test_aufraeumen_loescht_auch_im_objektspeicher(self, body: OParlBody, objektspeicher: Any) -> None:
        datei = _datei(body)
        file_cache.store_bytes(datei, PDF)
        file_store.upload_pending()
        file_cache.store_bytes(datei, ANDERS)
        assert file_store.cleanup_orphans(min_age=timedelta(0))["deleted"] == 1
        sha = _sha(PDF)
        assert objektspeicher.list_objects_v2(Bucket=BUCKET, Prefix=f"sha256/{sha[:2]}/").get("KeyCount") == 0

    def test_objektspeicher_gestoert_rueckfall_auf_die_quelle(
        self, body: OParlBody, objektspeicher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datei = _datei(body)
        pfad = file_cache.store_bytes(datei, PDF)
        file_store.upload_pending()
        pfad.unlink()

        def kaputt(sha256: str, target: Any, **kwargs: Any) -> None:
            raise ConnectionError("Objektspeicher antwortet nicht")

        monkeypatch.setattr(object_storage, "download", kaputt)

        def quelle(self: Any, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", quelle)
        monkeypatch.setattr("insight_core.services.safe_fetch._resolve", lambda host: ["93.184.215.14"])
        datei.refresh_from_db()
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        assert response["X-Mandari-Cache"] == "miss"
        assert b"".join(cast(Any, response).streaming_content) == PDF

    def test_zugriff_aendert_die_aenderungszeit_nicht(self, body: OParlBody, objektspeicher: Any) -> None:
        """Aus der Änderungszeit bildet der Webserver ETag und Last-Modified: sie muss stabil bleiben."""
        datei = _datei(body)
        pfad = file_cache.store_bytes(datei, PDF)
        vorher = time.time_ns() - 7200 * 10**9
        os.utime(pfad, ns=(vorher, vorher))
        datei.refresh_from_db()
        for _ in range(2):
            assert file_store.local_copy(datei) == pfad
            assert pfad.stat().st_mtime_ns == vorher
        assert pfad.stat().st_atime_ns > vorher, "der letzte Zugriff steht in der Zugriffszeit"

    def test_verdraengt_wird_nach_dem_letzten_zugriff(self, body: OParlBody, objektspeicher: Any) -> None:
        gelesen = file_cache.store_bytes(_datei(body, "a.pdf"), PDF)
        liegt = file_cache.store_bytes(_datei(body, "b.pdf"), ANDERS)
        file_store.upload_pending()
        jetzt = time.time_ns()
        # Zuletzt gelesen, aber älter geändert – und umgekehrt
        os.utime(gelesen, ns=(jetzt, jetzt - 10 * 3600 * 10**9))
        os.utime(liegt, ns=(jetzt - 5 * 3600 * 10**9, jetzt))
        assert file_store.evict_local(max_bytes=len(PDF))["evicted"] == 1
        assert gelesen.exists() and not liegt.exists()

    def test_pruefsummen_nur_wo_verlangt(self, objektspeicher: Any, settings: Any) -> None:
        assert object_storage.client().meta.config.request_checksum_calculation == "when_required"
        assert object_storage.client().meta.config.response_checksum_validation == "when_required"
        settings.OBJ_CHECKSUMS = "when_supported"
        assert object_storage.client().meta.config.request_checksum_calculation == "when_supported"

    def test_gesamtdauer_begrenzt(self, body: OParlBody, objektspeicher: Any, settings: Any) -> None:
        datei = _datei(body)
        pfad = file_cache.store_bytes(datei, PDF)
        file_store.upload_pending()
        pfad.unlink()
        with pytest.raises(object_storage.DeadlineExceededError):
            object_storage.download(_sha(PDF), BytesIO(), deadline=time.monotonic() - 1)
        settings.OBJ_FETCH_TOTAL_SECONDS = -1
        assert file_store.fetch_remote(_sha(PDF)) is None
        assert not pfad.exists()

    def test_falscher_inhalt_aus_dem_objektspeicher_wird_verworfen(self, body: OParlBody, objektspeicher: Any) -> None:
        datei = _datei(body)
        pfad = file_cache.store_bytes(datei, PDF)
        file_store.upload_pending()
        pfad.unlink()
        sha = _sha(PDF)
        objektspeicher.put_object(Bucket=BUCKET, Key=f"sha256/{sha[:2]}/{sha}", Body=b"manipuliert")
        assert file_store.fetch_remote(sha) is None
        assert not pfad.exists()


# =============================================================================
# Zeitplan im Worker (Issue #516)
# =============================================================================


class TestZeitplan:
    """``--aufraeumen`` läuft als Zeitplan; Kennzahlen laufen immer, die übrigen Schritte von Hand erzwungen."""

    @pytest.fixture
    def worker(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        from apps.events import presence, verwaltungsbefehle

        monkeypatch.delenv(verwaltungsbefehle.AUS_ZEITPLAN_ENV, raising=False)
        presence.announce("worker", ["scheduler", "tasks"], [])
        yield
        presence.withdraw("worker")

    @staticmethod
    def _lauf(*argumente: str) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command("dokumentablage", *argumente, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_alter_cron_eintrag_ueberspringt_die_kennzahlen_laufen(self, ablage: Path, worker: None) -> None:
        out, err = self._lauf("--aufraeumen")
        assert "läuft als Zeitplan im Worker – Aufruf übersprungen" in err
        assert out == ""

        out, err = self._lauf()
        assert "Aufruf übersprungen" not in err
        assert "Ablage (sha256)" in out

    @pytest.mark.parametrize("schritt", ["--umstellen", "--referenzen", "--aufraeumen"])
    def test_schritte_von_hand_mit_trotz_zeitplan(self, ablage: Path, worker: None, schritt: str) -> None:
        out, err = self._lauf(schritt)
        assert "Aufruf übersprungen" in err

        out, err = self._lauf(schritt, "--trotz-zeitplan")
        assert "Aufruf übersprungen" not in err
        assert "Ablage (sha256)" in out

    def test_ohne_worker_wie_bisher(self, ablage: Path) -> None:
        out, err = self._lauf("--aufraeumen")
        assert "Aufruf übersprungen" not in err
        assert "Verwaiste Inhalte: " in out

    def test_nur_die_kennzahlen_lesen_nur(self) -> None:
        from django.core.management import get_commands, load_command_class

        befehl = load_command_class(get_commands()["dokumentablage"], "dokumentablage")
        assert befehl.liest_nur({"umstellen": False, "aufraeumen": False, "hochladen": False, "referenzen": False})
        for schritt in ("umstellen", "aufraeumen", "hochladen", "referenzen"):
            assert not befehl.liest_nur({schritt: True}), schritt

    def test_zweiter_lauf_endet_sofort(self, ablage: Path) -> None:
        from apps.common.einmalig import Sperre

        with Sperre("dokumentablage"):
            out, err = self._lauf("--aufraeumen")
        assert "läuft bereits" in err
        assert out == ""
