# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine aus Teilpaket 4 von Issue #951: c-form.suche, c-form.radio und c-rahmen.liste (neu), ``dicht``/``id`` an
c-form.password. Zweite Wege, die etwas tun statt zu einer Seite zu führen, sind c-ui.text-button mit ``dicht``; Gruppen
von Feldern sind ein fieldset mit c-form.beschriftung als="legend" (beides gemeinsam mit den anderen Spuren). Ohne die
neuen Angaben bleiben Passwortfeld und Textlink Zeichen für Zeichen wie bisher (Anmeldung, Registrierung, alle
Textlinks).
"""

from __future__ import annotations

import re

from django.template import engines
from django_cotton.compiler_regex import CottonCompiler

#: Klassen des bisherigen Passwortfelds (vor #951) – ohne neue Angaben müssen sie unverändert bleiben
BISHER_PASSWORT = (
    "w-full min-w-0 px-4 py-3 rounded-l-lg border border-r-0 bg-white dark:bg-gray-700 text-gray-900 dark:text-white "
    "placeholder-gray-400 transition-colors focus:ring-2 focus:ring-primary-500 focus:border-primary-500 focus:z-10 "
    "border-gray-300 dark:border-gray-600"
)
BISHER_KNOPF = (
    "flex-none w-12 flex items-center justify-center rounded-r-lg border bg-gray-50 dark:bg-gray-700 text-gray-500 "
    "hover:text-gray-700 dark:text-gray-300 dark:hover:text-white focus:outline-none focus-visible:ring-2 "
    "focus-visible:ring-primary-500 focus:z-10 border-gray-300 dark:border-gray-600"
)


def render(source: str, **context: object) -> str:
    return engines["django"].from_string(CottonCompiler().process(source)).render(context)


def _klasse(html: str, tag: str) -> str:
    treffer = re.search(rf'<{tag}\b[^>]*class="([^"]*)"', html, re.S)
    assert treffer, html
    return " ".join(treffer.group(1).split())


def test_passwort_ohne_neue_angaben_wie_bisher() -> None:
    html = render('<c-form.password name="password" label="Passwort" required />')
    assert _klasse(html, "input") == BISHER_PASSWORT
    assert _klasse(html, "button") == BISHER_KNOPF
    assert 'id="id_password"' in html and 'for="id_password"' in html


def test_passwort_dicht_mit_eigener_kennung() -> None:
    html = render('<c-form.password name="password" id="dialog-passwort" label="Passwort" help="Hinweis" dicht />')
    klasse = _klasse(html, "input")
    assert "h-9" in klasse and "text-sm" in klasse and "py-3" not in klasse
    assert "w-9 h-9" in _klasse(html, "button")
    assert 'id="dialog-passwort"' in html and 'for="dialog-passwort"' in html
    assert 'aria-describedby="dialog-passwort-help"' in html and "text-[0.8125rem]" in html


def test_suchfeld() -> None:
    html = render('<c-form.suche id="team-suche" wert="A&amp;B" label="Name suchen" platzhalter="Name" class="w-80" />')
    assert 'type="search"' in html and 'name="q"' in html and 'id="team-suche"' in html
    assert '<label for="team-suche" class="sr-only">Name suchen</label>' in html
    assert 'placeholder="Name"' in html and "h-9" in _klasse(html, "input")
    assert 'class="relative w-80"' in html
    ohne = render('<c-form.suche name="such" />')
    assert 'id="suche-such"' in ohne and 'name="such"' in ohne and "placeholder=" not in ohne


def test_textlink_wie_bisher() -> None:
    html = render('<c-ui.textlink href="/ziel/" class="text-sm" data-x="1">Weiter</c-ui.textlink>')
    assert html.strip() == (
        '<a href="/ziel/" class="font-medium text-primary-700 dark:text-primary-300 hover:underline underline-offset-4 '
        'text-sm" data-x="1">Weiter</a>'
    )


def test_textknopf_dicht() -> None:
    knopf = render('<c-ui.text-button dicht @click="offen = true">Aus Protokollen</c-ui.text-button>')
    assert knopf.strip().startswith('<button type="button"') and '@click="offen = true"' in knopf
    assert {"text-sm", "min-h-8", "text-primary-700", "hover:underline"} <= set(_klasse(knopf, "button").split())
    absenden = render('<c-ui.text-button dicht type="submit">Wieder öffnen</c-ui.text-button>')
    assert absenden.strip().startswith('<button type="submit"')


def test_radio_mit_hinweis_und_modell() -> None:
    html = render(
        '<c-form.radio name="visibility" value="shared" label="Geteilt" hinweis="Bestimmte Personen" modell="sicht" />'
    )
    assert 'type="radio" name="visibility" id="id_visibility_shared" value="shared" x-model="sicht"' in html
    assert '<label for="id_visibility_shared"' in html and "checked" not in html
    assert "Geteilt</span>" in html and "Bestimmte Personen</span>" in html
    ohne = render('<c-form.radio name="k" value="email" id="k-mail" checked="1" disabled="1">E-Mail</c-form.radio>')
    assert 'id="k-mail"' in ohne and " checked" in ohne and " disabled" in ohne and "x-model" not in ohne
    assert "E-Mail</span></span>" in ohne


def test_legende_einer_gruppe() -> None:
    legende = " ".join(
        render('<fieldset><c-form.beschriftung als="legend">Tags</c-form.beschriftung></fieldset>').split()
    )
    assert legende.startswith("<fieldset> <legend") and ">Tags</legend> </fieldset>" in legende
    assert {"text-sm", "font-medium"} <= set(_klasse(legende, "legend").split())


def test_liste() -> None:
    liste = render('<c-rahmen.liste class="mt-3" data-testid="x"><li>A</li></c-rahmen.liste>')
    assert liste.strip().startswith("<ul class=") and 'data-testid="x"' in liste and liste.strip().endswith("</ul>")
    geordnet = render("<c-rahmen.liste geordnet><li>A</li></c-rahmen.liste>")
    assert geordnet.strip().startswith("<ol") and geordnet.strip().endswith("</ol>")
