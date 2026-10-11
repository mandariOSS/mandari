# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine aus Teilpaket 4 von Issue #951: c-form.suche und c-rahmen.liste (neu), ``knopf`` an c-ui.textlink. Ohne
``knopf`` bleibt der Textlink Zeichen für Zeichen wie bisher.
"""

from __future__ import annotations

import re

from django.template import engines
from django_cotton.compiler_regex import CottonCompiler


def render(source: str, **context: object) -> str:
    return engines["django"].from_string(CottonCompiler().process(source)).render(context)


def _klasse(html: str, tag: str) -> str:
    treffer = re.search(rf'<{tag}\b[^>]*class="([^"]*)"', html, re.S)
    assert treffer, html
    return " ".join(treffer.group(1).split())


def test_suchfeld() -> None:
    html = render('<c-form.suche id="team-suche" wert="A&amp;B" label="Name suchen" platzhalter="Name" class="w-80" />')
    assert 'type="search"' in html and 'name="q"' in html and 'id="team-suche"' in html
    assert '<label for="team-suche" class="sr-only">Name suchen</label>' in html
    assert 'placeholder="Name"' in html and "h-9" in _klasse(html, "input")
    assert 'class="relative w-80"' in html
    ohne = render('<c-form.suche name="such" />')
    assert 'id="suche-such"' in ohne and 'name="such"' in ohne and "placeholder=" not in ohne


def test_textlink_ohne_knopf_wie_bisher() -> None:
    html = render('<c-ui.textlink href="/ziel/" class="text-sm" data-x="1">Weiter</c-ui.textlink>')
    assert html.strip() == (
        '<a href="/ziel/" class="font-medium text-primary-700 dark:text-primary-300 hover:underline underline-offset-4 '
        'text-sm" data-x="1">Weiter</a>'
    )


def test_textlink_als_knopf() -> None:
    knopf = render('<c-ui.textlink knopf @click="offen = true">Aus Protokollen</c-ui.textlink>')
    assert knopf.strip().startswith('<button type="button" class="font-medium text-primary-700')
    assert '@click="offen = true"' in knopf and "href" not in knopf
    absenden = render('<c-ui.textlink knopf="submit">Wieder öffnen</c-ui.textlink>')
    assert absenden.strip().startswith('<button type="submit"')


def test_liste() -> None:
    liste = render('<c-rahmen.liste class="mt-3" data-testid="x"><li>A</li></c-rahmen.liste>')
    assert liste.strip().startswith("<ul class=") and 'data-testid="x"' in liste and liste.strip().endswith("</ul>")
    geordnet = render("<c-rahmen.liste geordnet><li>A</li></c-rahmen.liste>")
    assert geordnet.strip().startswith("<ol") and geordnet.strip().endswith("</ol>")
