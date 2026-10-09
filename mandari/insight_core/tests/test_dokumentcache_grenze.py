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
