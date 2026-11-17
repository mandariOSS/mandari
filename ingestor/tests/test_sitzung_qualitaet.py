# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Absagen im Sitzungsnamen und unplausible Endzeiten (Issue #1441).

Die Schreibweisen stammen aus dem Bestand: 18 Kommunen schreiben eine Absage in den Namen der Sitzung statt in
``cancelled``, und fast jede Quelle liefert ``end`` als Datum ohne Uhrzeit. Gremiennamen sind ausgedacht.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from mandari_oparl.qualitaet import plausibles_ende, sitzungsangaben, sitzungsname


@pytest.mark.parametrize(
    ("roh", "name", "vermerk"),
    [
        # Vermerk hinter dem Namen
        (
            "Beirat für Naturschutz Sitzung wurde abgesagt | Mail Frau Muster vom 10.11.",
            "Beirat für Naturschutz",
            "Sitzung wurde abgesagt",
        ),
        ("Ausschuss für Umwelt -ENTFÄLLT-", "Ausschuss für Umwelt", "ENTFÄLLT"),
        ("Ausschuss für Umwelt - ENTFÄLLT", "Ausschuss für Umwelt", "ENTFÄLLT"),
        ("Ausschuss für Umwelt  --ENTFÄLLT--", "Ausschuss für Umwelt", "ENTFÄLLT"),
        ("Rat Sitzung entfällt!", "Rat", "Sitzung entfällt!"),
        ("Rat - Sitzung fällt aus!", "Rat", "Sitzung fällt aus!"),
        ("Bezirksvertretung 3 (abgesagt)", "Bezirksvertretung 3", "abgesagt"),
        ("Hauptausschuss +++ abgesagt +++", "Hauptausschuss", "abgesagt"),
        ("Schulausschuss - entfällt! (27. Sitzung)", "Schulausschuss", "entfällt! (27. Sitzung)"),
        ("Verkehrsausschuss\r\nABGESAGT", "Verkehrsausschuss", "ABGESAGT"),
        (
            "Sportausschuss Sitzung fällt aus und wurde auf den 24.06.2025 verlegt",
            "Sportausschuss",
            "Sitzung fällt aus und wurde auf den 24.06.2025 verlegt",
        ),
        ("Jugendhilfeausschuss (JHA) entfaellt", "Jugendhilfeausschuss (JHA)", "entfaellt"),
        # Vermerk vor dem Namen
        ("abgesagt -Sitzung des Stadionausschusses", "Sitzung des Stadionausschusses", "abgesagt"),
        (
            "- ABGESAGT - öffentliche Sitzung des Schulausschusses",
            "öffentliche Sitzung des Schulausschusses",
            "ABGESAGT",
        ),
        (
            "***ENTFÄLLT***Sitzung des Haupt- und Finanzausschusses***ENTFÄLLT***",
            "Sitzung des Haupt- und Finanzausschusses",
            "ENTFÄLLT",
        ),
        (
            "Sitzung entfällt.\r\nöffentliche Sitzung des Ausschusses für Verkehr",
            "öffentliche Sitzung des Ausschusses für Verkehr",
            "Sitzung entfällt",
        ),
        # Nur der Vermerk
        ("Sitzung entfällt!", None, "Sitzung entfällt"),
        # Vermerk erst hinter dem Strich
        ("Seniorenvertretung | abgesagt, siehe Mail", "Seniorenvertretung", "abgesagt"),
        # „nicht öffentlich“ im Namen ist keine Verneinung der Absage
        (
            "öffentliche/nicht öffentliche Sitzung des Sportausschusses ENTFÄLLT",
            "öffentliche/nicht öffentliche Sitzung des Sportausschusses",
            "ENTFÄLLT",
        ),
        ("ENTFÄLLT nicht öffentliche Sitzung des Rates", "nicht öffentliche Sitzung des Rates", "ENTFÄLLT"),
        ("Entfällt: nichtöffentliche Sitzung des Rates", "nichtöffentliche Sitzung des Rates", "Entfällt"),
        ("Rat - Sitzung entfällt, nicht verschoben", "Rat", "Sitzung entfällt, nicht verschoben"),
    ],
)
def test_absage_im_namen(roh: str, name: str | None, vermerk: str) -> None:
    befund = sitzungsname(roh)
    assert befund.abgesagt is True
    assert befund.name == name
    assert befund.vermerk == vermerk
    # Der Verwaltungsvermerk hinter dem Strich steht weder im Namen noch im Vermerk
    assert "Mail" not in (befund.name or "") and "Muster" not in (befund.vermerk or "")


@pytest.mark.parametrize(
    "roh",
    [
        "Ausschuss für Umwelt und Verkehr",
        "Beratung über abgesagte Veranstaltungen",
        "Arbeitskreis Entfallende Buslinien",
        "Rat | Sondersitzung",
        "Ausfallbürgschaften",
        # Verneinung direkt am Absagewort: Die Sitzung findet statt
        "Rat – Sitzung entfällt nicht",
        "Rat - Sitzung ENTFÄLLT NICHT!",
        "Bauausschuss (nicht abgesagt)",
        "Hauptausschuss: Sitzung wurde nicht abgesagt",
        "Schulausschuss  ist  nicht\tabgesagt",
        "Sportausschuss, fällt nicht aus",
        "",
    ],
)
def test_ohne_absage_bleibt_der_name(roh: str) -> None:
    befund = sitzungsname(roh)
    assert befund.abgesagt is False and befund.vermerk is None
    assert befund.name == (roh or None)


def test_kein_name() -> None:
    assert sitzungsname(None) == sitzungsname("") and sitzungsname(None).name is None


def test_angaben_absage_abgeleitet_und_gekennzeichnet() -> None:
    angaben = sitzungsangaben({"name": "Beirat Sitzung wurde abgesagt | Mail Frau Muster", "cancelled": False})
    assert (angaben.name, angaben.abgesagt, angaben.abgeleitet) == ("Beirat", True, True)
    assert angaben.vermerk == "Sitzung wurde abgesagt"


def test_angaben_quelle_setzt_cancelled_selbst() -> None:
    # Die Quelle sagt ab und schreibt es zusätzlich in den Namen: Name bereinigt, nichts abgeleitet
    angaben = sitzungsangaben({"name": "Rat - ENTFÄLLT -", "cancelled": True, "meetingState": "terminiert"})
    assert (angaben.name, angaben.abgesagt, angaben.abgeleitet) == ("Rat", True, False)
    # Absage ohne Vermerk im Namen
    angaben = sitzungsangaben({"name": "Rat", "cancelled": True})
    assert (angaben.name, angaben.abgesagt, angaben.abgeleitet, angaben.vermerk) == ("Rat", True, False, None)


def test_angaben_ohne_absage_und_andere_werte() -> None:
    assert sitzungsangaben({"name": "Rat"}) == sitzungsangaben({"name": "Rat", "cancelled": False})
    angaben = sitzungsangaben({"name": "Rat", "cancelled": "true"})
    assert angaben.abgesagt is False  # nur der Wahrheitswert der Quelle zählt
    assert sitzungsangaben({}).name is None and sitzungsangaben({"name": 5}).name is None


@pytest.mark.parametrize("zustand", ["durchgeführt", "Abgeschlossen", " Durchgeführt "])
def test_stattgefundene_sitzung_ist_nicht_abgesagt(zustand: str) -> None:
    # Ein Absagewort im Namen einer Sitzung, die stattgefunden hat, ist keine Absage; der Name bleibt
    roh = {"name": "Rat (Ersatz für die Sitzung, die entfällt)", "meetingState": zustand}
    angaben = sitzungsangaben(roh)
    assert (angaben.name, angaben.abgesagt, angaben.abgeleitet) == (roh["name"], False, False)


def test_eingeladen_und_terminiert_gelten_als_offen() -> None:
    for zustand in ("eingeladen", "terminiert", None, 7):
        assert sitzungsangaben({"name": "Rat - entfällt -", "meetingState": zustand}).abgesagt is True


def test_ende_nur_nach_dem_beginn() -> None:
    start = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)
    spaeter = start + timedelta(hours=2)
    assert plausibles_ende(start, spaeter) == spaeter
    # Datum ohne Uhrzeit: Mitternacht am Sitzungstag liegt vor dem Beginn
    assert plausibles_ende(start, datetime(2026, 11, 18, 0, 0, tzinfo=UTC)) is None
    assert plausibles_ende(start, start) is None
    # Auf die Sekunde, wie die Ausgabe: Bruchteile derselben Sekunde sind kein späteres Ende
    assert plausibles_ende(start.replace(microsecond=100_000), start.replace(microsecond=900_000)) is None
    assert plausibles_ende(start.replace(microsecond=900_000), start + timedelta(seconds=1)) is not None
    assert plausibles_ende(start, None) is None
    # Ohne Beginn nichts verwerfen
    assert plausibles_ende(None, spaeter) == spaeter
    # Zeitzonen werden verglichen, nicht die Ziffern
    berlin = timezone(timedelta(hours=1))
    assert plausibles_ende(start, datetime(2026, 11, 18, 18, 30, tzinfo=berlin)) is not None
    assert plausibles_ende(start, datetime(2026, 11, 18, 17, 30, tzinfo=berlin)) is None
    # Mit und ohne Zeitzone: nicht vergleichbar, nichts verwerfen
    ohne_zone = datetime(2026, 11, 18, 0, 0)
    assert plausibles_ende(start, ohne_zone) == ohne_zone


# ---- Im Abruf: Processor und Ereignis ------------------------------------------------------------------------------


def _sitzung(**werte: object) -> dict[str, object]:
    return {
        "id": "https://ris.example.org/meeting/7",
        "type": "https://schema.oparl.org/1.1/Meeting",
        "body": "https://ris.example.org/body/1",
        "name": "Beirat für Naturschutz Sitzung wurde abgesagt | Mail Frau Muster vom 10.11.",
        "start": "2026-11-18T17:00:00+01:00",
        "end": "2026-11-18",
        **werte,
    }


def test_processor_schreibt_gepruefte_angaben_und_behaelt_die_rohdaten() -> None:
    from src.sync.processor import OParlProcessor

    roh = _sitzung()
    sitzung = OParlProcessor().process_meeting(roh)

    assert sitzung.name == "Beirat für Naturschutz"
    assert sitzung.cancelled is True
    assert sitzung.end is None  # Datum ohne Uhrzeit liegt vor dem Beginn
    assert sitzung.start is not None and sitzung.start.hour == 17
    # Die Angaben der Quelle bleiben nachvollziehbar
    assert sitzung.raw_json == roh and "cancelled" not in sitzung.raw_json


def test_processor_laesst_ordentliche_angaben_unveraendert() -> None:
    from src.sync.processor import OParlProcessor

    sitzung = OParlProcessor().process_meeting(
        _sitzung(name="Ausschuss für Umwelt und Verkehr", end="2026-11-18T19:30:00+01:00", cancelled=False)
    )
    assert (sitzung.name, sitzung.cancelled) == ("Ausschuss für Umwelt und Verkehr", False)
    assert sitzung.end is not None and sitzung.end.hour == 19

    von_quelle = OParlProcessor().process_meeting(_sitzung(name="Rat", cancelled=True))
    assert (von_quelle.name, von_quelle.cancelled) == ("Rat", True)


def test_ereignis_nennt_die_abgeleitete_absage() -> None:
    from uuid import uuid4

    from src.storage.ris_events import Prior, meeting_events

    vorher = _sitzung(name="Beirat für Naturschutz")
    ereignisse = meeting_events(uuid4(), _sitzung(), Prior(raw_json=vorher, deleted=False))
    assert [e.type for e in ereignisse] == ["ris.meeting.changed"]
    assert ereignisse[0].payload["cancelled"] is True
