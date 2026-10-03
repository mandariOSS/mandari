# SPDX-License-Identifier: AGPL-3.0-or-later
"""
robots.txt (RFC 9309) für Abrufe aus Django: Dokument-Cache, Textextraktion, Vorschau, Personenfotos,
Ausnahmen je Quelle mit Pflicht-Vermerk, Bericht der gesperrten Quellen. Keine Abrufe fremder Server: die
robots.txt kommt aus ``robots._fetch`` (nachgebildet), Dateien aus httpx.MockTransport.
"""

from __future__ import annotations

import json
import uuid
from io import StringIO
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlPerson, OParlSource
from insight_core.services import document_extraction, file_cache, person_photos, robots

pytestmark = pytest.mark.django_db

NUR_DOKUMENTE_GESPERRT = b"User-Agent: *\nDisallow: /*.pdf$\n"
VERMERK = "Freigabe der Stelle per E-Mail, offizielle Anfrage läuft"


@pytest.fixture
def robots_txt(echte_robots: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """robots.txt je Host nachbilden; zählt Abrufe."""
    stand: dict[str, Any] = {"antworten": {}, "abrufe": []}

    def fetch(url: str) -> tuple[int | None, bytes]:
        host = httpx.URL(url).host
        stand["abrufe"].append(host)
        antwort: tuple[int | None, bytes] = stand["antworten"].get(host, (404, b""))
        return antwort

    monkeypatch.setattr(robots, "_fetch", fetch)
    return stand


def _quelle(sync_config: dict[str, Any] | None = None) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {uuid.uuid4().hex[:6]}",
        url=f"https://rat.example.de/oparl/{uuid.uuid4().hex[:6]}/system",
        sync_config=sync_config or {},
    )
    return OParlBody.objects.create(
        source=source,
        external_id=f"https://rat.example.de/oparl/bodies/{uuid.uuid4()}",
        name="Beispiel",
        is_listed=True,
    )


def _datei(body: OParlBody, url: str = "https://rat.example.de/dokumente/vorlage.pdf") -> OParlFile:
    return OParlFile.objects.create(
        body=body, external_id=f"https://rat.example.de/oparl/files/{uuid.uuid4()}", name="Vorlage", download_url=url
    )


def _pdf_client(gesehen: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"})

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestPruefung:
    def test_dokumentsperre_trifft_nur_dateien(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        assert not robots.check("https://rat.example.de/dokumente/vorlage.pdf").allowed
        assert robots.check("https://rat.example.de/oparl/system", "api").allowed
        assert robots.check("https://rat.example.de/getfile.asp?id=1").allowed
        # Einmal je Host abgerufen, danach aus dem Cache
        assert robots_txt["abrufe"] == ["rat.example.de"]

    def test_nicht_erreichbar_sperrt_und_letzte_gueltige_fassung_gilt_weiter(
        self, robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jetzt = [1_000_000.0]
        monkeypatch.setattr(robots, "_now", lambda: jetzt[0])
        robots_txt["antworten"]["rat.example.de"] = (503, b"")
        assert not robots.check("https://rat.example.de/a.pdf").allowed
        jetzt[0] += 16 * 60
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        assert robots.check("https://rat.example.de/a.html").allowed
        jetzt[0] += 25 * 3600
        robots_txt["antworten"]["rat.example.de"] = (None, b"")
        # Neuer Abruf scheitert: die letzte gültige Fassung gilt weiter (HTML frei, PDF gesperrt)
        assert robots.check("https://rat.example.de/a.html").allowed
        assert not robots.check("https://rat.example.de/a.pdf").allowed

    def test_ausnahme_braucht_vermerk(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        url = "https://rat.example.de/a.pdf"
        assert not robots.check(url, sync_config={"robots_override": {"scope": "files"}}).allowed
        assert robots.check(url, sync_config={"robots_override": {"scope": "files", "note": VERMERK}}).allowed
        assert not robots.check(url, sync_config={"robots_override": {"scope": "api", "note": VERMERK}}).allowed


class TestDokumentCache:
    def test_gesperrte_datei_wird_nicht_geladen(
        self, robots_txt: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        datei = _datei(_quelle())
        gesehen: list[str] = []
        assert file_cache.fetch_and_cache(datei, client=_pdf_client(gesehen)) == "robots"
        assert gesehen == []
        datei.refresh_from_db()
        assert datei.local_status == "error" and datei.local_error.startswith("robots.txt sperrt")

    def test_ausnahme_laedt_und_nutzt_eigenen_user_agent(
        self, robots_txt: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        datei = _datei(_quelle({"robots_override": {"scope": "files", "note": VERMERK}}))
        gesehen: list[str] = []
        assert file_cache.fetch_and_cache(datei, client=_pdf_client(gesehen)) == "ok"
        assert gesehen == ["https://rat.example.de/dokumente/vorlage.pdf"]
        assert file_cache.USER_AGENT == "mandari-ingestor (+https://mandari.de/crawler/; support@mandari.de)"


class TestTextextraktionUndFotos:
    def test_textextraktion_ohne_abruf(self, robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)

        def kein_client(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("gesperrtes Dokument darf nicht abgerufen werden")

        monkeypatch.setattr("insight_core.services.safe_fetch.guarded_client", kein_client)
        with pytest.raises(document_extraction.RobotsBlockedError) as fehler:
            document_extraction.download_and_extract(url="https://rat.example.de/dokumente/a.pdf")
        assert fehler.value.reason.startswith("robots.txt")

    def test_personenfoto_gesperrt(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, b"User-agent: mandari-ingestor\nDisallow: /fotos/\n")
        person = OParlPerson.objects.create(
            body=_quelle(),
            external_id=f"https://rat.example.de/oparl/persons/{uuid.uuid4()}",
            name="Beispiel",
            raw_json={"image": "https://rat.example.de/fotos/1.jpg"},
        )

        def kein_abruf(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("gesperrtes Foto darf nicht abgerufen werden")

        client = httpx.Client(transport=httpx.MockTransport(kein_abruf))
        assert person_photos.fetch_person_photo(person, client=client) == "error"
        person.refresh_from_db()
        assert person.photo_error.startswith("robots.txt sperrt")


class TestVorschau:
    def test_gesperrtes_dokument_verweist_auf_das_original(
        self, robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)

        def kein_abruf(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("gesperrtes Dokument darf nicht abgerufen werden")

        monkeypatch.setattr("insight_core.services.safe_fetch.download_to", kein_abruf)
        datei = _datei(_quelle())
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        body = response.content.decode()
        assert "robots.txt" in body
        assert "https://rat.example.de/dokumente/vorlage.pdf" in body


class TestBefehle:
    def test_bericht_nennt_gesperrte_quellen_und_regel(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        frei = _quelle()
        _datei(frei, url="https://dateien.example.org/a.pdf")
        gesperrt = _quelle()
        _datei(gesperrt)
        mit_ausnahme = _quelle({"robots_override": {"scope": "files", "note": VERMERK}})
        _datei(mit_ausnahme)
        ohne_vermerk = _quelle({"robots_override": {"scope": "files"}})
        _datei(ohne_vermerk)

        out = StringIO()
        call_command("robots_report", "--json", "--nur-gesperrte", stdout=out)
        zeilen = {zeile["id"]: zeile for zeile in json.loads(out.getvalue())}
        assert set(zeilen) == {str(gesperrt.source_id), str(ohne_vermerk.source_id)}
        assert zeilen[str(gesperrt.source_id)]["schnittstelle"]["erlaubt"] is True
        assert zeilen[str(gesperrt.source_id)]["dateien"][0]["regel"] == "Disallow: /*.pdf$"
        assert "Vermerk" in zeilen[str(ohne_vermerk.source_id)]["ausnahme_problem"]

        text = StringIO()
        call_command("robots_report", stdout=text)
        assert VERMERK in text.getvalue()
        assert "[GESPERRT]" in text.getvalue()

    def test_ausnahme_setzen_reiht_uebersprungene_dateien_neu_ein(self, robots_txt: dict[str, Any]) -> None:
        body = _quelle()
        datei = _datei(body)
        OParlFile.objects.filter(pk=datei.pk).update(
            text_extraction_status="skipped",
            text_extraction_error="robots.txt sperrt den Abruf (Disallow: /*.pdf$)",
            local_status="error",
            local_error="robots.txt sperrt den Abruf (Disallow: /*.pdf$)",
        )
        anderer_fehler = _datei(body, url="https://rat.example.de/b.pdf")
        OParlFile.objects.filter(pk=anderer_fehler.pk).update(local_status="error", local_error="HTTP 500")

        with pytest.raises(CommandError):
            call_command("robots_override", str(body.source_id), "--scope", "files", "--note", "kurz")

        call_command("robots_override", str(body.source_id), "--scope", "files", "--note", VERMERK, stdout=StringIO())
        body.source.refresh_from_db()
        assert body.source.sync_config["robots_override"] == {"scope": "files", "note": VERMERK}
        datei.refresh_from_db()
        anderer_fehler.refresh_from_db()
        assert (datei.text_extraction_status, datei.local_status) == ("pending", "none")
        assert anderer_fehler.local_status == "error"

        call_command("robots_override", body.source.url, "--entfernen", stdout=StringIO())
        body.source.refresh_from_db()
        assert "robots_override" not in body.source.sync_config
