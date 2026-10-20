# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlagennummern in jeder Schreibweise (Issue #1102): Schlüssel, Erkennung, Abfrage je Kommune, Rang und Index.

Die Schreibweisen stammen aus dem Befund auf Prod (Münster, ``V/0989/2017``) und aus Nummern anderer Kreise
(``A-R/0052/2026``, ``AN/0591/2026`` in Köln, ``VO/0455/22``, ``4305/IX``).
"""

from __future__ import annotations

import importlib
from datetime import date
from typing import Any

import pytest
from django.db import connection

from hub.ris import selectors as ris
from hub.ris import vorlagennummer
from insight_core.models import OParlBody, OParlPaper, OParlSource

#: Schreibweisen, die ``V/0591/2026`` finden müssen
SCHREIBWEISEN = ["V/0591/2026", "V/591/2026", "V 0591 2026", "v0591/2026", "V0591/2026", "v-591-2026", "V.0591.2026"]


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        *((s, "V/591/2026") for s in SCHREIBWEISEN),
        ("0591/2026", "591/2026"),
        ("V/0591/2026/1", "V/591/2026/1"),
        ("AN/0591/2026", "AN/591/2026"),
        ("A-R/0052/2026", "AR/52/2026"),
        ("AR 52 2026", "AR/52/2026"),
        ("VO/0455/22", "VO/455/22"),
        ("4305/IX", "4305/IX"),
        ("V/0/05", "V/0/5"),
        ("Ö-12/2026", "12/2026"),
        ("", ""),
        (None, ""),
    ],
)
def test_schluessel(eingabe: str | None, erwartet: str) -> None:
    assert vorlagennummer.schluessel(eingabe) == erwartet


@pytest.mark.parametrize(
    "eingabe",
    [*SCHREIBWEISEN, "0591/2026", "V 0989 2017", "A-R/0052/2026", "VO/0455/22", "4305/IX", "V/0591", "  V/591/2026 "],
)
def test_erkennt_nummern(eingabe: str) -> None:
    assert vorlagennummer.sieht_wie_nummer_aus(eingabe)


@pytest.mark.parametrize(
    "eingabe",
    [
        *("Radweg", "2026", "V/00", "Weg 12", "Haushalt 2026", "Etat 2026", "20.10.2026", "2026-10-20", ""),
        *("Radweg „B 51“", "12/2", "V 591"),
    ],
)
def test_erkennt_suchbegriffe_nicht_als_nummer(eingabe: str) -> None:
    assert not vorlagennummer.sieht_wie_nummer_aus(eingabe)


def test_rang_einzelner_nummern() -> None:
    mit_kreis = vorlagennummer.nummer("v 591 2026")
    assert mit_kreis is not None and mit_kreis.mit_kreis
    assert [mit_kreis.rang_von(n) for n in ("V/0591/2026", "V/0591/2026/1", "AN/0591/2026", "XV/0591/2026")] == [
        1,
        3,
        4,
        4,
    ]
    endstueck = vorlagennummer.nummer("0591/2026")
    assert endstueck is not None and not endstueck.mit_kreis
    assert [endstueck.rang_von(n) for n in ("591/2026", "V/0591/2026", "AN/0591/2026/2", "V/10591/2026")] == [
        1,
        2,
        3,
        4,
    ]
    assert vorlagennummer.nummer("Radweg") is None


def test_index_der_migration_rechnet_denselben_schluessel() -> None:
    """Der Ausdrucksindex hilft nur, wenn die Abfrage genau seinen Ausdruck rechnet (sonst neue Migration)."""
    migration = importlib.import_module("insight_core.migrations.0060_vorgang_nummer_index")
    assert migration.SQL_SCHLUESSEL == vorlagennummer.SQL_SCHLUESSEL
    assert migration.Migration.atomic is False


# --- Abfragen ---------------------------------------------------------------------------------------------------


def _kommune(n: int, name: str) -> OParlBody:
    quelle = OParlSource.objects.create(name=name, url=f"https://ris{n}.example.org/system")
    return OParlBody.objects.create(external_id=f"https://ris{n}.example.org/body/1", source=quelle, name=name)


def _vorgang(body: OParlBody, reference: str, tag: int = 1, name: str = "") -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"{body.external_id}/paper/{reference}",
        body=body,
        reference=reference,
        name=name or f"Vorlage {reference}",
        date=date(2026, 3, tag),
    )


@pytest.fixture
def bestand(db: Any) -> dict[str, Any]:
    muenster, koeln = _kommune(1, "Münster"), _kommune(2, "Köln")
    return {
        "muenster": muenster,
        "koeln": koeln,
        # Folgevorlage neuer als die Vorlage: steht trotzdem darunter
        "folge": _vorgang(muenster, "V/0591/2026/1", tag=20),
        "vorlage": _vorgang(muenster, "V/0591/2026", tag=2),
        "anderes_jahr": _vorgang(muenster, "V/0591/2025", tag=3),
        "fremder_kreis": _vorgang(muenster, "XV/0591/2026", tag=4),
        "alt": _vorgang(muenster, "V/0989/2017"),
        "ar": _vorgang(muenster, "A-R/0052/2026"),
        "vo": _vorgang(muenster, "VO/0455/22"),
        "roemisch": _vorgang(muenster, "4305/IX"),
        "ohne_nummer": OParlPaper.objects.create(
            external_id="https://ris1.example.org/paper/ohne", body=muenster, name="Radweg V/0591/2026"
        ),
        "koeln_an": _vorgang(koeln, "AN/0591/2026"),
    }


def _referenzen(papers: Any) -> list[str | None]:
    return [p.reference for p in papers]


def _rang(paper: OParlPaper) -> int | None:
    """Rang der Nummernsuche (Annotation ``nummer_rang``)."""
    return getattr(paper, "nummer_rang", None)


@pytest.mark.django_db
@pytest.mark.parametrize("eingabe", SCHREIBWEISEN)
def test_jede_schreibweise_findet_die_vorlage_zuerst(bestand: dict[str, Any], eingabe: str) -> None:
    gefunden = list(ris.search_papers_in(ris.papers([bestand["muenster"]]), eingabe))
    # Genau diese Nummer oben, die Folgevorlage direkt darunter, andere Kreise, Jahre und Kommunen nicht. „XV/0591/2026“
    # enthält „V/0591/2026“ und bleibt ein Treffer der bisherigen Textsuche, ist aber keine passende Nummer.
    assert _referenzen(gefunden)[:2] == ["V/0591/2026", "V/0591/2026/1"]
    assert _rang(gefunden[0]) == 1 and _rang(gefunden[1]) == 3
    assert all(_rang(p) == 4 for p in gefunden[2:])
    assert not {"V/0591/2025", "AN/0591/2026"} & set(_referenzen(gefunden))


@pytest.mark.django_db
def test_textsuche_bleibt_und_steht_hinter_der_nummer(bestand: dict[str, Any]) -> None:
    """Treffer der bisherigen Suche (Nummer im Titel) bleiben, stehen aber hinter den passenden Nummern."""
    gefunden = list(ris.search_papers_in(ris.papers([bestand["muenster"]]), "V/0591/2026"))
    assert gefunden[-1] == bestand["ohne_nummer"] and _rang(gefunden[-1]) == 4


@pytest.mark.django_db
def test_endstueck_findet_je_kommune(bestand: dict[str, Any]) -> None:
    muenster = list(ris.search_papers_in(ris.papers([bestand["muenster"]]), "0591/2026"))
    # Endstück genau (beide Kreise, neueste zuerst), dann die Folgevorlage
    assert _referenzen(muenster)[:3] == ["XV/0591/2026", "V/0591/2026", "V/0591/2026/1"]
    assert [_rang(p) for p in muenster[:3]] == [2, 2, 3]
    assert "AN/0591/2026" not in _referenzen(muenster)
    koeln = list(ris.search_papers_in(ris.papers([bestand["koeln"]]), "0591/2026"))
    assert _referenzen(koeln) == ["AN/0591/2026"]


@pytest.mark.django_db
def test_kreis_trennt_an_von_v(bestand: dict[str, Any]) -> None:
    beide = [bestand["muenster"], bestand["koeln"]]
    assert "AN/0591/2026" not in _referenzen(ris.search_papers_in(ris.papers(beide), "V/0591/2026"))
    assert _referenzen(ris.search_papers_in(ris.papers(beide), "an 591 2026")) == ["AN/0591/2026"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("V 0989 2017", "V/0989/2017"),
        ("V/989/2017", "V/0989/2017"),
        ("V0989/2017", "V/0989/2017"),
        ("AR 52 2026", "A-R/0052/2026"),
        ("a-r 0052-2026", "A-R/0052/2026"),
        ("VO 455 22", "VO/0455/22"),
        ("4305 ix", "4305/IX"),
    ],
)
def test_nummern_anderer_kreise(bestand: dict[str, Any], eingabe: str, erwartet: str) -> None:
    gefunden = ris.search_papers_in(ris.papers([bestand["muenster"]]), eingabe)
    assert gefunden[0].reference == erwartet and _rang(gefunden[0]) == 1


@pytest.mark.django_db
def test_suchbegriff_ohne_nummer_wie_bisher(bestand: dict[str, Any]) -> None:
    gefunden = ris.search_papers_in(ris.papers([bestand["muenster"]]), "Radweg")
    assert list(gefunden) == [bestand["ohne_nummer"]]
    assert not hasattr(gefunden[0], "nummer_rang")


@pytest.mark.django_db
def test_angeheftete_treffer_ohne_geloeschte(bestand: dict[str, Any]) -> None:
    bestand["anderes_jahr"].deleted = True
    bestand["anderes_jahr"].save(update_fields=["deleted"])
    treffer = ris.public_papers_by_number([bestand["muenster"].pk], "V 0591")
    assert _referenzen(treffer) == ["V/0591/2026/1", "V/0591/2026"]
    assert ris.public_papers_by_number([bestand["muenster"].pk], "Radweg") == []


@pytest.mark.django_db
def test_drehscheibe_suche_fuer_work(bestand: dict[str, Any]) -> None:
    """``search_papers`` (Fraktionssitzung, Anträge) und der Datenbank-Rückfall finden die Nummer ebenso."""
    assert ris.search_papers([bestand["muenster"]], "v0591/2026")[0] == bestand["vorlage"]
    assert ris.search_public_papers([bestand["muenster"]], "V 591 2026")[0] == bestand["vorlage"]


# --- PostgreSQL: derselbe Schlüssel wie in Python, Abfrage über den Ausdrucksindex -------------------------------


@pytest.mark.django_db
def test_datenbank_rechnet_denselben_schluessel(bestand: dict[str, Any]) -> None:
    for reference in ("  v 0012 /2026 ", "Ö-12/2026", "VO 455/22a", "a.b-c/007", "00/0", "--", "4305/IX"):
        _vorgang(bestand["koeln"], reference)
    werte = list(
        OParlPaper.objects.exclude(reference=None)
        .annotate(k=vorlagennummer.Schluessel("reference"))
        .values_list("reference", "k")
    )
    assert werte and all(k == vorlagennummer.schluessel(reference) for reference, k in werte)


@pytest.mark.django_db
def test_nummernsuche_nutzt_den_ausdrucksindex(bestand: dict[str, Any]) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Ausdrucksindex nur in PostgreSQL")
    nummer = vorlagennummer.nummer("V 591 2026")
    assert nummer is not None
    abfrage = OParlPaper.objects.filter(*nummer.bedingungen()).order_by().values("pk")
    sql, params = abfrage.query.sql_with_params()
    with connection.cursor() as cursor:
        # Im kleinen Testbestand wäre das Lesen der Tabelle billiger; gezeigt wird, dass der Index zum Ausdruck der
        # Abfrage passt (ohne Kommune und ohne Sortierung, damit kein anderer Index in Frage kommt)
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("SET LOCAL enable_indexscan = off")
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(zeile[0] for zeile in cursor.fetchall())
    assert "oparl_paper_nummer_trgm" in plan, plan
