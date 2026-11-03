# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Obergrenze des erkannten Texts je Datei und Lesewege ohne Volltext.

Speichern: Die Texterkennung der Anwendung (Upload in Session, ``extract_texts``, Erkennung bei Bedarf für die
KI-Zusammenfassung) speichert höchstens ``MAX_TEXT_CHARS`` Zeichen je Datei und vermerkt den Schnitt bei RIS-Dateien.

Anzeige: Die öffentliche Vorgangsseite, die Dokumentliste, die Suche, die KI (Zusammenfassung, Chat) und beide
OParl-Schnittstellen laden den Volltext nicht. Die Vorgangsseite zeigt einen in der Datenbank gekürzten Anfang,
begrenzt je Datei und je Seite; der Objekt-Endpunkt ``file`` gibt höchstens die Obergrenze aus. Das gilt auch für
längere Texte, die vor der Obergrenze gespeichert wurden.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
import mandari_oparl
import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.session.models import SessionFile, SessionMeeting, SessionOrganization, SessionTenant
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_core.services import document_extraction

#: Ein Wort mit zehn Zeichen: Wie oft es auf einer Seite steht, zeigt, wie viel Text sie enthält
WORT = "Bordstein "
#: Obergrenze laut Freigabe (Haushaltspläne erreichen gut 1 Mio. Zeichen)
GRENZE = 5_000_000


def _grenze() -> int:
    return int(getattr(mandari_oparl, "MAX_TEXT_CHARS", GRENZE))


@pytest.fixture
def kleine_grenze(monkeypatch: pytest.MonkeyPatch) -> int:
    """Obergrenze für schnelle Tests herabgesetzt (nur in diesem Prozess)."""
    try:
        modul = importlib.import_module("mandari_oparl.limits")
    except ModuleNotFoundError:
        modul = None
    if modul is not None:
        monkeypatch.setattr(modul, "MAX_TEXT_CHARS", 2_000)
    return 2_000


@pytest.fixture
def abruf(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Download einer Textdatei mit 5.000 Zeichen, ohne Netz."""
    abgerufen: list[str] = []

    def http_get(url: str, timeout: float = 60.0, extra_headers: dict[str, str] | None = None) -> httpx.Response:
        abgerufen.append(url)
        return httpx.Response(200, content=(WORT * 500).encode(), headers={"content-type": "text/plain"})

    monkeypatch.setattr(document_extraction, "_http_get", http_get)
    return abgerufen


def _quelle() -> OParlBody:
    kennung = uuid.uuid4().hex[:8]
    source = OParlSource.objects.create(name=f"Quelle {kennung}", url=f"https://{kennung}.example.org/oparl/system")
    return OParlBody.objects.create(
        source=source, external_id=f"https://{kennung}.example.org/oparl/body/1", name="Stadt", slug=kennung
    )


def _datei(body: OParlBody, text: str | None, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        mime_type="text/plain",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.txt",
        text_content=text,
        **felder,
    )


def _vorgang(texte: Sequence[str], body: OParlBody | None = None) -> OParlPaper:
    body = body or _quelle()
    vorgang = OParlPaper.objects.create(
        external_id=f"https://ris.example.org/papers/{uuid.uuid4()}", body=body, name="Vorgang mit Anlagen"
    )
    for nummer, text in enumerate(texte, start=1):
        _datei(body, text, paper=vorgang, name=f"Anlage {nummer}")
    return vorgang


def _volltext_geladen(dateien: Sequence[Any]) -> list[str]:
    """Dateien, deren Volltext im Speicher liegt (geladen oder beim Zugriff nachgeladen)."""
    return [str(datei.name) for datei in dateien if "text_content" in datei.__dict__]


# =============================================================================
# Speichern
# =============================================================================


def test_obergrenze_laesst_raum_fuer_lange_ratsdokumente() -> None:
    assert getattr(mandari_oparl, "MAX_TEXT_CHARS", None) == GRENZE


def test_erkennung_kuerzt_an_der_obergrenze(kleine_grenze: int) -> None:
    text, _ocr, _seiten, _methode = document_extraction.extract_text_from_file(
        (WORT * 500).encode(), "text/plain", "anlage.txt"
    )
    assert len(text) == kleine_grenze

    kurz, _ocr, _seiten, _methode = document_extraction.extract_text_from_file(
        (WORT * 10).encode(), "text/plain", "anlage.txt"
    )
    assert kurz == (WORT * 10).strip()


def test_abruf_kuerzt_und_vermerkt_den_schnitt(kleine_grenze: int, abruf: list[str]) -> None:
    ergebnis = document_extraction.download_and_extract(url="https://ris.example.org/a.txt", mime_type="text/plain")

    assert len(ergebnis.text) == kleine_grenze
    assert any("gekürzt" in hinweis for hinweis in getattr(ergebnis, "notes", ()))


@pytest.mark.django_db
def test_extract_texts_speichert_hoechstens_die_obergrenze(kleine_grenze: int, abruf: list[str]) -> None:
    from insight_core.management.commands.extract_texts import Command

    datei = _datei(_quelle(), None)

    assert Command()._process_file(datei, False)["success"] is True

    datei.refresh_from_db()
    assert len(datei.text_content or "") == kleine_grenze
    assert "gekürzt" in (datei.text_extraction_error or "")


@pytest.mark.django_db
def test_erkennung_fuer_die_zusammenfassung_speichert_hoechstens_die_obergrenze(
    kleine_grenze: int, abruf: list[str]
) -> None:
    from insight_ai.services import summarizer

    datei = _datei(_quelle(), None)
    dienst: Any = summarizer.SummaryService

    text = dienst(provider=object())._extract_text_from_file(datei)

    assert 0 < len(text) <= kleine_grenze
    datei.refresh_from_db()
    assert len(datei.text_content or "") == kleine_grenze
    assert "gekürzt" in (datei.text_extraction_error or "")


def test_anlage_in_session_speichert_hoechstens_die_obergrenze(kleine_grenze: int) -> None:
    from apps.session.services import file_service

    assert len(file_service.extract_text((WORT * 500).encode(), "text/plain", "anlage.txt")) == kleine_grenze


# =============================================================================
# Anzeige: Vorgang und Dokumentliste im Bürgerportal
# =============================================================================


@pytest.mark.django_db
def test_vorgangsseite_laedt_den_volltext_nicht_und_zeigt_einen_anfang() -> None:
    # Ein Text von 300.000 Zeichen, etwa aus der Zeit vor der Obergrenze
    vorgang = _vorgang([WORT * 30_000])

    antwort = Client().get(f"/insight/vorgaenge/{vorgang.pk}/")

    assert antwort.status_code == 200
    assert _volltext_geladen(antwort.context["files"]) == []
    assert len(antwort.context["files_with_text"]) == 1
    seite = antwort.content.decode()
    assert 0 < seite.count(WORT.strip()) <= 100_000 // len(WORT) + 1
    assert "gekürzt" in seite
    # Die Länge nennt weiter den ganzen gespeicherten Text
    assert "300000 Zeichen" in seite


@pytest.mark.django_db
def test_vorgangsseite_mit_vielen_anlagen_bleibt_unter_der_summe_je_seite(monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.views import papers

    monkeypatch.setattr(papers, "TEXTANFANG_ZEICHEN", 1_000, raising=False)
    monkeypatch.setattr(papers, "TEXTANFANG_JE_SEITE", 3_000, raising=False)
    vorgang = _vorgang([WORT * 200] * 6)  # je 2.000 Zeichen

    antwort = Client().get(f"/insight/vorgaenge/{vorgang.pk}/")

    # Jede Datei behält ihren Text, zusammen höchstens die Summe je Seite
    assert len(antwort.context["files_with_text"]) == 6
    assert antwort.content.decode().count(WORT.strip()) <= 3_000 // len(WORT) + 6


@pytest.mark.django_db
def test_kurzer_text_bleibt_vollstaendig_ohne_hinweis() -> None:
    vorgang = _vorgang(["Die Verwaltung nimmt wie folgt Stellung.", "   ", ""])

    antwort = Client().get(f"/insight/vorgaenge/{vorgang.pk}/")

    seite = antwort.content.decode()
    assert [datei.name for datei in antwort.context["files_with_text"]] == ["Anlage 1"]
    assert "Die Verwaltung nimmt wie folgt Stellung." in seite and "gekürzt" not in seite
    assert "40 Zeichen" in seite


@pytest.mark.django_db
def test_dokumentliste_laedt_den_volltext_nicht() -> None:
    vorgang = _vorgang([WORT * 1_000] * 2)
    client = Client()
    client.get(f"/insight/kommune/{vorgang.body_id}/")

    antwort = client.get("/insight/dokumente/")

    assert antwort.status_code == 200
    dateien = list(antwort.context["files"])
    assert len(dateien) == 2 and _volltext_geladen(dateien) == []


# =============================================================================
# Anzeige: Suche und KI
# =============================================================================


class _Indizes:
    def exists(self, index: str) -> bool:
        return True


class _Suchindex:
    """Elasticsearch-Ersatz mit einer Datei, die nur über ihren Namen trifft; beachtet ``_source`` wie der Index."""

    def __init__(self) -> None:
        self.aufrufe: list[dict[str, Any]] = []
        self.indices = _Indizes()

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.aufrufe.append(body)
        if index != "files":
            return {"hits": {"hits": [], "total": {"value": 0}}}
        quelle: dict[str, Any] = {
            "id": "f1",
            "type": "file",
            "name": "Haushaltsplan 2027",
            "text_content": WORT * 30_000,
            "text_preview": WORT * 50,
        }
        auswahl = body.get("_source", True)
        if isinstance(auswahl, dict):
            quelle = {k: v for k, v in quelle.items() if k not in auswahl.get("excludes", [])}
        # Nur der Name trifft: kein Fragment aus dem Text
        hit = {"_id": "f1", "_score": 3.0, "_source": quelle, "highlight": {"name": ["<mark>Haushalt</mark>splan"]}}
        return {"hits": {"hits": [hit], "total": {"value": 1}}}


def test_suche_laedt_keinen_volltext() -> None:
    from insight_core.services.search_service import ElasticsearchService, format_search_result

    dienst: Any = ElasticsearchService.__new__(ElasticsearchService)
    dienst.client = _Suchindex()

    (treffer,) = dienst.search_all("Haushalt", index_names=["files"])["results"]

    assert "text_content" not in treffer
    assert "text_content" not in treffer.get("_formatted", {})
    assert all("text_content" in body["_source"]["excludes"] for body in dienst.client.aufrufe)
    # Angezeigt wird der kurze Anfang aus dem Index
    assert format_search_result(treffer)["text_preview"] == WORT * 50


@pytest.mark.django_db
def test_chat_kontext_liest_nur_den_anfang_des_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_ai.services import chat_service

    datei = _datei(_quelle(), WORT * 30_000, name="Haushaltsplan 2027")

    class _Suche:
        def search_all(self, **kwargs: Any) -> dict[str, Any]:
            # Treffer wie aus dem Index, ohne Volltext
            return {"results": [{"id": str(datei.pk), "type": "file", "name": "Haushaltsplan 2027"}]}

    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: _Suche())

    kontext, quellen = chat_service.build_rag_context("Haushalt", None)

    zeichen = 4000 * chat_service.CHARS_PER_TOKEN
    assert 0 < kontext.count(WORT.strip()) <= zeichen // len(WORT) + 1
    assert "[...]" in kontext


@pytest.mark.django_db
def test_ki_zusammenfassung_liest_hoechstens_ein_festes_budget() -> None:
    from insight_ai.services import summarizer

    vorgang = _vorgang([WORT * 15_000] * 3 + ["Kurzer Antrag zum Radweg."])  # 450.000 Zeichen und ein kurzer Text

    dienst: Any = summarizer.SummaryService
    text = dienst(provider=object())._collect_text_content_with_extraction(vorgang)

    budget = getattr(summarizer, "TEXT_BUDGET_CHARS", 200_000)
    assert len(text) <= budget + 1_000
    assert "Kurzer Antrag zum Radweg." in text and "gekürzt" in text


# =============================================================================
# Anzeige: OParl
# =============================================================================


@pytest.mark.django_db
def test_oparl_listen_laden_den_text_eingebetteter_dateien_nicht() -> None:
    from oparl_api import views as oparl_ansichten

    views: Any = oparl_ansichten
    vorgang = _vorgang([WORT * 100])
    sitzung = OParlMeeting.objects.create(
        external_id=f"https://ris.example.org/meetings/{uuid.uuid4()}", body=vorgang.body, name="Sitzung"
    )
    _datei(vorgang.body, WORT * 100, meeting=sitzung, name="Einladung")

    vorgaenge = list(views._prepare_papers(OParlPaper.objects.filter(pk=vorgang.pk)))
    sitzungen = list(views._prepare_meetings(OParlMeeting.objects.filter(pk=sitzung.pk)))

    eingebettet = [*vorgaenge[0].files.all(), *sitzungen[0].files.all()]
    assert len(eingebettet) == 2 and _volltext_geladen(eingebettet) == []


@pytest.mark.django_db
def test_oparl_datei_mit_text_hoechstens_bis_zur_obergrenze() -> None:
    # Älterer Text über der Obergrenze, gespeichert vor ihrer Einführung
    vorgang = _vorgang(["x" * (_grenze() + 100)])
    datei = vorgang.files.get()
    cache.clear()

    with override_settings(OPARL_API_RATE_LIMIT=0):
        antwort = Client().get(f"/oparl/v1/file/{datei.pk}")

    assert antwort.status_code == 200
    assert len(antwort.json()["text"]) == _grenze()


@pytest.mark.django_db
def test_session_schnittstelle_gibt_text_hoechstens_bis_zur_obergrenze_aus() -> None:
    from apps.session.api import oparl

    session_ausgabe: Any = oparl

    tenant = SessionTenant.objects.create(name="Stadt Muster", slug="text-grenze")
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, organization=rat, name="Ratssitzung", start=timezone.now(), is_public=True
    )
    datei = SessionFile.objects.create(
        tenant=tenant, meeting=sitzung, name="Anlage.pdf", is_public=True, text_content="x" * (_grenze() + 100)
    )
    cache.clear()

    with override_settings(OPARL_API_RATE_LIMIT=0, OPARL_API_CACHE_SECONDS=0):
        objekt = Client().get(f"/session/{tenant.slug}/api/oparl/file/{datei.pk}/")
        sitzungen = Client().get(f"/session/{tenant.slug}/api/oparl/meetings/")

    assert objekt.status_code == 200 and len(objekt.json()["text"]) == _grenze()
    assert sitzungen.status_code == 200 and "x" * 100 not in sitzungen.content.decode()
    # Eingebettete Dateien, die Dateiliste und die Niederschrift einer Sitzung kommen ohne Text
    assert "text_content" in session_ausgabe._public_files_qs().query.deferred_loading[0]
    liste = session_ausgabe.LIST_SPECS["files"][1]
    assert "text_content" in liste(SessionFile.objects.all(), tenant).query.deferred_loading[0]
    vorbereitet = session_ausgabe.OBJECT_SPECS["meeting"][1](SessionMeeting.objects.all(), tenant)
    assert "protocol__public_file__text_content" in vorbereitet.query.deferred_loading[0]


# =============================================================================
# Anzeige: RIS in Work
# =============================================================================


@pytest.mark.django_db
def test_work_dokumente_und_vorgangsdateien_ohne_volltext() -> None:
    from apps.work.ris import selectors

    vorgang = _vorgang([WORT * 100])

    dateien, aus_rohdaten = selectors.paper_files(vorgang)
    liste = list(selectors.files_queryset(OParlBody.objects.filter(pk=vorgang.body_id)))

    assert aus_rohdaten is False
    assert len(liste) == 1 and _volltext_geladen([*dateien, *liste]) == []


@pytest.mark.django_db
def test_work_sitzungsvorbereitung_laedt_die_vorgangsdateien_ohne_volltext() -> None:
    from apps.work.meetings.selectors import prefetch_papers_for_agenda_items

    vorgang = _vorgang([WORT * 100])
    sitzung = OParlMeeting.objects.create(
        external_id=f"https://ris.example.org/meetings/{uuid.uuid4()}", body=vorgang.body, name="Sitzung"
    )
    top = OParlAgendaItem.objects.create(
        external_id=f"https://ris.example.org/agendaitems/{uuid.uuid4()}", meeting=sitzung, name="TOP 1"
    )
    OParlConsultation.objects.create(
        external_id=f"https://ris.example.org/consultations/{uuid.uuid4()}",
        body=vorgang.body,
        paper=vorgang,
        agenda_item_external_id=top.external_id,
    )

    vorgaenge = prefetch_papers_for_agenda_items([top])

    dateien = [datei for papier in vorgaenge[top.id] for datei in papier.files.all()]
    assert len(dateien) == 1 and _volltext_geladen(dateien) == []
