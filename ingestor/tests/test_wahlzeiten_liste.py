# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wahlzeiten aus der Liste ``legislativeTermList`` und Zähler nicht auflösbarer Verweise (Issue #1441).

Die meisten Quellen betten ihre Wahlzeiten in den Body ein. Manche nennen nur die Liste (OParl 1.1); bisher blieb
sie ungelesen, die Kommune hatte keine Wahlzeit. Jetzt liest der Vollabgleich die Liste, wenn der Body keine
Wahlzeit einbettet. Der Abruf ist eine Ergänzung: Eine leere, gesperrte oder gestörte Liste ändert nichts daran,
ob der Abgleich der Kommune vollständig ist.

Mehrere Systeme (SD.NET RIM, Stand 17.11.2026) beantworten die Liste mit einem leeren JSON-Array statt der Hülle
``data``/``links``; auch das gilt als leere Liste. Antworten aus ``httpx.MockTransport``, keine Abrufe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from src.client.source_options import SourceFetchOptions
from src.storage import database
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    BASE,
    BODY,
    Quelle,
    Speicher,
    _kommune,
    _lauf,
    _ohne_nebenlaeufe,
)

LISTE = f"{BODY}/legislativeterm"
PFAD = "/oparl/body/1/legislativeterm"


def _wahlzeit(nummer: int, **felder: Any) -> dict[str, Any]:
    return {
        "id": f"{BASE}/legislativeterm/{nummer}",
        "type": "https://schema.oparl.org/1.1/LegislativeTerm",
        "name": f"{nummer}. Wahlzeit",
        "startDate": f"{2000 + 5 * nummer}-11-01",
        "endDate": f"{2005 + 5 * nummer}-10-31",
        **felder,
    }


class MitWahlzeiten(Speicher):
    """Speicher, der die übernommenen Wahlzeiten festhält."""

    def __init__(self) -> None:
        super().__init__()
        self.wahlzeiten: list[tuple[str, str | None, UUID]] = []
        #: Kennungen, deren Speichern scheitert (etwa eine Wahlzeit einer anderen Quelle)
        self.abgewiesen: set[str] = set()

    async def upsert_legislative_term(self, term: Any, body_id: UUID) -> UUID:
        if term.external_id in self.abgewiesen:
            raise RuntimeError("gehört einer anderen Quelle")
        self.wahlzeiten.append((term.external_id, term.name, body_id))
        return UUID(int=len(self.wahlzeiten))


def _quelle(antwort: Any) -> Quelle:
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    quelle.fest[PFAD] = antwort
    return quelle


def _anfragen(quelle: Quelle) -> int:
    return quelle.anfragen_je_liste()["legislativeterm"]


async def test_vollabgleich_uebernimmt_wahlzeiten_aus_der_liste() -> None:
    quelle = _quelle({"data": [_wahlzeit(1), _wahlzeit(2)], "links": {}})
    speicher = MitWahlzeiten()

    stats = await _lauf(quelle, speicher, full=True, kommune=_kommune(legislativeTermList=LISTE))

    assert [(kennung, name) for kennung, name, _ in speicher.wahlzeiten] == [
        (f"{BASE}/legislativeterm/1", "1. Wahlzeit"),
        (f"{BASE}/legislativeterm/2", "2. Wahlzeit"),
    ]
    assert stats["legislative_terms"] == 2
    assert stats["incomplete"] == []
    assert stats["errors"] == []
    assert _anfragen(quelle) == 1


async def test_geloeschte_und_unbrauchbare_eintraege_bleiben_aus() -> None:
    eintraege = [_wahlzeit(1, deleted=True), "https://ris.example.org/oparl/legislativeterm/9", {"name": "ohne id"}]
    quelle = _quelle({"data": [*eintraege, _wahlzeit(2)], "links": {}})
    speicher = MitWahlzeiten()

    stats = await _lauf(quelle, speicher, full=True, kommune=_kommune(legislativeTermList=LISTE))

    assert [kennung for kennung, _, _ in speicher.wahlzeiten] == [f"{BASE}/legislativeterm/2"]
    assert stats["legislative_terms"] == 1


async def test_eine_abgewiesene_wahlzeit_haelt_die_uebrigen_nicht_auf() -> None:
    quelle = _quelle({"data": [_wahlzeit(1), _wahlzeit(2)], "links": {}})
    speicher = MitWahlzeiten()
    speicher.abgewiesen.add(f"{BASE}/legislativeterm/1")

    stats = await _lauf(quelle, speicher, full=True, kommune=_kommune(legislativeTermList=LISTE))

    assert [kennung for kennung, _, _ in speicher.wahlzeiten] == [f"{BASE}/legislativeterm/2"]
    assert stats["legislative_terms"] == 1
    assert stats["incomplete"] == []
    assert stats["errors"] == []


@pytest.mark.parametrize(
    "antwort",
    [
        pytest.param([], id="leeres_array_ohne_huelle"),
        pytest.param({"data": [], "links": {}}, id="leere_liste"),
        pytest.param(401, id="gesperrt"),
        pytest.param(404, id="gibt_es_nicht"),
        pytest.param(503, id="gestoert"),
    ],
)
async def test_leere_oder_unlesbare_liste_laesst_den_abgleich_vollstaendig(antwort: Any) -> None:
    quelle = _quelle(antwort)
    speicher = MitWahlzeiten()

    stats = await _lauf(quelle, speicher, full=True, kommune=_kommune(legislativeTermList=LISTE))

    assert speicher.wahlzeiten == []
    assert "legislative_terms" not in stats
    assert stats["incomplete"] == []
    assert stats["errors"] == []
    # Kein Vermerk als fehlende Liste: Die Ergänzung läuft neben der Buchführung der Listen
    assert LISTE not in speicher.fehlend


async def test_eingebettete_wahlzeiten_brauchen_die_liste_nicht() -> None:
    quelle = _quelle({"data": [_wahlzeit(2)], "links": {}})
    speicher = MitWahlzeiten()
    kommune = _kommune(legislativeTermList=LISTE, legislativeTerm=[_wahlzeit(1)])

    stats = await _lauf(quelle, speicher, full=True, kommune=kommune)

    assert _anfragen(quelle) == 0
    assert speicher.wahlzeiten == []
    assert "legislative_terms" not in stats


async def test_inkrementeller_lauf_fragt_die_liste_nicht_an() -> None:
    quelle = _quelle({"data": [_wahlzeit(1)], "links": {}})
    speicher = MitWahlzeiten()

    stats = await _lauf(quelle, speicher, kommune=_kommune(legislativeTermList=LISTE))

    assert _anfragen(quelle) == 0
    assert speicher.wahlzeiten == []
    assert stats["incomplete"] == []


async def test_ohne_adresse_der_liste_keine_anfrage() -> None:
    quelle = _quelle({"data": [_wahlzeit(1)], "links": {}})
    speicher = MitWahlzeiten()

    await _lauf(quelle, speicher, full=True)

    assert _anfragen(quelle) == 0
    assert speicher.wahlzeiten == []


async def test_abgewaehlte_liste_wird_nicht_angefragt() -> None:
    quelle = _quelle({"data": [_wahlzeit(1)], "links": {}})
    speicher = MitWahlzeiten()
    options = SourceFetchOptions.from_sync_config({"skip_lists": ["legislativeterm"]})
    assert "legislativeterm" in options.skip_lists

    await _lauf(quelle, speicher, full=True, kommune=_kommune(legislativeTermList=LISTE), options=options)

    assert _anfragen(quelle) == 0
    assert speicher.wahlzeiten == []


async def test_liste_wird_hoechstens_drei_seiten_weit_gelesen() -> None:
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    # Die nachgebildete Quelle blättert in Seiten zu zwei Objekten: neun Wahlzeiten sind fünf Seiten
    quelle.listen[PFAD] = [_wahlzeit(nummer) for nummer in range(1, 10)]
    speicher = MitWahlzeiten()

    stats = await _lauf(quelle, speicher, full=True, kommune=_kommune(legislativeTermList=LISTE))

    assert _anfragen(quelle) == 3
    assert stats["legislative_terms"] == 6
    assert stats["incomplete"] == []


# ---------------------------------------------------------------------------
# Zähler nicht auflösbarer Verweise
# ---------------------------------------------------------------------------


def test_zaehler_beginnt_je_abgleich_neu_und_zaehlt_je_art() -> None:
    erster = database.start_unresolved_references()
    database._note_unresolved(database.UNRESOLVED_MEETING_ORGANIZATION, 2)
    database._note_unresolved(database.UNRESOLVED_MEETING_ORGANIZATION)
    database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_PERSON)
    # Nichts aufzulösen ist kein Eintrag
    database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_ORGANIZATION, 0)

    assert erster == {"meeting.organization": 3, "membership.person": 1}

    zweiter = database.start_unresolved_references()
    database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_ORGANIZATION)

    assert zweiter == {"membership.organization": 1}
    assert erster == {"meeting.organization": 3, "membership.person": 1}


async def test_abgleich_nennt_nicht_aufloesbare_verweise_im_ergebnis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Was der Speicher während des Abgleichs zählt, steht unter ``unresolved`` im Ergebnis der Kommune."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    speicher = MitWahlzeiten()

    async def upsert_body(body: Any, source_id: UUID) -> UUID:
        # Stellvertretend für die Schreibwege des Speichers (``upsert_meeting``, ``upsert_membership``)
        database._note_unresolved(database.UNRESOLVED_MEETING_ORGANIZATION, 4)
        return UUID(int=1)

    monkeypatch.setattr(speicher, "upsert_body", upsert_body)

    stats = await _lauf(quelle, speicher, full=True)

    assert stats["unresolved"] == {"meeting.organization": 4}
    assert stats["incomplete"] == []


async def test_ohne_nicht_aufloesbare_verweise_kein_eintrag() -> None:
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))

    stats = await _lauf(quelle, MitWahlzeiten(), full=True)

    assert "unresolved" not in stats
