# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Obergrenze des Dokument-Caches (Issue #961): Gesamtgröße begrenzen, selten gebrauchte Dokumente verdrängen.

Ohne Objektspeicher gehen verdrängte Dokumente auf „Verdrängt“ und kommen bei Bedarf über die Vorschau von der
Quelle; mit Objektspeicher wird nur die lokale Kopie gelöscht. Text, Texterkennung und Fingerabdruck bleiben.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone

from apps.common.einmalig import Sperre
from insight_core.models import OParlBody, OParlFile, OParlFileBlob, OParlSource
from insight_core.services import file_cache, file_cache_limit, file_store, object_storage

pytestmark = pytest.mark.django_db

GROESSE = 1000
ENDPUNKT = "http://objektspeicher.test"
BUCKET = "mandari-test"


@pytest.fixture
def ablage(tmp_path: Path, settings: Any) -> Path:
    root = tmp_path / "ablage"
    root.mkdir()
    settings.OPARL_FILES_ROOT = root
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.OBJ_ENABLED = False
    settings.FILE_CACHE_MAX_TOTAL_GB = 0
    settings.FILE_CACHE_EVICT_TARGET_PERCENT = 90
    return root


def _quelle(name: str = "Fremd-RIS", url: str = "https://ris.fremd.example/oparl/system", **felder: Any) -> Any:
    return OParlSource.objects.create(name=name, url=url, **felder)


@pytest.fixture
def body() -> OParlBody:
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=_quelle(), name="Beispielstadt", is_listed=True
    )


def _inhalt(name: str) -> bytes:
    """Eindeutiger Inhalt fester Größe."""
    kopf = f"%PDF-1.4 {name} ".encode()
    return kopf + b"x" * (GROESSE - len(kopf))


def _datei(body: OParlBody, name: str, *, alter_tage: int, **felder: Any) -> OParlFile:
    werte: dict[str, Any] = {
        "external_id": f"https://ris.fremd.example/oparl/file/{name}-{body.pk}",
        "body": body,
        "name": name,
        "file_name": f"{name}.pdf",
        "mime_type": "application/pdf",
        "download_url": f"https://ris.fremd.example/files/{name}.pdf",
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


def _belegung(root: Path) -> int:
    """Inhalte unter ``sha256/`` ohne Teil-Downloads (``sha256/tmp``); nur der Pfad unterhalb der Ablage zählt."""
    ablage = root / "sha256"
    return sum(p.stat().st_size for p in ablage.rglob("*") if p.is_file() and p.relative_to(ablage).parts[0] != "tmp")


def _status(*dateien: OParlFile) -> list[str]:
    return [OParlFile.objects.get(pk=datei.pk).local_status for datei in dateien]


class TestEinstellungen:
    def test_standard_unbegrenzt(self, ablage: Path, body: OParlBody) -> None:
        _abgelegt(body, "a", alter_tage=900)
        ergebnis = file_cache_limit.enforce()
        assert ergebnis.disabled and ergebnis.units == 0
        assert file_cache_limit.room_bytes() is None

    def test_grenze_und_ziel(self, settings: Any) -> None:
        settings.FILE_CACHE_MAX_TOTAL_GB = 10
        settings.FILE_CACHE_EVICT_TARGET_PERCENT = 90
        assert file_cache_limit.limit_bytes() == 10 * 1024**3
        assert file_cache_limit.target_bytes(1000) == 900
        settings.FILE_CACHE_EVICT_TARGET_PERCENT = 5
        assert file_cache_limit.target_percent() == 50, "nie unter die Hälfte"
        settings.FILE_CACHE_MAX_TOTAL_GB = "kaputt"
        assert file_cache_limit.limit_bytes() == 0


class TestVerdraengenOhneObjektspeicher:
    def test_unter_der_grenze_bleibt_alles(self, ablage: Path, body: OParlBody) -> None:
        dateien = [_abgelegt(body, name, alter_tage=900) for name in "abc"]
        ergebnis = file_cache_limit.enforce(max_bytes=3 * GROESSE)
        assert ergebnis.units == 0 and ergebnis.before == 3 * GROESSE
        assert _status(*dateien) == ["ok", "ok", "ok"]

    def test_aelteste_zuerst_bis_zum_ziel(self, ablage: Path, body: OParlBody) -> None:
        dateien = [_abgelegt(body, f"d{i}", alter_tage=tage) for i, tage in enumerate((3000, 2000, 1000, 100, 10))]
        alt = OParlFile.objects.get(pk=dateien[0].pk)
        sha_alt = alt.blob_id
        assert sha_alt is not None
        pfad_alt = Path(alt.local_path or "")

        # Grenze 3,5 Dokumente, Ziel 90 % = 3,15: zwei müssen weichen
        ergebnis = file_cache_limit.enforce(max_bytes=3500)
        assert (ergebnis.units, ergebnis.files, ergebnis.freed) == (2, 2, 2 * GROESSE)
        assert ergebnis.before == 5 * GROESSE and ergebnis.after == 3 * GROESSE and ergebnis.reached
        assert ergebnis.per_body == {"Beispielstadt": [2, 2 * GROESSE]}
        assert _status(*dateien) == ["evicted", "evicted", "ok", "ok", "ok"]
        assert _belegung(ablage) == 3 * GROESSE

        alt.refresh_from_db()
        assert (alt.blob_id, alt.local_path, alt.local_size) == (None, None, None)
        assert not pfad_alt.exists()
        assert not OParlFileBlob.objects.filter(pk=sha_alt).exists()
        # Text, Texterkennung und Fingerabdruck bleiben: nichts wird neu erkannt
        assert alt.text_content == "Text von d0"
        assert alt.text_extraction_status == "completed"
        assert alt.sha256_hash == sha_alt

    def test_zuletzt_ausgeliefert_bleibt(self, ablage: Path, body: OParlBody) -> None:
        gelesen = _abgelegt(body, "gelesen", alter_tage=3000)
        liegt = _abgelegt(body, "liegt", alter_tage=50)
        neu = _abgelegt(body, "neu", alter_tage=1)
        file_cache_limit.mark_used(gelesen)
        file_cache_limit.enforce(max_bytes=2 * GROESSE + 500)
        assert _status(gelesen, liegt, neu) == ["ok", "evicted", "ok"]

    def test_geteilter_inhalt_geht_nur_mit_allen_verweisen(self, ablage: Path, body: OParlBody) -> None:
        geteilt = _inhalt("geteilt")
        alt = _abgelegt(body, "alt", alter_tage=3000, inhalt=geteilt)
        jung = _abgelegt(body, "jung", alter_tage=5, inhalt=geteilt)
        mittel = _abgelegt(body, "mittel", alter_tage=500)
        assert alt.blob_id == jung.blob_id

        # Der geteilte Inhalt zählt so jung wie seine jüngste Datei: zuerst geht der mittlere
        file_cache_limit.enforce(max_bytes=GROESSE + 500)
        assert _status(alt, jung, mittel) == ["ok", "ok", "evicted"]

        # Beide Verweise alt: Inhalt geht mit beiden Dateien, Referenzen danach 0, Zeile gelöscht
        OParlFile.objects.filter(pk=jung.pk).update(file_date=timezone.now() - timedelta(days=4000))
        ergebnis = file_cache_limit.enforce(max_bytes=500)
        assert (ergebnis.units, ergebnis.files) == (1, 2)
        assert _status(alt, jung) == ["evicted", "evicted"]
        assert not OParlFileBlob.objects.filter(pk=alt.blob_id).exists()
        assert _belegung(ablage) == 0

    def test_geschuetzte_dokumente_bleiben(self, ablage: Path, body: OParlBody) -> None:
        schonung = OParlBody.objects.create(
            external_id="https://koeln.example/oparl/body/1",
            source=_quelle("Schonung", "https://koeln.example/oparl/system", consecutive_failures=5),
            name="In Schonung",
            is_listed=True,
        )
        gesperrt = OParlBody.objects.create(
            external_id="https://altcha.example/oparl/body/1",
            source=_quelle("Gesperrt", "https://altcha.example/oparl/system", sync_config={"file_downloads": False}),
            name="Ohne Dateiabruf",
            is_listed=True,
        )
        demo = OParlBody.objects.create(
            external_id="https://demo.invalid/oparl/body/1",
            source=_quelle("Demo", "https://demo.invalid/oparl/system"),
            name="Demo",
            is_listed=True,
        )
        geschuetzt = [
            _abgelegt(body, "erkennung", alter_tage=4000, text_extraction_status="pending"),
            _abgelegt(schonung, "schonung", alter_tage=4000),
            _abgelegt(gesperrt, "gesperrt", alter_tage=4000),
            _abgelegt(demo, "demo", alter_tage=4000),
            _abgelegt(body, "ohne-adresse", alter_tage=4000, download_url=None, access_url=None),
        ]
        frisch = _abgelegt(body, "frisch", alter_tage=4000)
        OParlFile.objects.filter(pk=frisch.pk).update(local_cached_at=timezone.now())
        frei = _abgelegt(body, "frei", alter_tage=10)

        ergebnis = file_cache_limit.enforce(max_bytes=GROESSE)
        assert _status(*geschuetzt, frisch) == ["ok"] * 6
        assert _status(frei) == ["evicted"]
        assert not ergebnis.reached
        assert ergebnis.protected == {
            "texterkennung": GROESSE,
            "nicht_abrufbar": 4 * GROESSE,
            "frisch": GROESSE,
        }

    def test_probelauf_aendert_nichts(self, ablage: Path, body: OParlBody) -> None:
        andere = OParlBody.objects.create(
            external_id="https://ris.fremd.example/oparl/body/2", source=body.source, name="Nachbarstadt"
        )
        dateien = [
            _abgelegt(body, "a", alter_tage=3000),
            _abgelegt(andere, "b", alter_tage=2000),
            _abgelegt(body, "c", alter_tage=1),
        ]
        ergebnis = file_cache_limit.enforce(max_bytes=GROESSE + 500, dry_run=True)
        assert ergebnis.dry_run and (ergebnis.units, ergebnis.freed) == (2, 2 * GROESSE)
        assert ergebnis.per_body == {"Beispielstadt": [1, GROESSE], "Nachbarstadt": [1, GROESSE]}
        assert _status(*dateien) == ["ok", "ok", "ok"]
        assert _belegung(ablage) == 3 * GROESSE

    def test_paralleler_lauf_wartet_nicht(self, ablage: Path, body: OParlBody) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        with Sperre(file_cache_limit.LOCK_NAME, 60) as erhalten:
            assert erhalten
            ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.locked and ergebnis.units == 0
        assert _status(datei) == ["ok"]

    def test_inhalt_ausserhalb_der_ablage_wird_nie_geloescht(
        self, ablage: Path, body: OParlBody, tmp_path: Path
    ) -> None:
        verweis = _abgelegt(body, "verweis", alter_tage=4000)
        normal = _abgelegt(body, "normal", alter_tage=3000)
        pfad = Path(verweis.local_path or "")
        fremd = tmp_path / "fremd.pdf"
        fremd.write_bytes(_inhalt("fremd"))
        pfad.unlink()
        try:
            pfad.symlink_to(fremd)
        except (OSError, NotImplementedError):
            pytest.skip("Symbolische Verweise lassen sich hier nicht anlegen")
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        # Der Verweis zählt nicht zur Belegung und wird nie gelöscht, auch wenn er an der Reihe wäre
        assert ergebnis.before == GROESSE and ergebnis.units == 1
        assert fremd.exists() and pfad.is_symlink()
        assert _status(verweis, normal) == ["ok", "evicted"]


class TestAltesLayout:
    def test_kopien_je_kommune(self, ablage: Path, body: OParlBody, settings: Any, tmp_path: Path) -> None:
        settings.FILE_STORE_LAYOUT = "kommune"
        alt = _abgelegt(body, "alt", alter_tage=3000)
        neu = _abgelegt(body, "neu", alter_tage=1)
        pfad_alt, pfad_neu = Path(alt.local_path or ""), Path(neu.local_path or "")
        assert alt.blob_id is None and pfad_alt.is_relative_to(ablage)

        # Eine Kopie außerhalb der Ablage bleibt, auch wenn sie an der Reihe wäre
        draussen = tmp_path / "draussen.pdf"
        draussen.write_bytes(_inhalt("draussen"))
        aussen = _datei(body, "aussen", alter_tage=5000, local_status="ok", local_path=str(draussen))
        OParlFile.objects.filter(pk=aussen.pk).update(local_size=GROESSE, local_cached_at=timezone.now() - timedelta(2))

        ergebnis = file_cache_limit.enforce(max_bytes=2 * GROESSE + 500)
        assert ergebnis.units == 1
        assert _status(alt, neu, aussen) == ["evicted", "ok", "ok"]
        assert not pfad_alt.exists() and pfad_neu.exists() and draussen.exists()


class TestNachladen:
    def test_verdraengte_laedt_cache_files_nicht_nach(self, ablage: Path, body: OParlBody) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        file_cache_limit.enforce(max_bytes=1)
        assert _status(datei) == ["evicted"]
        assert datei.pk not in set(file_cache.pending_queryset().values_list("pk", flat=True))
        assert datei.pk in set(file_cache.pending_queryset(retry_evicted=True).values_list("pk", flat=True))

    def test_cache_files_haelt_die_grenze(
        self, ablage: Path, body: OParlBody, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        offen = [_datei(body, f"offen{i}", alter_tage=i) for i in range(5)]
        geholt: list[Any] = []

        def holen(file_obj: Any, client: Any = None) -> str:
            file_cache.store_bytes(file_obj, _inhalt(str(file_obj.name)))
            geholt.append(file_obj.pk)
            return "ok"

        monkeypatch.setattr(file_cache, "fetch_and_cache", holen)
        settings.FILE_CACHE_MAX_TOTAL_GB = 2500 / 1024**3
        ergebnis = file_cache.cache_pending(limit=10, sleep=0)
        assert ergebnis == {"ok": 3, "limit": 1}
        assert geholt == [datei.pk for datei in offen[:3]], "neueste zuerst, bis die Grenze erreicht ist"

        # Über der Grenze lädt der nächste Lauf nichts
        assert file_cache.cache_pending(limit=10, sleep=0) == {"limit": 1}

    def test_ohne_grenze_wie_bisher(self, ablage: Path, body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        for i in range(3):
            _datei(body, f"offen{i}", alter_tage=i)

        def holen(file_obj: Any, client: Any = None) -> str:
            file_cache.store_bytes(file_obj, _inhalt(str(file_obj.name)))
            return "ok"

        monkeypatch.setattr(file_cache, "fetch_and_cache", holen)
        assert file_cache.cache_pending(limit=10, sleep=0) == {"ok": 3}


def _quelle_liefert(monkeypatch: pytest.MonkeyPatch, inhalt: bytes, status: int = 200) -> list[str]:
    abrufe: list[str] = []

    def antwort(self: Any, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        abrufe.append(str(request.url))
        return httpx.Response(status, content=inhalt, headers={"content-type": "application/pdf"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", antwort)
    monkeypatch.setattr("insight_core.services.safe_fetch._resolve", lambda host: ["93.184.215.14"])
    return abrufe


class TestVorschau:
    def test_vorschau_vermerkt_die_nutzung(self, ablage: Path, body: OParlBody) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        assert datei.local_accessed_at is None
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200 and response["X-Mandari-Cache"] == "hit"
        datei.refresh_from_db()
        assert datei.local_accessed_at is not None
        assert timezone.now() - datei.local_accessed_at < timedelta(minutes=1)

    def test_nutzung_hoechstens_stuendlich(self, ablage: Path, body: OParlBody) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        jetzt = timezone.now()
        file_cache_limit.mark_used(datei, now=jetzt)
        file_cache_limit.mark_used(datei, now=jetzt + timedelta(minutes=30))
        datei.refresh_from_db()
        assert datei.local_accessed_at == jetzt
        file_cache_limit.mark_used(datei, now=jetzt + timedelta(hours=2))
        datei.refresh_from_db()
        assert datei.local_accessed_at == jetzt + timedelta(hours=2)

    def test_verdraengtes_holt_die_vorschau_neu(
        self, ablage: Path, body: OParlBody, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inhalt = _inhalt("a")
        datei = _abgelegt(body, "a", alter_tage=3000, inhalt=inhalt)
        file_cache_limit.enforce(max_bytes=1)
        assert _status(datei) == ["evicted"]

        abrufe = _quelle_liefert(monkeypatch, inhalt)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200 and response["X-Mandari-Cache"] == "miss"
        assert b"".join(cast(Any, response).streaming_content) == inhalt
        assert abrufe == [str(datei.download_url)]
        datei.refresh_from_db()
        assert datei.local_status == "ok" and datei.blob_id == hashlib.sha256(inhalt).hexdigest()
        assert datei.text_extraction_status == "completed" and datei.text_content == "Text von a"
        assert datei.local_accessed_at is not None

    def test_verdraengtes_ohne_quelle_wird_als_fehlend_vermerkt(
        self, ablage: Path, body: OParlBody, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        file_cache_limit.enforce(max_bytes=1)
        _quelle_liefert(monkeypatch, b"", status=404)
        Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert _status(datei) == ["missing"]


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


class TestMitObjektspeicher:
    def test_nur_lokal_verdraengt_vorschau_holt_aus_dem_objektspeicher(
        self, body: OParlBody, objektspeicher: Any, ablage: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alt = _abgelegt(body, "alt", alter_tage=3000)
        neu = _abgelegt(body, "neu", alter_tage=1)
        assert file_store.upload_pending() == {"uploaded": 2}
        nicht_oben = _abgelegt(body, "nicht-oben", alter_tage=4000)
        pfad_alt = Path(alt.local_path or "")

        ergebnis = file_cache_limit.enforce(max_bytes=2 * GROESSE + 500)
        assert ergebnis.mode == file_cache_limit.MODE_REMOTE
        assert ergebnis.units == 1 and not pfad_alt.exists()
        assert ergebnis.protected == {"nicht_hochgeladen": GROESSE}
        # Datenbank unverändert: Referenz und Status bleiben
        alt.refresh_from_db()
        assert (alt.local_status, alt.blob_id) == ("ok", hashlib.sha256(_inhalt("alt")).hexdigest())
        assert _status(neu, nicht_oben) == ["ok", "ok"]

        def keine_quelle(self: Any, request: httpx.Request) -> httpx.Response:
            raise AssertionError("Die Quelle darf nicht angefragt werden")

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", keine_quelle)
        response = Client().get(f"/insight/dokumente/{alt.id}/preview/")
        assert response.status_code == 200
        assert b"".join(cast(Any, response).streaming_content) == _inhalt("alt")
        assert pfad_alt.exists()


class TestBefehle:
    def test_prune_file_cache_braucht_eine_auswahl(self, ablage: Path) -> None:
        with pytest.raises(CommandError):
            call_command("prune_file_cache", stdout=StringIO())
        with pytest.raises(CommandError):
            call_command("prune_file_cache", "--max-gb", "0", stdout=StringIO())

    def test_prune_file_cache_max_gb(self, ablage: Path, body: OParlBody) -> None:
        dateien = [_abgelegt(body, f"d{i}", alter_tage=3000 - i) for i in range(4)]
        grenze = str(2500 / 1024**3)

        out = StringIO()
        call_command("prune_file_cache", "--max-gb", grenze, "--dry-run", stdout=out)
        assert "Beispielstadt" in out.getvalue() and "würden verdrängt" in out.getvalue()
        assert _status(*dateien) == ["ok"] * 4

        out = StringIO()
        call_command("prune_file_cache", "--max-gb", grenze, stdout=out)
        assert "2 Inhalte für 2 Dokumente verdrängt" in out.getvalue()
        assert _status(*dateien) == ["evicted", "evicted", "ok", "ok"]

    def test_dokumentablage_aufraeumen_haelt_die_grenze(self, ablage: Path, body: OParlBody, settings: Any) -> None:
        alt = _abgelegt(body, "alt", alter_tage=3000)
        neu = _abgelegt(body, "neu", alter_tage=1)
        settings.FILE_CACHE_MAX_TOTAL_GB = 1500 / 1024**3
        out = StringIO()
        call_command("dokumentablage", "--aufraeumen", stdout=out, stderr=StringIO())
        assert "Obergrenze" in out.getvalue()
        assert _status(alt, neu) == ["evicted", "ok"]

    def test_statistik_zeigt_verdraengte(self, ablage: Path, body: OParlBody, settings: Any) -> None:
        _abgelegt(body, "alt", alter_tage=3000)
        file_cache_limit.enforce(max_bytes=1)
        settings.FILE_CACHE_MAX_TOTAL_GB = 10
        stats = file_cache.cache_stats()
        assert stats["evicted"] == 1 and stats["max_total_bytes"] == 10 * 1024**3
        out = StringIO()
        call_command("cache_files", "--stats", stdout=out)
        assert "verdrängt=1" in out.getvalue() and "Obergrenze 10.00 GB" in out.getvalue()


def test_rangfolge_in_python_wie_in_der_datenbank(ablage: Path, body: OParlBody) -> None:
    datei = _abgelegt(body, "a", alter_tage=100)
    jetzt = timezone.now()
    zeile: dict[str, Any] = {
        "file_date": datei.file_date,
        "oparl_created": None,
        "created_at": datei.created_at,
        "local_accessed_at": None,
    }
    assert file_cache_limit.score_of(zeile) == datei.file_date
    zeile["local_accessed_at"] = jetzt
    assert file_cache_limit.score_of(zeile) == jetzt
    zeile["file_date"] = None
    zeile["local_accessed_at"] = datetime(2001, 1, 1, tzinfo=jetzt.tzinfo)
    assert file_cache_limit.score_of(zeile) == datei.created_at


# =============================================================================
# Nacharbeit aus der Prüfung (PR #971)
# =============================================================================


def _im_objektspeicher(*dateien: OParlFile) -> None:
    """Inhalte als hochgeladen vermerken (wie ``dokumentablage --hochladen`` im früheren Betrieb)."""
    OParlFileBlob.objects.filter(pk__in=[datei.blob_id for datei in dateien]).update(remote_at=timezone.now())


def _blob(datei: OParlFile) -> OParlFileBlob:
    return OParlFileBlob.objects.get(pk=OParlFile.objects.get(pk=datei.pk).blob_id)


def _vor_dem_verdraengen(monkeypatch: pytest.MonkeyPatch, aenderung: Any) -> None:
    """Zwischen Planung und Verdrängen (unter der Sperre) etwas ändern, einmal je Stapel."""
    original = file_cache_limit._apply

    def apply(run: Any, items: Any) -> bool:
        aenderung()
        return bool(original(run, items))

    monkeypatch.setattr(file_cache_limit, "_apply", apply)


class TestModuswechsel:
    """Inhalte im Objektspeicher verwaisen nie, auch wenn der Lauf ihn nicht kennt (stiller Moduswechsel)."""

    def test_lauf_setzt_aus_wenn_inhalte_im_objektspeicher_liegen(self, ablage: Path, body: OParlBody) -> None:
        oben = _abgelegt(body, "oben", alter_tage=3000)
        lokal = _abgelegt(body, "lokal", alter_tage=2000)
        _im_objektspeicher(oben)

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.mode == file_cache_limit.MODE_RELEASE
        assert ergebnis.aborted and ergebnis.remote_conflict == 1
        assert ergebnis.units == 0 and not ergebnis.reached
        assert _status(oben, lokal) == ["ok", "ok"]
        assert _belegung(ablage) == 2 * GROESSE
        blob = _blob(oben)
        assert (blob.ref_count, blob.orphaned_at) == (1, None)

    def test_ausdruecklich_ohne_objektspeicher_bleiben_inhalte_dort(
        self, body: OParlBody, objektspeicher: Any, ablage: Path, settings: Any
    ) -> None:
        oben = _abgelegt(body, "oben", alter_tage=3000)
        assert file_store.upload_pending() == {"uploaded": 1}
        lokal = _abgelegt(body, "lokal", alter_tage=2000)
        sha_oben = str(oben.blob_id)

        # Worker ohne OBJ_*: ausdrücklich ohne Objektspeicher weiter
        settings.OBJ_ENABLED = False
        ergebnis = file_cache_limit.enforce(max_bytes=1, without_object_storage=True)
        assert ergebnis.mode == file_cache_limit.MODE_RELEASE and not ergebnis.aborted
        assert ergebnis.remote_conflict == 1
        assert ergebnis.protected == {file_cache_limit.PROTECT_REMOTE: GROESSE}
        assert _status(oben, lokal) == ["ok", "evicted"]
        blob = _blob(oben)
        assert (blob.ref_count, blob.orphaned_at) == (1, None), "nie verwaist"
        assert Path(OParlFile.objects.get(pk=oben.pk).local_path or "").exists()

        # Objektspeicher wieder an: das Aufräumen verwaister Inhalte lässt ihn dort
        settings.OBJ_ENABLED = True
        file_store.cleanup_orphans(min_age=timedelta(0))
        assert object_storage.exists(sha_oben)
        assert OParlFileBlob.objects.filter(pk=sha_oben).exists()

    def test_unter_der_sperre_hochgeladen_verwaist_nicht(
        self, ablage: Path, body: OParlBody, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        _vor_dem_verdraengen(monkeypatch, lambda: _im_objektspeicher(datei))

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {file_cache_limit.PROTECT_REMOTE: 1} and ergebnis.units == 0
        assert _status(datei) == ["ok"]
        blob = _blob(datei)
        assert (blob.ref_count, blob.orphaned_at) == (1, None)
        assert _belegung(ablage) == GROESSE

    def test_dokumentablage_meldet_den_moduswechsel(self, ablage: Path, body: OParlBody, settings: Any) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        _im_objektspeicher(datei)
        settings.FILE_CACHE_MAX_TOTAL_GB = 500 / 1024**3
        out, err = StringIO(), StringIO()
        call_command("dokumentablage", "--aufraeumen", stdout=out, stderr=err)
        assert "Obergrenze ausgesetzt: 1 Inhalte liegen laut Datenbank im Objektspeicher" in err.getvalue()
        assert _status(datei) == ["ok"] and _belegung(ablage) == GROESSE

    def test_prune_file_cache_verlangt_die_ausdrueckliche_option(self, ablage: Path, body: OParlBody) -> None:
        oben = _abgelegt(body, "oben", alter_tage=3000)
        lokal = _abgelegt(body, "lokal", alter_tage=2000)
        _im_objektspeicher(oben)
        grenze = str(500 / 1024**3)

        with pytest.raises(CommandError, match="--ohne-objektspeicher"):
            call_command("prune_file_cache", "--max-gb", grenze, stdout=StringIO())
        assert _status(oben, lokal) == ["ok", "ok"]

        out = StringIO()
        call_command("prune_file_cache", "--max-gb", grenze, "--ohne-objektspeicher", stdout=out)
        assert "Modus: ohne Objektspeicher" in out.getvalue()
        assert "1 Inhalte liegen im Objektspeicher, der hier nicht konfiguriert ist" in out.getvalue()
        assert _status(oben, lokal) == ["ok", "evicted"]

    def test_prune_file_cache_nennt_den_modus(self, body: OParlBody, objektspeicher: Any, ablage: Path) -> None:
        _abgelegt(body, "a", alter_tage=3000)
        out = StringIO()
        call_command("prune_file_cache", "--max-gb", "1", "--dry-run", stdout=out)
        assert out.getvalue().startswith("Modus: mit Objektspeicher")


def _aenderung(art: str, datei: OParlFile) -> Any:
    eigene = OParlFile.objects.filter(pk=datei.pk)
    assert datei.body is not None
    quelle = OParlSource.objects.filter(pk=datei.body.source_id)
    aenderungen: dict[str, Any] = {
        "texterkennung": lambda: eigene.update(text_extraction_status="processing"),
        "neu_abgelegt": lambda: eigene.update(local_cached_at=timezone.now()),
        "genutzt": lambda: file_cache_limit.mark_used(OParlFile.objects.get(pk=datei.pk)),
        "schonung": lambda: quelle.update(consecutive_failures=99),
        "dateiabruf_aus": lambda: quelle.update(sync_config={"file_downloads": False}),
        "ohne_adresse": lambda: eigene.update(download_url=None, access_url=None),
    }
    return aenderungen[art]


_GRUND = {
    "texterkennung": "texterkennung",
    "neu_abgelegt": "frisch",
    "genutzt": "genutzt",
    "schonung": "nicht_abrufbar",
    "dateiabruf_aus": "nicht_abrufbar",
    "ohne_adresse": "nicht_abrufbar",
}


class TestNachpruefungUnterDerSperre:
    """Was sich zwischen Planung und Verdrängen ändert, hält den Inhalt (Nachprüfung unter der Sperre)."""

    @pytest.mark.parametrize("layout", ["sha256", "kommune"])
    @pytest.mark.parametrize("art", list(_GRUND))
    def test_aenderung_haelt_den_inhalt(
        self, ablage: Path, body: OParlBody, settings: Any, monkeypatch: pytest.MonkeyPatch, layout: str, art: str
    ) -> None:
        settings.FILE_STORE_LAYOUT = layout
        datei = _abgelegt(body, "a", alter_tage=3000)
        pfad = Path(datei.local_path or "")
        _vor_dem_verdraengen(monkeypatch, _aenderung(art, datei))

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {_GRUND[art]: 1}
        assert (ergebnis.units, ergebnis.files, ergebnis.freed) == (0, 0, 0)
        assert _status(datei) == ["ok"] and pfad.exists()

    @pytest.mark.parametrize("layout", ["sha256", "kommune"])
    def test_ohne_aenderung_wird_verdraengt(
        self, ablage: Path, body: OParlBody, settings: Any, monkeypatch: pytest.MonkeyPatch, layout: str
    ) -> None:
        """Gegenprobe: Der Haken selbst hält nichts."""
        settings.FILE_STORE_LAYOUT = layout
        datei = _abgelegt(body, "a", alter_tage=3000)
        _vor_dem_verdraengen(monkeypatch, lambda: None)
        assert file_cache_limit.enforce(max_bytes=1).units == 1
        assert _status(datei) == ["evicted"]

    def test_neuer_verweis_auf_den_inhalt(self, ablage: Path, body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ein junges Dokument legt denselben Inhalt ab: Verweiszahl und Rang ändern sich, der Inhalt bleibt."""
        inhalt = _inhalt("geteilt")
        alt = _abgelegt(body, "alt", alter_tage=3000, inhalt=inhalt)
        neu: list[OParlFile] = []

        def neu_ablegen() -> None:
            if not neu:
                neu.append(_abgelegt(body, "neu", alter_tage=1, inhalt=inhalt))

        _vor_dem_verdraengen(monkeypatch, neu_ablegen)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {"genutzt": 1} and ergebnis.units == 0
        assert _status(alt, *neu) == ["ok", "ok"]
        assert _blob(alt).ref_count == 2

    def test_verweis_weggefallen(self, ablage: Path, body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        sha = str(datei.blob_id)
        _vor_dem_verdraengen(monkeypatch, lambda: file_store.release(OParlFile.objects.get(pk=datei.pk)))

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {"ohne_verweis": 1} and ergebnis.units == 0
        # Das Aufräumen verwaister Inhalte löscht ihn, nicht die Grenze
        assert OParlFileBlob.objects.get(pk=sha).orphaned_at is not None

    def test_alte_kopie_an_anderem_ort(
        self, ablage: Path, body: OParlBody, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.FILE_STORE_LAYOUT = "kommune"
        datei = _abgelegt(body, "a", alter_tage=3000)
        pfad = Path(datei.local_path or "")
        anders = pfad.with_name("anders.pdf")
        anders.write_bytes(_inhalt("anders"))
        _vor_dem_verdraengen(monkeypatch, lambda: OParlFile.objects.filter(pk=datei.pk).update(local_path=str(anders)))

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {"unsicher": 1} and ergebnis.units == 0
        assert pfad.exists() and anders.exists()


def _sperrverbindung() -> Any:
    """Zweite Verbindung zur Testdatenbank an Django vorbei (eigene Transaktion, hält Zeilensperren)."""
    import psycopg
    from django.db import connection
    from psycopg.conninfo import make_conninfo

    einstellungen = connection.settings_dict
    teile = {
        "dbname": einstellungen.get("NAME"),
        "user": einstellungen.get("USER"),
        "password": einstellungen.get("PASSWORD"),
        "host": einstellungen.get("HOST"),
        "port": einstellungen.get("PORT"),
    }
    return psycopg.connect(make_conninfo("", **{k: v for k, v in teile.items() if v not in (None, "")}))


@pytest.fixture
def sperre_in_postgres() -> Iterator[Any]:
    """Sperrt Zeilen in einer zweiten Transaktion (``SELECT … FOR UPDATE``), bis der Test endet."""
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("Zeilensperren mit SKIP LOCKED gibt es nur in PostgreSQL (läuft in der CI)")
    verbindung = _sperrverbindung()

    def sperren(tabelle: str, spalte: str, wert: Any) -> None:
        verbindung.execute(f"SELECT 1 FROM {tabelle} WHERE {spalte} = %s FOR UPDATE", [wert])  # noqa: S608

    try:
        yield sperren
    finally:
        verbindung.rollback()
        verbindung.close()


class TestGesperrteZeilen:
    """Gerade gesperrte Zeilen (eine Datei wird eben abgelegt) überspringt der Lauf, statt zu warten."""

    @pytest.mark.django_db(transaction=True)
    def test_gesperrte_datei(self, ablage: Path, body: OParlBody, sperre_in_postgres: Any) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        sperre_in_postgres(OParlFile._meta.db_table, "id", datei.pk)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {"gesperrt": 1} and ergebnis.units == 0
        assert _status(datei) == ["ok"] and _belegung(ablage) == GROESSE

    @pytest.mark.django_db(transaction=True)
    def test_geteilter_inhalt_mit_einer_gesperrten_datei(
        self, ablage: Path, body: OParlBody, sperre_in_postgres: Any
    ) -> None:
        inhalt = _inhalt("geteilt")
        erste = _abgelegt(body, "erste", alter_tage=3000, inhalt=inhalt)
        zweite = _abgelegt(body, "zweite", alter_tage=3000, inhalt=inhalt)
        sperre_in_postgres(OParlFile._meta.db_table, "id", zweite.pk)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        # Die freie Datei allein ginge nicht: sonst verwiese die gesperrte auf einen gelöschten Inhalt
        assert ergebnis.skipped == {"gesperrt": 1} and ergebnis.units == 0
        assert _status(erste, zweite) == ["ok", "ok"]
        assert _blob(erste).ref_count == 2

    @pytest.mark.django_db(transaction=True)
    def test_gesperrter_inhalt(self, ablage: Path, body: OParlBody, sperre_in_postgres: Any) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        sperre_in_postgres(OParlFileBlob._meta.db_table, "sha256", datei.blob_id)
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.skipped == {"gesperrt": 1} and ergebnis.units == 0
        assert _status(datei) == ["ok"]


class TestQuelleVerweigertDateien:
    """Ohne Objektspeicher bleiben Dokumente von Quellen, die Dateiabrufe verweigern."""

    @pytest.fixture
    def sperrt(self) -> OParlBody:
        return OParlBody.objects.create(
            external_id="https://sperrt.example/oparl/body/1",
            source=_quelle("Sperrt", "https://sperrt.example/oparl/system"),
            name="Sperrt Dateien",
            is_listed=True,
        )

    @pytest.mark.parametrize(
        "vermerk",
        [
            {"local_status": "error", "local_error": "HTTP 403"},
            {"local_status": "error", "local_error": "HTTP 401"},
            {"local_status": "error", "local_error": "robots.txt sperrt den Abruf (Disallow: /)"},
            {"text_extraction_status": "skipped", "text_extraction_error": "robots.txt sperrt den Abruf"},
        ],
    )
    def test_vermerk_an_einem_dokument_schuetzt_die_quelle(
        self, ablage: Path, body: OParlBody, sperrt: OParlBody, vermerk: dict[str, str]
    ) -> None:
        geschuetzt = _abgelegt(sperrt, "alt", alter_tage=4000)
        _datei(sperrt, "neu-gesperrt", alter_tage=1, **vermerk)
        frei = _abgelegt(body, "frei", alter_tage=10)

        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert _status(geschuetzt, frei) == ["ok", "evicted"]
        assert ergebnis.protected == {"nicht_abrufbar": GROESSE}

    def test_andere_fehler_schuetzen_nicht(self, ablage: Path, sperrt: OParlBody) -> None:
        datei = _abgelegt(sperrt, "alt", alter_tage=4000)
        _datei(sperrt, "neu", alter_tage=1, local_status="error", local_error="HTTP 500")
        file_cache_limit.enforce(max_bytes=1)
        assert _status(datei) == ["evicted"]

    def test_robots_txt_im_cache_schuetzt(
        self, ablage: Path, sperrt: OParlBody, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from django.core.cache import cache

        from insight_core.services import robots

        datei = _abgelegt(sperrt, "alt", alter_tage=4000, download_url="https://sperrt.example/files/alt.pdf")
        url = str(datei.download_url)
        monkeypatch.setattr(robots, "_fetch", lambda *a, **k: (200, b"User-agent: *\nDisallow: /files/\n"))
        robots.load(url)
        try:
            assert file_cache_limit.refusing_sources() == {sperrt.source_id}
            ergebnis = file_cache_limit.enforce(max_bytes=1)
            assert _status(datei) == ["ok"] and ergebnis.protected == {"nicht_abrufbar": GROESSE}

            # Eine Freigabe für Dateien (robots_override) hebt den Schutz auf
            OParlSource.objects.filter(pk=sperrt.source_id).update(
                sync_config={"robots_override": {"scope": "files", "note": "Freigabe der Stelle liegt vor"}}
            )
            assert file_cache_limit.refusing_sources() == set()
            file_cache_limit.enforce(max_bytes=1)
            assert _status(datei) == ["evicted"]
        finally:
            cache.delete(robots._cache_key(url, robots.USER_AGENT))

    def test_ohne_robots_txt_im_cache_keine_anfrage(
        self, ablage: Path, sperrt: OParlBody, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from insight_core.services import robots

        def keine_anfrage(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Die Grenze darf die robots.txt nicht abrufen")

        monkeypatch.setattr(robots, "_fetch", keine_anfrage)
        _abgelegt(sperrt, "alt", alter_tage=4000, download_url="https://sperrt-ohne-cache.example/files/alt.pdf")
        assert file_cache_limit.refusing_sources() == set()


class TestCrawler:
    @pytest.mark.parametrize(
        "kennung",
        [
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
            "Mozilla/5.0 (compatible; Amazonbot/0.1; +https://developer.amazon.com/support/amazonbot)",
            "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 "
            "(compatible; Bytespider; spider-feedback@bytedance.com)",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
            "ExampleCrawler/3.1",
            "python-requests/2.32.3",
        ],
    )
    def test_crawler_bestimmen_die_rangfolge_nicht(self, ablage: Path, body: OParlBody, kennung: str) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        response = Client(HTTP_USER_AGENT=kennung).get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200 and response["X-Mandari-Cache"] == "hit"
        datei.refresh_from_db()
        assert datei.local_accessed_at is None
        assert file_cache_limit.is_crawler(kennung)

    @pytest.mark.parametrize(
        "kennung",
        [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1",
            "",
        ],
    )
    def test_menschen_bestimmen_sie(self, ablage: Path, body: OParlBody, kennung: str) -> None:
        datei = _abgelegt(body, "a", alter_tage=3000)
        Client(HTTP_USER_AGENT=kennung).get(f"/insight/dokumente/{datei.id}/preview/")
        datei.refresh_from_db()
        assert datei.local_accessed_at is not None
        assert not file_cache_limit.is_crawler(kennung)


class TestKennzahlen:
    def test_mit_objektspeicher_lokal_und_im_objektspeicher_getrennt(
        self, body: OParlBody, objektspeicher: Any, ablage: Path
    ) -> None:
        from insight_core.services.source_health import collect_system_health

        _abgelegt(body, "alt", alter_tage=3000)
        _abgelegt(body, "neu", alter_tage=1)
        assert file_store.upload_pending() == {"uploaded": 2}
        assert file_cache_limit.enforce(max_bytes=GROESSE + 500).units == 1

        stats = file_cache.cache_stats()
        assert stats["object_storage"] is True
        assert stats["local_bytes"] == GROESSE, "tatsächlich auf der Platte"
        assert stats["remote_bytes"] == 2 * GROESSE
        assert stats["stored_bytes"] == 2 * GROESSE
        out = StringIO()
        call_command("cache_files", "--stats", stdout=out)
        assert "2 von 2 Dokumenten abgelegt" in out.getvalue()
        assert "(Platte), im Objektspeicher" in out.getvalue()

        zustand = next(c for c in collect_system_health() if c["name"] == "Dokument-Cache")
        assert "Dokumenten abgelegt" in zustand["detail"] and "im Objektspeicher" in zustand["detail"]

    def test_ohne_objektspeicher_wie_bisher(self, ablage: Path, body: OParlBody) -> None:
        from insight_core.services.source_health import collect_system_health

        _abgelegt(body, "a", alter_tage=3000)
        stats = file_cache.cache_stats()
        assert stats["object_storage"] is False
        assert stats["local_bytes"] == stats["stored_bytes"] == GROESSE and stats["remote_bytes"] == 0
        out = StringIO()
        call_command("cache_files", "--stats", stdout=out)
        assert "1 von 1 Dokumenten lokal" in out.getvalue() and "im Objektspeicher" not in out.getvalue()
        zustand = next(c for c in collect_system_health() if c["name"] == "Dokument-Cache")
        assert "Dokumenten lokal" in zustand["detail"] and "im Objektspeicher" not in zustand["detail"]


class TestPruefungImObjektspeicher:
    def _hochgeladen(self, body: OParlBody, *namen: str) -> list[OParlFile]:
        dateien = [_abgelegt(body, name, alter_tage=3000 - i) for i, name in enumerate(namen)]
        assert file_store.upload_pending() == {"uploaded": len(namen)}
        return [OParlFile.objects.get(pk=datei.pk) for datei in dateien]

    def test_probelauf_findet_fehlende_und_aendert_nichts(
        self, body: OParlBody, objektspeicher: Any, ablage: Path
    ) -> None:
        fehlt, _da, falsch = self._hochgeladen(body, "fehlt", "da", "falsch")
        objektspeicher.delete_object(Bucket=BUCKET, Key=object_storage.key_for(str(fehlt.blob_id)))
        objektspeicher.put_object(Bucket=BUCKET, Key=object_storage.key_for(str(falsch.blob_id)), Body=b"kurz")

        ergebnis = file_cache_limit.enforce(max_bytes=1, dry_run=True, verify_remote=True)
        assert ergebnis.verified == {"vorhanden": 1, "fehlt": 1, "groesse_abweichend": 1}
        assert _belegung(ablage) == 3 * GROESSE
        assert OParlFileBlob.objects.filter(remote_at__isnull=False).count() == 3

    def test_echter_lauf_behaelt_was_fehlt_und_laedt_neu_hoch(
        self, body: OParlBody, objektspeicher: Any, ablage: Path
    ) -> None:
        fehlt, da, falsch = self._hochgeladen(body, "fehlt", "da", "falsch")
        objektspeicher.delete_object(Bucket=BUCKET, Key=object_storage.key_for(str(fehlt.blob_id)))
        objektspeicher.put_object(Bucket=BUCKET, Key=object_storage.key_for(str(falsch.blob_id)), Body=b"kurz")

        ergebnis = file_cache_limit.enforce(max_bytes=1, verify_remote=True)
        assert ergebnis.verified == {"vorhanden": 1, "fehlt": 1, "groesse_abweichend": 1}
        assert ergebnis.skipped == {"nicht_im_objektspeicher": 2} and ergebnis.units == 1
        assert Path(fehlt.local_path or "").exists() and Path(falsch.local_path or "").exists()
        assert not Path(da.local_path or "").exists()
        # Fehlt der Inhalt oder weicht seine Größe ab, gilt er als nicht hochgeladen: ein Lauf ohne Prüfung lässt die
        # Kopie stehen, und das nächste Hochladen ersetzt den Inhalt im Objektspeicher
        assert _blob(fehlt).remote_at is None and _blob(falsch).remote_at is None
        ohne_pruefung = file_cache_limit.enforce(max_bytes=1)
        assert ohne_pruefung.units == 0
        assert Path(fehlt.local_path or "").exists() and Path(falsch.local_path or "").exists()
        assert file_store.upload_pending() == {"uploaded": 2}
        assert object_storage.exists(str(fehlt.blob_id))
        assert object_storage.remote_size(str(falsch.blob_id)) == GROESSE

    def test_ohne_pruefung_wie_bisher(self, body: OParlBody, objektspeicher: Any, ablage: Path) -> None:
        fehlt, _da = self._hochgeladen(body, "fehlt", "da")
        objektspeicher.delete_object(Bucket=BUCKET, Key=object_storage.key_for(str(fehlt.blob_id)))
        ergebnis = file_cache_limit.enforce(max_bytes=1)
        assert ergebnis.units == 2 and not ergebnis.verified

    def test_stichprobe(self, body: OParlBody, objektspeicher: Any, ablage: Path) -> None:
        self._hochgeladen(body, "a", "b", "c")
        ergebnis = file_cache_limit.enforce(max_bytes=1, dry_run=True, verify_remote=True, sample=2)
        assert ergebnis.verified == {"vorhanden": 2}

    def test_objektspeicher_gestoert_bricht_ab(
        self, body: OParlBody, objektspeicher: Any, ablage: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (datei,) = self._hochgeladen(body, "a")

        def gestoert(sha256: str) -> int:
            raise OSError("Objektspeicher nicht erreichbar")

        monkeypatch.setattr(object_storage, "remote_size", gestoert)
        ergebnis = file_cache_limit.enforce(max_bytes=1, verify_remote=True)
        assert ergebnis.units == 0 and ergebnis.skipped == {"fehler": 1}
        assert Path(datei.local_path or "").exists()

    def test_befehl(self, body: OParlBody, objektspeicher: Any, ablage: Path) -> None:
        fehlt, _da = self._hochgeladen(body, "fehlt", "da")
        objektspeicher.delete_object(Bucket=BUCKET, Key=object_storage.key_for(str(fehlt.blob_id)))
        grenze = str(500 / 1024**3)

        with pytest.raises(CommandError, match="--stichprobe nur mit --dry-run"):
            call_command("prune_file_cache", "--max-gb", grenze, "--stichprobe", "5", stdout=StringIO())
        out = StringIO()
        call_command("prune_file_cache", "--max-gb", grenze, "--dry-run", "--pruefe-objektspeicher", stdout=out)
        assert "Objektspeicher geprüft (HEAD): 2 Inhalte, vorhanden 1, fehlen 1" in out.getvalue()
        assert "Vor dem echten Lauf klären" in out.getvalue()

    def test_befehl_ohne_objektspeicher(self, ablage: Path, body: OParlBody) -> None:
        _abgelegt(body, "a", alter_tage=3000)
        with pytest.raises(CommandError, match="braucht einen konfigurierten Objektspeicher"):
            call_command("prune_file_cache", "--max-gb", "1", "--pruefe-objektspeicher", stdout=StringIO())
