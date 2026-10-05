# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regeln „derselbe Punkt, dieselbe Vorlage“ nach einer Neuveröffentlichung (Issue #547), ohne Datenbank."""

from __future__ import annotations

import uuid

import pytest

from hub.ris.neuveroeffentlichung import (
    ABWEICHEND,
    BESTAETIGT,
    ENTFALLEN,
    MEHRDEUTIG,
    NACHFOLGER,
    TopKennung,
    TopStand,
    VorlagenKennung,
    VorlagenStand,
    name_key,
    reference_key,
    top_zuordnen,
    vorlage_zuordnen,
)

SITZUNG = str(uuid.uuid4())


def _top(name: str, nummer: str = "1", *, public: bool = True, vorlagen: tuple[str, ...] = ()) -> TopKennung:
    return TopKennung(
        meeting=SITZUNG, number=nummer, name=name_key(name), title=name, public=public, references=vorlagen
    )


def _stand(kennung: TopKennung, *, steht: bool = True) -> TopStand:
    return TopStand(id=uuid.uuid4(), kennung=kennung, auf_tagesordnung=steht, geloescht=not steht)


def test_normalisierung() -> None:
    assert name_key("  Bericht der  Verwaltung: Haushalt 2027!") == "bericht der verwaltung haushalt 2027"
    assert reference_key(" V / 2026/0042 ") == "v/2026/0042"
    assert reference_key("1/23") != reference_key("12/3")


def test_kennung_hin_und_zurueck() -> None:
    kennung = TopKennung(
        meeting=SITZUNG, number="3", name="a", title="A", public=False, papers=("p",), references=("r",)
    )
    assert TopKennung.from_dict(kennung.as_dict()) == kennung
    assert TopKennung.from_dict({}) is None


def test_vorlage_geht_vor_name() -> None:
    anker = _top("Spielplatz", vorlagen=("v/1",))
    selbst = _stand(_top("Spielplatz", vorlagen=("v/9",)))
    nachfolger = _stand(_top("Spielplatz Nord, geänderter Titel", "4", vorlagen=("v/1",)))
    assert top_zuordnen(anker, selbst.id, [selbst, nachfolger]) == top_zuordnen(anker, selbst.id, [nachfolger, selbst])
    assert top_zuordnen(anker, selbst.id, [selbst, nachfolger]).ziel == nachfolger.id


def test_vorlage_bleibt_wenn_die_beratung_fehlt() -> None:
    anker = _top("Spielplatz", vorlagen=("v/1",))
    selbst = _stand(_top("Spielplatz"))
    zuordnung = top_zuordnen(anker, selbst.id, [selbst])
    assert zuordnung.ergebnis == BESTAETIGT
    assert zuordnung.kennung.references == ("v/1",)


@pytest.mark.parametrize(
    ("heute", "ergebnis"),
    [
        ("Sanierung der Grundschule Nord, 2. Bauabschnitt", BESTAETIGT),
        ("Verschiedenes", ABWEICHEND),
    ],
)
def test_korrektur_oder_anderer_punkt(heute: str, ergebnis: str) -> None:
    anker = _top("Sanierung der Grundschule Nord")
    selbst = _stand(_top(heute))
    assert top_zuordnen(anker, selbst.id, [selbst]).ergebnis == ergebnis


def test_exakter_name_an_anderer_stelle_schlaegt_aehnlichen_eigenen() -> None:
    anker = _top("Bericht des Kämmerers 2025")
    selbst = _stand(_top("Bericht des Kämmerers 2026"))
    verschoben = _stand(_top("Bericht des Kämmerers 2025", "2"))
    assert top_zuordnen(anker, selbst.id, [selbst, verschoben]).ziel == verschoben.id


def test_oeffentlich_und_nichtoeffentlich_sind_verschieden() -> None:
    anker = _top("Mitteilungen")
    selbst = _stand(anker, steht=False)
    nichtoeffentlich = _stand(_top("Mitteilungen", public=False))
    assert top_zuordnen(anker, selbst.id, [selbst, nichtoeffentlich]).ergebnis == ENTFALLEN


def test_mehrdeutig_ohne_eingrenzung() -> None:
    anker = _top("Anfragen", "2")
    selbst = _stand(anker, steht=False)
    staende = [selbst, _stand(_top("Anfragen", "5")), _stand(_top("Anfragen", "6"))]
    assert top_zuordnen(anker, selbst.id, staende).ergebnis == MEHRDEUTIG
    staende.append(_stand(_top("Anfragen", "2")))
    assert top_zuordnen(anker, selbst.id, staende).ergebnis == NACHFOLGER


def test_vorlage_nur_wenn_geloescht_und_eindeutig() -> None:
    body = str(uuid.uuid4())
    anker = VorlagenKennung(body=body, reference="V/1", name="radweg")
    alt = VorlagenStand(id=uuid.uuid4(), kennung=anker, geloescht=False, geaendert=None)
    neu = VorlagenStand(
        id=uuid.uuid4(), kennung=VorlagenKennung(body=body, reference="v/1"), geloescht=False, geaendert=None
    )
    assert vorlage_zuordnen(anker, alt.id, alt, [neu]).ergebnis == BESTAETIGT

    geloescht = VorlagenStand(id=alt.id, kennung=anker, geloescht=True, geaendert=None)
    assert vorlage_zuordnen(anker, alt.id, geloescht, [neu]).ziel == neu.id
    fremd = VorlagenStand(
        id=uuid.uuid4(),
        kennung=VorlagenKennung(body=str(uuid.uuid4()), reference="V/1"),
        geloescht=False,
        geaendert=None,
    )
    assert vorlage_zuordnen(anker, alt.id, geloescht, [fremd]).ergebnis == ENTFALLEN
    zweite = VorlagenStand(
        id=uuid.uuid4(), kennung=VorlagenKennung(body=body, reference="V/1"), geloescht=False, geaendert=None
    )
    assert vorlage_zuordnen(anker, alt.id, geloescht, [neu, zweite]).ergebnis == MEHRDEUTIG
    ohne_nummer = VorlagenKennung(body=body, reference="")
    assert vorlage_zuordnen(ohne_nummer, alt.id, geloescht, [neu]).ergebnis == ENTFALLEN
