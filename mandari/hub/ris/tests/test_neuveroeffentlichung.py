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
    bestaetigte_kennung,
    name_key,
    reference_key,
    top_zuordnen,
    vorlage_zuordnen,
)

SITZUNG = str(uuid.uuid4())


def _top(
    name: str,
    nummer: str = "1",
    *,
    public: bool = True,
    vorlagen: tuple[str, ...] = (),
    geschwister: tuple[tuple[str, str], ...] | None = (),
) -> TopKennung:
    return TopKennung(
        meeting=SITZUNG,
        number=nummer,
        name=name_key(name),
        title=name,
        public=public,
        references=vorlagen,
        geschwister=geschwister,
    )


def _stand(kennung: TopKennung, *, steht: bool = True) -> TopStand:
    return TopStand(id=uuid.uuid4(), kennung=kennung, auf_tagesordnung=steht, geloescht=not steht)


def test_normalisierung() -> None:
    assert name_key("  Bericht der  Verwaltung: Haushalt 2027!") == "bericht der verwaltung haushalt 2027"
    assert reference_key(" V / 2026/0042 ") == "v/2026/0042"
    assert reference_key("1/23") != reference_key("12/3")


@pytest.mark.parametrize("geschwister", [None, (), (("a", "b"),)])
def test_kennung_hin_und_zurueck(geschwister: tuple[tuple[str, str], ...] | None) -> None:
    kennung = TopKennung(
        meeting=SITZUNG,
        number="3",
        name="a",
        title="A",
        public=False,
        papers=("p",),
        references=("r",),
        geschwister=geschwister,
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


def test_oeffentlich_und_nichtoeffentlich_auch_bei_gleicher_vorlage() -> None:
    anker = _top("Radweg", vorlagen=("v/1",))
    selbst = _stand(anker, steht=False)
    nichtoeffentlich = _stand(_top("Radweg", "20", public=False, vorlagen=("v/1",)))
    assert top_zuordnen(anker, selbst.id, [selbst, nichtoeffentlich]).ergebnis == ENTFALLEN


def test_mehrdeutig_ohne_eingrenzung() -> None:
    anker = _top("Anfragen", "2")
    selbst = _stand(anker, steht=False)
    staende = [selbst, _stand(_top("Anfragen", "5")), _stand(_top("Anfragen", "6"))]
    assert top_zuordnen(anker, selbst.id, staende).ergebnis == MEHRDEUTIG
    staende.append(_stand(_top("Anfragen", "2")))
    assert top_zuordnen(anker, selbst.id, staende).ergebnis == NACHFOLGER


# =============================================================================
# Dieselbe Vorlage an mehreren Punkten einer Sitzung
# =============================================================================


def _einbringung_und_beschluss() -> tuple[TopStand, TopStand]:
    einbringung = _stand(_top("Radweg – Einbringung", "5", vorlagen=("v/1",)))
    beschluss = _stand(_top("Radweg – Beschluss", "9", vorlagen=("v/1",)))
    return einbringung, beschluss


def test_bestaetigte_kennung_haelt_geschwister_fest() -> None:
    einbringung, beschluss = _einbringung_und_beschluss()
    anderer = _stand(_top("Haushalt", "2", vorlagen=("v/2",)))
    staende = [einbringung, beschluss, anderer]
    assert bestaetigte_kennung(einbringung, staende).geschwister == ((str(beschluss.id), "radweg beschluss"),)
    assert bestaetigte_kennung(anderer, staende).geschwister == ()
    geloescht = _stand(_top("Radweg – Einbringung", "5", vorlagen=("v/1",)), steht=False)
    assert bestaetigte_kennung(geloescht, [geloescht, beschluss]).geschwister is None, "unbekannt"


def test_geschwister_ist_kein_nachfolger() -> None:
    """Einbringung abgesetzt: Der Beschluss derselben Vorlage ist ein eigener Punkt, kein Nachfolger."""
    einbringung, beschluss = _einbringung_und_beschluss()
    anker = bestaetigte_kennung(einbringung, [einbringung, beschluss])
    abgesetzt = TopStand(id=einbringung.id, kennung=einbringung.kennung, auf_tagesordnung=False, geloescht=True)
    assert top_zuordnen(anker, einbringung.id, [abgesetzt, beschluss]).ergebnis == ENTFALLEN

    neu = _stand(_top("Radweg – Einbringung", "5", vorlagen=("v/1",)))
    zuordnung = top_zuordnen(anker, einbringung.id, [abgesetzt, beschluss, neu])
    assert (zuordnung.ergebnis, zuordnung.ziel) == (NACHFOLGER, neu.id)


@pytest.mark.parametrize(
    ("name", "nummer", "ergebnis"),
    [
        ("Radweg – Beschluss", "5", ENTFALLEN),
        ("Radweg Einbringung", "5", NACHFOLGER),
        # gleichnamig unter anderer Nummer: kann das Geschwister gewesen sein (TOP-Titel = Titel der Vorlage)
        ("Radweg – Einbringung", "9", ENTFALLEN),
    ],
)
def test_unbekannte_geschwister_verlangen_aehnlichen_namen_und_dieselbe_nummer(
    name: str, nummer: str, ergebnis: str
) -> None:
    """Kennung erst nach dem Absetzen erfasst: Über die Vorlage allein zählt dann kein Punkt."""
    anker = _top("Radweg – Einbringung", "5", vorlagen=("v/1",), geschwister=None)
    selbst = _stand(anker, steht=False)
    kandidat = _stand(_top(name, nummer, vorlagen=("v/1",)))
    assert top_zuordnen(anker, selbst.id, [selbst, kandidat]).ergebnis == ergebnis


@pytest.mark.parametrize(("nummer", "ergebnis"), [("2", NACHFOLGER), (" 2 ", NACHFOLGER), ("7", ENTFALLEN)])
def test_unbekannte_geschwister_ohne_vorlage_nur_unter_derselben_nummer(nummer: str, ergebnis: str) -> None:
    anker = _top("Mitteilungen", "2", geschwister=None)
    selbst = _stand(anker, steht=False)
    kandidat = _stand(_top("Mitteilungen", nummer))
    assert top_zuordnen(anker, selbst.id, [selbst, kandidat]).ergebnis == ergebnis


def test_bekannte_geschwister_erlauben_neue_nummer() -> None:
    """Im laufenden Betrieb sind die Geschwister bekannt: Ein neu veröffentlichter Punkt darf umnummeriert sein."""
    alt = _stand(_top("Mitteilungen", "2"))
    anker = bestaetigte_kennung(alt, [alt])
    abgesetzt = TopStand(id=alt.id, kennung=alt.kennung, auf_tagesordnung=False, geloescht=True)
    neu = _stand(_top("Mitteilungen", "7"))
    assert top_zuordnen(anker, alt.id, [abgesetzt, neu]).ziel == neu.id


def test_zeilenverschiebung_mit_geschwistern() -> None:
    """Nummer in der Adresse: Einfügung vor Einbringung (/5) und Beschluss (/6) derselben Vorlage."""
    zeile_5 = _stand(_top("Radweg – Einbringung", "5", vorlagen=("v/1",)))
    zeile_6 = _stand(_top("Radweg – Beschluss", "6", vorlagen=("v/1",)))
    anker_5 = bestaetigte_kennung(zeile_5, [zeile_5, zeile_6])
    anker_6 = bestaetigte_kennung(zeile_6, [zeile_5, zeile_6])

    heute = [
        TopStand(zeile_5.id, _top("Dringlichkeitsantrag", "5", vorlagen=("v/9",)), True, False),
        TopStand(zeile_6.id, _top("Radweg – Einbringung", "6", vorlagen=("v/1",)), True, False),
        _stand(_top("Radweg – Beschluss", "7", vorlagen=("v/1",))),
    ]
    assert top_zuordnen(anker_5, zeile_5.id, heute).ziel == zeile_6.id
    assert top_zuordnen(anker_6, zeile_6.id, heute).ziel == heute[2].id, "nicht über die Vorlage allein bestätigt"


# =============================================================================
# Vorlagen
# =============================================================================


def test_vorlage_nur_wenn_geloescht_und_eindeutig() -> None:
    body = str(uuid.uuid4())
    anker = VorlagenKennung(body=body, reference="V/1", name="radweg")
    alt = VorlagenStand(id=uuid.uuid4(), kennung=anker, geloescht=False, geaendert=None)
    neu = VorlagenStand(
        id=uuid.uuid4(),
        kennung=VorlagenKennung(body=body, reference="v/1", name="radweg"),
        geloescht=False,
        geaendert=None,
    )
    assert vorlage_zuordnen(anker, alt.id, alt, [neu]).ergebnis == BESTAETIGT

    geloescht = VorlagenStand(id=alt.id, kennung=anker, geloescht=True, geaendert=None)
    assert vorlage_zuordnen(anker, alt.id, geloescht, [neu]).ziel == neu.id
    fremd = VorlagenStand(
        id=uuid.uuid4(),
        kennung=VorlagenKennung(body=str(uuid.uuid4()), reference="V/1", name="radweg"),
        geloescht=False,
        geaendert=None,
    )
    assert vorlage_zuordnen(anker, alt.id, geloescht, [fremd]).ergebnis == ENTFALLEN
    zweite = VorlagenStand(
        id=uuid.uuid4(),
        kennung=VorlagenKennung(body=body, reference="V/1", name="radweg"),
        geloescht=False,
        geaendert=None,
    )
    assert vorlage_zuordnen(anker, alt.id, geloescht, [neu, zweite]).ergebnis == MEHRDEUTIG
    ohne_nummer = VorlagenKennung(body=body, reference="", name="radweg")
    assert vorlage_zuordnen(ohne_nummer, alt.id, geloescht, [neu]).ergebnis == ENTFALLEN


@pytest.mark.parametrize(
    ("name", "ergebnis"),
    [("Bebauungsplan Nord", NACHFOLGER), ("Bebauungsplan Nord-West", NACHFOLGER), ("Haushaltssatzung 2027", ENTFALLEN)],
)
def test_vorlage_braucht_aehnlichen_namen(name: str, ergebnis: str) -> None:
    """Gleiche Nummer, anderer Gegenstand (Nummer ohne Jahresteil wiederverwendet): kein Nachfolger."""
    body = str(uuid.uuid4())
    anker = VorlagenKennung(body=body, reference="123", name=name_key("Bebauungsplan Nord"))
    geloescht = VorlagenStand(id=uuid.uuid4(), kennung=anker, geloescht=True, geaendert=None)
    kandidat = VorlagenStand(
        id=uuid.uuid4(),
        kennung=VorlagenKennung(body=body, reference="123", name=name_key(name)),
        geloescht=False,
        geaendert=None,
    )
    assert vorlage_zuordnen(anker, geloescht.id, geloescht, [kandidat]).ergebnis == ergebnis
