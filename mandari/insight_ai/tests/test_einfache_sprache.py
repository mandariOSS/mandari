# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassung in Einfacher Sprache (Issue #1220): Claude schreibt sie in derselben Anfrage (JSON nach Schema),
beim Klick wie im Tagesbatch; andere Anbieter liefern weiter nur Text. Sie verfällt mit der Zusammenfassung.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.common.ki_anbieter import claude_fuer_zusammenfassung
from insight_ai.models import TagesbatchKommune
from insight_ai.providers.claude import ClaudeZusammenfassung
from insight_ai.services import zusammenfassung_batch
from insight_ai.services.prompts import ZUSAMMENFASSUNG_SCHEMA
from insight_ai.services.summarizer import SummaryService, zerlegen
from insight_ai.tests.test_zusammenfassung_claude import (  # noqa: F401 (_sauber: Fixture)
    _Client,
    _einrichten,
    _endpunkt,
    _nachricht,
    _sauber,
    _vorgang,
)
from insight_core.models import OParlBody, OParlFile, OParlSource
from insight_core.services import summary_store

pytestmark = pytest.mark.django_db

ANTWORT = {
    "zusammenfassung": "Die Stadt will am Markt einen Spielplatz bauen. Der Rat entscheidet im Dezember.",
    "einfache_sprache": "Die Stadt will einen Spielplatz bauen. Er soll am Markt sein.",
}


@pytest.fixture
def body() -> OParlBody:
    """Kommune im Tagesbatch ohne Prüfung der Verortung."""
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    kommune = OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt")
    TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
    return kommune


@pytest.fixture(autouse=True)
def _claude() -> None:
    _einrichten(summary_batch_enabled=True)


def test_zerlegen() -> None:
    assert zerlegen(json.dumps(ANTWORT)) == (ANTWORT["zusammenfassung"], ANTWORT["einfache_sprache"])
    assert zerlegen("Nur Text.") == ("Nur Text.", "")
    assert zerlegen("{kaputt") == ("", "")
    assert zerlegen("Nur Text.", json_pflicht=True) == ("", "")


def test_klick_mit_claude(body: OParlBody, monkeypatch: Any) -> None:
    vorgang = _vorgang(body)
    sdk = _Client(_nachricht(json.dumps(ANTWORT)))
    anbieter = ClaudeZusammenfassung(claude_fuer_zusammenfassung(), client=sdk)  # type: ignore[arg-type]
    monkeypatch.setattr("insight_ai.services.summarizer.get_summary_provider", lambda: anbieter)
    assert SummaryService().generate_summary(vorgang) == ANTWORT["zusammenfassung"]
    vorgang.refresh_from_db()
    assert vorgang.summary == ANTWORT["zusammenfassung"]
    assert vorgang.summary_einfach == ANTWORT["einfache_sprache"]
    (anfrage,) = sdk.anfragen
    assert anfrage["output_config"]["format"] == {"type": "json_schema", "schema": ZUSAMMENFASSUNG_SCHEMA}
    assert "EINFACHE SPRACHE" in anfrage["system"][0]["text"]
    assert anfrage["messages"][0]["content"].endswith("Fasse diesen Vorgang zusammen, auch in Einfacher Sprache.")


def test_tagesbatch_speichert_einfache_sprache(body: OParlBody) -> None:
    vorgang = _vorgang(body)
    sdk = _Client()
    anbieter = ClaudeZusammenfassung(_endpunkt(), client=sdk)
    assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
    params = sdk.batches.eingereicht[0][0]["params"]
    # Ohne Prüfung der Verortung (Fixture): Schema nur mit beiden Fassungen
    assert params["output_config"]["format"]["schema"] == ZUSAMMENFASSUNG_SCHEMA
    sdk.batches.stand = "ended"
    sdk.batches.ergebnisse = [
        SimpleNamespace(
            custom_id=str(vorgang.pk),
            result=SimpleNamespace(type="succeeded", message=_nachricht(json.dumps(ANTWORT))),
        )
    ]
    assert zusammenfassung_batch.abholen(provider=anbieter) == 1
    vorgang.refresh_from_db()
    assert (vorgang.summary, vorgang.summary_einfach) == (ANTWORT["zusammenfassung"], ANTWORT["einfache_sprache"])


def test_verfaellt_mit_der_zusammenfassung(body: OParlBody) -> None:
    vorgang = _vorgang(body, summary="Alt.", summary_einfach="Alt einfach.")
    summary_store.discard_for_files([OParlFile.objects.get(paper=vorgang).pk])
    vorgang.refresh_from_db()
    assert vorgang.summary is None and vorgang.summary_einfach is None


def test_umschalter_auf_der_seite(body: OParlBody, client: Client) -> None:
    vorgang = _vorgang(body, summary="Ausführlich.", summary_einfach="Ganz einfach.")
    html = client.get(reverse("insight_core:insight:paper_summary", args=[vorgang.pk])).content.decode()
    assert "Ganz einfach." in html and "In Einfacher Sprache" in html
    ohne = _vorgang(body, summary="Nur ausführlich.")
    html = client.get(reverse("insight_core:insight:paper_summary", args=[ohne.pk])).content.decode()
    assert "In Einfacher Sprache" not in html


def test_absaetze_mit_weiterlesen(body: OParlBody, client: Client) -> None:
    vorgang = _vorgang(body, summary="Kern des Vorgangs.\n\nEinzelheiten eins.\n\nEinzelheiten zwei.")
    html = client.get(reverse("insight_core:insight:paper_summary", args=[vorgang.pk])).content.decode()
    assert ">KI-Zusammenfassung</h2>" in html
    # Der Kern steht vor dem eingeklappten Rest, jeder Absatz in einem eigenen <p>
    assert (
        html.index("<p>Kern des Vorgangs.</p>") < html.index('x-show="offen"') < html.index("<p>Einzelheiten zwei.</p>")
    )
    assert "Weiterlesen" in html and 'aria-expanded="false"' in html
    # Nur ein Absatz: nichts einzuklappen
    kurz = _vorgang(body, summary="Nur ein Satz.")
    html = client.get(reverse("insight_core:insight:paper_summary", args=[kurz.pk])).content.decode()
    assert "<p>Nur ein Satz.</p>" in html and "Weiterlesen" not in html


def test_absaetze_aus_einfachen_zeilenumbruechen() -> None:
    from insight_core.templatetags.ki_bewertung import ki_absaetze

    assert ki_absaetze("Eins.\nZwei.") == ["Eins.", "Zwei."]
    assert ki_absaetze("Eins\nweiter.\n\nZwei.") == ["Eins weiter.", "Zwei."]
    assert ki_absaetze(None) == []
