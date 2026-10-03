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
    """robots.txt je Host nachbilden (Antwort oder Funktion des User-Agents); zählt Abrufe und User-Agents."""
    stand: dict[str, Any] = {"antworten": {}, "abrufe": [], "agents": []}

    def fetch(url: str, agent: str = robots.USER_AGENT) -> tuple[int | None, bytes]:
        host = httpx.URL(url).host
        stand["abrufe"].append(host)
        stand["agents"].append(agent)
        antwort = stand["antworten"].get(host, (404, b""))
        ergebnis: tuple[int | None, bytes] = antwort(agent) if callable(antwort) else antwort
        return ergebnis

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
        decision = robots.check("https://rat.example.de/a.pdf")
        assert not decision.allowed and decision.unreachable and not decision.blocked
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


def _wortfilter(agent: str) -> tuple[int, bytes]:
    """Server, der „crawler“ im User-Agent mit 403 beantwortet, auch für /robots.txt."""
    return (403, b"") if "crawler" in agent else (200, NUR_DOKUMENTE_GESPERRT)


KENNUNG_OHNE_INFOSEITE = "mandari-ingestor (+https://mandari.de; support@mandari.de)"


class TestUserAgentDerQuelle:
    def test_robots_txt_mit_dem_user_agent_des_abrufs(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = _wortfilter
        url = "https://rat.example.de/dokumente/a.pdf"
        # Standard: 403 auf /robots.txt, gilt als „nicht vorhanden“
        assert robots.check(url).allowed
        # Mit der Kennung der Quelle kommt die echte robots.txt, und die sperrt Dokumente
        assert not robots.check(url, agent=KENNUNG_OHNE_INFOSEITE).allowed
        assert robots_txt["agents"] == [robots.USER_AGENT, KENNUNG_OHNE_INFOSEITE]

    def test_user_agent_aus_den_download_headern(self) -> None:
        body = _quelle({"download_headers": {"user-agent": KENNUNG_OHNE_INFOSEITE, "Referer": "https://x/"}})
        assert robots.user_agent_for(_datei(body)) == KENNUNG_OHNE_INFOSEITE
        assert robots.user_agent_for(_datei(_quelle())) == robots.USER_AGENT

    def test_dokument_cache_prueft_mit_der_kennung_der_quelle(
        self, robots_txt: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["antworten"]["rat.example.de"] = _wortfilter
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        datei = _datei(_quelle({"download_headers": {"User-Agent": KENNUNG_OHNE_INFOSEITE}}))
        gesehen: list[str] = []
        assert file_cache.fetch_and_cache(datei, client=_pdf_client(gesehen)) == "robots"
        assert gesehen == []


class TestNichtErreichbar:
    """5xx, 429 oder Netzfehler bei der robots.txt: zurückstellen, nicht als gesperrt überspringen."""

    @pytest.fixture(autouse=True)
    def _stoerung(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (503, b"")

    def test_dokument_cache_vermerkt_nichts(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
        datei = _datei(_quelle())
        gesehen: list[str] = []
        assert file_cache.fetch_and_cache(datei, client=_pdf_client(gesehen)) == "deferred"
        assert gesehen == []
        datei.refresh_from_db()
        assert (datei.local_status, datei.local_error) == ("none", "")

    def test_textextraktion_bleibt_wartend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kein_client(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("ohne erreichbare robots.txt wird nichts geladen")

        monkeypatch.setattr("insight_core.services.safe_fetch.guarded_client", kein_client)
        with pytest.raises(document_extraction.RobotsUnreachableError) as fehler:
            document_extraction.download_and_extract(url="https://rat.example.de/dokumente/a.pdf")
        assert not isinstance(fehler.value, document_extraction.RobotsBlockedError)

        from insight_core.management.commands.extract_texts import Command as ExtractTexts

        datei = _datei(_quelle())
        ergebnis = ExtractTexts()._process_file(datei, False)
        assert ergebnis["deferred"] is True and not ergebnis.get("skipped")
        datei.refresh_from_db()
        assert datei.text_extraction_status == "pending" and not datei.text_extraction_error

    def test_personenfoto_vermerkt_nichts(self) -> None:
        person = OParlPerson.objects.create(
            body=_quelle(),
            external_id=f"https://rat.example.de/oparl/persons/{uuid.uuid4()}",
            name="Beispiel",
            raw_json={"image": "https://rat.example.de/fotos/1.jpg"},
        )

        def kein_abruf(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("ohne erreichbare robots.txt wird nichts geladen")

        client = httpx.Client(transport=httpx.MockTransport(kein_abruf))
        assert person_photos.fetch_person_photo(person, client=client) == "deferred"
        person.refresh_from_db()
        assert person.photo_fetched_at is None and not person.photo_error

    def test_vorschau_verweist_auf_das_original_mit_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kein_abruf(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("ohne erreichbare robots.txt wird nichts geladen")

        monkeypatch.setattr("insight_core.services.safe_fetch.download_to", kein_abruf)
        datei = _datei(_quelle())
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 503
        body = response.content.decode()
        assert "nicht erreichbar" in body and "untersagt" not in body
        assert "https://rat.example.de/dokumente/vorlage.pdf" in body


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
        assert person_photos.USER_AGENT == file_cache.USER_AGENT


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

    def test_personenfoto_mit_unserer_kennung(self, robots_txt: dict[str, Any]) -> None:
        person = OParlPerson.objects.create(
            body=_quelle(),
            external_id=f"https://rat.example.de/oparl/persons/{uuid.uuid4()}",
            name="Beispiel",
            raw_json={"image": "https://rat.example.de/fotos/1.jpg"},
        )
        agents: list[str] = []

        def foto(request: httpx.Request) -> httpx.Response:
            agents.append(request.headers["user-agent"])
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(foto), headers={"User-Agent": "Mozilla/5.0"})
        assert person_photos.fetch_person_photo(person, client=client) == "missing"
        assert agents == [robots.USER_AGENT]
        assert "Mozilla" not in person_photos.USER_AGENT


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


class TestVorschauReihenfolge:
    def test_geschonte_quelle_fragt_keine_robots_txt_an(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, NUR_DOKUMENTE_GESPERRT)
        body = _quelle()
        OParlSource.objects.filter(pk=body.source_id).update(consecutive_failures=99)
        datei = _datei(body)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 503
        assert robots_txt["abrufe"] == [], "Quellen-Schonung gilt vor dem Abruf der robots.txt"

    def test_robots_txt_ohne_gehaltene_datenbankverbindung(
        self, robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apps.common import db_connections

        ablauf: list[str] = []
        echte_freigabe = db_connections.release_idle_thread_connections

        def freigabe() -> None:
            ablauf.append("freigabe")
            echte_freigabe()

        def antwort(agent: str) -> tuple[int, bytes]:
            ablauf.append("robots.txt")
            return (200, NUR_DOKUMENTE_GESPERRT)

        monkeypatch.setattr(db_connections, "release_idle_thread_connections", freigabe)
        robots_txt["antworten"]["rat.example.de"] = antwort
        datei = _datei(_quelle())
        Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert ablauf[:2] == ["freigabe", "robots.txt"]


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
        # Die Quelle, die wir per Ausnahme gegen die robots.txt laden, steht auf der Liste
        assert set(zeilen) == {str(gesperrt.source_id), str(ohne_vermerk.source_id), str(mit_ausnahme.source_id)}
        assert zeilen[str(gesperrt.source_id)]["schnittstelle"]["erlaubt"] is True
        assert zeilen[str(gesperrt.source_id)]["dateien"][0]["regel"] == "Disallow: /*.pdf$"
        assert zeilen[str(gesperrt.source_id)]["gesperrt"] is True
        assert "Vermerk" in zeilen[str(ohne_vermerk.source_id)]["ausnahme_problem"]

        ausnahme = zeilen[str(mit_ausnahme.source_id)]
        assert ausnahme["robots_sperrt"] is True and ausnahme["gesperrt"] is False
        datei = ausnahme["dateien"][0]
        assert datei["erlaubt"] is True and datei["robots_erlaubt"] is False
        assert datei["regel"] == "Disallow: /*.pdf$" and datei["ausnahme_aktiv"] is True
        assert ausnahme["ausnahme"]["vermerk"] == VERMERK

        text = StringIO()
        call_command("robots_report", stdout=text)
        ausgabe = text.getvalue()
        assert VERMERK in ausgabe
        assert "[GESPERRT]" in ausgabe and "[AUSNAHME]" in ausgabe
        assert "gesperrt (Disallow: /*.pdf$), Ausnahme aktiv" in ausgabe

    def test_bericht_nennt_nicht_erreichbare_robots_txt(self, robots_txt: dict[str, Any]) -> None:
        robots_txt["antworten"]["rat.example.de"] = (503, b"")
        _datei(_quelle())
        out = StringIO()
        call_command("robots_report", "--json", "--nur-gesperrte", stdout=out)
        zeile = json.loads(out.getvalue())[0]
        assert zeile["nicht_erreichbar"] is True and zeile["robots_sperrt"] is False
        assert zeile["schnittstelle"]["robots_status"] == 503

    def test_quelle_anlegen_beachtet_robots_txt(
        self, robots_txt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        robots_txt["antworten"]["rat.example.de"] = (200, b"User-agent: *\nDisallow: /oparl/\n")

        def kein_abruf(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("gesperrte Schnittstelle darf nicht abgerufen werden")

        monkeypatch.setattr("insight_core.management.commands.add_oparl_source.httpx.Client", kein_abruf)
        out = StringIO()
        with pytest.raises(CommandError):
            call_command("add_oparl_source", "https://rat.example.de/oparl/system", stdout=out)
        assert "robots.txt sperrt" in out.getvalue()

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
