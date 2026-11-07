# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bewertung einer Prompt-Fassung an einer festen Stichprobe (Issue #1271, ``manage.py zusammenfassung_stichprobe``).

- Die Stichprobe ist fest: dieselbe Kommune, Fassung und Grenze ergeben dieselben Vorgänge in derselben Reihenfolge.
- Ohne ``--ausfuehren`` kein KI-Aufruf: Der Prüfbogen enthält die vorhandenen Zusammenfassungen mit ihrer Prüfung.
- Mit ``--ausfuehren`` je Vorgang ein Einzelaufruf; gespeichert, angezeigt oder protokolliert wird nichts, und über der
  Kostengrenze geht gar nichts hinaus.
- ``--auswerten`` zählt die Urteile gegen die Schwelle.

Kein echter KI-Aufruf: Der SDK-Client ist ein Ersatz. Nur erfundene Namen.
"""

from __future__ import annotations

import csv
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from insight_ai.models import KiVerbrauch, TagesbatchKommune, VerworfeneZusammenfassung
from insight_ai.providers.claude import ClaudeZusammenfassung
from insight_ai.services import zusammenfassung_stichprobe as stichprobe
from insight_ai.services.prompts import PROMPT_FASSUNG
from insight_ai.tests.test_zusammenfassung_claude import (  # noqa: F401 (_sauber: Fixture)
    _Client,
    _einrichten,
    _endpunkt,
    _nachricht,
    _sauber,
    _vorgang,
)
from insight_core.models import OParlBody, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db


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


def _zeilen(pfad: Path) -> list[dict[str, str]]:
    with pfad.open(encoding="utf-8", newline="") as datei:
        return list(csv.DictReader(datei, delimiter="\t"))


@pytest.fixture
def vorgaenge(body: OParlBody) -> list[OParlPaper]:
    return [_vorgang(body, tage=tag, text=f"Der Rat beschließt Maßnahme {tag}.") for tag in range(1, 9)]


def test_stichprobe_ist_fest(body: OParlBody, vorgaenge: list[OParlPaper]) -> None:
    erste = stichprobe.auswaehlen(body.pk, anzahl=5)
    assert len(erste) == 5 and [v.pk for v in stichprobe.auswaehlen(body.pk, anzahl=5)] == [v.pk for v in erste]
    # Mit Grenze kommen spätere Vorgänge nicht hinein
    grenze = timezone.localdate()
    vorher = [v.pk for v in stichprobe.auswaehlen(body.pk, anzahl=30, bis=grenze)]
    _vorgang(body, tage=-3, text="Später eingegangen.")
    assert [v.pk for v in stichprobe.auswaehlen(body.pk, anzahl=30, bis=grenze)] == vorher
    # Ohne Text keine Stichprobe
    ohne = _vorgang(body, tage=1, text=None)
    assert ohne.pk not in {v.pk for v in stichprobe.auswaehlen(body.pk, anzahl=30)}


def test_bestand_ohne_ki_aufruf(body: OParlBody, vorgaenge: list[OParlPaper], tmp_path: Path, monkeypatch: Any) -> None:
    OParlPaper.objects.filter(pk=vorgaenge[0].pk).update(summary="Die SPD will Maßnahme 1.", summary_prompt_fassung=1)
    OParlPaper.objects.filter(pk=vorgaenge[1].pk).update(summary="Eine Maßnahme ist geplant.")

    def kein_anbieter() -> Any:
        raise AssertionError("ohne --ausfuehren kein KI-Anbieter")

    monkeypatch.setattr("insight_ai.providers.get_summary_provider", kein_anbieter)
    ausgabe = tmp_path / "bogen.tsv"
    out = StringIO()
    call_command("zusammenfassung_stichprobe", "--kommune", str(body.pk), "--ausgabe", str(ausgabe), stdout=out)

    zeilen = {zeile["vorgang"]: zeile for zeile in _zeilen(ausgabe)}
    assert len(zeilen) == len(vorgaenge) and list(_zeilen(ausgabe)[0]) == list(stichprobe.SPALTEN)
    erste = zeilen[str(vorgaenge[0].pk)]
    assert erste["herkunft"] == "bestand" and erste["prompt_fassung"] == "1"
    assert erste["pruefung"] == "verworfen: Nicht in der Quelle: SPD" and erste["urteil"] == ""
    assert zeilen[str(vorgaenge[1].pk)]["pruefung"] == stichprobe.BESTANDEN
    assert zeilen[str(vorgaenge[1].pk)]["kurzfassung"] == "Eine Maßnahme ist geplant."
    assert zeilen[str(vorgaenge[2].pk)]["pruefung"] == "keine Zusammenfassung"
    assert "kein KI-Aufruf" in out.getvalue() and "1 bestanden, 1 von der Prüfung verworfen" in out.getvalue()


def test_ausfuehren_speichert_nichts(
    body: OParlBody, vorgaenge: list[OParlPaper], tmp_path: Path, monkeypatch: Any
) -> None:
    _einrichten()
    sdk = _Client(_nachricht("Eine Maßnahme ist geplant. Die CDU stimmt zu."))
    anbieter = ClaudeZusammenfassung(_endpunkt(), client=sdk)
    monkeypatch.setattr("insight_ai.providers.get_summary_provider", lambda: anbieter)
    ausgabe = tmp_path / "neu.tsv"

    call_command(
        "zusammenfassung_stichprobe",
        "--kommune",
        str(body.pk),
        "--anzahl",
        "3",
        "--ausgabe",
        str(ausgabe),
        "--ausfuehren",
        stdout=StringIO(),
    )

    zeilen = _zeilen(ausgabe)
    assert len(zeilen) == 3 and len(sdk.anfragen) == 3
    assert {zeile["herkunft"] for zeile in zeilen} == {"neu"}
    assert {zeile["prompt_fassung"] for zeile in zeilen} == {str(PROMPT_FASSUNG)}
    assert {zeile["pruefung"] for zeile in zeilen} == {"verworfen: Nicht in der Quelle: CDU"}
    # Nichts gespeichert, nichts protokolliert; die Kosten der Einzelaufrufe sind verbucht
    assert not OParlPaper.objects.filter(summary__isnull=False).exists()
    assert not VerworfeneZusammenfassung.objects.exists()
    assert KiVerbrauch.objects.count() == 3


def test_ausfuehren_ueber_der_kostengrenze(
    body: OParlBody, vorgaenge: list[OParlPaper], tmp_path: Path, monkeypatch: Any
) -> None:
    _einrichten()
    sdk = _Client()
    monkeypatch.setattr(
        "insight_ai.providers.get_summary_provider", lambda: ClaudeZusammenfassung(_endpunkt(), client=sdk)
    )
    monkeypatch.setattr(stichprobe, "schaetzung", lambda vorgaenge, provider: Decimal("9"))

    with pytest.raises(CommandError, match="über der Grenze"):
        call_command(
            "zusammenfassung_stichprobe",
            "--kommune",
            str(body.pk),
            "--ausgabe",
            str(tmp_path / "x.tsv"),
            "--ausfuehren",
            "--hoechstens-usd",
            "1",
            stdout=StringIO(),
        )
    assert sdk.anfragen == [] and not (tmp_path / "x.tsv").exists()


def test_auswerten(tmp_path: Path) -> None:
    pfad = tmp_path / "bogen.tsv"
    with pfad.open("w", encoding="utf-8", newline="") as datei:
        schreiber = csv.writer(datei, delimiter="\t", lineterminator="\n")
        schreiber.writerow(stichprobe.SPALTEN)
        for nr in range(1, 31):
            urteil = "zutreffend" if nr <= 27 else "teilweise"
            pruefung = "verworfen: Zahl nicht in der Quelle: 12" if nr == 30 else stichprobe.BESTANDEN
            schreiber.writerow([nr, "", "", "", "", 2, "neu", pruefung, "", "", "", urteil, ""])

    auswertung = stichprobe.auswerten(pfad)
    assert auswertung.urteile == {"zutreffend": 27, "teilweise": 3, "falsch": 0}
    assert auswertung.verworfen == 1 and auswertung.offen == 0 and auswertung.freigabe_erreicht

    out = StringIO()
    call_command("zusammenfassung_stichprobe", "--auswerten", str(pfad), stdout=out)
    assert "27 zutreffend, 3 teilweise, 0 falsch" in out.getvalue() and "Schwelle erreicht" in out.getvalue()


def test_auswerten_mit_falschem_urteil(tmp_path: Path) -> None:
    pfad = tmp_path / "bogen.tsv"
    zeilen = [
        stichprobe.Zeile(nr=nr, vorgang="", vorlagen_nr="", titel="", adresse="", prompt_fassung=2, herkunft="neu")
        for nr in (1, 2)
    ]
    stichprobe.schreiben(zeilen, pfad)
    inhalt = pfad.read_text(encoding="utf-8").splitlines()
    # Ein Mensch trägt ein: einmal falsch, einmal noch nichts
    felder = inhalt[1].split("\t")
    felder[stichprobe.SPALTEN.index("urteil")] = "falsch"
    inhalt[1] = "\t".join(felder)
    pfad.write_text("\n".join(inhalt) + "\n", encoding="utf-8")
    auswertung = stichprobe.auswerten(pfad)
    assert auswertung.urteile["falsch"] == 1 and auswertung.offen == 1 and not auswertung.freigabe_erreicht


def test_ohne_kommune() -> None:
    with pytest.raises(CommandError, match="--kommune fehlt"):
        call_command("zusammenfassung_stichprobe", stdout=StringIO())


def test_schaetzung_nur_mit_bekannten_preisen() -> None:
    """Die Grenze der Kosten gilt nur mit Claude; ohne Preise keine Schätzung. Jede Fassung hat ihre Stichprobe."""
    assert stichprobe.schaetzung([], object()) is None
    assert stichprobe.startwert(2) != stichprobe.startwert(3)
