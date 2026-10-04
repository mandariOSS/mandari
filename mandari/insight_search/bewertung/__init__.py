# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bewertungssatz und Messung der Suchqualität (Konzept Insight-Suche, 1.3 und 4.4), nur lesend.

Misst je Anfrage aus ``anfragen.json`` mit dem Suchdienst:

* **Z1 Aktualität oben:** Anteil der datierten Treffer unter den ersten zehn, die jünger als zwei Jahre sind
  (gesamt nur über Themenanfragen, Absicht B).
* **Z2 Neues gefunden:** Platz des ersten Treffers zum erwarteten neuesten Vorgang (Vorgang selbst oder eine
  seiner Dateien) in der gemischten Trefferliste.
* **Z4 Rauschen:** Anteil der ersten 20 Treffer ohne einen der relevanten Begriffe (Präfix) in den
  durchsuchten Feldern.
* **Z8 Leere Seiten:** Seiten ohne Treffer, obwohl die gemeldete Zahl sie füllen müsste.
* **Z13 Antwortzeit:** Dauer der ersten Ergebnisseite samt Hervorhebung (p95 über alle Anfragen).

Elasticsearch bekommt nur Such- und Zählanfragen; ausgegeben werden Zahlen und Aktenzeichen, keine Inhalte.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

#: Anfragen des Bewertungssatzes
ANFRAGEN_DATEI: Final = Path(__file__).with_name("anfragen.json")
TIEFE: Final = 1000
SEITE: Final = 20
JUNG_TAGE: Final = 730
#: Felder je Index für die Prüfung auf Rauschen (ohne Gewichte)
PRUEFFELDER: Final[dict[str, tuple[str, ...]]] = {
    "papers": ("name", "file_contents_preview", "organization_names", "reference", "file_names"),
    "meetings": ("name", "organization_names", "location_name"),
    "persons": ("name", "given_name", "family_name", "title"),
    "organizations": ("name", "short_name"),
    "files": ("name", "text_content", "paper_name", "organization_names", "file_name", "paper_reference"),
}
DATUMSFELD: Final[dict[str, str]] = {"papers": "date", "meetings": "start", "files": "meeting_date"}


@dataclass(frozen=True)
class Anfrage:
    """Eine Anfrage des Bewertungssatzes."""

    kommune: str
    text: str
    absicht: str
    relevant: tuple[str, ...]
    neuester: str = ""
    typ: str = ""
    seiten: int = 1


@dataclass
class Messwert:
    """Messwerte einer Anfrage (nur Zahlen und Aktenzeichen)."""

    kommune: str
    text: str
    absicht: str
    treffer: int = 0
    aehnliche_schreibweisen: bool = False
    top10_datiert: int = 0
    top10_jung: int = 0
    neuester_erwartet: bool = False
    platz_neuester: int | None = None
    top20: int = 0
    top20_rauschen: int = 0
    leere_seiten: int = 0
    dauer_ms: float = 0.0


@dataclass
class Bericht:
    """Messwerte je Anfrage und Kennzahlen gesamt."""

    ranking: str
    messwerte: list[Messwert] = field(default_factory=list)

    def kennzahlen(self) -> dict[str, Any]:
        """Z1, Z2, Z4, Z8 und Z13 über alle Anfragen."""
        themen = [m for m in self.messwerte if m.absicht == "B"]
        datiert = sum(m.top10_datiert for m in themen)
        plaetze = [m.platz_neuester for m in self.messwerte if m.platz_neuester is not None]
        erwartet = [m for m in self.messwerte if m.neuester_erwartet]
        top20 = sum(m.top20 for m in self.messwerte)
        dauern = sorted(m.dauer_ms for m in self.messwerte)
        return {
            "ranking": self.ranking,
            "anfragen": len(self.messwerte),
            "z1_aktualitaet_top10_prozent": _prozent(sum(m.top10_jung for m in themen), datiert),
            "z2_platz_neuester_median": statistics.median(plaetze) if plaetze else None,
            "z2_neuester_nicht_gefunden": len(erwartet) - len(plaetze),
            "z4_rauschen_top20_prozent": _prozent(sum(m.top20_rauschen for m in self.messwerte), top20),
            "z8_leere_seiten": sum(m.leere_seiten for m in self.messwerte),
            "z13_p95_ms": round(dauern[max(0, int(len(dauern) * 0.95 + 0.5) - 1)], 1) if dauern else None,
        }

    def als_dict(self) -> dict[str, Any]:
        return {"kennzahlen": self.kennzahlen(), "messwerte": [asdict(m) for m in self.messwerte]}


def _prozent(teil: int, ganzes: int) -> float | None:
    return round(100 * teil / ganzes, 1) if ganzes else None


def lade_anfragen(datei: Path = ANFRAGEN_DATEI, kommune: str = "") -> list[Anfrage]:
    """Anfragen aus der JSON-Datei, optional nur einer Kommune (Kurzname wie ``muenster``)."""
    daten = json.loads(datei.read_text(encoding="utf-8"))
    anfragen = [
        Anfrage(
            kommune=eintrag["kommune"],
            text=eintrag["text"],
            absicht=eintrag["absicht"],
            relevant=tuple(p.lower() for p in eintrag.get("relevant", ())),
            neuester=eintrag.get("neuester", ""),
            typ=eintrag.get("typ", ""),
            seiten=int(eintrag.get("seiten", 1)),
        )
        for eintrag in daten["anfragen"]
    ]
    return [a for a in anfragen if not kommune or a.kommune == kommune]


def messen(
    dienst: Any,
    anfragen: list[Anfrage],
    kommunen: dict[str, str],
    ranking: str,
    heute: datetime | None = None,
) -> Bericht:
    """Misst alle Anfragen, deren Kommune in ``kommunen`` (Kurzname → Kennung) steht.

    ``dienst`` ist der Suchdienst (``insight_core.services.search_service``) mit ``search_all``, ``rank_hits``
    und ``client``.
    """
    stichtag = (heute or datetime.now(UTC)) - timedelta(days=JUNG_TAGE)
    bericht = Bericht(ranking=ranking)
    for anfrage in anfragen:
        body_id = kommunen.get(anfrage.kommune)
        if not body_id:
            continue
        bericht.messwerte.append(_messe(dienst, anfrage, body_id, ranking, stichtag))
    return bericht


def _messe(dienst: Any, anfrage: Anfrage, body_id: str, ranking: str, stichtag: datetime) -> Messwert:
    indexe = [anfrage.typ] if anfrage.typ else None
    wert = Messwert(kommune=anfrage.kommune, text=anfrage.text, absicht=anfrage.absicht)

    beginn = time.perf_counter()
    seite1 = dienst.search_all(
        anfrage.text, body_id=body_id, page=1, page_size=SEITE, index_names=indexe, ranking=ranking
    )
    wert.dauer_ms = round((time.perf_counter() - beginn) * 1000, 1)
    wert.treffer = int(seite1["total"])
    wert.aehnliche_schreibweisen = bool(seite1.get("similar_spelling"))
    for seite in range(1, anfrage.seiten + 1):
        ergebnis = (
            seite1
            if seite == 1
            else dienst.search_all(
                anfrage.text, body_id=body_id, page=seite, page_size=SEITE, index_names=indexe, ranking=ranking
            )
        )
        if int(ergebnis["total"]) > (seite - 1) * SEITE and not ergebnis["results"]:
            wert.leere_seiten += 1

    rangfolge = dienst.rank_hits(anfrage.text, body_id=body_id, index_names=indexe, depth=TIEFE, ranking=ranking)
    eintraege = [(index, doc_id) for _score, _pos, index, doc_id in rangfolge.entries]
    quellen = _quellen(dienst.client, eintraege)

    for index, doc_id in eintraege[:10]:
        datum = _datum(quellen.get((index, doc_id), {}), index)
        if datum is not None:
            wert.top10_datiert += 1
            wert.top10_jung += datum >= stichtag
    if anfrage.neuester:
        wert.neuester_erwartet = True
        gesucht = anfrage.neuester.lower()
        for platz, schluessel in enumerate(eintraege, start=1):
            quelle = quellen.get(schluessel, {})
            if gesucht in (str(quelle.get("reference", "")).lower(), str(quelle.get("paper_reference", "")).lower()):
                wert.platz_neuester = platz
                break
    top20 = eintraege[:SEITE]
    wert.top20 = len(top20)
    wert.top20_rauschen = len(top20) - _relevante(dienst.client, top20, anfrage.relevant)
    return wert


def _quellen(client: Any, eintraege: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Aktenzeichen und Datum der Treffer (gebündelt je Index)."""
    felder = ["reference", "paper_reference", "date", "start", "meeting_date"]
    quellen: dict[tuple[str, str], dict[str, Any]] = {}
    for index in dict.fromkeys(index for index, _doc_id in eintraege):
        ids = [doc_id for i, doc_id in eintraege if i == index]
        antwort = client.search(
            index=index, body={"query": {"ids": {"values": ids}}, "size": len(ids), "_source": felder}
        )
        for hit in antwort["hits"]["hits"]:
            quellen[(index, hit["_id"])] = hit.get("_source") or {}
    return quellen


def _datum(quelle: dict[str, Any], index: str) -> datetime | None:
    wert = quelle.get(DATUMSFELD.get(index, ""))
    if not wert:
        return None
    try:
        datum = datetime.fromisoformat(str(wert).replace("Z", "+00:00"))
    except ValueError:
        return None
    return datum if datum.tzinfo else datum.replace(tzinfo=UTC)


def _relevante(client: Any, eintraege: list[tuple[str, str]], praefixe: tuple[str, ...]) -> int:
    """Wie viele Treffer enthalten einen der relevanten Begriffe (Präfix eines Index-Terms)?"""
    if not praefixe:
        return len(eintraege)
    anzahl = 0
    for index in dict.fromkeys(index for index, _doc_id in eintraege):
        ids = [doc_id for i, doc_id in eintraege if i == index]
        bedingungen = [
            {"prefix": {feld: praefix}} for feld in PRUEFFELDER.get(index, ("name",)) for praefix in praefixe
        ]
        antwort = client.count(
            index=index,
            query={"bool": {"filter": [{"ids": {"values": ids}}], "should": bedingungen, "minimum_should_match": 1}},
        )
        anzahl += int(antwort["count"])
    return anzahl


def als_text(berichte: list[Bericht]) -> str:
    """Tabelle je Anfrage (Treffer, Z1, Z2, Z4, Dauer) und Kennzahlen je Version nebeneinander."""
    zeilen: list[str] = []
    for bericht in berichte:
        zeilen.append(f"== Ranking {bericht.ranking}")
        zeilen.append(
            f"{'Kommune':<10} {'Anfrage':<24} {'Treffer':>7} {'Z1 jung':>8} {'Z2 Platz':>8} {'Z4':>6} {'ms':>7}"
        )
        for m in bericht.messwerte:
            platz = "-" if m.platz_neuester is None else str(m.platz_neuester)
            z1 = f"{m.top10_jung}/{m.top10_datiert}"
            z4 = f"{m.top20_rauschen}/{m.top20}"
            unscharf = "~" if m.aehnliche_schreibweisen else " "
            zeilen.append(
                f"{m.kommune:<10} {m.text[:24]:<24} {m.treffer:>6}{unscharf} {z1:>8} {platz:>8} {z4:>6} {m.dauer_ms:>7.0f}"
            )
        zeilen.append("Kennzahlen: " + json.dumps(bericht.kennzahlen(), ensure_ascii=False))
        zeilen.append("")
    return "\n".join(zeilen)
