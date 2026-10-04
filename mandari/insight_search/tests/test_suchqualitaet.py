# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bewertungssatz und ``manage.py suchqualitaet messen`` (Konzept Insight-Suche, 4.4): Kennzahlen, nur lesend."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import call_command

from insight_core.models import OParlBody
from insight_search import bewertung

HEUTE = datetime(2026, 10, 4, tzinfo=UTC)


class _Client:
    """Quellen (Aktenzeichen, Datum) und Prüfung auf Rauschen; zählt nur, schreibt nie."""

    def __init__(self, quellen: dict[str, dict[str, Any]], relevant: set[str]) -> None:
        self.quellen = quellen
        self.relevant = relevant
        self.aufrufe: list[str] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.aufrufe.append("search")
        ids = body["query"]["ids"]["values"]
        return {"hits": {"hits": [{"_id": i, "_source": self.quellen.get(i, {})} for i in ids]}}

    def count(self, index: str, query: dict[str, Any]) -> dict[str, int]:
        self.aufrufe.append("count")
        ids = query["bool"]["filter"][0]["ids"]["values"]
        return {"count": sum(1 for i in ids if i in self.relevant)}


class _Dienst:
    def __init__(self, reihenfolge: list[tuple[str, str]], client: _Client, leere_seite: int = 0) -> None:
        self.reihenfolge = reihenfolge
        self.client = client
        self.leere_seite = leere_seite
        self.versionen: list[str] = []

    def search_all(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.versionen.append(kwargs["ranking"])
        seite = kwargs["page"]
        ergebnisse = [] if seite == self.leere_seite else [{"id": d} for _i, d in self.reihenfolge[:20]]
        return {"total": len(self.reihenfolge), "results": ergebnisse, "similar_spelling": False}

    def rank_hits(self, query: str, **kwargs: Any) -> Any:
        return SimpleNamespace(entries=[(1.0, 0, i, d) for i, d in self.reihenfolge])


def _fall() -> tuple[_Dienst, _Client]:
    # 12 Treffer: 10 Dateien (3 jung, 1 ohne Datum), der erwartete neueste Vorgang auf Platz 11, 2 Rauschen
    reihenfolge = [("files", f"f{n}") for n in range(10)] + [("papers", "neu"), ("papers", "alt")]
    quellen: dict[str, dict[str, Any]] = {f"f{n}": {"meeting_date": "2016-09-28"} for n in range(10)}
    quellen.update({f"f{n}": {"meeting_date": "2026-03-25"} for n in range(3)})
    quellen["f9"] = {}
    quellen["neu"] = {"reference": "V/0573/2025", "date": "2026-03-25"}
    quellen["alt"] = {"reference": "V/0169/2016", "date": "2016-03-01"}
    client = _Client(quellen, relevant={f"f{n}" for n in range(8)} | {"neu", "alt"})
    return _Dienst(reihenfolge, client, leere_seite=2), client


def test_kennzahlen_einer_anfrage() -> None:
    dienst, client = _fall()
    anfrage = bewertung.Anfrage(
        kommune="ms", text="von-witzleben", absicht="B", relevant=("witzleb",), neuester="V/0573/2025", seiten=2
    )

    bericht = bewertung.messen(dienst, [anfrage], {"ms": "b1"}, "v2", heute=HEUTE)

    wert = bericht.messwerte[0]
    assert (wert.top10_jung, wert.top10_datiert) == (3, 9)
    assert wert.platz_neuester == 11
    assert (wert.top20_rauschen, wert.top20) == (2, 12)
    assert wert.leere_seiten == 0  # Seite 2 wäre bei 12 Treffern ohnehin leer
    kennzahlen = bericht.kennzahlen()
    assert kennzahlen["z1_aktualitaet_top10_prozent"] == 33.3
    assert kennzahlen["z2_platz_neuester_median"] == 11
    assert kennzahlen["z4_rauschen_top20_prozent"] == 16.7
    assert set(client.aufrufe) == {"search", "count"}
    assert set(dienst.versionen) == {"v2"}


def test_leere_seite_trotz_gemeldeter_treffer_zaehlt() -> None:
    dienst, _client = _fall()
    dienst.reihenfolge = dienst.reihenfolge * 3  # 36 Treffer: Seite 2 müsste gefüllt sein
    anfrage = bewertung.Anfrage(kommune="ms", text="Haushalt", absicht="B", relevant=("haushalt",), seiten=2)

    bericht = bewertung.messen(dienst, [anfrage], {"ms": "b1"}, "v1", heute=HEUTE)

    assert bericht.kennzahlen()["z8_leere_seiten"] == 1


def test_bewertungssatz_enthaelt_die_anfragen_der_ist_analyse() -> None:
    anfragen = bewertung.lade_anfragen()

    texte = {(a.kommune, a.text) for a in anfragen}
    assert ("muenster", "von-witzleben") in texte
    assert ("darmstadt", "Haushalt") in texte
    assert {a.kommune for a in anfragen} == {"muenster", "koeln", "darmstadt"}
    # 11 Themen je Kommune plus Darmstadt „Haushalt“ nur in Dokumenten (Z8, leere Seiten)
    assert sum(1 for a in anfragen if a.absicht == "B") == 34


@pytest.mark.django_db
def test_befehl_misst_beide_versionen(
    kommune: Callable[[str], OParlBody], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from insight_core.services import search_service

    body = kommune("Münster")
    datei = tmp_path / "anfragen.json"
    datei.write_text(
        json.dumps({"anfragen": [{"kommune": body.slug, "text": "Kita", "absicht": "B", "relevant": ["kita"]}]}),
        encoding="utf-8",
    )
    dienst, _client = _fall()
    monkeypatch.setattr(search_service, "get_search_service", lambda: dienst)
    ausgabe = StringIO()

    call_command(
        "suchqualitaet", "messen", "--ranking", "v1", "--ranking", "v2", "--datei", str(datei), "--json", stdout=ausgabe
    )

    berichte = json.loads(ausgabe.getvalue())
    assert [b["kennzahlen"]["ranking"] for b in berichte] == ["v1", "v2"]
    assert berichte[0]["messwerte"][0]["treffer"] == 12
