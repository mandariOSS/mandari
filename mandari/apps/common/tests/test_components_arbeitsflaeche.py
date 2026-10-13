# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ergänzungen der gemeinsamen Bausteine für Arbeitsflächen (Issue #951).

``dicht`` setzt die Maße einer Arbeitsplattform (Felder 36 px, Schrift 14 px), ``modell`` ein Alpine-Modell am
Eingabefeld selbst (weitere Attribute landen am Rahmen), ``maxlength`` die Höchstlänge, ``zeile`` die Beschriftung
neben das Auswahlfeld; die Checkbox kennt ``value``, ``modell`` und ``disabled``, das Textfeld ``id``. Der Reiter
zeigt eine Zahl ohne Pille, ``c-form.datei`` ist ein Dateifeld mit Ablagefläche. Ohne diese Angaben bleibt das Markup
Zeichen für Zeichen wie bisher – andere Seiten (Anmeldung, Session, Insight) ändern sich nicht.
"""

from __future__ import annotations

import re

from django.template import engines
from django_cotton.compiler_regex import CottonCompiler

#: Klassen der bisherigen Felder (vor #951) – ohne neue Angaben müssen sie unverändert bleiben
BISHER_FELD = (
    "w-full px-4 py-3 rounded-lg border bg-white dark:bg-gray-700 text-gray-900 dark:text-white placeholder-gray-400 "
    "transition-colors focus:ring-2 focus:ring-primary-500 focus:border-primary-500 border-gray-300 dark:border-gray-600"
)
BISHER_AUSWAHL = (
    "w-full px-4 py-3 rounded-lg border bg-white dark:bg-gray-700 text-gray-900 dark:text-white transition-colors "
    "focus:ring-2 focus:ring-primary-500 focus:border-primary-500 border-gray-300 dark:border-gray-600"
)
BISHER_LABEL = "block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
BISHER_HILFE = "mt-1 text-xs text-gray-500 dark:text-gray-400"


def render(source: str, **context: object) -> str:
    return engines["django"].from_string(CottonCompiler().process(source)).render(context)


def _tag(html: str, name: str) -> str:
    treffer = re.search(rf"<{name}\b[^>]*>", html)
    assert treffer, html
    return treffer.group(0)


def _klasse(tag: str) -> str:
    treffer = re.search(r'class="([^"]*)"', tag)
    assert treffer, tag
    return " ".join(treffer.group(1).split())


def _attribute(tag: str) -> set[str]:
    """Attributnamen eines Tags (ohne Werte), um ungewollte neue Attribute zu finden."""
    ohne_werte = re.sub(r'="[^"]*"', "=", tag)
    return set(re.findall(r"\s([@:\w.-]+)(?==|\s|>)", ohne_werte))


class TestFeld:
    def test_ohne_ergaenzung_wie_bisher(self) -> None:
        html = render('<c-form.field name="titel" label="Titel" help="Kurz" />')
        feld = _tag(html, "input")
        assert _klasse(feld) == BISHER_FELD
        assert _attribute(feld) == {"type", "name", "id", "value", "class", "aria-describedby"}
        assert _klasse(_tag(html, "label")) == BISHER_LABEL
        assert _klasse(re.findall(r"<p\b[^>]*>", html)[-1]) == BISHER_HILFE

    def test_fehler_ohne_ergaenzung_wie_bisher(self) -> None:
        class Feld:
            name = "email"
            id_for_label = "id_email"
            value = ""
            errors = ["Pflichtfeld"]

        klasse = _klasse(_tag(render('<c-form.field :field="f" label="E-Mail" />', f=Feld()), "input"))
        assert klasse.endswith("border-red-400 dark:border-red-600")

    def test_dicht_modell_und_hoechstlaenge_am_feld(self) -> None:
        html = render(
            '<c-form.field name="title" id="title" label="Titel" modell="title" maxlength="500" dicht required '
            '@input="geaendert = true" />'
        )
        feld = _tag(html, "input")
        assert 'x-model="title"' in feld and 'maxlength="500"' in feld and "required" in feld
        assert "h-9" in feld and "text-sm" in feld and "bg-band-hell" in feld and "px-4 py-3" not in feld
        # Weitere Attribute gehen an den Rahmen; Ereignisse des Felds steigen dorthin auf
        assert '@input="geaendert = true"' in _tag(html, "div")

    def test_wert_wird_genau_einmal_maskiert(self) -> None:
        html = render('<c-form.field name="t" :value="v" dicht />', v='A & B "<x>"')
        assert 'value="A &amp; B &quot;&lt;x&gt;&quot;"' in html

    def test_inputmode_am_feld(self) -> None:
        """``inputmode`` (Issue #944) steht am Eingabefeld selbst; ohne Angabe bleibt das Markup Zeichen für Zeichen."""
        for dicht in ("", " dicht"):
            mit = render(f'<c-form.field name="amount" label="Betrag" value="3,50" inputmode="decimal"{dicht} />')
            ohne = render(f'<c-form.field name="amount" label="Betrag" value="3,50"{dicht} />')
            assert 'inputmode="decimal"' in _tag(mit, "input") and "inputmode" not in _tag(mit, "div")
            assert "inputmode" not in ohne
            assert mit.replace(' inputmode="decimal"', "") == ohne


class TestAuswahl:
    def test_ohne_ergaenzung_wie_bisher(self) -> None:
        html = render('<c-form.select name="sort" label="Sortierung"><option>A</option></c-form.select>')
        auswahl = _tag(html, "select")
        assert _klasse(auswahl) == BISHER_AUSWAHL
        assert _attribute(auswahl) == {"name", "id", "class"}
        assert _klasse(_tag(html, "label")) == BISHER_LABEL
        assert _klasse(_tag(html, "div")) == ""

    def test_zeile_setzt_die_beschriftung_neben_das_feld(self) -> None:
        html = render(
            '<c-form.select name="status" id="filter-status" label="Status" dicht zeile @change="x()">'
            '<option value="">Alle</option></c-form.select>'
        )
        rahmen = _tag(html, "div")
        assert "flex items-center" in rahmen and '@change="x()"' in rahmen
        assert "w-40" in _tag(html, "select") and 'id="filter-status"' in _tag(html, "select")
        assert "block" not in _tag(html, "label")

    def test_modell_am_auswahlfeld(self) -> None:
        html = render(
            '<c-form.select name="art" label="Art" modell="documentType" dicht><option>A</option></c-form.select>'
        )
        auswahl = _tag(html, "select")
        assert 'x-model="documentType"' in auswahl and "h-9" in auswahl and "w-full" in auswahl


class TestTextfeld:
    def test_id_modell_und_hoechstlaenge(self) -> None:
        html = render(
            '<c-form.textarea name="message" id="message" label="Anschreiben" rows="5" maxlength="5000" '
            'modell="text" :value="v" dicht />',
            v="Zeile 1 & 2",
        )
        feld = _tag(html, "textarea")
        assert 'id="message"' in feld and 'maxlength="5000"' in feld and 'x-model="text"' in feld
        assert "text-sm" in feld and "px-4 py-3" not in feld
        assert 'for="message"' in html and ">Zeile 1 &amp; 2</textarea>" in html
        # Die ID gehört dem Feld, nicht dem Rahmen
        assert 'id="message"' not in _tag(html, "div")

    def test_ohne_ergaenzung_wie_bisher(self) -> None:
        html = render('<c-form.textarea name="notiz" label="Notiz" />')
        feld = _tag(html, "textarea")
        assert 'name="notiz" id="id_notiz" rows="4"' in feld
        assert _klasse(feld) == BISHER_FELD
        assert _attribute(feld) == {"name", "id", "rows", "class"}


class TestCheckbox:
    def test_wert_modell_und_gesperrt(self) -> None:
        html = render(
            '<c-form.checkbox name="contacts" id="kontakt-1" value="7" modell="auswahl" disabled="1" dicht>'
            "Ratsbüro</c-form.checkbox>"
        )
        feld = _tag(html, "input")
        assert 'value="7"' in feld and 'x-model="auswahl"' in feld and "disabled" in feld
        assert "items-start" in _tag(html, "label") and "Ratsbüro" in html

    def test_leeres_disabled_sperrt_nicht(self) -> None:
        feld = _tag(render('<c-form.checkbox name="confirm" disabled="" dicht />'), "input")
        assert "disabled" not in feld and "value=" not in feld

    def test_ohne_ergaenzung_wie_bisher(self) -> None:
        html = render('<c-form.checkbox name="remember_me" label="Angemeldet bleiben" checked />')
        feld = _tag(html, "input")
        assert _attribute(feld) == {"type", "name", "id", "checked", "class"}
        assert _klasse(feld) == (
            "w-4 h-4 rounded border-gray-300 dark:border-gray-600 text-primary-600 focus:ring-primary-500 dark:bg-gray-700"
        )
        assert _klasse(_tag(html, "label")) == "inline-flex items-center gap-2 cursor-pointer min-h-[24px]"
        assert _klasse(_tag(html, "span")) == "text-sm text-gray-600 dark:text-gray-400"


class TestTabellenkopf:
    def test_dicht_ohne_versalien(self) -> None:
        zelle = _tag(render("<c-ui.th dicht>Status</c-ui.th>"), "th")
        assert "text-[0.8125rem]" in zelle and "uppercase" not in zelle and "px-3 py-2" in zelle

    def test_ohne_ergaenzung_wie_bisher(self) -> None:
        assert _klasse(_tag(render("<c-ui.th>Titel</c-ui.th>"), "th")) == (
            "px-4 py-3 text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 text-left"
        )


class TestReiter:
    def test_zahl_gedimmt_mit_text_fuer_bildschirmleser(self) -> None:
        html = render(
            '<c-rahmen.reiter label="Vorlagen"><c-rahmen.reiter-link href="/z/" aktiv :zahl="n" zahl_text="offen">'
            "Zu prüfen</c-rahmen.reiter-link></c-rahmen.reiter>",
            n=3,
        )
        link = re.search(r"<a\b.*?</a>", html, re.S)
        assert link
        text = re.sub(r"<[^>]+>", "", link.group(0))
        assert " ".join(text.split()) == "Zu prüfen, 3 offen"
        assert "tabular-nums" in link.group(0) and "rounded-full" not in link.group(0)
        assert 'aria-current="page"' in link.group(0)

    def test_null_und_leer_zeigen_keine_zahl(self) -> None:
        for quelle in (':zahl="n"', 'zahl="0"', ""):
            html = render(f'<c-rahmen.reiter-link href="/a/" {quelle}>Alle</c-rahmen.reiter-link>', n=0)
            assert "tabular-nums" not in html

    def test_ohne_zahl_wie_bisher(self) -> None:
        html = render('<c-rahmen.reiter-link href="/a/">Alle</c-rahmen.reiter-link>')
        assert re.search(r">Alle</a>", html)


class TestDateifeld:
    def test_allein_mit_beschriftung_hinweis_und_eigener_anzeige(self) -> None:
        html = render(
            '<c-form.datei name="datei" label="Unterlagen (PDF)" accept=".pdf,application/pdf" '
            'hinweis="Bis 20 MB" required />'
        )
        feld = _tag(html, "input")
        assert 'type="file"' in feld and 'name="datei"' in feld and 'id="id_datei"' in feld
        assert 'accept=".pdf,application/pdf"' in feld and "required" in feld and "multiple" not in feld
        assert 'aria-describedby="id_datei-hinweis"' in feld and 'id="id_datei-hinweis"' in html
        assert 'for="id_datei"' in html and "Datei wählen" in html and "oder hierher ziehen" in html
        # Ohne Komponente der Seite: eigener Zustand für Ziehen und die gewählten Namen
        assert 'x-data="{ ziehen: false, namen: [] }"' in html and "Gewählt:" in html
        assert "x-ref" not in feld

    def test_gesteuert_von_einer_komponente(self) -> None:
        html = render(
            '<c-form.datei name="import_files" id="import_files" accept=".pdf,.docx" multiple ref="fileInput" '
            'ziehen="isDragging" labelledby="dateien-titel" @drop.prevent="handleDrop($event)" '
            '@change="handleFileSelect($event)"><ul id="liste"></ul></c-form.datei>'
        )
        feld = _tag(html, "input")
        assert 'x-ref="fileInput"' in feld and "multiple" in feld and 'aria-labelledby="dateien-titel"' in feld
        assert "Dateien wählen" in html and "x-data" not in html and "Gewählt:" not in html
        assert ':class="(isDragging) ?' in html
        rahmen = _tag(html, "div")
        assert '@drop.prevent="handleDrop($event)"' in rahmen and '@change="handleFileSelect($event)"' in rahmen
        assert '<ul id="liste"></ul>' in html

    def test_fehler_am_feld(self) -> None:
        html = render('<c-form.datei name="datei" label="Datei" hinweis="PDF" fehler="Bitte eine PDF-Datei wählen." />')
        feld = _tag(html, "input")
        assert 'aria-invalid="true"' in feld
        assert 'aria-describedby="id_datei-fehler id_datei-hinweis"' in feld
        assert "Bitte eine PDF-Datei wählen." in html
