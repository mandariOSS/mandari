# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Golden-File-Tests der Parser des ALLRIS-3-Adapters (Issue #121).

Seiten von sieben ALLRIS-3-Instanzen und Sonderfälle aus dem Projekt bv-hh (MIT), dazu synthetische Seiten
(``fixtures/scrapers/allris3/README.md``). Ohne Netz. Bricht ein Umbau der Parser Felder, schlagen die Vergleiche mit
``expected/*.json`` fehl; die Sonderfälle prüfen Zeichensatz, leere Abschnitte und nicht öffentliche Seiten.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.scrapers import allris3
from src.scrapers.allris3 import (
    decode_page,
    html_to_text,
    is_calendar_page,
    is_not_public,
    page_kind,
    parse_agenda_item,
    parse_calendar,
    parse_factions,
    parse_meeting,
    parse_members,
    parse_paper,
    parse_paper_list,
    repair_c1,
)

FIXTURES = Path(__file__).parent / "fixtures" / "scrapers" / "allris3"
INSTANCES = ["altona", "bergedorf", "eimsbuettel", "hamburg_mitte", "hamburg_nord", "harburg", "wandsbek"]


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def page(name: str) -> str:
    return decode_page(raw(name))


def expected(instance: str, name: str) -> Any:
    return json.loads((FIXTURES / instance / "expected" / f"{name}.json").read_text(encoding="utf-8"))


def meeting_json(meeting: allris3.MeetingPage) -> dict[str, Any]:
    data = dataclasses.asdict(meeting)
    data["committee"] = list(meeting.committee) if meeting.committee else None
    return data


# ---------------------------------------------------------------------------
# Golden Files je Instanz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instance", INSTANCES)
class TestGoldenFiles:
    def test_meeting(self, instance: str) -> None:
        meeting = parse_meeting(page(f"{instance}/to010.html"))
        assert meeting_json(meeting) == expected(instance, "meeting")
        assert meeting.title and meeting.date and meeting.time_start
        assert meeting.committee is not None and meeting.committee[0] == "au"
        assert meeting.rows and all(row.tolfdnr for row in meeting.rows)
        assert any(row.volfdnr for row in meeting.rows), "mindestens ein Punkt mit Drucksache"

    def test_agenda_item(self, instance: str) -> None:
        item = parse_agenda_item(page(f"{instance}/to020.html"))
        assert item == expected(instance, "agenda_item")
        # Jede Seite stammt aus einer Sitzung mit Protokoll (bv-hh)
        assert item["minutes"] and len(item["minutes"]) >= 40

    def test_paper(self, instance: str) -> None:
        paper = parse_paper(page(f"{instance}/vo020.html"))
        assert paper == expected(instance, "paper")
        assert re.match(r"^\d{2}-\d", paper["reference"])
        assert paper["name"] and paper["paper_type"]
        text = f"{paper['explanation'] or ''}{paper['proposal'] or ''}"
        assert text, "Sachverhalt oder Beschlussvorschlag"
        # Word-Formatierung und Entitäten sind weg
        assert not re.search(r"<span|style=|&nbsp;|\xa0|&(amp|lt|gt|quot);", text)

    def test_committee(self, instance: str) -> None:
        committee = parse_members(page(f"{instance}/au020.html"))
        assert committee == expected(instance, "committee")
        assert committee["name"] and committee["members"]
        assert all(member["kplfdnr"] for member in committee["members"])

    def test_faction(self, instance: str) -> None:
        faction = parse_members(page(f"{instance}/fr020.html"))
        assert faction == expected(instance, "faction")
        assert faction["name"] and faction["members"]


class TestListen:
    def test_kalender(self) -> None:
        html = page("hamburg_nord/si010_e.html")
        assert page_kind(html) == "si010"
        assert is_calendar_page(html)
        assert allris3.calendar_month(html) == (2023, 10)
        stubs = parse_calendar(html, 2023, 10)
        assert [dataclasses.asdict(stub) for stub in stubs] == expected("hamburg_nord", "calendar")
        # Fünf Sitzungen mit Tagesordnung, die übrigen nur mit Nummer im Kalender (Fraktionen, entfallene)
        assert sum(stub.agenda_linked for stub in stubs) == 5
        assert len(stubs) == 14
        assert all(stub.date.startswith("2023-10-") for stub in stubs)

    def test_drucksachenliste(self) -> None:
        rows = parse_paper_list(page("hamburg_nord/vo040.html"))
        assert rows == expected("hamburg_nord", "paper_list")
        assert len(rows) == 50
        assert rows[0]["volfdnr"] == max(row["volfdnr"] for row in rows)

    def test_fraktionsliste(self) -> None:
        factions = parse_factions(page("hamburg_nord/fr010.html"))
        assert factions == expected("hamburg_nord", "factions")
        assert {"frlfdnr": 1120, "name": "Fraktion Bündnis 90/DIE GRÜNEN"} in factions

    def test_sitzung_eines_hauptorgans(self) -> None:
        meeting = parse_meeting(page("hamburg_nord/to010_pa.html"))
        assert meeting_json(meeting) == expected("hamburg_nord", "meeting_pa")
        assert meeting.committee == ("pa", 211, "Bezirksversammlung")


# ---------------------------------------------------------------------------
# Zeichensatz
# ---------------------------------------------------------------------------


class TestZeichensatz:
    TITEL = "Sichere Überwege dort, wo sie dringend gebraucht werden – Lichtsignalanlage im Kalckreuthweg"

    def test_windows_1252_aus_den_rohbytes(self) -> None:
        """ALLRIS deklariert ISO-8859-1, der Gedankenstrich kommt aber als 0x96 (Windows-1252)."""
        data = raw("altona/to010.html")
        assert b"\x96" in data
        titles = [row.title for row in parse_meeting(decode_page(data)).rows]
        assert any(title.startswith(self.TITEL) for title in titles)

    def test_als_iso_8859_1_gelesener_text_wird_repariert(self) -> None:
        """Hat der HTTP-Client nach der Deklaration (ISO-8859-1) dekodiert, steht dort U+0096; daraus wird „–“."""
        latin1 = raw("altona/to010.html").decode("latin-1")
        assert "\x96" in latin1
        titles = [row.title for row in parse_meeting(repair_c1(latin1)).rows]
        assert any(title.startswith(self.TITEL) for title in titles)
        assert not any("\x96" in title for title in titles)

    def test_utf8_bleibt(self) -> None:
        text = "Straße – „Mitte“"
        assert decode_page(text.encode("utf-8")) == text
        assert repair_c1(text) == text

    def test_synthetische_seite_mit_windows_1252(self) -> None:
        members = parse_members(page("synthetisch/pa020.html"))
        assert members["name"] == "Rat"
        assert [m["name"] for m in members["members"]] == [
            "Muster, Erika",
            "Müller-Lüdenscheidt, Jürgen",
            "Beispiel, Max",
        ]
        assert members["members"][1]["faction"] == "Fraktion B – Gruppe „Mitte“"
        # Ohne Personenseite nur mit Namen, beratend ohne Stimmrecht
        assert members["members"][2]["kplfdnr"] is None
        assert members["members"][2]["voting_right"] is False

    def test_wingdings_werden_aufzaehlungszeichen(self) -> None:
        paper = parse_paper(page("cases/harburg_wingdings_bullets.html"))
        text = f"{paper['explanation']}\n{paper['proposal']}"
        assert "\n▪ Fischbeker Heidbrook im Bereich Einkaufszentrum\n" in text
        assert not re.search(r"[\uf000-\uf0ff]", text)

    def test_word_spans_mitten_im_wort(self) -> None:
        """Word trennt Wörter an Umlauten in eigene Spans; dazwischen entsteht kein Leerzeichen."""
        html = "<div><p><span>Mobilitä</span><span>tsausschuss</span><span> </span><span>tagt</span></p></div>"
        assert html_to_text(html) == "Mobilitätsausschuss tagt"


# ---------------------------------------------------------------------------
# Abschnitte der Drucksache (Sonderfälle aus bv-hh)
# ---------------------------------------------------------------------------


def case(name: str) -> dict[str, Any]:
    return parse_paper(page(f"cases/{name}.html"))


class TestAbschnitte:
    def test_platzhalter_sind_leer(self) -> None:
        paper = case("bergedorf_placeholders")
        assert paper["explanation"].startswith("Auskunftsersuchen")
        assert paper["proposal"] is None

    def test_sachverhalt_und_petitum_in_einer_bezeichnung(self) -> None:
        paper = case("hamburg_nord_sachverhalt_und_petitum")
        assert paper["explanation"].startswith("Die GRÜNE Fraktion beantragt")
        assert paper["proposal"] is None

    def test_beschluss_als_bezeichnung(self) -> None:
        paper = case("eimsbuettel_beschluss_label")
        assert paper["proposal"].startswith("Der Vorsitzende der Bezirksversammlung wird gebeten")
        assert "Beschluss:" not in f"{paper['explanation']}{paper['proposal']}"

    def test_bloecke_ohne_bezeichnung_gehoeren_zum_sachverhalt(self) -> None:
        paper = case("harburg_question_and_answer")
        assert paper["explanation"].startswith("Der Winterdienst ist von großer Bedeutung")
        assert "BEZIRKSVERSAMMLUNG HARBURG" in paper["explanation"]
        assert paper["explanation"].endswith("gez. Böhm\nf.d.R. Leptien")
        assert paper["proposal"] is None

    def test_fussnote_bleibt_im_sachverhalt(self) -> None:
        paper = case("hamburg_mitte_footnote")
        assert paper["explanation"].startswith("Fragesteller: Roland Hoitz")
        assert re.search(r"\[1\] https://www\.abendblatt\.de/", paper["explanation"])

    def test_tabelle_und_fussnotenzeichen_aus_der_schrift_symbol(self) -> None:
        paper = case("hamburg_mitte_table_and_footnote_mark")
        assert "Nach § 2 Absatz" in paper["explanation"]
        assert "[*]" in paper["explanation"]
        assert paper["proposal"].startswith("Um Kenntnisnahme")

    def test_bezeichnung_in_einem_umschliessenden_block(self) -> None:
        paper = case("harburg_wrapped_resolution")
        assert paper["explanation"].startswith("Die Grundschule Neugraben")
        assert paper["proposal"].startswith("Der Vorsitzende wird gebeten")
        assert "Petitum" not in f"{paper['explanation']}{paper['proposal']}"

    def test_sachverhalt_ohne_bezeichnung(self) -> None:
        paper = parse_paper(page("altona/vo020.html"))
        assert "Mobilitätsausschuss empfiehlt" in paper["explanation"]
        assert "Petitum" not in paper["explanation"]
        assert paper["proposal"] == "Der Hauptauschuss wird um Zustimmung gebeten."

    def test_leere_absaetze_im_protokoll(self) -> None:
        """Bis zum Protokoll stehen auf to020 nur leere Absätze: kein Protokoll, kein Beschluss (bv-hh)."""
        blank = '<div><p style="font-size:11pt"><span>&nbsp;</span></p><p><span></span></p></div>'
        html = (
            '<div id="allriscontainer" class="to020"><table class="risdeco"><tr><td>'
            f'<a name="allrisWP"></a>{blank}<a name="allrisBS"></a><a name="allrisAE"></a>{blank}'
            "</td></tr></table></div>"
        )
        assert parse_agenda_item(html) == {"minutes": None, "resolution": None}


# ---------------------------------------------------------------------------
# Nicht öffentliche und fremde Seiten
# ---------------------------------------------------------------------------


class TestSeitenart:
    def test_nicht_oeffentliche_drucksache(self) -> None:
        html = page("synthetisch/vo020_nicht_oeffentlich.html")
        assert page_kind(html) == "vo020"
        assert is_not_public(html)

    def test_anmeldeseite(self) -> None:
        html = page("synthetisch/noauth.html")
        assert is_not_public(html)
        assert is_not_public("<html></html>", "https://ris.example.org/bi/noauth.asp?x=1")

    @pytest.mark.parametrize("instance", INSTANCES)
    def test_echte_seiten_sind_oeffentlich(self, instance: str) -> None:
        for name, kind in (("vo020", "vo020"), ("to010", "to010"), ("to020", "to020"), ("au020", "au020")):
            html = page(f"{instance}/{name}.html")
            assert page_kind(html) == kind
            assert not is_not_public(html)

    def test_fremde_seite_hat_keinen_allris_rahmen(self) -> None:
        sperrseite = "<html><head><title>Just a moment...</title></head><body>Prüfung</body></html>"
        assert page_kind(sperrseite) is None
        assert not is_calendar_page(page("hamburg_nord/vo040.html"))
