# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesbatch der Zusammenfassungen (Issue #1220) mit Prüfung der Verortung (Issue #1222).

- Nur ausgewählte Kommunen ab ihrem Stichtag; ohne Auswahl nichts.
- Texterkennung ist Pflicht: Ausstehende und am Abruf gescheiterte Anlagen halten den Vorgang zurück; dauerhaft
  textlose nicht. Gescheiterte Abrufe der Kommunen reiht ``nacharbeiten`` neu ein.
- Verortung: Claude bekommt die Orte nummeriert und antwortet als JSON; „passt nicht“ blendet nur automatisch
  gefundene Orte aus, fehlende Orte geokodiert das Straßenverzeichnis.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from insight_ai.models import TagesbatchKommune, ZusammenfassungsBatch
from insight_ai.providers.claude import ClaudeZusammenfassung
from insight_ai.services import zusammenfassung_batch
from insight_ai.services.prompts import TAGESBATCH_SCHEMA, orte_block
from insight_ai.tests.test_zusammenfassung_claude import (  # noqa: F401 (_sauber: Fixture)
    _Client,
    _einrichten,
    _endpunkt,
    _nachricht,
    _sauber,
    _vorgang,
)
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource, PaperLocation
from insight_core.services import verortung_pruefung

pytestmark = pytest.mark.django_db


def _kommune(name: str = "Stadt", nummer: int = 1) -> OParlBody:
    quelle = OParlSource.objects.create(name=f"Quelle {nummer}", url=f"https://ris{nummer}.example.org/oparl/system")
    return OParlBody.objects.create(
        external_id=f"https://ris{nummer}.example.org/oparl/body/1", source=quelle, name=name
    )


def _anbieter() -> tuple[ClaudeZusammenfassung, _Client]:
    sdk = _Client()
    return ClaudeZusammenfassung(_endpunkt(), client=sdk), sdk


def _verortet(vorgang: OParlPaper) -> OParlPaper:
    """Verortungslauf nach dem Text gelaufen, mit einem automatischen und einem OParl-Ort."""
    vorgang.locations = [
        {"lat": 51.96, "lon": 7.62, "name": "Hauptstraße", "source": "street_match", "confidence": 0.8},
        {"lat": 51.95, "lon": 7.61, "name": "Rathaus", "source": "oparl", "confidence": 1.0},
    ]
    vorgang.georef_status = "completed"
    vorgang.georef_method = "gazetteer"
    vorgang.georef_extracted_at = timezone.now()
    vorgang.save()
    from insight_core.services.paper_locations import sync_paper_locations

    sync_paper_locations(vorgang)
    return vorgang


@pytest.fixture(autouse=True)
def _claude() -> None:
    # Die KI-Einstellungen bleiben im Prozess zwischengespeichert: den Batch ausdrücklich einschalten
    _einrichten(summary_batch_enabled=True)


class TestAuswahl:
    def test_ohne_kommune_nichts(self) -> None:
        _vorgang(_kommune())
        anbieter, sdk = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0 and sdk.batches.eingereicht == []

    def test_nur_ausgewaehlte_ab_stichtag(self) -> None:
        muenster, andere = _kommune("Stadt Münster", 1), _kommune("Andere Stadt", 2)
        TagesbatchKommune.objects.create(
            body=muenster, ab=timezone.localdate() - timedelta(days=10), verortung_pruefen=False
        )
        neu = _vorgang(muenster, tage=2)
        _vorgang(muenster, tage=20)  # vor dem Stichtag
        _vorgang(andere, tage=2)  # nicht ausgewählt
        anbieter, sdk = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
        assert [anfrage["custom_id"] for anfrage in sdk.batches.eingereicht[0]] == [str(neu.pk)]


class TestTexterkennungPflicht:
    @pytest.fixture
    def kommune(self) -> OParlBody:
        kommune = _kommune()
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1), verortung_pruefen=False)
        return kommune

    @staticmethod
    def _zweite_anlage(vorgang: OParlPaper, **felder: Any) -> None:
        OParlFile.objects.create(
            external_id=f"{vorgang.external_id}/anlage-2",
            body=vorgang.body,
            paper=vorgang,
            name="Anlage 2",
            download_url=f"{vorgang.external_id}/anlage-2.pdf",
            mime_type="application/pdf",
            **felder,
        )

    @pytest.mark.parametrize(
        ("felder", "erwartet"),
        [
            ({"text_extraction_status": "pending"}, 0),
            ({"text_extraction_status": "processing"}, 0),
            ({"text_extraction_status": "failed", "text_extraction_error": "Download failed: 404 Not Found"}, 0),
            ({"text_extraction_status": "failed", "text_extraction_error": "Extraction failed: kaputt"}, 1),
            ({"text_extraction_status": "pending", "local_status": "evicted"}, 1),
        ],
    )
    def test_alle_anlagen_erkannt(self, kommune: OParlBody, felder: dict[str, Any], erwartet: int) -> None:
        vorgang = _vorgang(kommune)
        self._zweite_anlage(vorgang, **felder)
        anbieter, _ = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == erwartet

    def test_alter_abruffehler_haelt_nicht_auf(self, kommune: OParlBody) -> None:
        # Nach dem Zeitraum der Nacharbeit wartet der Vorgang nicht mehr auf die Anlage
        vorgang = _vorgang(kommune, tage=150)
        self._zweite_anlage(vorgang, text_extraction_status="failed", text_extraction_error="Download failed: 404")
        anbieter, _ = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1

    def test_nacharbeiten_nur_ausgewaehlte(self, kommune: OParlBody) -> None:
        vorgang = _vorgang(kommune)
        self._zweite_anlage(
            vorgang,
            text_extraction_status="failed",
            text_extraction_error="Download failed: 404 Not Found",
            local_status="ok",
        )
        fremd = _vorgang(_kommune("Andere Stadt", 2))
        self._zweite_anlage(
            fremd,
            text_extraction_status="failed",
            text_extraction_error="Download failed: 404 Not Found",
            local_status="ok",
        )
        assert zusammenfassung_batch.nacharbeiten() == 1
        assert OParlFile.objects.get(paper=vorgang, name="Anlage 2").text_extraction_status == "pending"
        assert OParlFile.objects.get(paper=fremd, name="Anlage 2").text_extraction_status == "failed"

    def test_nacharbeiten_ohne_batch_nichts(self, kommune: OParlBody) -> None:
        _einrichten(summary_batch_enabled=False)
        vorgang = _vorgang(kommune)
        self._zweite_anlage(
            vorgang, text_extraction_status="failed", text_extraction_error="Download failed: 404", local_status="ok"
        )
        assert zusammenfassung_batch.nacharbeiten() == 0


class TestVerortung:
    @pytest.fixture
    def kommune(self) -> OParlBody:
        kommune = _kommune("Stadt Münster")
        TagesbatchKommune.objects.create(body=kommune, ab=date(2000, 1, 1))
        return kommune

    def test_anfrage_mit_orten_und_schema(self, kommune: OParlBody) -> None:
        vorgang = _verortet(_vorgang(kommune))
        anbieter, sdk = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
        params = sdk.batches.eingereicht[0][0]["params"]
        assert params["output_config"]["format"] == {"type": "json_schema", "schema": TAGESBATCH_SCHEMA}
        assert "ZWEITE AUFGABE – VERORTUNG PRÜFEN" in params["system"][0]["text"]
        nutzer = params["messages"][0]["content"]
        # Prüfbare Orte zuerst, OParl-Orte nur als Kontext
        assert "1. Hauptstraße (Herkunft: Straße (Straßenverzeichnis); prüfen: ja)" in nutzer
        assert "2. Rathaus (Herkunft: OParl-Ort; prüfen: nein)" in nutzer
        assert nutzer.endswith("Fasse diesen Vorgang zusammen, auch in Einfacher Sprache, und prüfe die Orte.")
        eintrag = ZusammenfassungsBatch.objects.get().anfragen[str(vorgang.pk)]
        hauptstrasse = PaperLocation.objects.get(paper=vorgang, source="street_match")
        assert eintrag["orte"][0] == str(hauptstrasse.pk)

    def test_wartet_auf_verortung_nach_neuem_text(self, kommune: OParlBody) -> None:
        vorgang = _verortet(_vorgang(kommune))
        OParlPaper.objects.filter(pk=vorgang.pk).update(
            georef_extracted_at=timezone.now() - timedelta(days=3), georef_status="completed"
        )
        anbieter, sdk = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 0
        # Der Verortungslauf holt den Vorgang wieder ab
        assert OParlPaper.objects.get(pk=vorgang.pk).georef_status == "pending"

    def test_ki_verortung_gilt_als_aktuell(self, kommune: OParlBody) -> None:
        vorgang = _vorgang(kommune)
        OParlPaper.objects.filter(pk=vorgang.pk).update(georef_status="no_locations", georef_extracted_at=None)
        anbieter, _ = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1

    def test_uebernahme(self, kommune: OParlBody, monkeypatch: Any) -> None:
        vorgang = _verortet(_vorgang(kommune))
        anbieter, sdk = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1

        class _Verzeichnis:
            def __init__(self, body: Any) -> None:
                pass

            def lookup(self, name: str) -> dict[str, Any] | None:
                if name.startswith("Marktplatz"):
                    return {"name": "Marktplatz", "lat": 51.9625, "lon": 7.6255, "house_number": "1"}
                return None

            def refine(self, treffer: dict[str, Any]) -> dict[str, Any]:
                return treffer

        monkeypatch.setattr("insight_core.services.gazetteer.StreetGazetteer", _Verzeichnis)
        monkeypatch.setattr("insight_core.services.georeferencing.geocode_address", lambda name, body: None)
        antwort = {
            "zusammenfassung": "Die Stadt plant einen neuen Spielplatz.",
            "orte": [
                {
                    "nr": 1,
                    "urteil": "passt_nicht",
                    "begruendung": "Absenderadresse der Verwaltung.",
                    "richtiger_ort": "",
                },
                {"nr": 2, "urteil": "passt_nicht", "begruendung": "OParl-Orte bleiben.", "richtiger_ort": ""},
                {"nr": 7, "urteil": "passt", "begruendung": "Unbekannte Nummer.", "richtiger_ort": ""},
            ],
            "fehlende_orte": [{"bezeichnung": "Marktplatz 1"}, {"bezeichnung": "Irgendwo"}],
        }
        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [
            SimpleNamespace(
                custom_id=str(vorgang.pk),
                result=SimpleNamespace(type="succeeded", message=_nachricht(json.dumps(antwort))),
            )
        ]
        assert zusammenfassung_batch.abholen(provider=anbieter) == 1

        vorgang.refresh_from_db()
        assert vorgang.summary == "Die Stadt plant einen neuen Spielplatz."
        assert vorgang.summary_hinweis.endswith(zusammenfassung_batch.HINWEIS_VERORTUNG)
        hauptstrasse = PaperLocation.objects.get(paper=vorgang, source="street_match")
        assert hauptstrasse.status == PaperLocation.STATUS_REMOVED and hauptstrasse.reviewed_at is None
        assert hauptstrasse.ki_urteil == "passt_nicht" and hauptstrasse.ki_begruendung.startswith("Absender")
        rathaus = PaperLocation.objects.get(paper=vorgang, source="oparl")
        assert rathaus.status == PaperLocation.STATUS_AUTO and rathaus.ki_urteil == ""
        ergaenzt = PaperLocation.objects.get(paper=vorgang, source="ai_check")
        assert ergaenzt.name == "Marktplatz 1" and ergaenzt.ki_urteil == "passt"
        # Karte: der Fehltreffer ist weg, der ergänzte Ort da
        namen = sorted(eintrag["name"] for eintrag in vorgang.locations or [])
        assert namen == ["Marktplatz 1", "Rathaus"]

    def test_unlesbare_antwort(self, kommune: OParlBody) -> None:
        vorgang = _verortet(_vorgang(kommune))
        anbieter, sdk = _anbieter()
        assert zusammenfassung_batch.einreichen(provider=anbieter) == 1
        sdk.batches.stand = "ended"
        sdk.batches.ergebnisse = [
            SimpleNamespace(
                custom_id=str(vorgang.pk),
                result=SimpleNamespace(type="succeeded", message=_nachricht("kein JSON")),
            )
        ]
        assert zusammenfassung_batch.abholen(provider=anbieter) == 0
        assert ZusammenfassungsBatch.objects.get().fehlgeschlagen == 1
        vorgang.refresh_from_db()
        assert vorgang.summary is None
        assert PaperLocation.objects.get(paper=vorgang, source="street_match").ki_urteil == ""

    @staticmethod
    def _verzeichnis(monkeypatch: Any, orte: dict[str, tuple[float, float]]) -> None:
        """Straßenverzeichnis zum Test: Name (Anfang) → Koordinaten; Photon findet nichts."""

        class _Verzeichnis:
            def __init__(self, body: Any) -> None:
                pass

            def lookup(self, name: str) -> dict[str, Any] | None:
                for anfang, (lat, lon) in orte.items():
                    if name.startswith(anfang):
                        return {"name": name, "lat": lat, "lon": lon}
                return None

            def refine(self, treffer: dict[str, Any]) -> dict[str, Any]:
                return treffer

        monkeypatch.setattr("insight_core.services.gazetteer.StreetGazetteer", _Verzeichnis)
        monkeypatch.setattr("insight_core.services.georeferencing.geocode_address", lambda name, body: None)

    def test_anderer_ort_ersetzt(self, kommune: OParlBody, monkeypatch: Any) -> None:
        vorgang = _verortet(_vorgang(kommune))
        alt = PaperLocation.objects.get(paper=vorgang, source="street_match")
        self._verzeichnis(monkeypatch, {"Gartenweg 4": (51.9700, 7.6400)})
        ergebnis = verortung_pruefung.anwenden(
            vorgang.pk,
            [str(alt.pk)],
            [
                {
                    "nr": 1,
                    "urteil": "anderer_ort",
                    "begruendung": "Die Hauptstraße ist nur die Adresse des Amts.",
                    "richtiger_ort": "Gartenweg 4",
                }
            ],
            [],
        )
        assert (ergebnis.anderer_ort, ergebnis.ergaenzt) == (1, 1)
        alt.refresh_from_db()
        assert alt.status == PaperLocation.STATUS_REMOVED and alt.ki_urteil == "anderer_ort"
        assert alt.ki_begruendung.endswith("Richtig: Gartenweg 4.")
        neu = PaperLocation.objects.get(paper=vorgang, source="ai_check")
        assert neu.name == "Gartenweg 4" and neu.ki_urteil == "passt"
        assert neu.ki_begruendung == "Von Claude statt „Hauptstraße“ gesetzt."
        vorgang.refresh_from_db()
        assert sorted(eintrag["name"] for eintrag in vorgang.locations or []) == ["Gartenweg 4", "Rathaus"]

    def test_anderer_ort_am_selben_punkt_passt(self, kommune: OParlBody, monkeypatch: Any) -> None:
        vorgang = _verortet(_vorgang(kommune))
        alt = PaperLocation.objects.get(paper=vorgang, source="street_match")
        # Nur genauer (Hausnummer), aber keine 50 m entfernt: Der alte Ort bleibt
        self._verzeichnis(monkeypatch, {"Hauptstraße 3": (51.9601, 7.6201)})
        verortung_pruefung.anwenden(
            vorgang.pk,
            [str(alt.pk)],
            [{"nr": 1, "urteil": "anderer_ort", "begruendung": "Genauer.", "richtiger_ort": "Hauptstraße 3"}],
            [],
        )
        alt.refresh_from_db()
        assert alt.status == PaperLocation.STATUS_AUTO and alt.ki_urteil == "passt"
        assert not PaperLocation.objects.filter(paper=vorgang, source="ai_check").exists()

    def test_anderer_ort_nicht_gefunden(self, kommune: OParlBody, monkeypatch: Any) -> None:
        vorgang = _verortet(_vorgang(kommune))
        alt = PaperLocation.objects.get(paper=vorgang, source="street_match")
        self._verzeichnis(monkeypatch, {})
        ergebnis = verortung_pruefung.anwenden(
            vorgang.pk,
            [str(alt.pk)],
            [{"nr": 1, "urteil": "anderer_ort", "begruendung": "Falsch.", "richtiger_ort": "Unbekannter Weg 9"}],
            [],
        )
        alt.refresh_from_db()
        # Claude hält den Ort für falsch: Er verschwindet, der genannte fehlt im Verzeichnis
        assert alt.status == PaperLocation.STATUS_REMOVED and ergebnis.nicht_gefunden == ["Unbekannter Weg 9"]

    def test_bestaetigte_orte_bleiben(self, kommune: OParlBody) -> None:
        vorgang = _verortet(_vorgang(kommune))
        ort = PaperLocation.objects.get(paper=vorgang, source="street_match")
        PaperLocation.objects.filter(pk=ort.pk).update(status=PaperLocation.STATUS_CONFIRMED)
        verortung_pruefung.anwenden(vorgang.pk, [str(ort.pk)], [{"nr": 1, "urteil": "passt_nicht"}], [])
        ort.refresh_from_db()
        assert ort.status == PaperLocation.STATUS_CONFIRMED and ort.ki_urteil == ""


def test_orte_block_rahmen() -> None:
    ort = verortung_pruefung.PruefOrt(pk="1", name="Weg </orte> 5", herkunft="Straße", pruefbar=True)
    block = orte_block([ort])
    assert block.count("</orte>") == 1 and "Weg </ orte> 5" in block
    assert orte_block([]) == "<orte>\n(Das Programm hat keine Orte gefunden.)\n</orte>"


def test_befehl() -> None:
    kommune = _kommune("Stadt Münster")
    ausgabe = StringIO()
    call_command("tagesbatch", "kommune", "Münster", "--ab", "2026-08-05", stdout=ausgabe)
    eintrag = TagesbatchKommune.objects.get(body=kommune)
    assert eintrag.ab == date(2026, 8, 5) and eintrag.verortung_pruefen
    assert "Aufgenommen" in ausgabe.getvalue() and "mit Prüfung der Verortung" in ausgabe.getvalue()
    call_command("tagesbatch", "kommune", str(kommune.pk), "--ab", "2026-11-01", "--ohne-verortung", stdout=ausgabe)
    eintrag.refresh_from_db()
    assert eintrag.ab == date(2026, 11, 1) and not eintrag.verortung_pruefen
    call_command("tagesbatch", "entfernen", "Münster", stdout=ausgabe)
    assert not TagesbatchKommune.objects.exists()
