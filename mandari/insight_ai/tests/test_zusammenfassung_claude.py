# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassungen mit der Claude API (Anthropic): Auflösung, Einzelaufruf, Kosten, Monatsbudget, Message Batches.

- Nur die Zusammenfassungen nutzen Claude; KI-Assistent und KI-Verortung bleiben beim Endpunkt des Bürgerportals.
- Fail-closed: ohne Host in ``KI_ERLAUBTE_HOSTS``, ohne Schlüssel, mit Schalter aus und in der Demo keine Anfrage und
  kein stiller Rückfall auf einen anderen Anbieter.
- Unter jeder Zusammenfassung steht, wer sie erstellt hat (``summary_hinweis``).
- Der Batch reicht nur Vorgänge mit Text ein, so viele das Budget trägt, und übernimmt ein Ergebnis nur, wenn es noch
  passt (kein neuer Text, keine Rücknahme, keine vorhandene Zusammenfassung).

Kein Netz: Der SDK-Client ist ein Ersatz. Nur erfundene Namen.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx2
import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.admin import AISettingsAdminForm
from apps.common.ki_anbieter import (
    ANTHROPIC,
    ANTHROPIC_HOST,
    ClaudeEndpunkt,
    claude_fuer_zusammenfassung,
    claude_host_freigegeben,
    erlaubte_hosts,
    pruefe_basis_url,
    pruefe_claude,
    pruefe_ki_einstellungen,
)
from apps.common.models import AISettings
from insight_ai import kosten
from insight_ai.models import KiVerbrauch, TagesbatchKommune, ZusammenfassungsBatch
from insight_ai.providers import NichtEingerichtet, get_insight_provider, get_summary_provider
from insight_ai.providers.base import ChatMessage
from insight_ai.providers.claude import BudgetErschoepftError, ClaudeZusammenfassung, antwort_text
from insight_ai.providers.openai_kompatibel import KiAnbieterError, OpenAIKompatiblerProvider
from insight_ai.services import zusammenfassung_batch
from insight_ai.services.summarizer import BudgetExhaustedError, SummaryService
from insight_core import publication
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SCHLUESSEL = "sk-ant-test-nur-fuer-tests-0123456789abcdef"
_nummer = count()


# --- Ersatz für den SDK-Client --------------------------------------------------------------------------------------


def _usage(eingabe: int = 10_000, ausgabe: int = 1_000) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=eingabe, output_tokens=ausgabe, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )


def _nachricht(text: str = "Kurzfassung des Vorgangs.", *, stop: str = "end_turn", **usage: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model="claude-haiku-5-5",
        usage=_usage(**usage),
    )


def _als_json(nachricht: Any) -> Any:
    """Antwort auf eine Anfrage mit Schema: der Text als JSON (Zusammenfassung, leere Einfache Sprache)."""
    bloecke = list(getattr(nachricht, "content", None) or [])
    if not any(getattr(b, "type", "") == "text" and getattr(b, "text", "").startswith("{") for b in bloecke):
        bloecke = [
            SimpleNamespace(type="text", text=json.dumps({"zusammenfassung": b.text, "einfache_sprache": ""}))
            if getattr(b, "type", "") == "text" and getattr(b, "text", "")
            else b
            for b in bloecke
        ]
    return SimpleNamespace(**{**vars(nachricht), "content": bloecke})


class _Batches:
    def __init__(self) -> None:
        self.eingereicht: list[list[Any]] = []
        self.abgebrochen: list[str] = []
        self.stand = "in_progress"
        self.ergebnisse: list[Any] = []
        #: Ausnahme beim Abruf des Stands (``None``: keine)
        self.fehler: Exception | None = None

    def create(self, *, requests: list[Any]) -> SimpleNamespace:
        self.eingereicht.append(list(requests))
        return SimpleNamespace(id=f"msgbatch_{len(self.eingereicht)}")

    def cancel(self, batch_id: str) -> SimpleNamespace:
        self.abgebrochen.append(batch_id)
        return SimpleNamespace(id=batch_id, processing_status="canceling")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        if self.fehler is not None:
            raise self.fehler
        return SimpleNamespace(id=batch_id, processing_status=self.stand)

    def results(self, batch_id: str) -> list[Any]:
        return list(self.ergebnisse)


class _Client:
    def __init__(self, antwort: Any = None) -> None:
        self.anfragen: list[dict[str, Any]] = []
        self.antwort = antwort if antwort is not None else _nachricht()
        self.batches = _Batches()
        self.messages = SimpleNamespace(create=self._create, batches=self.batches)
        self.models = SimpleNamespace(retrieve=lambda modell: SimpleNamespace(id=modell))

    def _create(self, **params: Any) -> Any:
        self.anfragen.append(params)
        if isinstance(self.antwort, Exception):
            raise self.antwort
        if "format" in (params.get("output_config") or {}):
            return _als_json(self.antwort)
        return self.antwort


# --- Einrichtung und Testdaten --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sauber(settings: Any) -> None:
    settings.KI_ERLAUBTE_HOSTS = [ANTHROPIC_HOST]
    cache.clear()


def _einrichten(**werte: Any) -> AISettings:
    ki = AISettings.get_settings()
    ki.insight_enabled = True
    ki.summary_provider = ANTHROPIC
    ki.summary_model = "claude-haiku-5-5"
    ki.insight_max_output_tokens = 4096
    for name, wert in werte.items():
        setattr(ki, name, wert)
    ki.set_summary_api_key(SCHLUESSEL)
    ki.save()
    return ki


def _endpunkt(**werte: Any) -> ClaudeEndpunkt:
    angaben: dict[str, Any] = {
        "api_key": SCHLUESSEL,
        "modell": "claude-haiku-5-5",
        "max_output_tokens": 4096,
        "batch": True,
        "batch_tage": 30,
        "monatsbudget_usd": Decimal("100"),
        **werte,
    }
    return ClaudeEndpunkt(**angaben)


@pytest.fixture
def body() -> OParlBody:
    """Kommune im Tagesbatch (Vorgänge der letzten 30 Tage, ohne Prüfung der Verortung; die prüft test_tagesbatch)."""
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    kommune = OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt")
    TagesbatchKommune.objects.create(
        body=kommune, ab=timezone.localdate() - timedelta(days=30), verortung_pruefen=False
    )
    return kommune


def _vorgang(
    body: OParlBody, *, tage: int = 1, text: str | None = "Der Rat beschließt den Bau einer Brücke.", **felder: Any
) -> OParlPaper:
    nummer = next(_nummer)
    vorgang = OParlPaper.objects.create(
        external_id=f"https://ris.example.org/oparl/paper/{nummer}",
        body=body,
        name=f"Vorlage {nummer}",
        date=timezone.localdate() - timedelta(days=tage),
        **felder,
    )
    if text is not None:
        OParlFile.objects.create(
            external_id=f"https://ris.example.org/oparl/file/{nummer}",
            body=body,
            paper=vorgang,
            name="Anlage",
            download_url=f"https://ris.example.org/dokumente/{nummer}.pdf",
            mime_type="application/pdf",
            text_extraction_status="completed",
            text_extracted_at=timezone.now() - timedelta(days=1),
            text_content=text,
        )
    return vorgang


# --- Auflösung ------------------------------------------------------------------------------------------------------


class TestAufloesung:
    def test_ohne_auswahl_wie_buergerportal(self) -> None:
        assert isinstance(get_summary_provider(), NichtEingerichtet)
        assert claude_fuer_zusammenfassung() is None

    def test_claude_nur_fuer_zusammenfassungen(self, settings: Any) -> None:
        settings.KI_ERLAUBTE_HOSTS = [ANTHROPIC_HOST, "api.openai-compat.model-serving.eu01.onstackit.cloud"]
        ki = _einrichten(provider="stackit", insight_model="modell-portal")
        ki.set_api_key("anderer-schluessel")
        ki.save()
        anbieter = get_summary_provider()
        assert isinstance(anbieter, ClaudeZusammenfassung)
        assert anbieter.model_name == "claude-haiku-5-5"
        # KI-Assistent und KI-Verortung bleiben beim OpenAI-kompatiblen Endpunkt
        assert isinstance(get_insight_provider(), OpenAIKompatiblerProvider)

    def test_claude_host_gilt_nur_fuer_zusammenfassungen(self, settings: Any) -> None:
        # Steht der Claude-Host in der Positivliste, nutzen ihn trotzdem weder Assistent, Verortung noch OCR
        settings.KI_ERLAUBTE_HOSTS = [ANTHROPIC_HOST, "api.mistral.ai"]
        assert erlaubte_hosts() == ("api.mistral.ai",)
        assert claude_host_freigegeben()
        with pytest.raises(ValidationError):
            pruefe_basis_url(f"https://{ANTHROPIC_HOST}/v1")

    def test_host_nicht_freigegeben_kein_rueckfall(self, settings: Any) -> None:
        settings.KI_ERLAUBTE_HOSTS = ["api.openai-compat.model-serving.eu01.onstackit.cloud"]
        ki = _einrichten(provider="stackit")
        ki.set_api_key("anderer-schluessel")
        ki.save()
        assert claude_fuer_zusammenfassung() is None
        assert isinstance(get_summary_provider(), NichtEingerichtet)

    @pytest.mark.parametrize(
        "werte", [{"insight_enabled": False}, {"summary_model": " "}], ids=["schalter-aus", "ohne-modell"]
    )
    def test_unvollstaendig_aus(self, werte: dict[str, Any]) -> None:
        _einrichten(**werte)
        assert isinstance(get_summary_provider(), NichtEingerichtet)

    def test_ohne_schluessel_aus(self) -> None:
        ki = _einrichten()
        ki.set_summary_api_key("")
        ki.save()
        assert isinstance(get_summary_provider(), NichtEingerichtet)

    def test_demo_aus(self, settings: Any) -> None:
        settings.DEMO_INSTANCE = True
        _einrichten()
        assert isinstance(get_summary_provider(), NichtEingerichtet)

    def test_schluessel_nie_im_repr(self) -> None:
        _einrichten()
        endpunkt = claude_fuer_zusammenfassung()
        assert endpunkt is not None and endpunkt.api_key == SCHLUESSEL
        assert SCHLUESSEL not in repr(endpunkt) and SCHLUESSEL not in str(endpunkt)
        assert SCHLUESSEL not in repr(ClaudeZusammenfassung(endpunkt))
        assert endpunkt.base_url == f"https://{ANTHROPIC_HOST}"


# --- Einzelaufruf ---------------------------------------------------------------------------------------------------


class TestEinzelaufruf:
    NACHRICHTEN = [ChatMessage(role="system", content="Systemprompt"), ChatMessage(role="user", content="Text")]

    def test_anfrage_ohne_sampling_mit_effort(self) -> None:
        client = _Client()
        antwort = ClaudeZusammenfassung(_endpunkt(), client=client).chat_completion(self.NACHRICHTEN, max_tokens=2048)
        assert antwort.content == "Kurzfassung des Vorgangs."
        (anfrage,) = client.anfragen
        # Systemprompt als Cache-Präfix (Prompt Caching)
        assert anfrage["system"] == [{"type": "text", "text": "Systemprompt", "cache_control": {"type": "ephemeral"}}]
        assert anfrage["messages"] == [{"role": "user", "content": "Text"}]
        assert anfrage["model"] == "claude-haiku-5-5" and anfrage["max_tokens"] == 2048
        assert anfrage["output_config"] == {"effort": "low"}
        assert "temperature" not in anfrage

    def test_verbrauch_verbucht(self) -> None:
        ClaudeZusammenfassung(_endpunkt(), client=_Client()).chat_completion(self.NACHRICHTEN)
        (verbrauch,) = KiVerbrauch.objects.all()
        assert verbrauch.art == "sofort" and (verbrauch.eingabe_tokens, verbrauch.ausgabe_tokens) == (10_000, 1_000)
        # 10.000 × 0,10 $ + 1.000 × 0,50 $ je Million Token
        assert verbrauch.kosten_usd == Decimal("0.0015")

    @pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
    def test_abgelehnt_oder_abgeschnitten(self, stop: str) -> None:
        with pytest.raises(KiAnbieterError):
            ClaudeZusammenfassung(_endpunkt(), client=_Client(_nachricht(stop=stop))).chat_completion(self.NACHRICHTEN)
        # Auch die bezahlte, unbrauchbare Antwort zählt zum Budget
        assert KiVerbrauch.objects.count() == 1

    def test_nur_textbloecke(self) -> None:
        nachricht = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="Überlegung"), SimpleNamespace(type="text", text=" A ")],
            stop_reason="end_turn",
        )
        assert antwort_text(nachricht) == "A"

    def test_fehler_ohne_inhalt(self) -> None:
        fehler = anthropic.RateLimitError(
            "geheimer Inhalt",
            response=httpx2.Response(429, request=httpx2.Request("POST", "https://beispiel.example")),
            body=None,
        )
        with pytest.raises(KiAnbieterError) as info:
            ClaudeZusammenfassung(_endpunkt(), client=_Client(fehler)).chat_completion(self.NACHRICHTEN)
        assert str(info.value) == "Claude API meldet HTTP 429"

    def test_budget_erschoepft_ohne_anfrage(self) -> None:
        KiVerbrauch.objects.create(art="sofort", modell="claude-haiku-5-5", kosten_usd=Decimal("100"))
        client = _Client()
        with pytest.raises(BudgetErschoepftError):
            ClaudeZusammenfassung(_endpunkt(), client=client).chat_completion(self.NACHRICHTEN)
        assert client.anfragen == []


# --- Kosten ---------------------------------------------------------------------------------------------------------


class TestKosten:
    def test_preise(self) -> None:
        # Bis 100.000 Token Prompt: 0,10 $ Eingabe und 0,50 $ Ausgabe je Million Token, im Batch die Hälfte
        assert kosten.kosten("claude-haiku-5-5", eingabe=100_000, ausgabe=1_000_000) == Decimal("0.51")
        assert kosten.kosten("claude-haiku-5-5", eingabe=100_000, ausgabe=1_000_000, batch=True) == Decimal("0.255")
        # Über 100.000 Token Prompt gilt der höhere Satz
        assert kosten.kosten("claude-haiku-5-5", eingabe=200_000, ausgabe=0) == Decimal("0.1")
        # Unbekanntes Modell: höchster Listenpreis
        assert kosten.kosten("unbekannt", eingabe=1_000_000, ausgabe=0) == Decimal("10")

    def test_monat_und_reservierung(self) -> None:
        KiVerbrauch.objects.create(art="sofort", modell="m", kosten_usd=Decimal("2"))
        alt = KiVerbrauch.objects.create(art="sofort", modell="m", kosten_usd=Decimal("50"))
        KiVerbrauch.objects.filter(pk=alt.pk).update(zeitpunkt=kosten.monatsbeginn() - timedelta(minutes=1))
        ZusammenfassungsBatch.objects.create(
            batch_id="msgbatch_offen", modell="m", eingereicht_am=timezone.now(), geschaetzt_usd=Decimal("3")
        )
        ZusammenfassungsBatch.objects.create(
            batch_id="msgbatch_fertig",
            modell="m",
            eingereicht_am=timezone.now(),
            geschaetzt_usd=Decimal("40"),
            status=ZusammenfassungsBatch.Status.ABGEHOLT,
        )
        assert kosten.monatsverbrauch() == Decimal("2")
        assert kosten.reserviert() == Decimal("3")
        assert kosten.budget_rest(_endpunkt(monatsbudget_usd=Decimal("10"))) == Decimal("5")


# --- Zusammenfassung und Hinweis auf der Vorgangsseite --------------------------------------------------------------


class TestVorgangsseite:
    def test_hinweis_gespeichert_und_angezeigt(self, body: OParlBody, client: Client, monkeypatch: Any) -> None:
        _einrichten()
        vorgang = _vorgang(body)
        anbieter = ClaudeZusammenfassung(claude_fuer_zusammenfassung(), client=_Client())  # type: ignore[arg-type]
        monkeypatch.setattr("insight_ai.services.summarizer.get_summary_provider", lambda: anbieter)
        antwort = client.post(reverse("insight_core:insight:paper_summary", args=[vorgang.pk]))
        vorgang.refresh_from_db()
        assert vorgang.summary == "Kurzfassung des Vorgangs."
        assert vorgang.summary_hinweis.startswith("Claude von Anthropic (claude-haiku-5-5)")
        html = antwort.content.decode()
        assert "Mit KI aus den Dokumenten erstellt. Die Zusammenfassung kann Fehler enthalten." in html
        assert (
            "KI-Anbieter: Claude von Anthropic (claude-haiku-5-5); Verarbeitungsort: weltweit, auch in den USA" in html
        )

    def test_ohne_hinweis_wie_bisher(self, body: OParlBody, client: Client) -> None:
        vorgang = _vorgang(body, summary="Ältere Zusammenfassung.")
        html = client.get(reverse("insight_core:insight:paper_summary", args=[vorgang.pk])).content.decode()
        assert "Ältere Zusammenfassung." in html and "KI-Anbieter" not in html

    def test_budget_erschoepft(self, body: OParlBody, client: Client, monkeypatch: Any) -> None:
        _einrichten(summary_monthly_budget_usd=Decimal("1"))
        KiVerbrauch.objects.create(art="sofort", modell="claude-haiku-5-5", kosten_usd=Decimal("1"))
        vorgang = _vorgang(body)
        sdk = _Client()
        anbieter = ClaudeZusammenfassung(claude_fuer_zusammenfassung(), client=sdk)  # type: ignore[arg-type]
        monkeypatch.setattr("insight_ai.services.summarizer.get_summary_provider", lambda: anbieter)
        with pytest.raises(BudgetExhaustedError):
            SummaryService().generate_summary(vorgang)
        html = client.post(reverse("insight_core:insight:paper_summary", args=[vorgang.pk])).content.decode()
        assert "derzeit nicht verfügbar" in html and "Erneut versuchen" not in html
        assert sdk.anfragen == []


# --- Message Batches ------------------------------------------------------------------------------------------------


def _batch_anbieter(**werte: Any) -> tuple[ClaudeZusammenfassung, _Client]:
    sdk = _Client()
    return ClaudeZusammenfassung(_endpunkt(**werte), client=sdk), sdk


class TestBatchEinreichen:
    @pytest.fixture(autouse=True)
    def _claude(self, monkeypatch: Any) -> None:
        _einrichten()

    def test_auswahl(self, body: OParlBody) -> None:
        neu = _vorgang(body, tage=1)
        aelter = _vorgang(body, tage=5)
        _vorgang(body, tage=1, summary="Schon da.")
        _vorgang(body, tage=40)  # außerhalb des Zeitraums
        _vorgang(body, tage=1, text=None)  # ohne Text
        _vorgang(body, tage=1, text="")  # leerer Text
        _vorgang(body, tage=1, deleted=True)
        anbieter, sdk = _batch_anbieter()

        assert zusammenfassung_batch.einreichen(provider=anbieter) == 2
        (anfragen,) = sdk.batches.eingereicht
        assert [anfrage["custom_id"] for anfrage in anfragen] == [str(neu.pk), str(aelter.pk)]
        params = anfragen[0]["params"]
        assert params["model"] == "claude-haiku-5-5" and "temperature" not in params
        assert "Der Rat beschließt den Bau einer Brücke." in params["messages"][0]["content"]
        batch = ZusammenfassungsBatch.objects.get()
        assert batch.batch_id == "msgbatch_1" and batch.anzahl == 2 and batch.geschaetzt_usd > 0
        assert batch.anfragen[str(neu.pk)]["dateien"] == [str(neu.files.get().pk)]

    def test_ungelistete_kommune_nicht(self, body: OParlBody) -> None:
        OParlBody.objects.filter(pk=body.pk).update(is_listed=False)
        _vorgang(body)
        anbieter, sdk = _batch_anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0 and sdk.batches.eingereicht == []

    @pytest.mark.parametrize("stand", [publication.PAUSED, publication.WITHDRAWN])
    def test_pausierte_kommune_nicht(self, body: OParlBody, stand: str) -> None:
        _vorgang(body)
        publication.set_source_state(body.source, stand)
        anbieter, sdk = _batch_anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0 and sdk.batches.eingereicht == []

    def test_archivierte_kommune_schon(self, body: OParlBody) -> None:
        _vorgang(body)
        publication.set_source_state(body.source, publication.ARCHIVED)
        anbieter, sdk = _batch_anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1

    def test_nicht_gespeichert_wird_abgebrochen(self, body: OParlBody, monkeypatch: Any) -> None:
        _vorgang(body)
        anbieter, sdk = _batch_anbieter()

        def kaputt(**_werte: Any) -> None:
            raise RuntimeError("Datenbank nicht erreichbar")

        monkeypatch.setattr(ZusammenfassungsBatch.objects, "create", kaputt)
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0
        assert sdk.batches.abgebrochen == ["msgbatch_1"]

    def test_kein_zweiter_batch_solange_einer_offen(self, body: OParlBody) -> None:
        _vorgang(body)
        anbieter, sdk = _batch_anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
        _vorgang(body)
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0
        assert len(sdk.batches.eingereicht) == 1

    def test_budget_begrenzt(self, body: OParlBody) -> None:
        for _ in range(3):
            _vorgang(body)
        schaetzung = kosten.schaetzung("claude-haiku-5-5", zeichen=3_000, max_ausgabe=4096, batch=True)
        _einrichten(summary_monthly_budget_usd=Decimal("0.01"))
        KiVerbrauch.objects.create(art="sofort", modell="m", kosten_usd=Decimal("0.01") - schaetzung * 2)
        anbieter, sdk = _batch_anbieter(monatsbudget_usd=Decimal("0.01"))
        assert 1 <= zusammenfassung_batch.einreichen(provider=anbieter) < 3

    def test_aus(self, body: OParlBody) -> None:
        _vorgang(body)
        _einrichten(summary_batch_enabled=False)
        anbieter, sdk = _batch_anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0 and sdk.batches.eingereicht == []


class TestBatchAbholen:
    @pytest.fixture
    def eingereicht(self, body: OParlBody) -> tuple[ClaudeZusammenfassung, _Client, list[OParlPaper]]:
        _einrichten()
        vorgaenge = [_vorgang(body) for _ in range(4)]
        anbieter, sdk = _batch_anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 4
        return anbieter, sdk, vorgaenge

    @staticmethod
    def _ergebnis(vorgang: OParlPaper, nachricht: Any = None, art: str = "succeeded") -> SimpleNamespace:
        if art != "succeeded":
            return SimpleNamespace(custom_id=str(vorgang.pk), result=SimpleNamespace(type=art))
        return SimpleNamespace(
            custom_id=str(vorgang.pk),
            result=SimpleNamespace(
                type=art, message=_als_json(nachricht or _nachricht(f"Kurzfassung {vorgang.name}."))
            ),
        )

    def test_laeuft_noch(self, eingereicht: Any) -> None:
        anbieter, sdk, _ = eingereicht
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        assert ZusammenfassungsBatch.objects.get().status == ZusammenfassungsBatch.Status.EINGEREICHT

    def test_uebernahme(self, eingereicht: Any) -> None:
        anbieter, sdk, (gut, neuer_text, zurueckgenommen, abgelehnt) = eingereicht
        # Neuer Text nach dem Einreichen, Anlage zurückgenommen: Ergebnis passt nicht mehr
        OParlFile.objects.filter(paper=neuer_text).update(text_extracted_at=timezone.now() + timedelta(seconds=1))
        OParlFile.objects.filter(paper=zurueckgenommen).update(deleted=True)
        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [
            self._ergebnis(gut),
            self._ergebnis(neuer_text),
            self._ergebnis(zurueckgenommen),
            self._ergebnis(abgelehnt, _nachricht("", stop="refusal")),
        ]

        assert zusammenfassung_batch.abholen(provider=anbieter) == 1
        gut.refresh_from_db()
        assert gut.summary == f"Kurzfassung {gut.name}."
        assert gut.summary_hinweis.startswith("Claude von Anthropic (claude-haiku-5-5)")
        for vorgang in (neuer_text, zurueckgenommen, abgelehnt):
            vorgang.refresh_from_db()
            assert vorgang.summary is None
        batch = ZusammenfassungsBatch.objects.get()
        assert batch.status == ZusammenfassungsBatch.Status.ABGEHOLT
        assert (batch.gespeichert, batch.verworfen, batch.fehlgeschlagen) == (1, 2, 1)
        # Batch zum halben Preis: je Ergebnis 10.000 Eingabe- und 1.000 Ausgabe-Token
        assert batch.kosten_usd == Decimal("0.003") and KiVerbrauch.objects.filter(art="batch").count() == 4
        assert kosten.reserviert() == 0

        # Genau einmal: ein zweiter Lauf übernimmt nichts und verbucht nichts
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        assert KiVerbrauch.objects.count() == 4
        # Abgelehnt: eine Woche Pause; der Vorgang mit neuem Text kommt beim nächsten Einreichen wieder, der ohne
        # aktuelle Anlage nicht
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
        assert [anfrage["custom_id"] for anfrage in sdk.batches.eingereicht[-1]] == [str(neuer_text.pk)]

    def test_vorhandene_zusammenfassung_bleibt(self, eingereicht: Any) -> None:
        anbieter, sdk, (vorgang, *_rest) = eingereicht
        OParlPaper.objects.filter(pk=vorgang.pk).update(summary="Per Klick erstellt.")
        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [self._ergebnis(vorgang)]
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        vorgang.refresh_from_db()
        assert vorgang.summary == "Per Klick erstellt."

    def test_nicht_mehr_abrufbar_verloren(self, eingereicht: Any) -> None:
        anbieter, sdk, _ = eingereicht
        sdk.batches.fehler = anthropic.NotFoundError(
            "nf", response=httpx2.Response(404, request=httpx2.Request("GET", "https://beispiel.example")), body=None
        )
        cent = Decimal("0.000001")
        geschaetzt = ZusammenfassungsBatch.objects.get().geschaetzt_usd
        assert geschaetzt > 0 and kosten.reserviert().quantize(cent) == geschaetzt
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        batch = ZusammenfassungsBatch.objects.get()
        assert batch.status == ZusammenfassungsBatch.Status.VERLOREN and batch.kosten_usd == geschaetzt
        # Budget vorsichtig: die Schätzung gilt als verbraucht, reserviert ist nichts mehr
        assert kosten.reserviert() == 0 and kosten.monatsverbrauch().quantize(cent) == geschaetzt
        # Der nächste Batch kann kommen
        sdk.batches.fehler = None
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 4

    def test_voruebergehender_fehler_bleibt_offen(self, eingereicht: Any) -> None:
        anbieter, sdk, _ = eingereicht
        sdk.batches.fehler = anthropic.APIConnectionError(request=httpx2.Request("GET", "https://beispiel.example"))
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        assert ZusammenfassungsBatch.objects.get().status == ZusammenfassungsBatch.Status.EINGEREICHT
        # Nach der Aufbewahrungsfrist der Ergebnisse gilt er als verloren
        ZusammenfassungsBatch.objects.update(eingereicht_am=timezone.now() - timedelta(days=30))
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        assert ZusammenfassungsBatch.objects.get().status == ZusammenfassungsBatch.Status.VERLOREN

    def test_abgelaufen_ohne_pause(self, eingereicht: Any) -> None:
        anbieter, sdk, (vorgang, *_rest) = eingereicht
        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [self._ergebnis(vorgang, art="expired")]
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        assert not cache.get(zusammenfassung_batch._pause_schluessel(vorgang.pk))


# --- Admin und Verbindungsprüfung -----------------------------------------------------------------------------------


def _formular(**werte: Any) -> Any:
    ki = AISettings.get_settings()
    daten: dict[str, Any] = {
        "provider": "",
        "base_url": "",
        "anzeigename": "",
        "verarbeitungsort": "",
        "model_name": ki.model_name,
        "insight_model": "",
        "fallback_model": "",
        "max_output_tokens": ki.max_output_tokens,
        "insight_max_output_tokens": ki.insight_max_output_tokens,
        "default_org_monthly_token_limit": ki.default_org_monthly_token_limit,
        "insight_enabled": "on",
        "summary_provider": ANTHROPIC,
        "summary_model": "claude-haiku-5-5",
        "summary_batch_enabled": "on",
        "summary_batch_days": 30,
        "summary_monthly_budget_usd": "100",
        **werte,
    }
    return cast(Any, AISettingsAdminForm)(data=daten, instance=ki)


class TestAdmin:
    def test_schluessel_verschluesselt(self) -> None:
        form = _formular(summary_api_key=SCHLUESSEL)
        assert form.is_valid(), form.errors
        form.save()
        ki = AISettings.objects.get(pk=1)
        assert ki.get_summary_api_key() == SCHLUESSEL
        assert SCHLUESSEL.encode() not in bytes(ki.summary_api_key_encrypted or b"")

    def test_ohne_schluessel_nicht_gespeichert(self) -> None:
        form = _formular()
        assert not form.is_valid() and "summary_api_key" in form.errors

    def test_host_nicht_freigegeben(self, settings: Any) -> None:
        settings.KI_ERLAUBTE_HOSTS = []
        form = _formular(summary_api_key=SCHLUESSEL)
        assert not form.is_valid() and "summary_provider" in form.errors


class TestVerbindungspruefung:
    def test_ok_ohne_kosten(self) -> None:
        ergebnis = pruefe_claude(_endpunkt(), client=_Client())
        assert ergebnis.ok and ergebnis.zweck == "Zusammenfassungen (Claude)"
        assert SCHLUESSEL not in ergebnis.text

    def test_modell_unbekannt(self) -> None:
        def nicht_gefunden(modell: str) -> None:
            raise anthropic.NotFoundError(
                "nf",
                response=httpx2.Response(404, request=httpx2.Request("GET", "https://beispiel.example")),
                body=None,
            )

        sdk = _Client()
        sdk.models = SimpleNamespace(retrieve=nicht_gefunden)
        assert pruefe_claude(_endpunkt(), client=sdk).art == "nicht_gefunden"

    def test_gesperrt_in_der_uebersicht(self, settings: Any) -> None:
        _einrichten()
        settings.KI_ERLAUBTE_HOSTS = []
        ergebnisse = pruefe_ki_einstellungen()
        assert any("Zusammenfassungen (Claude)" in e.meldung and "KI_ERLAUBTE_HOSTS" in e.meldung for e in ergebnisse)


class TestBefehl:
    def test_claude_einrichten_und_zurueck(self, monkeypatch: Any) -> None:
        from io import StringIO

        from django.core.management import call_command

        ki = AISettings.get_settings()
        ki.insight_enabled = True
        ki.save()
        monkeypatch.setenv("CLAUDE_TEST_SCHLUESSEL", SCHLUESSEL)
        ausgabe = StringIO()
        call_command(
            "ki_anbieter",
            "zusammenfassung",
            "--claude",
            "--schluessel-aus-umgebung",
            "CLAUDE_TEST_SCHLUESSEL",
            "--budget",
            "25",
            "--batch-tage",
            "14",
            stdout=ausgabe,
        )
        ki = AISettings.objects.get(pk=1)
        assert (ki.summary_provider, ki.summary_model, ki.summary_batch_days) == (ANTHROPIC, "claude-haiku-5-5", 14)
        assert ki.summary_monthly_budget_usd == Decimal("25") and ki.get_summary_api_key() == SCHLUESSEL
        assert SCHLUESSEL not in ausgabe.getvalue() and "Claude API (Anthropic)" in ausgabe.getvalue()
        assert isinstance(get_summary_provider(), ClaudeZusammenfassung)

        call_command("ki_anbieter", "zusammenfassung", "--wie-buergerportal", stdout=StringIO())
        assert AISettings.objects.get(pk=1).summary_provider == ""
        assert isinstance(get_summary_provider(), NichtEingerichtet)

    def test_ohne_freigabe_nicht_gespeichert(self, settings: Any, monkeypatch: Any) -> None:
        from django.core.management import CommandError, call_command

        settings.KI_ERLAUBTE_HOSTS = []
        monkeypatch.setenv("CLAUDE_TEST_SCHLUESSEL", SCHLUESSEL)
        with pytest.raises(CommandError) as info:
            call_command(
                "ki_anbieter", "zusammenfassung", "--claude", "--schluessel-aus-umgebung", "CLAUDE_TEST_SCHLUESSEL"
            )
        assert "KI_ERLAUBTE_HOSTS" in str(info.value) and SCHLUESSEL not in str(info.value)
        assert AISettings.get_settings().summary_provider == ""
