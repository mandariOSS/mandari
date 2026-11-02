# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Obergrenze des erkannten Texts je Datei und Lesewege ohne Volltext.

Speichern: Jeder Weg der Texterkennung speichert höchstens ``MAX_TEXT_CHARS`` Zeichen je Datei und vermerkt den
Schnitt – die erste Stufe im eigenen Prozess (``file.read_text_layer``), die zweite Stufe (``file.extract_text``) und
``_speichern`` auch für Ergebnisse, die nicht aus der Bibliothek kommen.

Anzeige: Die Vorgangsseiten (Insight, Work), die Sitzungsseite in Work und die OParl-Listen laden den Volltext nicht.
Die Dokumentzeile zeigt einen in der Datenbank gekürzten Anfang, begrenzt je Datei und je Seite; der Objekt-Endpunkt
``file`` gibt höchstens die Obergrenze aus. Das gilt auch für längere Texte, die vor der Obergrenze gespeichert wurden.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from mandari_dokumente import ExtractionResult, extract_text
from mandari_dokumente import texterkennung as bibliothek

from apps.session.api import oparl as session_ausgabe
from apps.session.models import SessionFile, SessionMeeting, SessionOrganization, SessionTenant
from hub.ris import canonical, erkennung
from hub.ris import selectors as ris
from hub.ris.mapping import bestand
from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource
from insight_core.services import file_cache

#: Ein Wort mit zehn Zeichen: Wie oft es auf einer Seite steht, zeigt, wie viel Text sie enthält
WORT = "Bordstein "
SITE = "https://mandari.example"


@pytest.fixture
def ablage(tmp_path: Path, settings: Any) -> Path:
    settings.OPARL_FILES_ROOT = str(tmp_path / "ablage")
    settings.FILE_STORE_LAYOUT = "sha256"
    settings.FILE_CACHE_MIN_FREE_GB = 0
    settings.OBJ_ENABLED = False
    return tmp_path / "ablage"


@pytest.fixture
def kleine_grenze(monkeypatch: pytest.MonkeyPatch) -> int:
    """Obergrenze der Bibliothek für schnelle Tests herabgesetzt (nur in diesem Prozess)."""
    monkeypatch.setattr(bibliothek, "MAX_TEXT_CHARS", 2_000, raising=False)
    return 2_000


def _pdf(seiten: Sequence[str]) -> bytes:
    """Minimales PDF mit einer Textzeile je Seite."""
    objekte: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kinder: list[int] = []
    for text in seiten:
        inhalt = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
        objekte.append(b"<< /Length %d >>\nstream\n" % len(inhalt) + inhalt + b"\nendstream")
        objekte.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {len(objekte)} 0 R >>"
            ).encode()
        )
        kinder.append(len(objekte))
    objekte[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objekte[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kinder)}] /Count {len(kinder)} >>".encode()
    aus = bytearray(b"%PDF-1.4\n")
    versatz: list[int] = []
    for nummer, rumpf in enumerate(objekte, start=1):
        versatz.append(len(aus))
        aus += f"{nummer} 0 obj\n".encode() + rumpf + b"\nendobj\n"
    xref = len(aus)
    aus += f"xref\n0 {len(objekte) + 1}\n".encode() + b"0000000000 65535 f \n"
    for wert in versatz:
        aus += f"{wert:010d} 00000 n \n".encode()
    aus += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(aus)


def _quelle() -> OParlBody:
    kennung = uuid.uuid4().hex[:8]
    source = OParlSource.objects.create(name=f"Quelle {kennung}", url=f"https://{kennung}.example.org/oparl/system")
    return OParlBody.objects.create(
        source=source, external_id=f"https://{kennung}.example.org/oparl/body/1", name="Stadt"
    )


def _abgelegt(inhalt: bytes) -> OParlFile:
    datei = OParlFile.objects.create(
        body=_quelle(),
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        name="Anlage",
        file_name="anlage.pdf",
        mime_type="application/pdf",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.pdf",
    )
    file_cache.store_bytes(datei, inhalt)
    datei.refresh_from_db()
    return datei


def _datei(body: OParlBody, text: str, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        body=body,
        external_id=f"https://ris.example.org/files/{uuid.uuid4()}",
        mime_type="application/pdf",
        download_url=f"https://ris.example.org/dokumente/{uuid.uuid4().hex[:8]}.pdf",
        text_content=text,
        **felder,
    )


def _vorgang(texte: Sequence[str]) -> OParlPaper:
    body = _quelle()
    vorgang = OParlPaper.objects.create(
        external_id=f"https://ris.example.org/papers/{uuid.uuid4()}", body=body, name="Vorgang mit Anlagen"
    )
    for nummer, text in enumerate(texte, start=1):
        _datei(body, text, paper=vorgang, name=f"Anlage {nummer}")
    return vorgang


def _volltext_geladen(dateien: Sequence[OParlFile]) -> list[str]:
    """Dateien, deren Volltext im Speicher liegt (geladen oder beim Zugriff nachgeladen)."""
    return [str(datei.name) for datei in dateien if "text_content" in datei.__dict__]


# =============================================================================
# Speichern
# =============================================================================


def test_obergrenze_laesst_raum_fuer_lange_ratsdokumente() -> None:
    # Lange Haushaltspläne erreichen gut 1 Mio. Zeichen
    assert 1_000_000 < getattr(bibliothek, "MAX_TEXT_CHARS", 0) <= 10_000_000


def test_bibliothek_kuerzt_an_der_grenze_und_vermerkt_es(kleine_grenze: int) -> None:
    lang = extract_text((WORT * 500).encode(), "text/plain")
    assert len(lang.text) == kleine_grenze
    assert any("gekürzt" in hinweis for hinweis in lang.notes)

    kurz = extract_text((WORT * 10).encode(), "text/plain")
    assert kurz.text == (WORT * 10).strip() and kurz.notes == ()


@pytest.mark.django_db
def test_erste_stufe_im_eigenen_prozess_speichert_hoechstens_die_obergrenze(ablage: Path, kleine_grenze: int) -> None:
    # Der Prozess der ersten Stufe kennt die herabgesetzte Grenze nicht: Es kürzt die Anwendung
    datei = _abgelegt(_pdf([WORT * 300] * 3))

    assert erkennung.textebene_lesen(str(datei.pk)) == erkennung.ERLEDIGT

    datei.refresh_from_db()
    assert datei.text_extraction_status == "completed"
    assert 0 < len(datei.text_content or "") <= kleine_grenze
    assert "gekürzt" in (datei.text_extraction_error or "")


@pytest.mark.django_db
def test_zweite_stufe_speichert_hoechstens_die_obergrenze(ablage: Path, kleine_grenze: int) -> None:
    datei = _abgelegt(_pdf([WORT * 300] * 3))

    assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT

    datei.refresh_from_db()
    assert 0 < len(datei.text_content or "") <= kleine_grenze
    assert "gekürzt" in (datei.text_extraction_error or "")


@pytest.mark.django_db
def test_speichern_kuerzt_auch_ergebnisse_ausserhalb_der_bibliothek(ablage: Path, kleine_grenze: int) -> None:
    datei = _abgelegt(b"%PDF-1.4 Anlage zur Vorlage")
    ergebnis = ExtractionResult(WORT * 500, "pypdf", 1)

    with mock.patch.object(erkennung, "extract_text", return_value=ergebnis):
        assert erkennung.erkennen(str(datei.pk)) == erkennung.ERLEDIGT

    datei.refresh_from_db()
    assert len(datei.text_content or "") == kleine_grenze
    assert "gekürzt" in (datei.text_extraction_error or "")


# =============================================================================
# Anzeige: Vorgang und Sitzung
# =============================================================================


@pytest.mark.django_db
def test_vorgangsseite_laedt_den_volltext_nicht_und_zeigt_einen_anfang() -> None:
    # Ein Text von 300.000 Zeichen, etwa aus der Zeit vor der Obergrenze
    vorgang = _vorgang([WORT * 30_000])

    antwort = Client().get(f"/insight/vorgaenge/{vorgang.pk}/")

    assert antwort.status_code == 200
    assert _volltext_geladen(antwort.context["files"]) == []
    seite = antwort.content.decode()
    je_datei = getattr(ris, "TEXTANFANG_ZEICHEN", 100_000)
    assert 0 < seite.count(WORT.strip()) <= je_datei // len(WORT) + 1
    assert "gekürzt" in seite and ">Text<" in seite


@pytest.mark.django_db
def test_vorgangsseite_mit_vielen_anlagen_bleibt_unter_der_summe_je_seite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ris, "TEXTANFANG_ZEICHEN", 1_000, raising=False)
    monkeypatch.setattr(ris, "TEXTANFANG_JE_SEITE", 3_000, raising=False)
    vorgang = _vorgang([WORT * 200] * 6)  # je 2.000 Zeichen

    seite = Client().get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()

    # Jede Datei behält ihren Text, zusammen höchstens die Summe je Seite
    assert seite.count(">Text<") == 6
    assert seite.count(WORT.strip()) <= 3_000 // len(WORT) + 6


@pytest.mark.django_db
def test_kurzer_text_bleibt_vollstaendig_ohne_hinweis() -> None:
    vorgang = _vorgang(["Die Verwaltung nimmt wie folgt Stellung."])

    seite = Client().get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()

    assert "Die Verwaltung nimmt wie folgt Stellung." in seite and "gekürzt" not in seite


@pytest.mark.django_db
def test_work_vorgang_und_sitzung_laden_den_volltext_nicht(org: Any, make_member: Any, client_for: Any) -> None:
    vorgang = _vorgang([WORT * 30_000])
    sitzung = OParlMeeting.objects.create(
        external_id=f"https://ris.example.org/meetings/{uuid.uuid4()}",
        body=vorgang.body,
        name="Sitzung",
        start=timezone.now(),
    )
    _datei(vorgang.body, WORT * 30_000, meeting=sitzung, name="Einladung")
    org.body = vorgang.body
    org.work_new_design = True
    org.save(update_fields=["body", "work_new_design"])
    client = client_for(make_member(org, ["ris.view"]).user)
    je_datei = getattr(ris, "TEXTANFANG_ZEICHEN", 100_000)

    for name, kennung, dateien in (
        ("work:ris_paper_detail", {"paper_id": vorgang.pk}, "dateien"),
        ("work:ris_meeting_detail", {"meeting_id": sitzung.pk}, "sitzungsdateien"),
    ):
        antwort = client.get(reverse(name, kwargs={"org_slug": org.slug, **kennung}))
        assert antwort.status_code == 200
        assert len(antwort.context[dateien]) == 1 and _volltext_geladen(antwort.context[dateien]) == [], name
        assert 0 < antwort.content.decode().count(WORT.strip()) <= je_datei // len(WORT) + 1, name


# =============================================================================
# Anzeige: OParl
# =============================================================================


@pytest.mark.django_db
def test_oparl_listen_laden_den_text_eingebetteter_dateien_nicht() -> None:
    vorgang = _vorgang([WORT * 100])
    sitzung = OParlMeeting.objects.create(
        external_id=f"https://ris.example.org/meetings/{uuid.uuid4()}", body=vorgang.body, name="Sitzung"
    )
    _datei(vorgang.body, WORT * 100, meeting=sitzung, name="Einladung")

    vorgaenge = list(bestand.prepare_papers(OParlPaper.objects.filter(pk=vorgang.pk)))
    sitzungen = list(bestand.prepare_meetings(OParlMeeting.objects.filter(pk=sitzung.pk)))

    eingebettet = [*vorgaenge[0].files.all(), *sitzungen[0].files.all()]
    assert len(eingebettet) == 2 and _volltext_geladen(eingebettet) == []
    # Session-Schnittstelle: eingebettete Dateien und die Dateiliste ebenso ohne Text
    session: Any = session_ausgabe
    assert "text_content" in session._public_files_qs().query.deferred_loading[0]
    liste = session.LIST_SPECS["files"][1]
    assert "text_content" in liste(SessionFile.objects.all(), None).query.deferred_loading[0]


@pytest.mark.django_db
def test_einzelne_datei_mit_text_hoechstens_bis_zur_obergrenze() -> None:
    # Älterer Text über der Obergrenze, gespeichert vor ihrer Einführung
    grenze = getattr(bibliothek, "MAX_TEXT_CHARS", 5_000_000)
    vorgang = _vorgang(["x" * (grenze + 100)])
    datei = vorgang.files.get()
    cache.clear()

    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_API_RATE_LIMIT=0):
        antwort = Client().get(f"/oparl/v1/file/{datei.pk}")

    assert antwort.status_code == 200
    assert len(antwort.json()["text"]) == grenze
    # Dokumentabschnitte des KI-Assistenten lesen über dieselbe Grenze, ohne den gespeicherten Text zu laden
    mit_text = ris.public_file_with_text([vorgang.body_id], datei.pk)
    assert mit_text is not None and _volltext_geladen([mit_text]) == []
    assert len(canonical.file_text(mit_text) or "") == grenze


@pytest.mark.django_db
def test_session_schnittstelle_gibt_text_hoechstens_bis_zur_obergrenze_aus() -> None:
    grenze = getattr(bibliothek, "MAX_TEXT_CHARS", 5_000_000)
    tenant = SessionTenant.objects.create(name="Stadt Muster", slug="text-grenze", oparl_public_since=timezone.now())
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, organization=rat, name="Ratssitzung", start=timezone.now(), is_public=True
    )
    datei = SessionFile.objects.create(
        tenant=tenant, meeting=sitzung, name="Anlage.pdf", is_public=True, text_content="x" * (grenze + 100)
    )
    cache.clear()

    with override_settings(OPARL_API_RATE_LIMIT=0, OPARL_API_CACHE_SECONDS=0):
        objekt = Client().get(f"/session/{tenant.slug}/api/oparl/file/{datei.pk}/")
        sitzungen = Client().get(f"/session/{tenant.slug}/api/oparl/meetings/")

    assert objekt.status_code == 200 and len(objekt.json()["text"]) == grenze
    assert sitzungen.status_code == 200 and "x" * 100 not in sitzungen.content.decode()
    # Die eingebettete Niederschrift einer Sitzung kommt ohne Text
    session: Any = session_ausgabe
    vorbereitet = session.OBJECT_SPECS["meeting"][1](SessionMeeting.objects.all(), tenant)
    assert "protocol__public_file__text_content" in vorbereitet.query.deferred_loading[0]


# =============================================================================
# Anzeige: Unterlagen der Fraktionssitzung und KI-Zusammenfassung
# =============================================================================


@pytest.mark.django_db
def test_unterlage_der_fraktionssitzung_zeigt_nur_den_anfang() -> None:
    from django.template.loader import render_to_string

    from apps.work.faction import sitzung
    from apps.work.faction.views.session import unterlage_kontext

    vorgang = _vorgang([WORT * 30_000])
    datei = OParlFile.objects.defer("text_content", "raw_json").get(paper=vorgang)

    kontext = unterlage_kontext(sitzung.Unterlage(f"r-{datei.pk}", "ris_datei", "Anlage", datei, vorgang))

    assert _volltext_geladen([datei]) == []
    je_datei = getattr(ris, "TEXTANFANG_ZEICHEN", 100_000)
    assert 0 < len(kontext["text"]) <= je_datei and kontext.get("text_gekuerzt") is True
    html = render_to_string("work/faction/sitzung/_unterlage.html", {**kontext, "org_slug": "fraktion"})
    assert "Hier gekürzt" in html and html.count(WORT.strip()) <= je_datei // len(WORT) + 1


@pytest.mark.django_db
def test_ki_zusammenfassung_liest_hoechstens_ein_festes_budget() -> None:
    from insight_ai.services import summarizer

    vorgang = _vorgang([WORT * 15_000] * 3 + ["Kurzer Antrag zum Radweg."])  # 450.000 Zeichen und ein kurzer Text

    text = summarizer.SummaryService(provider=object())._collect_text_content(vorgang)

    budget = getattr(summarizer, "TEXT_BUDGET_CHARS", 200_000)
    assert len(text) <= budget + 1_000
    assert "Kurzer Antrag zum Radweg." in text and "gekürzt" in text
