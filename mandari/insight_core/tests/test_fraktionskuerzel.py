# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionskürzel in Listen und Sitzverteilung (Issue #1272).

- Absender-Präfixe im Titel („Grüne-Antrag:“, „Antrag der Fraktion …:“, „Bürgerantrag:“) werden nach engen Regeln
  abgetrennt; was nicht ganz aufgeht (Stellungnahmen, Personen, Themenkürzel), bleibt stehen. Der gespeicherte Titel
  bleibt unverändert.
- Das Kürzel kommt aus den antragstellenden Fraktionen (#1138), sonst aus dem Titel; Farben aus der gepflegten
  Tabelle, Unbekanntes grau.
- Vorgangsliste, Startseite, Suche und Vorgangsseite zeigen Titel ohne Absender und Kürzel; Work bleibt unverändert.
- Die Gremienseite zeigt die Sitzverteilung als einen Balken mit Legende als Textalternative.

Nur erfundene Namen und Titel.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest
from django.template import engines
from django.test import Client
from django.utils import timezone
from django.utils.html import escape
from django_cotton.compiler_regex import CottonCompiler

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    VorgangUrheber,
)
from insight_core.services import fraktionskuerzel as fk
from insight_core.services import sitzverteilung
from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE, _safe_highlight

RIS = "https://ris.kuerzel.example/oparl"
_nummer = count()


def _render(quelle: str, **kontext: object) -> str:
    return engines["django"].from_string(CottonCompiler().process(quelle)).render(kontext)


def _urheber(bezeichnung: str, kurzform: str = "", art: str = "fraktion") -> SimpleNamespace:
    return SimpleNamespace(bezeichnung=bezeichnung, kurzform=kurzform, art=art)


# =============================================================================
# Regeln für den Titel
# =============================================================================


@pytest.mark.parametrize(
    ("titel", "art", "anzeige", "kuerzel", "von"),
    [
        # Absender vor der Art
        ("Grüne-Antrag: Nahversorgung im Baugebiet Nord", "Antrag", "Nahversorgung im Baugebiet Nord", ["Grüne"], ""),
        ("BBB-Fraktion-Große Anfrage: Leistungen an Nachbarkommunen", "Große Anfrage", "Leistungen an Nachbarkommunen", ["BBB"], ""),
        ("Antrag der AfB-Gruppe: Mehr Bänke", "Antrag", "Mehr Bänke", ["AfB"], ""),
        ("CDU-SPD-FDP-Kooperation-Antrag: Parkplatz am Nordring", "Antrag", "Parkplatz am Nordring", ["CDU", "SPD", "FDP"], ""),
        ("BSW/SBI-Gruppe-Große Anfrage: Stadtbahnwagen", "Große Anfrage", "Stadtbahnwagen", ["BSW/SBI"], ""),
        ("SPD-Bezirksfraktion-Antrag: Radweg am Bach", "Antrag", "Radweg am Bach", ["SPD"], ""),
        ("Volt Antrag: Digitale Verwaltung", "Antrag", "Digitale Verwaltung", ["Volt"], ""),
        ("Grüne-Antrag: Bäume", "Antrag", "Bäume", ["Grüne"], ""),
        ("FWG-Antrag: Feuerwehrhaus Nord", "Antrag", "Feuerwehrhaus Nord", ["FWG"], ""),
        ("Kooperation-Antrag: Haushalt sichern", "Antrag", "Haushalt sichern", [], "Kooperation"),
        # Art vor dem Absender
        ("Antrag der Fraktion Bündnis 90/Die Grünen: Radweg Hauptstraße", "Antrag", "Radweg Hauptstraße", ["Grüne"], ""),
        ("Anfrage der SPD-Fraktion vom 12.03.2026 – Zustand der Schulen", "Anfrage", "Zustand der Schulen", ["SPD"], ""),
        ("Antrag der Fraktion Die PARTEI/ÖDP: Mehr Bänke", "Antrag", "Mehr Bänke", ["PARTEI/ÖDP"], ""),
        (
            "Antrag der Ratsfraktionen von CDU und Bündnis 90/Die Grünen/GAL: Klimaschutz jetzt",
            "Antrag",
            "Klimaschutz jetzt",
            ["CDU", "Grüne/GAL"],
            "",
        ),
        (
            "Antrag der Fraktionen Bündnis 90/Die Grünen, CDU und Volt betreffend „Radverkehr in der Innenstadt“",
            "Antrag",
            "Radverkehr in der Innenstadt",
            ["Grüne", "CDU", "Volt"],
            "",
        ),
        # Nur der Absender
        ("CDU-Fraktion: Mehr Bänke im Stadtpark", "Antrag", "Mehr Bänke im Stadtpark", ["CDU"], ""),
        ("Fraktion DIE LINKE – Sozialticket erhalten", "Antrag", "Sozialticket erhalten", ["Linke"], ""),
        # Einwohnerschaft und interfraktionell: Wort statt Kürzel
        ("Bürgerantrag gem. § 24 GO NRW: Gefahrenstelle am Radweg", "Bürgerantrag", "Gefahrenstelle am Radweg", [], "Bürgerantrag"),
        ("Einwohnerfrage: Parkplätze am Bahnhof", "Einwohnerfrage", "Parkplätze am Bahnhof", [], "Einwohnerfrage"),
        ("Interfraktioneller Antrag: Gedenkort am Markt", "Antrag", "Gedenkort am Markt", [], "interfraktionell"),
        # Absender als Klammer am Ende
        ("Radweg Hauptstraße (Antrag der SPD-Fraktion)", "Antrag", "Radweg Hauptstraße", ["SPD"], ""),
        ("Mobilitätskonzept (Antrag CDU, SPD)", "Antrag", "Mobilitätskonzept", ["CDU", "SPD"], ""),
    ],
)  # fmt: skip
def test_absender_wird_abgetrennt(titel: str, art: str, anzeige: str, kuerzel: list[str], von: str) -> None:
    ergebnis = fk.absender(titel, art)

    assert ergebnis.titel == anzeige
    assert [k.text for k in ergebnis.kuerzel] == kuerzel
    assert ergebnis.von == von
    assert ergebnis.gekuerzt and ergebnis.abgetrennt
    # Aus dem Titel abgeleitet: so im Tooltip gekennzeichnet
    assert all(k.titel == fk.HINWEIS_TITEL for k in ergebnis.kuerzel)


@pytest.mark.parametrize(
    ("titel", "art"),
    [
        # Antworten und Stellungnahmen der Verwaltung behalten ihren Titel
        ("Stellungnahme der Verwaltung zum Grüne-Antrag: Nahversorgung", "Antrag"),
        ("Anfrage der CDU-Fraktion: Schulen am Ring", "Beantwortung einer Anfrage"),
        ("Beantwortung der Anfrage der CDU-Fraktion: Schulen", "Mitteilung"),
        # Absender, die nicht ganz aufgehen
        ("Antrag zur Änderung der Geschäftsordnung: Redezeiten", "Antrag"),
        ("Anfrage des Ratsmitglieds Max Muster (FDP): Bäume", "Anfrage"),
        ("Antrag der CDU-Fraktion zum Haushalt 2026: Kürzungen", "Antrag"),
        ("Antrag der Verwaltung: Haushaltssatzung 2026", "Antrag"),
        ("Gemeinsamer Antrag: Klimaschutz", "Antrag"),
        ("Antrag der Fraktion Unabhängige Wählergemeinschaft: Mehr Bänke", "Antrag"),
        # Ein Gruppenkürzel allein ist ohne bekannte Fraktion oft ein Thema oder Verein, keine Gruppe
        ("BBB-Große Anfrage: Leistungen an Nachbarkommunen", "Große Anfrage"),
        ("OGS-Antrag: Ausbau der Ganztagsbetreuung", "Antrag"),
        ("KiTa-Antrag: Neue Plätze", "Antrag"),
        ("TOP-Antrag: Reihenfolge", "Antrag"),
        ("WLAN-Antrag: Schulen", "Antrag"),
        ("LED-Antrag: Straßenbeleuchtung", "Antrag"),
        ("SGB-Antrag: Eingliederungshilfe", "Antrag"),
        ("TSV-Antrag: Kunstrasenplatz", "Antrag"),
        ("THW-Anfrage: Hochwasserschutz", "Anfrage"),
        ("DGB-Antrag: Tariftreue", "Antrag"),
        ("IT-Antrag: Ausstattung der Schulen", "Antrag"),
        ("UN-Antrag: Nachhaltigkeitsziele", "Antrag"),
        ("DRK-Antrag: Zuschuss Rettungswache", "Vorlage"),
        ("Antrag DRK: Zuschuss Rettungswache", "Antrag"),
        ("Zuschuss Rettungswache (Antrag DRK)", "Antrag"),
        ("CDU-WLAN-Antrag: Schulen", "Antrag"),
        # Gewöhnliche Titel mit Doppelpunkt
        ("Haushalt 2026: Eckpunkte", "Beschlussvorlage"),
        ("Sachstand: Radweg", "Mitteilungsvorlage"),
        ("Arbeitsgruppe Radverkehr: Bericht", "Mitteilungsvorlage"),
        ("Fraktionsvorsitzendenkonferenz: Termine", "Mitteilungsvorlage"),
        # Zu wenig Titel nach dem Absender
        ("Grüne-Antrag: 12", "Antrag"),
        ("", "Antrag"),
    ],
)
def test_absender_bleibt_stehen(titel: str, art: str) -> None:
    ergebnis = fk.absender(titel, art)

    assert ergebnis.titel == titel
    assert not ergebnis.gekuerzt and not ergebnis.kuerzel and not ergebnis.von


def test_art_bleibt_vorn_wenn_die_vorlagenart_sie_nicht_nennt() -> None:
    assert fk.absender("Grüne-Dringlichkeitsantrag: Beschwerde zurücknehmen", "Antrag").titel == (
        "Dringlichkeitsantrag: Beschwerde zurücknehmen"
    )
    assert fk.absender("Grüne-Dringlichkeitsantrag: Beschwerde zurücknehmen", "Dringlichkeitsantrag").titel == (
        "Beschwerde zurücknehmen"
    )
    assert fk.absender("Anfrage der SPD-Fraktion: Schulen am Ring", "").titel == "Anfrage: Schulen am Ring"
    # Klammer am Ende: keine Art davor
    assert fk.absender("Radweg Hauptstraße (Antrag der SPD-Fraktion)", "").titel == "Radweg Hauptstraße"


def test_leerraum_und_unicode_werden_vereinheitlicht() -> None:
    ergebnis = fk.absender("Grüne-Antrag:\n  Radweg   Nord", "Antrag")

    assert ergebnis.titel == "Radweg Nord" and [k.text for k in ergebnis.kuerzel] == ["Grüne"]


# =============================================================================
# Kürzel und Farben
# =============================================================================


def test_farbtabelle_ist_gepflegt_und_neutral() -> None:
    from insight_core.services import urheber_erkennung

    # Jede erkannte Partei hat ein Kürzel, jede Farbe ist ein Hexwert; ohne gesicherte Farbe grau
    assert set(fk.PARTEIEN) == set(urheber_erkennung.PARTEIEN)
    for partei in fk.PARTEIEN.values():
        assert re.fullmatch(r"#[0-9a-f]{6}", partei.farbe), partei
        assert 2 <= len(partei.kurz) <= fk.KUERZEL_MAX
    assert fk.PARTEIEN["tierschutz"].farbe == fk.GRAU


@pytest.mark.parametrize(
    ("bezeichnung", "kurzform", "text", "farbe"),
    [
        ("SPD-Fraktion im Rat der Stadt Beispiel", "", "SPD", "#e3000f"),
        ("Fraktion Bündnis 90/Die Grünen", "B90/Grüne", "Grüne", "#1aa037"),
        ("Fraktion DIE LINKE im Rat", "", "Linke", "#be3075"),
        ("SPD-Fraktion", "Fraktion", "SPD", "#e3000f"),
        ("Fraktion SPD/Volt", "", "SPD/Volt", "#e3000f"),
        ("Fraktion Mitte", "Mitte", "Mitte", fk.GRAU),
        ("Bürger Bund Beispiel (BBB)", "", "BBB", fk.GRAU),
        ("Freie Wähler Beispiel (FWB)", "", "FWB", "#f39200"),
        ("Bürgerliste", "", "Bürgerliste", fk.GRAU),
        ("Gruppe Die PARTEI & MOVE Beispiel", "Die PARTEI & MOVE", "PARTEI/MOVE", "#b5152b"),
    ],
)
def test_kuerzel_der_fraktion(bezeichnung: str, kurzform: str, text: str, farbe: str) -> None:
    kuerzel = fk.kuerzel_der_fraktion(bezeichnung, kurzform)

    assert kuerzel is not None
    assert (kuerzel.text, kuerzel.farbe, kuerzel.titel) == (text, farbe, bezeichnung)


def test_ohne_kurzes_kuerzel_keines() -> None:
    assert fk.kuerzel_der_fraktion("Unabhängige Wählergemeinschaft Beispielstadt") is None


def test_art_der_fraktion_im_tooltip() -> None:
    gruppe = fk.kuerzel_der_fraktion("Ratsgruppe Die PARTEI", art="gruppe")
    einzel = fk.kuerzel_der_fraktion("FDP", "FDP", art="einzelmandat")

    assert gruppe is not None and gruppe.titel == "Ratsgruppe Die PARTEI (Ratsgruppe)"
    assert einzel is not None and einzel.titel == "FDP (Einzelmandat)"


def test_gespeicherte_fraktionen_gehen_vor() -> None:
    urheber = [_urheber("SPD-Fraktion"), _urheber("Fraktion Bündnis 90/Die Grünen", "GRÜNE")]
    ergebnis = fk.absender("Antrag der Fraktionen SPD und Grüne: Radweg Nord", "Antrag", urheber)

    assert ergebnis.titel == "Radweg Nord"
    assert [(k.text, k.titel) for k in ergebnis.kuerzel] == [
        ("SPD", "SPD-Fraktion"),
        ("Grüne", "Fraktion Bündnis 90/Die Grünen"),
    ]


def test_gespeicherte_fraktion_ohne_praefix_im_titel() -> None:
    ergebnis = fk.absender("Radweg Nord", "Antrag", [_urheber("CDU-Fraktion")])

    assert ergebnis.titel == "Radweg Nord" and not ergebnis.gekuerzt
    assert [k.text for k in ergebnis.kuerzel] == ["CDU"]


def test_oertliche_fraktion_mit_gespeicherter_zuordnung_wird_abgetrennt() -> None:
    titel = "Antrag der Fraktion Unabhängige Wählergemeinschaft: Mehr Bänke"
    urheber = [_urheber("Fraktion Unabhängige Wählergemeinschaft", "UWG")]

    ergebnis = fk.absender(titel, "Antrag", urheber)

    assert ergebnis.titel == "Mehr Bänke" and [k.text for k in ergebnis.kuerzel] == ["UWG"]
    # … aber nie eine Stellungnahme, auch wenn eine Fraktion bekannt ist
    stellungnahme = fk.absender("Stellungnahme zum Antrag der Fraktion X: Bänke", "Antrag", urheber)
    assert stellungnahme.titel == "Stellungnahme zum Antrag der Fraktion X: Bänke"


def test_gruppenkuerzel_nur_mit_bekannter_fraktion_abgetrennt() -> None:
    bbb = [_urheber("Bürger Bund Beispiel (BBB)")]
    cdu = [_urheber("CDU-Fraktion")]

    ergebnis = fk.absender("BBB-Große Anfrage: Leistungen an Nachbarkommunen", "Große Anfrage", bbb)

    assert ergebnis.titel == "Leistungen an Nachbarkommunen" and ergebnis.gekuerzt
    assert [(k.text, k.titel) for k in ergebnis.kuerzel] == [("BBB", "Bürger Bund Beispiel (BBB)")]
    # Eine andere bekannte Fraktion bestätigt kein Themenkürzel: Titel bleibt, Kürzel aus der Erkennung
    wlan = fk.absender("WLAN-Antrag: Schulen", "Antrag", cdu)
    assert wlan.titel == "WLAN-Antrag: Schulen" and not wlan.gekuerzt
    assert [k.text for k in wlan.kuerzel] == ["CDU"]
    # Neben einer Partei braucht jedes Gruppenkürzel die Bestätigung
    assert not fk.absender("CDU-BBB-Antrag: Radweg Nord", "Antrag", cdu).gekuerzt
    beide = fk.absender("CDU-BBB-Antrag: Radweg Nord", "Antrag", [*cdu, *bbb])
    assert beide.titel == "Radweg Nord" and [k.text for k in beide.kuerzel] == ["CDU", "BBB"]


def test_gespeicherte_fraktion_ohne_kuerzel_laesst_den_titel_entscheiden() -> None:
    urheber = [_urheber("Unabhängige Wählergemeinschaft Beispielstadt")]

    assert fk.absender("Grüne-Antrag: Radweg Nord", "Antrag", urheber).kuerzel[0].text == "Grüne"
    assert fk.absender("Radweg Nord", "Antrag", urheber).kuerzel == ()


def test_hoechstens_drei_kuerzel_sichtbar() -> None:
    ergebnis = fk.absender("CDU-SPD-FDP-Volt-Antrag: Radweg Nord", "Antrag")

    assert [k.text for k in ergebnis.sichtbar] == ["CDU", "SPD", "FDP"] and ergebnis.weitere == 1
    assert ergebnis.teile == ("CDU", "SPD", "FDP", "Volt")


# =============================================================================
# Bausteine
# =============================================================================


def test_baustein_absender() -> None:
    html = _render(
        '<c-insight.absender :absender="a" hinweis="eingebracht von" />',
        a=fk.absender("CDU-SPD-FDP-Volt-Antrag: Radweg Nord", "Antrag"),
    )

    assert '<span class="sr-only">eingebracht von </span>' in html
    assert 'fill="#1f1f1f"' in html and 'fill="#e3000f"' in html and 'fill="#e6c300"' in html
    assert ">CDU</span>" in html and ">SPD</span>" in html and "und 1 weitere" in html
    assert 'title="aus dem Titel abgeleitet"' in html
    # Gleich groß für alle, die Farbe nur im Strich, ohne style-Attribut (Frontend-Ratchet)
    assert html.count("text-[0.8125rem] font-semibold") == 3 and "style=" not in html
    assert _render('<c-insight.absender :absender="a" />', a=fk.absender("Radweg Nord", "Antrag")).strip() == ""
    buerger = _render('<c-insight.absender :absender="a" />', a=fk.absender("Bürgerantrag: Radweg Nord"))
    assert "Bürgerantrag</span>" in buerger and "<svg" not in buerger


def test_buergerantrag_nicht_doppelt_neben_der_art() -> None:
    mit_art = fk.absender("Bürgerantrag: Radweg Nord", "Bürgerantrag § 24 GO - Öffentlich")

    assert mit_art.von == "Bürgerantrag" and mit_art.von_zusatz == "" and not mit_art.neben_art
    assert _render('<c-insight.absender :absender="a" neben_art />', a=mit_art).strip() == ""
    assert "Bürgerantrag</span>" in _render('<c-insight.absender :absender="a" />', a=mit_art)
    ohne_art = fk.absender("Bürgerantrag: Radweg Nord", "Anregung")
    assert "Bürgerantrag</span>" in _render('<c-insight.absender :absender="a" neben_art />', a=ohne_art)


def test_schwarz_hat_im_dunkelmodus_eine_eigene_farbe() -> None:
    cdu = fk.absender("CDU-Antrag: Radweg Nord", "Antrag").kuerzel[0]
    spd = fk.absender("SPD-Antrag: Radweg Nord", "Antrag").kuerzel[0]

    assert (cdu.farbe, cdu.farbe_dunkel) == ("#1f1f1f", "#e5e5e5") and spd.farbe_dunkel == spd.farbe
    html = _render('<c-insight.kuerzel :kuerzel="k" />', k=cdu)
    assert 'fill="#1f1f1f" class="dark:hidden"' in html and 'fill="#e5e5e5" class="hidden dark:block"' in html
    assert html.count("<rect") == 1 + 1 and _render('<c-insight.kuerzel :kuerzel="k" />', k=spd).count("<rect") == 1


def _vorgang(**felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {"id": uuid.uuid4(), "name": "", "reference": "", "paper_type": "Antrag", "date": None}
    return SimpleNamespace(**(werte | felder))


def test_vorgangsliste_mit_und_ohne_absender() -> None:
    mit = _vorgang(name="Grüne-Antrag: Radweg Nord")
    mit.absender = fk.absender(mit.name, mit.paper_type)
    html = _render('<c-liste.vorgaenge :vorgaenge="v" />', v=[mit])

    assert ">Von</th>" in html and "Radweg Nord" in html and "Grüne-Antrag" not in html
    assert html.count(">Grüne</span>") == 2  # Spalte ab sm und Zeile darunter

    # Nur Bürgeranträge: Die Spalte „Typ“ sagt es schon, keine Spalte „Von“
    buerger = _vorgang(name="Bürgerantrag: Radweg Nord", paper_type="Bürgerantrag")
    buerger.absender = fk.absender(buerger.name, buerger.paper_type)
    nur_buerger = _render('<c-liste.vorgaenge :vorgaenge="v" />', v=[buerger])
    assert ">Von</th>" not in nur_buerger and "Radweg Nord" in nur_buerger

    # Work hängt keinen Absender an: Titel des RIS, keine Spalte
    work = _render('<c-liste.vorgaenge :vorgaenge="v" dicht ohne_merken />', v=[_vorgang(name="Grüne-Antrag: Radweg")])
    assert "Grüne-Antrag: Radweg" in work and ">Von</th>" not in work


# =============================================================================
# Suche: Hervorhebung bleibt
# =============================================================================


def _markiert(text: str) -> str:
    return text.replace("[", HIGHLIGHT_PRE).replace("]", HIGHLIGHT_POST)


@pytest.mark.parametrize(
    ("markiert", "erwartet"),
    [
        ("Grüne-Antrag: Radweg [Nord]", "Radweg [Nord]"),
        ("[Grüne]-Antrag: Radweg Nord", "Radweg Nord"),
        ("Grüne-[Antrag: Radweg] Nord", "[Radweg] Nord"),
        ("Grüne-Antrag: [Radweg] & <Nord>", "[Radweg] &amp; &lt;Nord&gt;"),
    ],
)
def test_hervorhebung_ohne_absender(markiert: str, erwartet: str) -> None:
    roh = markiert.replace("[", "").replace("]", "")
    fund = fk.praefix_finden(roh)
    assert fund is not None

    gekuerzt = fk._markiert_kuerzen(str(_safe_highlight(_markiert(markiert))), fund)

    assert gekuerzt == _markiert(erwartet)


def test_hervorhebung_mit_anderem_text_faellt_zurueck() -> None:
    fund = fk.praefix_finden("Grüne-Antrag: Radweg Nord")
    assert fund is not None

    assert fk._markiert_kuerzen(_markiert("Radweg [Nord] …"), fund) is None


@pytest.mark.django_db
def test_suchtreffer_anreichern() -> None:
    pk = uuid.uuid4()
    treffer: list[dict[str, Any]] = [
        {
            "kind": "vorgang",
            "pk": str(pk),
            "title": _safe_highlight(_markiert("Grüne-Dringlichkeitsantrag: [Radweg] Nord")),
            "titel_roh": "Grüne-Dringlichkeitsantrag: Radweg Nord",
            "art_roh": "Antrag",
        },
        {"kind": "vorgang", "pk": "keine-kennung", "title": escape("Grüne-Antrag: Bänke"), "titel_roh": "x"},
        {"kind": "person", "pk": str(uuid.uuid4()), "title": "Mara Muster"},
    ]
    VorgangUrheber.objects.create(
        paper_id=pk, body_id=uuid.uuid4(), bezeichnung="Fraktion Bündnis 90/Die Grünen", kurzform="", herkunft="title"
    )

    fk.treffer_anreichern(treffer)

    assert str(treffer[0]["title"]) == f"Dringlichkeitsantrag: {HIGHLIGHT_PRE}Radweg{HIGHLIGHT_POST} Nord"
    assert [k.titel for k in treffer[0]["absender"].kuerzel] == ["Fraktion Bündnis 90/Die Grünen"]
    assert "absender" not in treffer[1] and "absender" not in treffer[2]


@pytest.mark.django_db
def test_suchtreffer_mit_bestaetigtem_gruppenkuerzel() -> None:
    pk = uuid.uuid4()
    treffer: list[dict[str, Any]] = [
        {
            "kind": "vorgang",
            "pk": str(pk),
            "title": _safe_highlight(_markiert("BBB-Große Anfrage: [Leistungen] an Nachbarkommunen")),
            "titel_roh": "BBB-Große Anfrage: Leistungen an Nachbarkommunen",
            "art_roh": "Große Anfrage",
        },
        {
            "kind": "vorgang",
            "pk": str(uuid.uuid4()),
            "title": _safe_highlight(_markiert("WLAN-Antrag: [Schulen]")),
            "titel_roh": "WLAN-Antrag: Schulen",
            "art_roh": "Antrag",
        },
    ]
    VorgangUrheber.objects.create(
        paper_id=pk, body_id=uuid.uuid4(), bezeichnung="Bürger Bund Beispiel (BBB)", kurzform="", herkunft="title"
    )

    fk.treffer_anreichern(treffer)

    assert str(treffer[0]["title"]) == f"{HIGHLIGHT_PRE}Leistungen{HIGHLIGHT_POST} an Nachbarkommunen"
    assert [k.text for k in treffer[0]["absender"].kuerzel] == ["BBB"]
    # Ohne bekannte Fraktion bleibt das Themenkürzel samt Hervorhebung stehen
    assert str(treffer[1]["title"]) == f"WLAN-Antrag: {HIGHLIGHT_PRE}Schulen{HIGHLIGHT_POST}"
    assert not treffer[1]["absender"].gekuerzt and not treffer[1]["absender"].kuerzel


@pytest.mark.django_db
def test_suchtreffer_tragen_titel_und_art_des_index() -> None:
    from insight_core.services.search_presentation import present_groups

    gruppen = [{"paper": {"id": str(uuid.uuid4()), "name": "Grüne-Antrag:  Radweg", "paper_type": "Antrag"}}]
    treffer = present_groups(gruppen)[0]

    assert (treffer["titel_roh"], treffer["art_roh"]) == ("Grüne-Antrag: Radweg", "Antrag")


# =============================================================================
# Seiten
# =============================================================================


@pytest.fixture
def body() -> OParlBody:
    quelle = OParlSource.objects.create(name="Kürzel-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=quelle, name="Stadt Beispiel", slug="beispiel", is_listed=True
    )


def _client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


def _paper(body: OParlBody, name: str, paper_type: str = "Antrag") -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"{RIS}/paper/{next(_nummer)}", body=body, name=name, paper_type=paper_type, reference="A/1/2026"
    )


@pytest.mark.django_db
def test_vorgangsliste_zeigt_kuerzel(body: OParlBody) -> None:
    _paper(body, "Grüne-Antrag: Radweg am Bach")
    _paper(body, "Neubau Feuerwache Süd", "Beschlussvorlage")

    html = _client(body).get("/insight/vorgaenge/").content.decode()

    assert "Radweg am Bach" in html and "Grüne-Antrag" not in html
    assert ">Von</th>" in html and 'fill="#1aa037"' in html and "Neubau Feuerwache Süd" in html


@pytest.mark.django_db
def test_vorgangsseite_mit_titel_im_ris(body: OParlBody) -> None:
    vorgang = _paper(body, "Antrag der SPD-Fraktion: Radweg am Bach")
    VorgangUrheber.objects.create(
        paper_id=vorgang.pk, body_id=body.pk, bezeichnung="SPD-Fraktion", kurzform="SPD", herkunft="title"
    )

    html = _client(body).get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert h1 and h1.group(1).strip() == "Radweg am Bach"
    assert 'data-testid="titel-im-ris">Antrag der SPD-Fraktion: Radweg am Bach</dd>' in html
    assert 'title="SPD-Fraktion"' in html and 'fill="#e3000f"' in html
    # Gespeichert bleibt der Titel des RIS
    vorgang.refresh_from_db()
    assert vorgang.name == "Antrag der SPD-Fraktion: Radweg am Bach"


@pytest.mark.django_db
def test_vorgangsseite_ohne_absender_unveraendert(body: OParlBody) -> None:
    vorgang = _paper(body, "Neubau Feuerwache Süd", "Beschlussvorlage")

    html = _client(body).get(f"/insight/vorgaenge/{vorgang.pk}/").content.decode()

    assert "Titel im RIS" not in html and 'data-testid="absender"' not in html


@pytest.mark.django_db
def test_startseite_zuletzt_beschlossen_mit_kuerzel(body: OParlBody) -> None:
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/rat", body=body, name="Rat", classification="Rat")
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1", body=body, name="Rat", start=timezone.now() - timedelta(days=2)
    )
    sitzung.organizations.add(rat)
    punkt = OParlAgendaItem.objects.create(
        external_id=f"{RIS}/top/1", meeting=sitzung, name="TOP 1", result="beschlossen", public=True
    )
    vorgang = _paper(body, "CDU-Antrag: Bänke im Stadtpark")
    OParlConsultation.objects.create(
        external_id=f"{RIS}/beratung/1",
        body=body,
        paper=vorgang,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
    )

    html = _client(body).get("/insight/").content.decode()

    abschnitt = re.search(r'id="zuletzt-beschlossen".*?</section>', html, re.S)
    assert abschnitt, "keine Liste „Zuletzt beschlossen“"
    assert "Bänke im Stadtpark" in abschnitt.group(0) and "CDU-Antrag" not in abschnitt.group(0)
    assert ">CDU</span>" in abschnitt.group(0)


# =============================================================================
# Sitzverteilung
# =============================================================================


@pytest.mark.parametrize(
    ("rolle", "art"),
    [
        ("Mitglied", sitzverteilung.SITZ),
        ("", sitzverteilung.SITZ),
        ("Sachkundiger Bürger", sitzverteilung.SITZ),
        ("Vorsitzende", sitzverteilung.SITZ),
        ("Stellv. Vorsitz", sitzverteilung.SITZ),
        ("Stellvertreterin des Vorsitzenden", sitzverteilung.SITZ),
        ("Stellv. Mitglied", sitzverteilung.STELLVERTRETEND),
        ("stellvertretendes Mitglied", sitzverteilung.STELLVERTRETEND),
        ("Persönliche Vertreterin", sitzverteilung.STELLVERTRETEND),
        ("Vertreter/in", sitzverteilung.STELLVERTRETEND),
        ("beratendes Mitglied", sitzverteilung.BERATEND),
        ("Sachk. Bürger/in (ohne Stimmr.)", sitzverteilung.BERATEND),
        ("Schriftführerin", sitzverteilung.KEINER),
        ("Vertreter der Verwaltung", sitzverteilung.KEINER),
        ("Gast", sitzverteilung.KEINER),
    ],
)
def test_rollen(rolle: str, art: str) -> None:
    assert sitzverteilung.rolle_einordnen(rolle) == art


def _org(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{RIS}/org/{next(_nummer)}", body=body, name=name, **felder)


def _mitglied(body: OParlBody, gremium: OParlOrganization, rolle: str, fraktion: OParlOrganization | None) -> None:
    n = next(_nummer)
    person = OParlPerson.objects.create(
        external_id=f"{RIS}/person/{n}", body=body, name=f"Person {n}", given_name="Person", family_name=f"N{n}"
    )
    OParlMembership.objects.create(
        external_id=f"{RIS}/membership/{n}-g", person=person, organization=gremium, role=rolle
    )
    if fraktion is not None:
        OParlMembership.objects.create(
            external_id=f"{RIS}/membership/{n}-f", person=person, organization=fraktion, role="Mitglied"
        )


@pytest.fixture
def ausschuss(body: OParlBody) -> OParlOrganization:
    gremium = _org(body, "Ausschuss für Mobilität", classification="Ausschuss")
    spd = _org(body, "SPD-Fraktion im Rat", short_name="SPD", organization_type="Fraktion")
    cdu = _org(body, "CDU-Fraktion", organization_type="Fraktion")
    mitte = _org(body, "Fraktion Mitte", short_name="Mitte", organization_type="Fraktion")
    for _ in range(3):
        _mitglied(body, gremium, "Mitglied", spd)
    for _ in range(3):
        _mitglied(body, gremium, "Mitglied", cdu)
    _mitglied(body, gremium, "Vorsitzende", mitte)
    _mitglied(body, gremium, "Sachkundige Bürgerin", None)
    _mitglied(body, gremium, "Stellv. Mitglied", spd)
    _mitglied(body, gremium, "Stellv. Mitglied", cdu)
    _mitglied(body, gremium, "beratendes Mitglied", None)
    _mitglied(body, gremium, "Schriftführerin", None)
    return gremium


@pytest.mark.django_db
def test_gremienseite_mit_sitzverteilung(body: OParlBody, ausschuss: OParlOrganization) -> None:
    html = _client(body).get(f"/insight/gremien/{ausschuss.pk}/").content.decode()

    balken = re.search(r'<section[^>]*data-testid="sitzverteilung".*?</section>', html, re.S)
    assert balken, "keine Sitzverteilung"
    teil = balken.group(0)
    # Reihenfolge nach Sitzen, bei Gleichstand nach Kürzel; ohne Fraktion am Ende
    legende = re.findall(
        r">([^<>]+)</span>\s*<span class=\"tabular-nums[^\"]*\"><span class=\"sr-only\">: </span>(\d+)", teil
    )
    assert legende == [("CDU", "3"), ("SPD", "3"), ("Mitte", "1"), ("ohne Fraktionsangabe", "1")]
    assert 'viewBox="0 0 8 1"' in teil and 'aria-hidden="true"' in teil
    assert "<title>SPD-Fraktion im Rat: 3 Sitze</title>" in teil
    assert "8 Sitze aus den laufenden Mitgliedschaften" in teil
    assert "dazu 2 Stellvertretungen und 1 beratendes Mitglied." in teil
    assert "style=" not in teil


@pytest.mark.django_db
def test_keine_sitzverteilung_ohne_aussage(body: OParlBody) -> None:
    gremium = _org(body, "Beirat", classification="Beirat")
    spd = _org(body, "SPD-Fraktion", organization_type="Fraktion")
    _mitglied(body, gremium, "Mitglied", spd)
    for _ in range(3):
        _mitglied(body, gremium, "Mitglied", None)
    client = _client(body)

    assert 'data-testid="sitzverteilung"' not in client.get(f"/insight/gremien/{gremium.pk}/").content.decode()
    # Nie auf der Seite einer Fraktion
    for _ in range(3):
        _mitglied(body, spd, "Mitglied", None)
    assert 'data-testid="sitzverteilung"' not in client.get(f"/insight/gremien/{spd.pk}/").content.decode()


def test_berechnen_zaehlt_personen_einmal() -> None:
    fraktion = SimpleNamespace(name="SPD-Fraktion", short_name="SPD")
    mitgliedschaften = [
        SimpleNamespace(person_id=1, role="Stellv. Mitglied"),
        SimpleNamespace(person_id=1, role="Mitglied"),
        SimpleNamespace(person_id=2, role="Mitglied"),
        SimpleNamespace(person_id=3, role="Mitglied"),
    ]

    ergebnis = sitzverteilung.berechnen(mitgliedschaften, {1: fraktion, 2: fraktion, 3: fraktion})

    assert ergebnis is not None
    assert (ergebnis.sitze, ergebnis.stellvertretungen, [a.sitze for a in ergebnis.anteile]) == (3, 0, [3])
    assert ergebnis.grenzen == ()


def test_gepflegte_fraktion_zaehlt() -> None:
    gepflegt = SimpleNamespace(bezeichnung="Grüne", name="Fraktion Bündnis 90/Die Grünen", hinweis="")
    mitgliedschaften = [SimpleNamespace(person_id=n, role="Mitglied") for n in range(3)]

    ergebnis = sitzverteilung.berechnen(mitgliedschaften, dict.fromkeys(range(3), gepflegt))

    assert ergebnis is not None and ergebnis.anteile[0].text == "Grüne" and ergebnis.anteile[0].farbe == "#1aa037"
