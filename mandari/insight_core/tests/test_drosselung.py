# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drosselung und Grenzen der öffentlichen Insight-Endpunkte.

- KI-Zusammenfassung: Erzeugen nur per POST, je IP und Tag begrenzt, mit Tagesbudget und einer
  Sperre je Vorgang; die Datenbankverbindung ist während des KI-Aufrufs frei.
- Formulare, die E-Mails auslösen (Kontakt, Abos, Beschluss-Abos, Ratsfragen): je IP und je
  Empfängeradresse gedrosselt; Bestätigungsmails enthalten keine frei eingegebenen Texte.
- Kachel-Proxy und Dateivorschau: nur gültige Kacheln, Drosselung, Obergrenzen für Cache,
  Dateigröße, Dauer und gleichzeitige Abrufe; Abrufe nur an öffentliche Adressen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from django.core import mail
from django.test import Client

from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlPerson, OParlSource, TileCache
from insight_core.services import file_cache, safe_fetch

pytestmark = pytest.mark.django_db


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Fremd-RIS", url="https://ris.fremd.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=source, name="Beispielstadt", slug="beispiel"
    )


def _ip(nummer: int) -> dict[str, Any]:
    return {"REMOTE_ADDR": f"198.51.100.{nummer}"}


# =============================================================================
# KI-Zusammenfassung
# =============================================================================


@dataclass
class _Antwort:
    content: str
    input_tokens: int = 1
    output_tokens: int = 1


class _Provider:
    def __init__(self, fehler: Exception | None = None, ereignisse: list[str] | None = None) -> None:
        self.aufrufe = 0
        self.fehler = fehler
        self.ereignisse = ereignisse if ereignisse is not None else []

    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
        self.aufrufe += 1
        self.ereignisse.append("ki")
        if self.fehler:
            raise self.fehler
        return _Antwort("Kurzfassung des Vorgangs")


@pytest.fixture
def provider(monkeypatch: Any) -> _Provider:
    anbieter = _Provider()
    monkeypatch.setattr("insight_ai.services.summarizer.NebiusProvider", lambda: anbieter)
    return anbieter


def _vorgang(body: OParlBody, nummer: int = 1) -> OParlPaper:
    paper = OParlPaper.objects.create(
        external_id=f"https://ris.fremd.example/oparl/paper/{nummer}", body=body, name=f"Vorgang {nummer}"
    )
    OParlFile.objects.create(
        external_id=f"https://ris.fremd.example/oparl/file/{nummer}",
        body=body,
        paper=paper,
        name="Vorlage",
        text_content="Der Rat beschließt den Radweg.",
    )
    return paper


def _url(paper: OParlPaper) -> str:
    return f"/insight/vorgaenge/{paper.id}/zusammenfassung/"


class TestZusammenfassung:
    def test_get_erzeugt_nichts(self, body: OParlBody, provider: _Provider) -> None:
        paper = _vorgang(body)
        response = Client().get(_url(paper))
        assert response.status_code == 200
        assert provider.aufrufe == 0
        assert 'hx-post="' in response.content.decode()

    def test_get_liefert_gespeicherte(self, body: OParlBody, provider: _Provider) -> None:
        paper = _vorgang(body)
        OParlPaper.objects.filter(pk=paper.pk).update(summary="Schon da")
        assert "Schon da" in Client().get(_url(paper)).content.decode()
        assert provider.aufrufe == 0

    def test_post_erzeugt_und_speichert(self, body: OParlBody, provider: _Provider) -> None:
        paper = _vorgang(body)
        response = Client().post(_url(paper))
        assert response.status_code == 200
        assert "Kurzfassung des Vorgangs" in response.content.decode()
        paper.refresh_from_db()
        assert paper.summary == "Kurzfassung des Vorgangs"
        # Die gespeicherte Fassung kostet nichts mehr
        Client().post(_url(paper))
        assert provider.aufrufe == 1

    def test_je_ip_begrenzt(self, body: OParlBody, provider: _Provider, settings: Any) -> None:
        settings.INSIGHT_SUMMARY_PER_IP_HOUR = 2
        client = Client()
        antworten = [client.post(_url(_vorgang(body, n)), **_ip(1)) for n in range(1, 4)]
        assert [a.status_code for a in antworten] == [200, 200, 429]
        assert provider.aufrufe == 2
        # Andere Adresse ist nicht betroffen
        assert client.post(_url(_vorgang(body, 9)), **_ip(2)).status_code == 200

    def test_tagesbudget(self, body: OParlBody, provider: _Provider, settings: Any) -> None:
        settings.INSIGHT_SUMMARY_DAILY_LIMIT = 1
        client = Client()
        assert client.post(_url(_vorgang(body, 1)), **_ip(1)).status_code == 200
        assert client.post(_url(_vorgang(body, 2)), **_ip(2)).status_code == 429
        assert provider.aufrufe == 1

    def test_sperre_je_vorgang(self, body: OParlBody, provider: _Provider) -> None:
        from insight_core.services import summary_guard

        paper = _vorgang(body)
        assert summary_guard.acquire(paper.pk)
        response = Client().post(_url(paper))
        assert response.status_code == 409
        assert "gerade erstellt" in response.content.decode()
        assert provider.aufrufe == 0

    def test_kein_ausnahmetext(self, body: OParlBody, monkeypatch: Any) -> None:
        anbieter = _Provider(fehler=RuntimeError("Token sk-geheim-123 abgelehnt"))
        monkeypatch.setattr("insight_ai.services.summarizer.NebiusProvider", lambda: anbieter)
        response = Client().post(_url(_vorgang(body)))
        assert "sk-geheim" not in response.content.decode()

    def test_verbindung_waehrend_ki_aufruf_frei(self, body: OParlBody, monkeypatch: Any) -> None:
        ereignisse: list[str] = []
        anbieter = _Provider(ereignisse=ereignisse)
        monkeypatch.setattr("insight_ai.services.summarizer.NebiusProvider", lambda: anbieter)
        monkeypatch.setattr(
            "insight_ai.services.summarizer.release_idle_thread_connections", lambda: ereignisse.append("frei")
        )
        Client().post(_url(_vorgang(body)))
        assert ereignisse[:2] == ["frei", "ki"]

    def test_ruecknahme_waehrend_der_erstellung(self, body: OParlBody, monkeypatch: Any) -> None:
        paper = _vorgang(body)
        anlage = paper.files.get()

        class _Zuruecknehmend(_Provider):
            def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
                OParlFile.objects.filter(pk=anlage.pk).update(deleted=True)
                return super().chat_completion(messages, **kwargs)

        monkeypatch.setattr("insight_ai.services.summarizer.NebiusProvider", lambda: _Zuruecknehmend())
        response = Client().post(_url(paper))
        assert "Kurzfassung des Vorgangs" not in response.content.decode()
        paper.refresh_from_db()
        assert not paper.summary

    def test_ohne_text_kein_erneuter_versuch(self, body: OParlBody, provider: _Provider, monkeypatch: Any) -> None:
        paper = OParlPaper.objects.create(external_id="https://ris.fremd.example/oparl/paper/77", body=body, name="X")
        aufrufe: list[str] = []

        def ohne_text(self: Any, p: Any) -> str:
            aufrufe.append("text")
            return ""

        monkeypatch.setattr(
            "insight_ai.services.summarizer.SummaryService._collect_text_content_with_extraction", ohne_text
        )
        client = Client()
        client.post(_url(paper))
        client.post(_url(paper))
        assert aufrufe == ["text"]
        assert provider.aufrufe == 0


# =============================================================================
# Formulare mit E-Mail-Versand
# =============================================================================


def _mailtext(nachricht: Any) -> str:
    """Betreff, Text- und HTML-Teil einer E-Mail."""
    teile = [str(nachricht.subject), str(nachricht.body)]
    teile += [str(alternative[0]) for alternative in getattr(nachricht, "alternatives", [])]
    return "\n".join(teile)


def _kontakt(client: Client, email: str = "opfer@example.org", **extra: Any) -> Any:
    daten = {
        "name": "Klick hier: https://phish.example",
        "email": email,
        "subject": "sonstiges",
        "message": "Ihr Konto wird gesperrt, bitte https://phish.example besuchen",
    }
    return client.post("/api/contact/", json.dumps(daten), content_type="application/json", **extra)


class TestMailFormulare:
    def test_kontakt_je_ip_gedrosselt(self, settings: Any) -> None:
        settings.INSIGHT_MAILS_PER_IP_HOUR = 3
        client = Client()
        codes = [_kontakt(client, f"person{n}@example.org", **_ip(1)).status_code for n in range(5)]
        assert codes == [201, 201, 201, 429, 429]

    def test_kontakt_bestaetigung_ohne_freitext(self) -> None:
        mail.outbox.clear()
        _kontakt(Client())
        bestaetigung = [m for m in mail.outbox if m.to == ["opfer@example.org"]]
        assert len(bestaetigung) == 1
        assert "phish.example" not in _mailtext(bestaetigung[0])

    def test_kontakt_je_empfaenger_gedrosselt(self, settings: Any) -> None:
        settings.INSIGHT_MAILS_PER_ADDRESS_DAY = 2
        mail.outbox.clear()
        for n in range(4):
            _kontakt(Client(), **_ip(n + 10))
        assert len([m for m in mail.outbox if m.to == ["opfer@example.org"]]) == 2

    def test_abo_je_ip_gedrosselt(self, body: OParlBody, settings: Any) -> None:
        settings.INSIGHT_MAILS_PER_IP_HOUR = 2
        client = Client()
        client.get(f"/insight/kommune/{body.id}/")
        mail.outbox.clear()
        for n in range(4):
            client.post("/insight/benachrichtigungen/", {"email": f"a{n}@example.org", "keyword": "Radweg"}, **_ip(1))
        assert len(mail.outbox) == 2

    def test_abo_bestaetigung_ohne_freitext(self, body: OParlBody) -> None:
        client = Client()
        client.get(f"/insight/kommune/{body.id}/")
        mail.outbox.clear()
        client.post(
            "/insight/benachrichtigungen/",
            {"email": "opfer@example.org", "keyword": "Konto gesperrt: https://phish.example"},
        )
        assert len(mail.outbox) == 1
        assert "phish.example" not in _mailtext(mail.outbox[0])

    def test_ratsfrage_je_ip_gedrosselt(self, body: OParlBody, settings: Any, monkeypatch: Any) -> None:
        from insight_core.services import question_service

        settings.INSIGHT_MAILS_PER_IP_HOUR = 1
        person = OParlPerson.objects.create(
            external_id="https://ris.fremd.example/oparl/person/1", body=body, name="Anna Rat", family_name="Rat"
        )
        monkeypatch.setattr(question_service, "is_mandate_holder", lambda p: True)
        client = Client()
        client.get(f"/insight/kommune/{body.id}/")
        mail.outbox.clear()
        for n in range(3):
            client.post(
                f"/insight/personen/{person.id}/frage-stellen/",
                {
                    "questioner_name": "Frieda",
                    "questioner_email": f"f{n}@example.org",
                    "subject": "Radweg",
                    "question_text": "Wann kommt der Radweg an der Hauptstraße? " * 3,
                    "topic": "verkehr",
                    "privacy_accepted": "on",
                },
                **_ip(1),
            )
        assert len(mail.outbox) <= 1

    def test_ratsfrage_bestaetigung_ohne_freitext(self, body: OParlBody) -> None:
        from insight_core.models import PublicQuestion
        from insight_core.services import question_service

        person = OParlPerson.objects.create(
            external_id="https://ris.fremd.example/oparl/person/2", body=body, name="Anna Rat", family_name="Rat"
        )
        frage = PublicQuestion.objects.create(
            body=body,
            recipient=person,
            questioner_name="Klick https://phish.example",
            questioner_email="opfer@example.org",
            subject="Konto gesperrt https://phish.example",
            question_text="Bitte https://phish.example besuchen",
        )
        mail.outbox.clear()
        question_service.send_verification_email(frage)
        assert "phish.example" not in _mailtext(mail.outbox[0])


# =============================================================================
# Kachel-Proxy
# =============================================================================


PNG = b"\x89PNG\r\n\x1a\nkachel"


@pytest.fixture
def osm(monkeypatch: Any) -> list[str]:
    abrufe: list[str] = []

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            abrufe.append(url)
            return httpx.Response(200, content=PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr("insight_core.views.maps.httpx.Client", _Client)
    return abrufe


class TestKachelProxy:
    @pytest.mark.parametrize("pfad", ["20/0/0", "25/1/1", "3/8/0", "3/0/8", "0/1/0"])
    def test_ungueltige_kacheln(self, osm: list[str], pfad: str) -> None:
        assert Client().get(f"/insight/tiles/{pfad}").status_code == 404
        assert osm == []
        assert TileCache.objects.count() == 0

    def test_gueltige_kachel(self, osm: list[str]) -> None:
        response = Client().get("/insight/tiles/3/4/2")
        assert response.status_code == 200
        assert response.content == PNG
        assert TileCache.objects.count() == 1

    def test_abrufe_je_ip_gedrosselt(self, osm: list[str], settings: Any) -> None:
        settings.INSIGHT_TILE_FETCHES_PER_IP_MINUTE = 2
        client = Client()
        codes = [client.get(f"/insight/tiles/5/{x}/1", **_ip(1)).status_code for x in range(4)]
        assert codes == [200, 200, 429, 429]
        assert len(osm) == 2
        # Kacheln aus dem Cache bleiben erreichbar
        assert client.get("/insight/tiles/5/0/1", **_ip(1)).status_code == 200

    def test_cache_obergrenze(self, osm: list[str], settings: Any) -> None:
        settings.INSIGHT_TILE_CACHE_MAX_TILES = 1
        client = Client()
        assert client.get("/insight/tiles/5/1/1").status_code == 200
        assert client.get("/insight/tiles/5/2/1").status_code == 200
        assert TileCache.objects.count() == 1


# =============================================================================
# Dateivorschau
# =============================================================================


def _datei(body: OParlBody, url: str = "https://ris.fremd.example/files/vorlage.pdf") -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"https://ris.fremd.example/oparl/file/{url.rsplit('/', 1)[-1]}",
        body=body,
        name="Vorlage",
        file_name="vorlage.pdf",
        mime_type="application/pdf",
        download_url=url,
    )


@pytest.fixture
def quelle(monkeypatch: Any, tmp_path: Path) -> dict[str, Any]:
    """
    Quell-RIS auf Transportebene: Jede ausgehende Verbindung landet hier statt im Netz.

    ``abrufe`` hält jede Adresse fest, die der Server tatsächlich abrufen wollte; ``antworten``
    bestimmt die Antwort je URL (sonst ein kleines PDF).
    """
    zustand: dict[str, Any] = {"abrufe": [], "antworten": {}}

    def handle_request(self: Any, request: httpx.Request) -> httpx.Response:
        zustand["abrufe"].append(str(request.url))
        antwort = zustand["antworten"].get(str(request.url))
        if antwort is None:
            return httpx.Response(200, content=b"%PDF-1.4 klein", headers={"content-type": "application/pdf"})
        return cast(httpx.Response, antwort)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    # Testadressen (*.example) lösen nicht auf; die Prüfung sieht dann eine öffentliche Adresse
    monkeypatch.setattr(safe_fetch, "_resolve", lambda host: ["93.184.215.14"])
    monkeypatch.setattr(file_cache, "cache_root", lambda: tmp_path)
    return zustand


class TestDateivorschau:
    def test_zu_gross_laut_kopf(self, body: OParlBody, quelle: dict[str, Any], settings: Any) -> None:
        settings.FILE_CACHE_MAX_MB = 1
        datei = _datei(body)
        quelle["antworten"][datei.download_url] = httpx.Response(
            200, content=b"%PDF" + b"0" * 10, headers={"content-type": "application/pdf", "content-length": "5000000"}
        )
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 413
        assert b"%PDF" not in response.content

    def test_zu_gross_beim_lesen(self, body: OParlBody, quelle: dict[str, Any], settings: Any) -> None:
        settings.FILE_CACHE_MAX_MB = 1
        datei = _datei(body)
        quelle["antworten"][datei.download_url] = httpx.Response(
            200, content=b"%PDF" + b"0" * (2 * 1024 * 1024), headers={"content-type": "application/pdf"}
        )
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 413

    def test_normale_datei(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        datei = _datei(body)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"%PDF-1.4 klein"  # type: ignore[attr-defined]
        assert response["Content-Type"] == "application/pdf"

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8000/admin/",
            "http://[::1]/",
            "http://10.0.0.5/intern.pdf",
            "file:///etc/passwd",
        ],
    )
    def test_keine_internen_ziele(self, body: OParlBody, quelle: dict[str, Any], url: str) -> None:
        datei = _datei(body, url)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert quelle["abrufe"] == []
        assert response.status_code != 200 or b"%PDF" not in response.content

    def test_keine_weiterleitung_auf_interne_ziele(self, body: OParlBody, quelle: dict[str, Any]) -> None:
        datei = _datei(body)
        quelle["antworten"][datei.download_url] = httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert quelle["abrufe"] == [datei.download_url]
        assert b"%PDF" not in response.content

    def test_liveabrufe_je_ip_gedrosselt(self, body: OParlBody, quelle: dict[str, Any], settings: Any) -> None:
        settings.FILE_PROXY_FETCHES_PER_IP_MINUTE = 1
        settings.FILE_CACHE_MIN_FREE_GB = 10**6  # nichts zwischenspeichern: jeder Aufruf geht an die Quelle
        datei = _datei(body)
        client = Client()
        assert client.get(f"/insight/dokumente/{datei.id}/preview/", **_ip(1)).status_code == 200
        assert client.get(f"/insight/dokumente/{datei.id}/preview/", **_ip(1)).status_code == 429
        assert len(quelle["abrufe"]) == 1

    def test_gleichzeitige_abrufe_begrenzt(self, body: OParlBody, quelle: dict[str, Any], monkeypatch: Any) -> None:
        from insight_core.views import files

        monkeypatch.setattr(files, "_LIVE_FETCH_SLOTS", _BesetzteSlots())
        datei = _datei(body)
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 503
        assert quelle["abrufe"] == []

    def test_gesamtdauer_begrenzt(self, body: OParlBody, quelle: dict[str, Any], monkeypatch: Any) -> None:
        uhr = iter([0.0])
        monkeypatch.setattr(safe_fetch, "_now", lambda: next(uhr, 1000.0))
        datei = _datei(body)
        quelle["antworten"][datei.download_url] = httpx.Response(
            200, content=b"%PDF" + b"0" * (256 * 1024), headers={"content-type": "application/pdf"}
        )
        response = Client().get(f"/insight/dokumente/{datei.id}/preview/")
        assert response.status_code == 504


class _BesetzteSlots:
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("nicht belegt")
