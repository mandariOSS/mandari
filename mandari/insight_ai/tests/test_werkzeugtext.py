# SPDX-License-Identifier: AGPL-3.0-or-later
"""Werkzeugaufrufe im Antworttext eines Modells erkennen und abtrennen (Issue #1436)."""

from __future__ import annotations

from insight_ai.services.chat_tools import TOOL_NAMES
from insight_ai.services.werkzeugtext import lesen

KENNUNG = "c745ce6a-8687-5757-a531-9c933d3364e5"


def test_text_ohne_aufruf_bleibt_unveraendert() -> None:
    text = "Der Rat tagt am 07.10.2026. Für x < 3 gilt die alte Satzung, siehe [Vorlage](/insight/vorgaenge/1/)."
    gelesen = lesen(text, TOOL_NAMES)

    assert gelesen.text == text
    assert gelesen.aufrufe == []
    assert gelesen.markiert is False


def test_aufruf_mit_schluesseln_und_werten() -> None:
    text = (
        f"<tool_call>dokument_abschnitt<arg_key>id</arg_key><arg_value>{KENNUNG}</arg_value>"
        "<arg_key>frage</arg_key><arg_value>Planungskosten Gesamtkosten Sporthalle</arg_value></tool_call>"
    )
    gelesen = lesen(text, TOOL_NAMES)

    assert gelesen.text == ""
    assert gelesen.aufrufe == [
        ("dokument_abschnitt", {"id": KENNUNG, "frage": "Planungskosten Gesamtkosten Sporthalle"})
    ]
    assert gelesen.markiert is True


def test_einleitender_satz_bleibt_und_klammer_statt_schluesselmarke() -> None:
    text = (
        "Ich rufe den Ausschnitt aus der Beschlussvorlage auf.<tool_call>dokumente_suchen(text</arg_key>"
        "<arg_value>Gebühr 2027 Satzung</arg_value></tool_call>"
    )
    gelesen = lesen(text, TOOL_NAMES)

    assert gelesen.text == "Ich rufe den Ausschnitt aus der Beschlussvorlage auf."
    assert gelesen.aufrufe == [("dokumente_suchen", {"text": "Gebühr 2027 Satzung"})]


def test_aufruf_als_json() -> None:
    text = '<tool_call>\n{"name": "vorgaenge_suchen", "arguments": {"text": "Radweg"}}\n</tool_call>'
    assert lesen(text, TOOL_NAMES).aufrufe == [("vorgaenge_suchen", {"text": "Radweg"})]

    als_text = '<tool_call>{"name": "gremien", "arguments": "{\\"suchtext\\": \\"Rat\\"}"}</tool_call>'
    assert lesen(als_text, TOOL_NAMES).aufrufe == [("gremien", {"suchtext": "Rat"})]


def test_wahrheitswerte_und_mehrere_aufrufe() -> None:
    text = (
        "<tool_call>sitzungen_im_zeitraum<arg_key>von</arg_key><arg_value>2026-10-05</arg_value>"
        "<arg_key>bis</arg_key><arg_value>2026-10-11</arg_value>"
        "<arg_key>tagesordnung</arg_key><arg_value>true</arg_value></tool_call>\n"
        "<tool_call>gremien</tool_call>"
    )
    gelesen = lesen(text, TOOL_NAMES)

    assert gelesen.aufrufe == [
        ("sitzungen_im_zeitraum", {"von": "2026-10-05", "bis": "2026-10-11", "tagesordnung": True}),
        ("gremien", {}),
    ]


def test_unbekanntes_werkzeug_und_unlesbares_werden_nur_entfernt() -> None:
    text = (
        "Vorab.<tool_call>datei_loeschen<arg_key>pfad</arg_key><arg_value>/</arg_value></tool_call>"
        "<tool_call>{kein json</tool_call> Danach."
    )
    gelesen = lesen(text, TOOL_NAMES)

    assert gelesen.text == "Vorab. Danach."
    assert gelesen.aufrufe == []
    assert gelesen.markiert is True


def test_abgeschnittener_aufruf_und_einzelne_marken() -> None:
    abgeschnitten = lesen("Moment.<tool_call>dokumente_suchen<arg_key>text</arg_key><arg_value>Haush", TOOL_NAMES)
    assert abgeschnitten.text == "Moment."
    assert abgeschnitten.aufrufe == [("dokumente_suchen", {})]

    marke = lesen("Die Antwort.</tool_call>", TOOL_NAMES)
    assert marke.text == "Die Antwort."
    assert marke.markiert is True
