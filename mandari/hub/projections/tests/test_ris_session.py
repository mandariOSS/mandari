# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine des RIS-Projektors (Issue #536): Spalten wie der Spiegel, Zerlegen, Schreiben der Schatten-Quelle und die
Regel, dass außer Projektor und Vergleich niemand die Schatten-Quelle liest. Der Weg von Session bis zum Vergleich
steht in ``apps/session/tests/test_ris_projektor.py``.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from mandari_oparl.ids import canonical_id

from apps.events.models import Operation
from hub.projections import ris_session, ris_vergleich
from hub.projections.models import RisSchatten
from hub.ris import uebernahme

BASIS = "https://mandari.example/session/nord/api/oparl/"
KOMMUNE = f"{BASIS}body/"


def _adresse(art: str, nummer: int) -> str:
    return f"{BASIS}{art}/{nummer}/"


# -- Spalten ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typ", sorted(uebernahme.FUNKTIONEN))
def test_spalten_je_typ_sind_genau_die_der_funktion(typ: str) -> None:
    assert tuple(uebernahme.FUNKTIONEN[typ]({})) == uebernahme.SPALTEN[typ]


@pytest.mark.parametrize("typ", ris_vergleich.TYPEN)
def test_spalten_und_bezuege_gibt_es_im_bestand(typ: str) -> None:
    """Der Vergleich liest genau diese Spalten und Bezüge aus dem Bestand; eine fehlende fiele erst dort auf."""
    abfrage = ris_vergleich._bestand(typ, [uuid.uuid4()])
    str(abfrage.values(*uebernahme.SPALTEN[typ], *ris_vergleich.VERWEISE[typ].values()).query)


def test_wert_normiert_wie_im_bestand() -> None:
    berlin = timezone(timedelta(hours=2))
    assert ris_session.wert(datetime(2026, 10, 5, 12, 0, tzinfo=berlin)) == "2026-10-05T10:00:00+00:00"
    assert ris_session.wert(datetime(2026, 10, 5, 10, 0, tzinfo=UTC)) == "2026-10-05T10:00:00+00:00"
    assert ris_session.wert(date(2026, 10, 5)) == "2026-10-05"
    assert ris_session.wert("") is None and ris_session.wert([]) is None and ris_session.wert({}) is None
    assert ris_session.wert(False) is False and ris_session.wert(0) == 0
    assert ris_session.wert([{"name": "A", "vote": ""}]) == [{"name": "A", "vote": None}]


@pytest.mark.parametrize(
    ("ref", "erwartet"),
    [
        ("session:6f1c1a52-6b62-4f5e-9e1d-3f0a0c0d0e0f", uuid.UUID("6f1c1a52-6b62-4f5e-9e1d-3f0a0c0d0e0f")),
        ("source:6f1c1a52-6b62-4f5e-9e1d-3f0a0c0d0e0f", None),
        ("session:kaputt", None),
        ("", None),
    ],
)
def test_nur_session_mandanten(ref: str, erwartet: uuid.UUID | None) -> None:
    assert ris_session.session_mandant(ref) == erwartet


# -- Zerlegen wie der Spiegel ----------------------------------------------------------------------------


def test_sitzung_bringt_tagesordnung_und_dateien_mit() -> None:
    sitzung = {
        "id": _adresse("meeting", 1),
        "name": "Rat",
        "start": "2026-10-05T18:00:00+02:00",
        "organization": [_adresse("organization", 2), _adresse("organization", 1), _adresse("organization", 1)],
        "location": {"description": "Rathaus", "streetAddress": "Markt 1", "postalCode": "12345", "locality": "Nord"},
        "agendaItem": [{"id": _adresse("agendaitem", 1), "meeting": _adresse("meeting", 1), "number": "1"}],
        "auxiliaryFile": [{"id": _adresse("file", 1), "name": "Lageplan", "date": "2026-10-01"}],
        "resultsProtocol": {"id": _adresse("file", 2), "name": "Niederschrift"},
        "modified": "2026-10-05T08:00:00+00:00",
    }
    zeilen = {zeile.external_id: zeile for zeile in ris_session.zerlegen("meeting", sitzung, KOMMUNE)}
    assert [zeile.typ for zeile in zeilen.values()] == ["meeting", "agendaitem", "file", "file"]
    rat = zeilen[_adresse("meeting", 1)]
    assert rat.spalten["start"] == "2026-10-05T16:00:00+00:00"
    assert (rat.spalten["location_name"], rat.spalten["location_address"]) == ("Rathaus", "Markt 1, 12345 Nord")
    assert rat.verweise == {
        "body": KOMMUNE,
        "organizations": [_adresse("organization", 1), _adresse("organization", 2)],
    }
    assert rat.oparl_modified == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    assert zeilen[_adresse("agendaitem", 1)].verweise == {"meeting": _adresse("meeting", 1)}
    for nummer in (1, 2):
        assert zeilen[_adresse("file", nummer)].verweise == {
            "body": KOMMUNE,
            "paper": None,
            "meeting": _adresse("meeting", 1),
        }
    assert zeilen[_adresse("file", 1)].spalten["file_date"] == "2026-10-01T00:00:00+00:00"


def test_vorlage_bringt_dateien_und_beratungen_mit() -> None:
    vorlage = {
        "id": _adresse("paper", 1),
        "name": "Radweg",
        "date": "2026-09-01",
        "mainFile": {"id": _adresse("file", 1), "name": "Vorlage"},
        "auxiliaryFile": [{"id": _adresse("file", 2), "name": "Anlage"}],
        "consultation": [{"id": _adresse("consultation", 1), "paper": _adresse("paper", 1), "authoritative": True}],
    }
    zeilen = ris_session.zerlegen("paper", vorlage, KOMMUNE)
    assert [(zeile.typ, zeile.external_id) for zeile in zeilen] == [
        ("paper", _adresse("paper", 1)),
        ("file", _adresse("file", 1)),
        ("file", _adresse("file", 2)),
        ("consultation", _adresse("consultation", 1)),
    ]
    assert zeilen[0].spalten["date"] == "2026-09-01"
    assert zeilen[1].verweise == {"body": KOMMUNE, "paper": _adresse("paper", 1), "meeting": None}
    assert zeilen[3].spalten["authoritative"] is True
    assert zeilen[3].verweise == {"body": KOMMUNE, "paper": _adresse("paper", 1)}


def test_anlage_aus_der_liste_nur_mit_vorlage_oder_sitzung() -> None:
    nur_punkt = {"id": _adresse("file", 1), "agendaItem": [_adresse("agendaitem", 1)]}
    assert ris_session.zerlegen("file", nur_punkt, KOMMUNE) == []
    mit_vorlage = {"id": _adresse("file", 2), "paper": [_adresse("paper", 1), _adresse("paper", 2)]}
    [zeile] = ris_session.zerlegen("file", mit_vorlage, KOMMUNE)
    assert zeile.verweise == {"body": KOMMUNE, "paper": _adresse("paper", 1), "meeting": None}


def test_person_mit_mitgliedschaften_und_koerperschaft_mit_wahlperioden() -> None:
    person = {
        "id": _adresse("person", 1),
        "name": "Petra Muster",
        "title": ["Dr."],
        "email": ["petra@example.org"],
        "membership": [{"id": _adresse("membership", 1), "organization": _adresse("organization", 1)}],
    }
    personen = ris_session.zerlegen("person", person, KOMMUNE)
    assert personen[0].spalten["title"] == "Dr." and personen[0].spalten["email"] == "petra@example.org"
    assert personen[1].verweise == {"person": _adresse("person", 1), "organization": _adresse("organization", 1)}
    koerperschaft = {"id": KOMMUNE, "name": "Nord", "legislativeTerm": [{"id": _adresse("legislativeterm", 1)}]}
    assert [zeile.typ for zeile in ris_session.zerlegen("body", koerperschaft, KOMMUNE)] == ["body", "legislativeterm"]
    with pytest.raises(ValueError):
        ris_session.zerlegen("location", {"id": _adresse("location", 1)}, KOMMUNE)


# -- Schreiben -------------------------------------------------------------------------------------------


def _kennung(adresse: str) -> uuid.UUID:
    return canonical_id(adresse)


@pytest.mark.django_db
def test_schreiben_ist_idempotent_und_nennt_aenderungen() -> None:
    mandant = uuid.uuid4()
    schatten = ris_session.Schatten(mandant, _kennung, seq=7)
    sitzung = {"id": _adresse("meeting", 1), "name": "Rat", "modified": "2026-10-05T08:00:00+00:00"}
    zeilen = ris_session.zerlegen("meeting", sitzung, KOMMUNE)

    [neu] = schatten.schreiben(zeilen)
    assert (neu.typ, neu.operation, neu.neu, neu.kennung) == (
        "meeting",
        Operation.UPSERT,
        True,
        _kennung(_adresse("meeting", 1)),
    )
    assert schatten.schreiben(zeilen) == []
    zeile = RisSchatten.objects.get()
    assert (zeile.mandant, zeile.seq, zeile.spalten["name"]) == (mandant, 7, "Rat")

    umbenannt = ris_session.zerlegen("meeting", {**sitzung, "name": "Rat (neu)", "cancelled": True}, KOMMUNE)
    [geaendert] = schatten.schreiben(umbenannt)
    assert geaendert.geaendert == ("cancelled", "name") and not geaendert.neu

    kennung = _kennung(_adresse("meeting", 1))
    [weg] = schatten.entfernen({kennung: "nichtoeffentlich"})
    assert (weg.operation, weg.grund) == (Operation.DELETE, "nichtoeffentlich")
    # Der erste Grund gilt; Zeilen, die es nicht gibt, entstehen nicht
    assert schatten.entfernen({kennung: "quelle_geloescht", uuid.uuid4(): "quelle_geloescht"}) == []
    zeile.refresh_from_db()
    assert (zeile.deleted, zeile.deletion_reason, zeile.spalten, zeile.verweise) == (True, "nichtoeffentlich", {}, {})

    [wieder] = schatten.schreiben(zeilen)
    assert wieder.neu
    zeile.refresh_from_db()
    assert (zeile.deleted, zeile.deletion_reason) == (False, None)
    assert RisSchatten.objects.count() == 1


# -- Unsichtbar nach außen ---------------------------------------------------------------------------------

#: Außer dem Paket ``hub.projections`` selbst das einzige Modul, das die Schatten-Quelle lesen darf (ohne Tests)
ERLAUBT = {"apps/session/management/commands/ris_projektor_schatten.py"}
#: Pakete des Projekts (wie ``root_packages`` des import-linter) und die Einstellungen
PAKETE = ("apps", "hub", "insight_core", "insight_ai", "insight_search", "insight_sync", "mandari")
_VERWEIS = re.compile(r"RisSchatten|hub_ris_schatten|projections\.models|projections import models")


def test_nur_projektor_und_vergleich_kennen_die_schatten_quelle() -> None:
    """
    Bürgerportal, Suche, Sitemaps, OParl-Schnittstelle, Änderungsfeed, Snapshot, Work und Aufträge lesen den
    RIS-Bestand; die Schatten-Quelle steht daneben und bleibt so unsichtbar. Wer sie lesen will, braucht einen Grund
    und eine Änderung an dieser Liste.
    """
    basis = Path(settings.BASE_DIR)
    fremd = []
    for paket in PAKETE:
        for datei in (basis / paket).rglob("*.py"):
            relativ = datei.relative_to(basis).as_posix()
            if "/tests/" in relativ or relativ.startswith("hub/projections/") or relativ in ERLAUBT:
                continue
            if _VERWEIS.search(datei.read_text(encoding="utf-8", errors="replace")):
                fremd.append(relativ)
    assert fremd == []


def test_schatten_quelle_ohne_verwaltungsseite() -> None:
    from django.contrib import admin

    assert RisSchatten not in admin.site._registry


def test_abonnement_wie_vorgesehen() -> None:
    assert ris_session.NAME == "ris.session_projektor"
    assert "ris.object.depublished" in ris_session.TYPES
    assert all(muster.startswith("ris.") for muster in ris_session.TYPES)


def test_schalter_und_auswahl(settings: Any) -> None:
    settings.RIS_SESSION_PROJECTOR = "schatten"
    settings.RIS_SESSION_PROJECTOR_TENANTS = []
    assert ris_session.modus() == "schatten" and ris_session.mandanten() is None
    kennung = uuid.uuid4()
    settings.RIS_SESSION_PROJECTOR_TENANTS = [str(kennung)]
    assert ris_session.mandanten() == frozenset({kennung})
    settings.RIS_SESSION_PROJECTOR = "aktiv"  # erst mit #537; bis dahin wie aus
    assert ris_session.modus() == "aus"
