# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Worum es geht“ im Bürgerportal (Issue #1271): Kurzfassung oben auf dem Vorgang, als Vorschauzeile in Liste und Suche.

- Vorgangsseite: Titel „Worum es geht“ mit dem Zeichen „KI-Kurzfassung“, zuerst der erste Absatz, darunter in einer
  Zeile Hinweis, Einfache Sprache und Bewertung; Work bleibt bei „KI-Zusammenfassung“.
- Vorgangsliste: der erste Absatz unter dem Titel (gekennzeichnet, ein Hinweis über der Liste), die Dokumentzeile
  dann ohne Dateityp und Größe; ohne Kurzfassung bleibt alles wie bisher. Veraltete Zusammenfassungen (anderer
  Beratungsstand) erscheinen nicht in der Liste. Höchstens eine Abfrage mehr, keine je Vorgang.
- Suche: die Kurzfassung statt des Textausschnitts.

Nur erfundene Namen.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.template import engines
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_cotton.compiler_regex import CottonCompiler

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_core.services import kurzfassung, summary_store

pytestmark = pytest.mark.django_db

RIS = "https://ris.vorschaustadt.example/oparl"
LISTE = "/insight/k/vorschaustadt/vorgaenge/"
KURZ = "Die Verwaltung schlägt vor, am Kanal einen Radweg zu bauen. Der Rat hat am 07.10.2026 zugestimmt."
REST = "Der Weg soll 2027 fertig sein. Er kostet rund 1,2 Millionen Euro."


@pytest.fixture
def kommune() -> OParlBody:
    source = OParlSource.objects.create(name="RIS Vorschaustadt", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Vorschaustadt", slug="vorschaustadt"
    )


def _vorgang(body: OParlBody, nummer: int, **felder: Any) -> OParlPaper:
    felder.setdefault("name", f"Radweg am Kanal {nummer}")
    felder.setdefault("reference", f"V/{nummer:04d}/2026")
    vorgang = OParlPaper.objects.create(external_id=f"{RIS}/paper/{nummer}", body=body, **felder)
    OParlFile.objects.create(
        external_id=f"{RIS}/file/{nummer}",
        body=body,
        paper=vorgang,
        name=f"Vorlage {nummer}",
        mime_type="application/pdf",
        size=245760,
        download_url=f"https://ris.vorschaustadt.example/datei/{nummer}.pdf",
        text_content="Die Verwaltung schlägt einen Radweg am Kanal vor.",
    )
    return vorgang


def _beschlossen(vorgang: OParlPaper, nummer: int) -> None:
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{nummer}", body=vorgang.body, name="Rat", start=timezone.now() - timedelta(days=3)
    )
    punkt = OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agendaitem/{nummer}", meeting=sitzung, number="4", public=True, result="beschlossen"
    )
    OParlConsultation.objects.create(
        external_id=f"{RIS}/consultation/{nummer}",
        body=vorgang.body,
        paper=vorgang,
        paper_external_id=vorgang.external_id,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
    )


def _mit_zusammenfassung(vorgang: OParlPaper, text: str = f"{KURZ}\n\n{REST}", *, passend: bool = True) -> None:
    stand = summary_store.beratungsstaende([vorgang.pk]).get(vorgang.pk, "") if passend else "anderer-stand"
    OParlPaper.objects.filter(pk=vorgang.pk).update(summary=text, summary_stand=stand, summary_prompt_fassung=2)


# --- Kurzfassung ------------------------------------------------------------------------------------------------------


def test_kurzfassung_ist_der_erste_absatz() -> None:
    assert kurzfassung.kurzfassung(f"{KURZ}\n\n{REST}") == KURZ
    assert kurzfassung.kurzfassung("") == ""
    # Ein einziger langer Satz ohne Absatz: am Wortende gekürzt
    lang = "Der Radweg am Kanal " + " ".join(["und noch ein Abschnitt"] * 40) + "."
    gekuerzt = kurzfassung.kurzfassung(lang)
    assert len(gekuerzt) <= kurzfassung.VORSCHAU_ZEICHEN and gekuerzt.endswith("…")


# --- Vorgangsseite -----------------------------------------------------------------------------------------------------


def test_vorgangsseite_worum_es_geht(kommune: OParlBody) -> None:
    vorgang = _vorgang(kommune, 1)
    _mit_zusammenfassung(vorgang)
    OParlPaper.objects.filter(pk=vorgang.pk).update(summary_einfach="Am Kanal kommt ein Radweg.")

    html = Client().get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()

    assert ">Worum es geht</h2>" in html and 'data-testid="ki-marke"' in html and "KI-Kurzfassung" in html
    # Kern zuerst, der Rest nach „Weiterlesen“
    assert html.index(f"<p>{KURZ}</p>") < html.index("Weiterlesen") and f"<p>{REST}</p>" in html
    assert "Mit KI zusammengefasst; Fehler sind möglich." in html
    assert "In Einfacher Sprache" in html and "Hilfreich?" in html and 'x-data="kiBewertung"' in html
    assert ">KI-Zusammenfassung</h2>" not in html


# --- Vorgangsliste -----------------------------------------------------------------------------------------------------


def test_liste_zeigt_kurzfassung_statt_dateigroesse(kommune: OParlBody) -> None:
    mit = _vorgang(kommune, 1)
    _beschlossen(mit, 1)
    _mit_zusammenfassung(mit)
    ohne = _vorgang(kommune, 2)

    html = Client().get(LISTE).content.decode()

    assert 'data-testid="ki-hinweis"' in html and "fasst eine KI aus den Dokumenten zusammen" in html
    assert f'data-testid="ki-vorschau"><span class="sr-only">KI-Kurzfassung: </span>{KURZ}</p>' in html
    assert 'data-ai-generated="true"' in html and REST not in html
    # Die Vorlage bleibt verlinkt; Typ und Größe nur noch beim Vorgang ohne Kurzfassung
    assert html.count('data-testid="vorgang-dokumente"') == 2 and html.count("PDF, 240.0 KB") == 1
    assert str(ohne.name) in html


def test_vorschau_ungepruefter_zusammenfassung_ohne_hausnummer(kommune: OParlBody) -> None:
    """Zusammenfassungen von vor der Prüfung (Fassung 0 und 1) zeigt die Vorschau nur ohne Hausnummer (#1269)."""
    from insight_core.services.ortsschutz import OHNE_NR

    alt = _vorgang(kommune, 1)
    _beschlossen(alt, 1)
    _mit_zusammenfassung(alt, "Vor dem Haus Hauptstraße 12 soll ein Radweg entstehen.\n\nMehr dazu.")
    OParlPaper.objects.filter(pk=alt.pk).update(summary_prompt_fassung=0)

    html = Client().get(LISTE).content.decode()
    assert f"Hauptstraße {OHNE_NR}" in html and "Hauptstraße 12" not in html

    # Geprüft (Fassung 2) und kein geschützter Vorgang: Text unverändert
    OParlPaper.objects.filter(pk=alt.pk).update(summary_prompt_fassung=2)
    assert "Hauptstraße 12" in Client().get(LISTE).content.decode()


def test_liste_ohne_kurzfassung_ohne_hinweis(kommune: OParlBody) -> None:
    _vorgang(kommune, 1)
    html = Client().get(LISTE).content.decode()
    assert "ki-hinweis" not in html and "ki-vorschau" not in html and "PDF, 240.0 KB" in html


def test_liste_ohne_veraltete_zusammenfassung(kommune: OParlBody) -> None:
    vorgang = _vorgang(kommune, 1)
    _beschlossen(vorgang, 1)
    _mit_zusammenfassung(vorgang, passend=False)
    assert summary_store.veraltet(OParlPaper.objects.get(pk=vorgang.pk))

    html = Client().get(LISTE).content.decode()

    assert KURZ not in html and "ki-hinweis" not in html


def test_liste_hoechstens_eine_abfrage_mehr(kommune: OParlBody) -> None:
    def abfragen() -> int:
        client = Client()
        client.get(LISTE)  # Caches anwärmen
        with CaptureQueriesContext(connection) as erfasst:
            assert client.get(LISTE).status_code == 200
        return len(erfasst.captured_queries)

    vorgaenge = [_vorgang(kommune, nummer) for nummer in range(1, 6)]
    ohne = abfragen()
    for vorgang in vorgaenge:
        _mit_zusammenfassung(vorgang)
    mit = abfragen()
    assert mit - ohne <= 1


# --- Suche -----------------------------------------------------------------------------------------------------------


def test_suchtreffer_mit_kurzfassung_statt_ausschnitt(kommune: OParlBody) -> None:
    vorgang = _vorgang(kommune, 1)
    _mit_zusammenfassung(vorgang)
    treffer: list[dict[str, Any]] = [
        {"kind": "vorgang", "pk": str(vorgang.pk), "url": "/v/", "title": "Radweg", "snippet": "… Kanal …"},
        {"kind": "vorgang", "pk": "keine-kennung", "url": "/w/", "title": "Ohne", "snippet": "Ausschnitt bleibt"},
        {"kind": "sitzung", "pk": str(vorgang.pk), "url": "/s/", "title": "Rat"},
    ]

    kurzfassung.an_treffer(treffer)

    assert treffer[0]["vorschau"] == KURZ and treffer[1]["vorschau"] == "" and "vorschau" not in treffer[2]
    vorlage = engines["django"].from_string(
        CottonCompiler().process('<ol><c-suche.treffer-vorgang :treffer="t" /></ol>')
    )
    html = vorlage.render({"t": treffer[0]})
    assert KURZ in html and 'data-ai-generated="true"' in html and "Kanal …" not in html
    html = vorlage.render({"t": treffer[1]})
    assert "Ausschnitt bleibt" in html and "data-ai-generated" not in html
