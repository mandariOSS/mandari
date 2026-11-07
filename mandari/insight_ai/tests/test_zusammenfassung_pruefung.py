# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mechanische Prüfung der KI-Zusammenfassung vor dem Speichern (Issue #1271).

Geprüft wird:

- die Regeln einzeln: Zahlen, Daten und Beträge nur aus der Quelle (gerundete große Beträge erlaubt, ausgerechnete
  Summen nicht), Parteien und Fraktionen nur aus der Quelle, keine Übernahme von mehr als zehn Wörtern am Stück (Titel
  und Gremien ausgenommen), keine Hausnummern,
- Klick auf „Zusammenfassen“: eine verworfene Zusammenfassung wird protokolliert, nicht gespeichert und nicht
  ausgegeben; fällt nur die Fassung in Einfacher Sprache durch, entfällt nur sie; gespeichert wird mit der
  Prompt-Fassung,
- Tagesbatch: verworfene Ergebnisse werden protokolliert und nicht übernommen, und derselbe Vorgang geht mit derselben
  Prompt-Fassung nicht erneut in einen Batch.

Kein Netz: Der SDK-Client ist ein Ersatz. Nur erfundene Namen und Anschriften.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_ai.models import TagesbatchKommune, VerworfeneZusammenfassung, ZusammenfassungsBatch
from insight_ai.providers.claude import ClaudeZusammenfassung
from insight_ai.services import zusammenfassung_batch
from insight_ai.services.prompts import PAPER_SUMMARY_SYSTEM_PROMPT, PROMPT_FASSUNG
from insight_ai.services.summarizer import SummaryRejectedError, SummaryService
from insight_ai.services.zusammenfassung_pruefung import (
    Quelle,
    fremde_fraktion,
    fremde_zahl,
    fremdes_datum,
    hausnummer,
    pruefen,
    uebernahme,
)
from insight_ai.tests.test_zusammenfassung_claude import (  # noqa: F401 (_sauber: Fixture)
    _als_json,
    _Client,
    _einrichten,
    _endpunkt,
    _nachricht,
    _sauber,
    _vorgang,
)
from insight_core.models import OParlBody, OParlPaper, OParlSource


@pytest.fixture
def body() -> OParlBody:
    """Kommune im Tagesbatch (Vorgänge der letzten 30 Tage, ohne Prüfung der Verortung)."""
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    kommune = OParlBody.objects.create(
        external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt", slug="stadt"
    )
    TagesbatchKommune.objects.create(
        body=kommune, ab=timezone.localdate() - timedelta(days=30), verortung_pruefen=False
    )
    return kommune


QUELLE = Quelle(
    text=(
        "<metadaten>\nKommune: Stadt Beispielhausen\nTitel: Sanierung der Brücke an der Kanalstraße im Ortsteil Mitte\n"
        "Datum: 2026-11-02\n</metadaten>\n<beratungsverlauf>\nStand: Am 07.10.2026 im Rat mehrheitlich beschlossen.\n"
        "- 07.10.2026, Rat (entscheidend): Ergebnis: mehrheitlich beschlossen\n</beratungsverlauf>\n<dokumente>\n"
        "Die Verwaltung gewährt einen Zuschuss von 20.000 EUR für 2023 und 1.661.000 EUR für 2024. Antrag der Fraktion "
        "BÜNDNIS 90/DIE GRÜNEN. Die Sitzung beginnt um 18:00 Uhr. Die Kosten betragen 14,9 Mio. €. Es werden 1 200 "
        "Bäume gepflanzt. Vorlage V/0459/2026. Die Brücke ist seit 1987 in Betrieb und muss bis Ende 2028 erneuert "
        "werden, weil der Beton an mehreren Stellen geschädigt ist und die Tragfähigkeit nicht mehr ausreicht.\n"
        "</dokumente>"
    ),
    erlaubt=("Sanierung der Brücke an der Kanalstraße im Ortsteil Mitte", "Ausschuss für Bauen und Verkehr"),
)


# --- Regeln einzeln --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Der Zuschuss beträgt 20.000 Euro für 2023.",
        "Für 2024 sind es rund 1,7 Millionen Euro.",  # gerundet, mit „rund“
        "Für 2024 sind es 1,66 Millionen Euro.",  # großer Betrag, knapp gerundet
        "Insgesamt kostet die Sanierung etwa 15 Millionen Euro.",
        "Es werden 1.200 Bäume gepflanzt, die Sitzung beginnt um 18 Uhr.",
        "Es geht um die Vorlage 0459 aus 2026.",
        "Drei Brücken und 2 Wege.",  # einstellige Zahlen schreibt man oft als Wort
    ],
)
def test_zahlen_aus_der_quelle(text: str) -> None:
    assert fremde_zahl(text, QUELLE) is None


@pytest.mark.parametrize(
    ("text", "zahl"),
    [
        ("Insgesamt sind es 1.681.000 Euro.", "1.681.000"),  # ausgerechnete Summe
        ("Es kostet 2 Millionen Euro.", "2 Millionen"),  # Betrag mit Größenwort auch einstellig
        ("Die Brücke ist 39 Jahre alt.", "39"),  # erschlossen, steht nicht im Text
        ("25 % der Bäume", "25"),
    ],
)
def test_fremde_zahlen(text: str, zahl: str) -> None:
    assert fremde_zahl(text, QUELLE) == zahl


def test_daten() -> None:
    assert fremdes_datum("Der Rat hat am 7. Oktober 2026 zugestimmt.", QUELLE) is None
    assert fremdes_datum("Am 07.10.2026 beschlossen, vorgelegt am 2. November.", QUELLE) is None  # ISO in der Quelle
    assert fremdes_datum("Der Rat hat am 8. Oktober 2026 zugestimmt.", QUELLE) == "8. Oktober 2026"
    assert fremdes_datum("Beschluss vom 07.10.2025.", QUELLE) == "07.10.2025"


def test_fraktionen() -> None:
    # „die Grünen“ ist durch „BÜNDNIS 90/DIE GRÜNEN“ belegt, die CDU nicht
    assert fremde_fraktion("Die Grünen beantragen einen Zuschuss.", QUELLE) is None
    assert fremde_fraktion("Die CDU lehnt den Zuschuss ab.", QUELLE) == "CDU"
    assert fremde_fraktion("Die linke Fahrspur wird gesperrt.", QUELLE) is None


def test_uebernahme_und_erlaubte_angaben() -> None:
    abgeschrieben = "Die Brücke muss bis Ende 2028 erneuert werden, weil der Beton an mehreren Stellen geschädigt ist."
    assert uebernahme(abgeschrieben, QUELLE) is not None
    eigene_worte = "Weil der Beton Schäden hat und die Brücke nicht mehr genug trägt, wird sie bis 2028 erneuert."
    assert uebernahme(eigene_worte, QUELLE) is None
    # Der Titel darf wörtlich stehen, auch wenn er länger als zehn Wörter ist
    assert uebernahme("Es geht um die Sanierung der Brücke an der Kanalstraße im Ortsteil Mitte.", QUELLE) is None


@pytest.mark.parametrize(
    "text",
    [
        "Der Bauantrag für die Hauptstraße 12a wird beraten.",
        "Am Burggraben 3 entsteht ein Haus.",
        "Der Spielplatz an der Talweg-Ecke Ringstraße 7 wird erneuert.",
        "Ein Anwohner der Kanalstr. 5 beschwert sich.",
        "Vor dem Haus Bonner Platz 3 fehlen Bänke.",
        "Gebaut wird am Markt 2.",
        "Die Häuser Hauptstraße 12-14 werden saniert.",
    ],
)
def test_hausnummern(text: str) -> None:
    assert hausnummer(text)


@pytest.mark.parametrize(
    "text",
    [
        "An der Hauptstraße werden 12 Bäume gepflanzt.",
        "Die Bundesstraße 51 wird gesperrt.",
        "Der Antrag steht auf Platz 3 der Liste.",
        "Zur Vorlage 123/2026 gibt es eine Stellungnahme.",
        "Am Montag 12 neue Anträge.",
        "an der Hauptstraße, 12 Bäume und zwei Bänke",
        "Der Radweg wird 300 Meter lang.",
    ],
)
def test_keine_hausnummern(text: str) -> None:
    assert not hausnummer(text)


def test_reihenfolge_und_entwurf() -> None:
    befund = pruefen("Hauptstraße 12: Die CDU will 99 Bäume.", QUELLE)
    assert befund is not None and befund.regel == "hausnummer" and not befund.entwurf_aufbewahren
    assert "12" not in befund.grund  # die Hausnummer steht auch nicht im Protokoll
    befund = pruefen("Die CDU will 99 Bäume.", QUELLE)
    assert befund is not None and befund.regel == "zahl" and befund.entwurf_aufbewahren
    leer = pruefen("", QUELLE)
    assert leer is not None and leer.regel == "leer"
    assert pruefen("Die Brücke an der Kanalstraße wird für rund 15 Millionen Euro saniert.", QUELLE) is None


def test_prompt_nennt_die_regeln() -> None:
    assert PROMPT_FASSUNG >= 2
    assert "Worum es geht" in PAPER_SUMMARY_SYSTEM_PROMPT and "zwei bis vier kurze Sätze" in PAPER_SUMMARY_SYSTEM_PROMPT
    assert "keine Hausnummern" in PAPER_SUMMARY_SYSTEM_PROMPT
    assert "mehr als zehn Wörtern" in PAPER_SUMMARY_SYSTEM_PROMPT


# --- Klick auf „Zusammenfassen“ -------------------------------------------------------------------------------------


def _claude(text: str) -> tuple[ClaudeZusammenfassung, _Client]:
    sdk = _Client(_nachricht(text))
    return ClaudeZusammenfassung(_endpunkt(), client=sdk), sdk


@pytest.mark.django_db
class TestKlick:
    @pytest.fixture(autouse=True)
    def _einrichtung(self) -> None:
        _einrichten()

    def test_verworfen_nicht_gespeichert_und_protokolliert(self, body: OParlBody, monkeypatch: Any) -> None:
        vorgang = _vorgang(body, text="Der Rat beschließt den Bau einer Brücke für 2.500.000 Euro.")
        anbieter, _sdk = _claude("Die CDU will eine Brücke für 3 Millionen Euro bauen.")
        monkeypatch.setattr("insight_ai.services.summarizer.get_summary_provider", lambda: anbieter)

        html = Client().post(reverse("insight_core:insight:paper_summary", args=[vorgang.pk])).content.decode()

        vorgang.refresh_from_db()
        assert vorgang.summary is None and vorgang.summary_prompt_fassung == 0
        assert "automatische Prüfung nicht bestanden" in html and "3 Millionen" not in html
        eintrag = VerworfeneZusammenfassung.objects.get()
        assert eintrag.vorgang_id == vorgang.pk and eintrag.kommune_id == body.pk
        assert (eintrag.regel, eintrag.weg, eintrag.teil, eintrag.prompt_fassung) == (
            "zahl",
            "klick",
            "zusammenfassung",
            PROMPT_FASSUNG,
        )
        assert eintrag.entwurf.startswith("Die CDU") and eintrag.modell == "claude-haiku-5-5"

    def test_bestanden_mit_prompt_fassung(self, body: OParlBody) -> None:
        vorgang = _vorgang(body, text="Der Rat beschließt den Bau einer Brücke für 2.500.000 Euro.")
        anbieter, _sdk = _claude("Eine neue Brücke soll gebaut werden. Sie kostet rund 2,5 Millionen Euro.")

        SummaryService(provider=anbieter).generate_summary(vorgang)

        vorgang.refresh_from_db()
        assert (vorgang.summary or "").startswith("Eine neue Brücke")
        assert vorgang.summary_prompt_fassung == PROMPT_FASSUNG
        assert not VerworfeneZusammenfassung.objects.exists()

    def test_nur_einfache_sprache_verworfen(self, body: OParlBody) -> None:
        vorgang = _vorgang(body, text="Der Rat beschließt den Bau einer Brücke.")
        antwort = {
            "zusammenfassung": "Eine neue Brücke soll gebaut werden.",
            "einfache_sprache": "Die Stadt baut eine Brücke. Die SPD findet das gut.",
        }
        anbieter, _sdk = _claude(json.dumps(antwort))

        assert SummaryService(provider=anbieter).generate_summary(vorgang) == antwort["zusammenfassung"]

        vorgang.refresh_from_db()
        assert vorgang.summary == antwort["zusammenfassung"] and vorgang.summary_einfach is None
        eintrag = VerworfeneZusammenfassung.objects.get()
        assert (eintrag.teil, eintrag.regel, eintrag.grund) == (
            "einfache_sprache",
            "fraktion",
            "Nicht in der Quelle: SPD",
        )

    def test_hausnummer_ohne_entwurf(self, body: OParlBody) -> None:
        vorgang = _vorgang(body, text="Bauantrag für das Grundstück Ahornweg 17 in Beispielhausen.")
        anbieter, _sdk = _claude("Für das Grundstück Ahornweg 17 liegt ein Bauantrag vor.")

        with pytest.raises(SummaryRejectedError):
            SummaryService(provider=anbieter).generate_summary(vorgang)

        eintrag = VerworfeneZusammenfassung.objects.get()
        assert eintrag.regel == "hausnummer" and eintrag.entwurf == "" and "17" not in eintrag.grund


# --- Tagesbatch ------------------------------------------------------------------------------------------------------


def _ergebnis(vorgang: OParlPaper, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        custom_id=str(vorgang.pk), result=SimpleNamespace(type="succeeded", message=_als_json(_nachricht(text)))
    )


@pytest.mark.django_db
class TestTagesbatch:
    @pytest.fixture(autouse=True)
    def _einrichtung(self) -> None:
        _einrichten(summary_batch_enabled=True)

    def test_verworfen_nicht_uebernommen_und_nicht_erneut_eingereicht(self, body: OParlBody) -> None:
        gut = _vorgang(body, tage=2, text="Der Rat beschließt den Bau einer Brücke.")
        schlecht = _vorgang(body, tage=1, text="Der Rat beschließt den Bau einer Brücke.")
        anbieter, sdk = _claude("Platzhalter.")
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 2

        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [
            _ergebnis(gut, "Eine neue Brücke soll gebaut werden."),
            _ergebnis(schlecht, "Die FDP will die Brücke bis 2031 bauen."),
        ]
        assert zusammenfassung_batch.abholen(provider=anbieter) == 1

        gut.refresh_from_db()
        schlecht.refresh_from_db()
        assert gut.summary == "Eine neue Brücke soll gebaut werden." and gut.summary_prompt_fassung == PROMPT_FASSUNG
        assert schlecht.summary is None
        eintrag = VerworfeneZusammenfassung.objects.get()
        assert (eintrag.vorgang_id, eintrag.weg, eintrag.regel) == (schlecht.pk, "batch", "zahl")
        assert eintrag.batch_id and eintrag.kommune_id == body.pk
        assert ZusammenfassungsBatch.objects.get().verworfen == 1

        # Mit derselben Prompt-Fassung nicht erneut: kostet nur wieder
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0
        assert len(sdk.batches.eingereicht) == 1

    def test_prompt_fassung_aus_dem_einreichen(self, body: OParlBody) -> None:
        vorgang = _vorgang(body, text="Der Rat beschließt den Bau einer Brücke.")
        anbieter, sdk = _claude("Platzhalter.")
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
        batch = ZusammenfassungsBatch.objects.get()
        assert batch.anfragen[str(vorgang.pk)]["fassung"] == PROMPT_FASSUNG
        # Ein Batch von vor der Zählung (ohne Fassung) speichert 0, auch wenn inzwischen eine neue Fassung gilt
        del batch.anfragen[str(vorgang.pk)]["fassung"]
        batch.save(update_fields=["anfragen"])

        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [_ergebnis(vorgang, "Eine neue Brücke soll gebaut werden.")]
        assert zusammenfassung_batch.abholen(provider=anbieter) == 1
        vorgang.refresh_from_db()
        assert vorgang.summary_prompt_fassung == 0
