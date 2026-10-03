# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Löschabgleich der Dokumente mit den Quellen (Issue #787).

Entfernt oder ändert eine Kommune ein Dokument, sperren wir es sofort (keine Bytes, kein Text, nicht in der
Suche), ersetzen geänderte Inhalte nach Hashvergleich und löschen Kopie und Text nach 30 Tagen Sperre.
HEAD-Stichproben sind gedrosselt und beachten die robots.txt der Quelle.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from insight_core import signals
from insight_core.models import OParlBody, OParlFile, OParlFileAccessDay, OParlPaper, OParlSource
from insight_core.services import file_reconcile, file_robots

pytestmark = pytest.mark.django_db

PDF_ALT = b"%PDF-1.4 Fassung mit Namen"
PDF_NEU = b"%PDF-1.4 Fassung geschwaerzt"
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


@pytest.fixture
def source() -> OParlSource:
    return OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")


@pytest.fixture
def body(source: OParlSource) -> OParlBody:
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt", is_listed=True
    )


@pytest.fixture
def ablage(tmp_path: Path, settings: Any) -> Path:
    root = tmp_path / "ablage"
    settings.OPARL_FILES_ROOT = root
    settings.FILE_CACHE_MIN_FREE_GB = 0
    return root


def _datei(body: OParlBody, tmp_path: Path, name: str = "anlage.pdf", **felder: Any) -> OParlFile:
    pfad = tmp_path / f"{name}.kopie"
    pfad.write_bytes(PDF_ALT)
    werte: dict[str, Any] = {
        "external_id": f"https://ris.fremd.example/oparl/file/{name}",
        "body": body,
        "name": name,
        "file_name": name,
        "mime_type": "application/pdf",
        "download_url": f"https://ris.fremd.example/files/{name}",
        "local_path": str(pfad),
        "local_status": "ok",
        "local_size": len(PDF_ALT),
        "local_cached_at": timezone.now() - timedelta(days=10),
        "sha256_hash": hashlib.sha256(PDF_ALT).hexdigest(),
        "text_content": "Text mit Namen",
        "text_extraction_status": "completed",
        "text_extracted_at": timezone.now() - timedelta(days=10),
        "oparl_modified": timezone.now() - timedelta(days=20),
    }
    werte.update(felder)
    return OParlFile.objects.create(**werte)


def _client(handler: Handler, aufrufe: list[str] | None = None) -> httpx.Client:
    def mitschreiben(request: httpx.Request) -> httpx.Response:
        if aufrufe is not None:
            aufrufe.append(f"{request.method} {request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(mitschreiben))


def _liefert(inhalt: bytes, *, head_status: int = 200, get_status: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(head_status, headers={"content-length": str(len(inhalt))})
        if get_status != 200:
            return httpx.Response(get_status)
        return httpx.Response(200, content=inhalt, headers={"content-type": "application/pdf"})

    return handler


# =============================================================================
# robots.txt
# =============================================================================


class TestRobots:
    def test_pdf_sperre_mit_platzhaltern(self) -> None:
        regeln = file_robots.parse("User-agent: *\nDisallow: /*.pdf$\n")
        assert not file_robots.is_allowed(regeln, "https://ris.example/files/a.pdf")
        assert file_robots.is_allowed(regeln, "https://ris.example/oparl/body/1")
        # "$" verankert am Ende: eine Adresse mit Parametern dahinter passt nicht
        assert file_robots.is_allowed(regeln, "https://ris.example/files/a.pdf?x=1")

    def test_laengste_regel_gewinnt_allow_bei_gleichstand(self) -> None:
        regeln = file_robots.parse("User-agent: *\nDisallow: /files/\nAllow: /files/oeffentlich/\n")
        assert file_robots.is_allowed(regeln, "https://ris.example/files/oeffentlich/a.pdf")
        assert not file_robots.is_allowed(regeln, "https://ris.example/files/intern/a.pdf")
        assert file_robots.is_allowed(
            file_robots.parse("User-agent: *\nDisallow: /a\nAllow: /a\n"), "https://ris.example/a"
        )

    def test_gruppe_fuer_unseren_namen_vor_stern(self) -> None:
        text = "User-agent: *\nDisallow: /\n\nUser-agent: Mandari\nAllow: /\n"
        assert file_robots.is_allowed(file_robots.parse(text), "https://ris.example/files/a.pdf")
        assert not file_robots.is_allowed(file_robots.parse("User-agent: *\nDisallow: /\n"), "https://ris.example/x")
        assert file_robots.is_allowed(file_robots.parse("User-agent: *\nDisallow:\n"), "https://ris.example/x")

    def test_mehrere_agenten_teilen_eine_gruppe(self) -> None:
        text = "User-agent: googlebot\nUser-agent: *\nDisallow: /files/\n"
        assert not file_robots.is_allowed(file_robots.parse(text), "https://ris.example/files/a.pdf")

    def test_ausnahme_mit_vermerk(self, source: OParlSource) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="User-agent: *\nDisallow: /*.pdf$\n")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        url = "https://ris.fremd.example/files/a.pdf"
        assert file_robots.file_fetch_status(source, url, client) == file_robots.DISALLOWED
        vermerk = "Zustimmung liegt vor, Freigabe angefragt"
        for bereich in ("files", "all"):
            source.sync_config = {"robots_override": {"scope": bereich, "note": vermerk}}
            assert file_robots.file_fetch_status(source, url, client) == file_robots.ALLOWED
        # Ohne Vermerk, mit zu kurzem Vermerk oder nur für die Schnittstelle: keine Ausnahme für Dateien
        for ausnahme in ({"scope": "files", "note": "  "}, {"note": "ja"}, {"scope": "api", "note": vermerk}, "ja"):
            source.sync_config = {"robots_override": ausnahme}
            assert file_robots.file_fetch_status(source, url, client) == file_robots.DISALLOWED

    def test_user_agent_nach_anderer_zeile_beginnt_neue_gruppe(self) -> None:
        # Crawl-delay schließt die Gruppe von "*"; die Sperre gilt nur für den folgenden Agenten
        text = "User-agent: *\nCrawl-delay: 5\nUser-agent: badbot\nDisallow: /\n"
        assert file_robots.is_allowed(file_robots.parse(text), "https://ris.example/files/a.pdf")
        text = "User-agent: *\nRequest-rate: 1/5\nUser-agent: mandari\nDisallow: /files/\n"
        assert not file_robots.is_allowed(file_robots.parse(text), "https://ris.example/files/a.pdf")
        anderer = file_robots.parse(text.replace("mandari", "anderer"))
        assert file_robots.is_allowed(anderer, "https://ris.example/files/a.pdf")
        # Sitemap gehört zu keiner Gruppe und trennt nichts
        text = "User-agent: anderer\nSitemap: https://ris.example/sitemap.xml\nUser-agent: *\nDisallow: /files/\n"
        assert not file_robots.is_allowed(file_robots.parse(text), "https://ris.example/files/a.pdf")

    def test_nicht_lesbare_robots_sperrt(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        assert file_robots.file_fetch_status(None, "https://ris.example/a.pdf", client) == file_robots.UNREACHABLE


# =============================================================================
# Abgleich per Hash
# =============================================================================


class TestAbgleichPerHash:
    def test_gleicher_inhalt_aendert_nichts(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        assert file_reconcile.verify(datei, _client(_liefert(PDF_ALT))) == file_reconcile.UNCHANGED
        datei.refresh_from_db()
        assert datei.text_content == "Text mit Namen"
        assert datei.source_checked_at is not None
        assert Path(datei.local_path or "").read_bytes() == PDF_ALT

    def test_anderer_inhalt_ersetzt_kopie_und_text(self, body: OParlBody, tmp_path: Path, ablage: Path) -> None:
        paper = OParlPaper.objects.create(
            external_id="https://ris.fremd.example/oparl/paper/1", body=body, name="Vorlage", summary="mit Namen"
        )
        datei = _datei(body, tmp_path, paper=paper)
        alte_kopie = Path(datei.local_path or "")
        assert file_reconcile.verify(datei, _client(_liefert(PDF_NEU))) == file_reconcile.CHANGED
        datei.refresh_from_db()
        assert Path(datei.local_path or "").read_bytes() == PDF_NEU
        assert not alte_kopie.exists()
        assert datei.sha256_hash == hashlib.sha256(PDF_NEU).hexdigest()
        assert datei.text_content is None
        assert datei.text_extraction_status == "pending"
        paper.refresh_from_db()
        assert paper.summary is None

    def test_quelle_liefert_404_sperrt(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        assert file_reconcile.verify(datei, _client(_liefert(b"", get_status=404))) == file_reconcile.MISSING
        datei.refresh_from_db()
        assert datei.source_missing_since is not None
        assert file_reconcile.is_blocked(datei)

    def test_hinweisseite_statt_datei_aendert_nichts(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        seite = b"<!doctype html><html><body>Wartung</body></html>"
        assert file_reconcile.verify(datei, _client(_liefert(seite))) == file_reconcile.ERROR
        datei.refresh_from_db()
        assert datei.text_content == "Text mit Namen"
        assert datei.source_missing_since is None

    def test_auswahl_der_geaenderten(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        geaendert = _datei(body, tmp_path, "neu.pdf", oparl_modified=jetzt - timedelta(days=1))
        _datei(body, tmp_path, "alt.pdf")
        _datei(
            body,
            tmp_path,
            "geprueft.pdf",
            oparl_modified=jetzt - timedelta(days=2),
            source_checked_at=jetzt - timedelta(days=1),
        )
        assert [f.pk for f in file_reconcile.changed_queryset()] == [geaendert.pk]

        aufrufe: list[str] = []
        ergebnis = file_reconcile.check_changed(interval=0, client=_client(_liefert(PDF_ALT), aufrufe))
        assert ergebnis == {file_reconcile.UNCHANGED: 1}
        assert aufrufe == ["GET /robots.txt", "GET /files/neu.pdf"]
        assert not file_reconcile.changed_queryset().exists()


# =============================================================================
# HEAD-Stichproben
# =============================================================================


class TestStichproben:
    def test_vorhanden_gleiche_groesse(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        aufrufe: list[str] = []
        assert file_reconcile.head_check(datei, _client(_liefert(PDF_ALT), aufrufe)) == file_reconcile.PRESENT
        assert aufrufe == ["GET /robots.txt", "HEAD /files/anlage.pdf"]

    def test_404_nur_bei_head_sperrt_nicht(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        ergebnis = file_reconcile.head_check(datei, _client(_liefert(PDF_ALT, head_status=404)))
        assert ergebnis == file_reconcile.UNCHANGED
        datei.refresh_from_db()
        assert datei.source_missing_since is None

    def test_404_bestaetigt_sperrt(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        handler = _liefert(b"", head_status=410, get_status=410)
        assert file_reconcile.head_check(datei, _client(handler)) == file_reconcile.MISSING

    def test_andere_groesse_gleicht_per_hash_ab(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        assert file_reconcile.head_check(datei, _client(_liefert(PDF_NEU + b"x"))) == file_reconcile.CHANGED

    def test_robots_sperrt_ohne_dateiabruf(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path)
        aufrufe: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            aufrufe.append(f"{request.method} {request.url.path}")
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /*.pdf$\n")
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert file_reconcile.head_check(datei, client) == file_reconcile.ROBOTS
        assert aufrufe == ["GET /robots.txt"]
        datei.refresh_from_db()
        assert datei.source_missing_since is None

    def test_quelle_in_schonung_bleibt_unberuehrt(self, body: OParlBody, source: OParlSource, tmp_path: Path) -> None:
        _datei(body, tmp_path)
        source.consecutive_failures = 10
        source.save(update_fields=["consecutive_failures"])
        aufrufe: list[str] = []
        assert not file_reconcile.sample_heads(interval=0, client=_client(_liefert(PDF_ALT), aufrufe))
        assert aufrufe == []

    def test_gesperrtes_wird_wieder_freigegeben(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path, source_missing_since=timezone.now() - timedelta(days=2))
        ergebnis = file_reconcile.sample_heads(body, per_source=5, interval=0, client=_client(_liefert(PDF_ALT)))
        assert ergebnis == {file_reconcile.PRESENT: 1}
        datei.refresh_from_db()
        assert datei.source_missing_since is None

    def test_drosselung_je_host(self, body: OParlBody, tmp_path: Path, monkeypatch: Any) -> None:
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            _datei(body, tmp_path, name)
        pausen: list[float] = []
        monkeypatch.setattr("insight_core.services.file_reconcile.time.sleep", pausen.append)
        file_reconcile.sample_heads(body, per_source=3, interval=5.0, client=_client(_liefert(PDF_ALT)))
        assert len(pausen) == 2
        assert all(0 < pause <= 5.0 for pause in pausen)


# =============================================================================
# Sperre in Auslieferung, Vorgang und Suche
# =============================================================================


class TestSperre:
    def test_vorschau_liefert_gesperrtes_nicht_aus(self, body: OParlBody, tmp_path: Path) -> None:
        for felder in ({"deleted": True, "deleted_at": timezone.now()}, {"source_missing_since": timezone.now()}):
            datei = _datei(body, tmp_path, f"{len(felder)}-{next(iter(felder))}.pdf", **felder)
            response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
            assert response.status_code == 410
            assert PDF_ALT not in response.content
            assert "X-Accel-Redirect" not in response
        assert list(OParlFileAccessDay.objects.values_list("outcome", "count")) == [("blocked", 2)]

    def test_vorgangsseite_ohne_gesperrte_dokumente_und_text(self, body: OParlBody, tmp_path: Path) -> None:
        paper = OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/1", body=body, name="V")
        _datei(body, tmp_path, "sichtbar.pdf", paper=paper, text_content="Sichtbarer Text")
        _datei(
            body, tmp_path, "weg.pdf", paper=paper, text_content="Entfernter Text", source_missing_since=timezone.now()
        )
        html = Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()
        assert "Sichtbarer Text" in html
        assert "Entfernter Text" not in html
        assert "weg.pdf" not in html

    def test_suchindex_verliert_gesperrte_datei(self, body: OParlBody, tmp_path: Path, monkeypatch: Any) -> None:
        geloescht: list[str] = []
        monkeypatch.setattr(signals, "_delete_document", lambda index, doc_id: geloescht.append(f"{index}/{doc_id}"))
        datei = _datei(body, tmp_path)
        file_reconcile.mark_missing(datei, timezone.now())
        assert geloescht == [f"files/{datei.id}"]

    def test_verworfener_text_verlaesst_den_index(self, body: OParlBody, tmp_path: Path, monkeypatch: Any) -> None:
        geloescht: list[str] = []
        monkeypatch.setattr(signals, "_delete_document", lambda index, doc_id: geloescht.append(f"{index}/{doc_id}"))
        datei = _datei(body, tmp_path)
        file_reconcile.verify(datei, _client(_liefert(PDF_NEU)))
        assert f"files/{datei.id}" in geloescht


# =============================================================================
# Löschen nach Frist
# =============================================================================


class TestLoeschenNachFrist:
    def test_nach_30_tagen_kopie_und_text_weg(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        alt = _datei(body, tmp_path, "alt.pdf", deleted=True, deleted_at=jetzt - timedelta(days=31))
        fehlt = _datei(body, tmp_path, "fehlt.pdf", source_missing_since=jetzt - timedelta(days=31))
        frisch = _datei(body, tmp_path, "frisch.pdf", deleted=True, deleted_at=jetzt - timedelta(days=5))
        aktiv = _datei(body, tmp_path, "aktiv.pdf")

        aufrufe: list[str] = []
        quelle = _client(_liefert(b"", get_status=404), aufrufe)
        assert file_reconcile.purge_expired(now=jetzt, client=quelle, interval=0) == {"purged": 2, "copies": 2}
        # Vor dem Löschen fragt der Abgleich nur das nicht mehr abrufbare Dokument noch einmal ab
        assert aufrufe == ["GET /robots.txt", "GET /files/fehlt.pdf"]
        for datei in (alt, fehlt):
            kopie = Path(datei.local_path or "")
            datei.refresh_from_db()
            assert not kopie.exists()
            assert datei.text_content is None
            assert datei.local_path is None
            assert datei.local_status == "none"
            assert datei.content_purged_at == jetzt
            # Der Datensatz bleibt als Tombstone mit Fingerabdruck
            assert datei.sha256_hash
        for datei in (frisch, aktiv):
            datei.refresh_from_db()
            assert datei.text_content == "Text mit Namen"
            assert Path(datei.local_path or "").exists()
        # Wiederholt: nichts mehr zu tun
        assert not file_reconcile.purge_expired(now=jetzt, client=quelle, interval=0)

    def test_quelle_hebt_loeschung_auf(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path, deleted=True, deleted_at=timezone.now() - timedelta(days=40))
        file_reconcile.purge_expired()
        # Der Ingestor hebt den Tombstone auf, weil die Quelle das Objekt wieder liefert
        OParlFile.objects.filter(pk=datei.pk).update(deleted=False)
        assert file_reconcile.restore_reappeared() == 1
        datei.refresh_from_db()
        assert datei.content_purged_at is None
        assert datei.text_extraction_status == "pending"

    def test_befehl(self, body: OParlBody, tmp_path: Path) -> None:
        _datei(body, tmp_path, deleted=True, deleted_at=timezone.now() - timedelta(days=31))
        from io import StringIO

        out = StringIO()
        call_command("loeschabgleich", "--nur-loeschen", stdout=out)
        assert "purged=1" in out.getvalue()

    def test_quelle_liefert_vor_dem_loeschen_wieder(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        datei = _datei(body, tmp_path, source_missing_since=jetzt - timedelta(days=31))
        ergebnis = file_reconcile.purge_expired(now=jetzt, client=_client(_liefert(PDF_ALT)), interval=0)
        assert ergebnis == {"entsperrt": 1}
        datei.refresh_from_db()
        assert datei.source_missing_since is None
        assert datei.content_purged_at is None
        assert datei.text_content == "Text mit Namen"
        assert Path(datei.local_path or "").read_bytes() == PDF_ALT

    def test_ohne_antwort_der_quelle_zurueckgestellt_dann_geloescht(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        zurueck = _datei(body, tmp_path, "zurueck.pdf", source_missing_since=jetzt - timedelta(days=31))
        lange = _datei(body, tmp_path, "lange.pdf", source_missing_since=jetzt - timedelta(days=38))
        quelle = _client(_liefert(b"", get_status=500))
        ergebnis = file_reconcile.purge_expired(now=jetzt, client=quelle, interval=0)
        assert ergebnis == {"zurueckgestellt": 1, "purged": 1, "copies": 1, "ohne_rueckfrage": 1}
        zurueck.refresh_from_db()
        lange.refresh_from_db()
        assert zurueck.content_purged_at is None and zurueck.text_content == "Text mit Namen"
        assert lange.content_purged_at == jetzt and lange.text_content is None

    def test_geloescht_ohne_zeitpunkt_frist_beginnt_jetzt(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        datei = _datei(body, tmp_path, deleted=True, deleted_at=None)
        assert file_reconcile.purge_expired(now=jetzt) == {"frist_beginnt": 1}
        datei.refresh_from_db()
        assert datei.deleted_at == jetzt
        assert datei.text_content == "Text mit Namen"
        assert file_reconcile.purge_expired(now=jetzt + timedelta(days=31)) == {"purged": 1, "copies": 1}


# =============================================================================
# Erneut prüfen, Bremse, Ruhe je Host
# =============================================================================


class TestErneutPruefen:
    def test_faellige_stufen(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        beginn = jetzt - timedelta(days=8)
        faellig = _datei(body, tmp_path, "faellig.pdf", source_missing_since=beginn, source_checked_at=beginn)
        # Nach 1 Tag schon geprüft, die 7-Tage-Stufe steht aus
        stufe_7 = _datei(
            body, tmp_path, "stufe7.pdf", source_missing_since=beginn, source_checked_at=beginn + timedelta(days=2)
        )
        # Alle bisher erreichten Stufen erledigt
        _datei(body, tmp_path, "erledigt.pdf", source_missing_since=beginn, source_checked_at=jetzt)
        # Seit weniger als einem Tag gesperrt
        _datei(body, tmp_path, "frisch.pdf", source_missing_since=jetzt - timedelta(hours=3))
        _datei(body, tmp_path, "nicht_gesperrt.pdf")
        assert {f.pk for f in file_reconcile.recheck_queryset(body, jetzt)} == {faellig.pk, stufe_7.pk}

    def test_faellige_kommen_vor_den_stichproben(self, body: OParlBody, tmp_path: Path) -> None:
        jetzt = timezone.now()
        # Dokumente ohne Prüfung stünden in der Stichprobe vorn; das gesperrte wurde gerade erst geprüft
        for name in ("a.pdf", "b.pdf"):
            _datei(body, tmp_path, name)
        gesperrt = _datei(
            body,
            tmp_path,
            "gesperrt.pdf",
            source_missing_since=jetzt - timedelta(days=2),
            source_checked_at=jetzt - timedelta(days=2),
        )
        aufrufe: list[str] = []
        ergebnis = file_reconcile.sample_heads(
            body, per_source=1, interval=0, client=_client(_liefert(PDF_ALT), aufrufe)
        )
        assert ergebnis == {file_reconcile.PRESENT: 1}
        assert aufrufe == ["GET /robots.txt", "HEAD /files/gesperrt.pdf"]
        gesperrt.refresh_from_db()
        assert gesperrt.source_missing_since is None


class TestBremse:
    def _dateien(self, body: OParlBody, tmp_path: Path, anzahl: int) -> list[OParlFile]:
        return [_datei(body, tmp_path, f"d{nummer}.pdf") for nummer in range(anzahl)]

    def test_massenhaft_404_sperrt_nichts(self, body: OParlBody, tmp_path: Path) -> None:
        dateien = self._dateien(body, tmp_path, 6)
        aufrufe: list[str] = []
        quelle = _client(_liefert(b"", head_status=404, get_status=404), aufrufe)
        run = file_reconcile.Run(interval=0, max_missing=3)
        ergebnis = file_reconcile.sample_heads(body, per_source=6, client=quelle, run=run)
        assert ergebnis == {file_reconcile.BRAKED: 4, file_reconcile.SKIPPED: 2}
        assert not OParlFile.objects.filter(source_missing_since__isnull=False).exists()
        # Nach Auslösen der Bremse ruht die Quelle: keine weiteren Abrufe
        assert len([a for a in aufrufe if a.startswith("HEAD")]) == 4
        assert all(not file_reconcile.is_blocked(d) for d in dateien)

    def test_einzelne_404_unter_der_schwelle_sperren(self, body: OParlBody, tmp_path: Path) -> None:
        self._dateien(body, tmp_path, 3)
        quelle = _client(_liefert(b"", head_status=404, get_status=404))
        run = file_reconcile.Run(interval=0, max_missing=3)
        assert file_reconcile.sample_heads(body, per_source=3, client=quelle, run=run) == {file_reconcile.MISSING: 3}
        assert OParlFile.objects.filter(source_missing_since__isnull=False).count() == 3

    def test_bremse_im_befehl_gemeldet(self, body: OParlBody, tmp_path: Path, monkeypatch: Any) -> None:
        from io import StringIO

        self._dateien(body, tmp_path, 3)
        monkeypatch.setattr(
            file_reconcile, "fetch_client", lambda: _client(_liefert(b"", head_status=404, get_status=404))
        )
        out, err = StringIO(), StringIO()
        call_command(
            "loeschabgleich", "--ohne-loeschen", "--interval", "0", "--max-fehlend", "1", stdout=out, stderr=err
        )
        assert "gebremst=2" in out.getvalue()
        assert "Bremse" in err.getvalue()
        assert not OParlFile.objects.filter(source_missing_since__isnull=False).exists()


class TestRuheJeHost:
    def test_429_ohne_get_und_host_ruht(self, body: OParlBody, tmp_path: Path) -> None:
        for name in ("a.pdf", "b.pdf"):
            _datei(body, tmp_path, name)
        aufrufe: list[str] = []
        ergebnis = file_reconcile.sample_heads(
            body, per_source=2, interval=0, client=_client(_liefert(PDF_ALT, head_status=429), aufrufe)
        )
        assert ergebnis == {file_reconcile.THROTTLED: 1, file_reconcile.SKIPPED: 1}
        # Eine HEAD-Anfrage, danach weder ein GET hinterher noch die zweite Datei
        assert aufrufe[0] == "GET /robots.txt"
        assert len(aufrufe) == 2 and aufrufe[1].startswith("HEAD /files/")

    def test_fehler_zaehlen_je_host(self, body: OParlBody, tmp_path: Path) -> None:
        for nummer in range(6):
            _datei(body, tmp_path, f"kaputt{nummer}.pdf")
        _datei(
            body,
            tmp_path,
            "anderer-host.pdf",
            download_url="https://dokumente.fremd.example/files/anderer-host.pdf",
            source_checked_at=timezone.now(),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "dokumente.fremd.example":
                return _liefert(PDF_ALT)(request)
            return httpx.Response(500)

        ergebnis = file_reconcile.sample_heads(body, per_source=7, interval=0, client=_client(handler))
        # Fünf Fehler in Folge: der Host ruht, der andere Host derselben Kommune wird weiter geprüft
        assert ergebnis == {file_reconcile.ERROR: 5, file_reconcile.SKIPPED: 1, file_reconcile.PRESENT: 1}

    def test_weiche_404_gilt_nicht_als_vorhanden(self, body: OParlBody, tmp_path: Path) -> None:
        datei = _datei(body, tmp_path, source_missing_since=timezone.now() - timedelta(days=2))
        seite = b"<!doctype html><html><body>Seite nicht gefunden</body></html>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=seite, headers={"content-type": "text/html; charset=utf-8"})

        assert file_reconcile.head_check(datei, _client(handler)) == file_reconcile.ERROR
        datei.refresh_from_db()
        assert datei.source_missing_since is not None

    def test_nur_gelistete_kommunen_ohne_angabe(self, body: OParlBody, source: OParlSource, tmp_path: Path) -> None:
        pilot = OParlBody.objects.create(
            external_id="https://ris.fremd.example/oparl/body/2", source=source, name="Pilotgemeinde", is_listed=False
        )
        _datei(pilot, tmp_path, "pilot.pdf")
        aufrufe: list[str] = []
        assert not file_reconcile.sample_heads(interval=0, client=_client(_liefert(PDF_ALT), aufrufe))
        assert aufrufe == []
        # Ausdrücklich angegeben, wird auch eine ausgeblendete Kommune geprüft
        ergebnis = file_reconcile.sample_heads(pilot, interval=0, client=_client(_liefert(PDF_ALT)))
        assert ergebnis == {file_reconcile.PRESENT: 1}


class TestLaufsperre:
    def test_zweiter_lauf_endet_sofort(self, body: OParlBody, tmp_path: Path) -> None:
        from io import StringIO

        from apps.common.einmalig import Sperre

        _datei(body, tmp_path, deleted=True, deleted_at=timezone.now() - timedelta(days=31))
        out, err = StringIO(), StringIO()
        with Sperre("loeschabgleich"):
            call_command("loeschabgleich", "--nur-loeschen", stdout=out, stderr=err)
        assert "läuft bereits" in err.getvalue()
        assert "purged" not in out.getvalue()
        assert not OParlFile.objects.filter(content_purged_at__isnull=False).exists()


# =============================================================================
# Kein Text gesperrter Dokumente in OParl-Ausgabe, Zusammenfassung und Suchindex
# =============================================================================


class TestKeinTextGesperrter:
    def test_zusammenfassung_ohne_gesperrte_anlage(self, body: OParlBody, tmp_path: Path) -> None:
        from insight_ai.services.summarizer import SummaryService

        paper = OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/1", body=body, name="V")
        _datei(body, tmp_path, "sichtbar.pdf", paper=paper, text_content="Sichtbarer Text")
        weg = _datei(
            body, tmp_path, "weg.pdf", paper=paper, text_content="Entfernter Text", source_missing_since=timezone.now()
        )
        dienst = SummaryService(provider=object())
        text = dienst._collect_text_content_with_extraction(paper)
        assert "Sichtbarer Text" in text
        assert "Entfernter Text" not in text
        # Gesperrt während der Erstellung: das Ergebnis wird verworfen
        started = (False, [weg.pk])
        assert SummaryService._withdrawn_since(paper, started)

    def test_suchdokument_des_vorgangs_ohne_gesperrten_text(self, body: OParlBody, tmp_path: Path) -> None:
        from insight_core.services.search_documents import paper_to_doc

        paper = OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/1", body=body, name="V")
        _datei(body, tmp_path, "sichtbar.pdf", paper=paper, text_content="Sichtbarer Text")
        _datei(
            body, tmp_path, "weg.pdf", paper=paper, text_content="Entfernter Text", source_missing_since=timezone.now()
        )
        doc = str(paper_to_doc(paper))
        assert "Sichtbarer Text" in doc
        assert "Entfernter Text" not in doc
