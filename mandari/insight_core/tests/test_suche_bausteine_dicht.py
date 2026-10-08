# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bausteine der Suche für Bürgerportal und Work (Issue #853): ein Suchformular (``c-suche.feld``) und Filter, Reiter und
Auswahllisten mit dem Parameter ``dicht``.

Ohne ``dicht`` bleiben Markup und Maße des Bürgerportals (Bedienelemente 44 bis 48 px). Mit ``dicht`` gelten die Maße
einer Arbeitsplattform aus den Gestaltungsregeln: Bedienelemente 32 px, Schrift 14 px; nur das Suchfeld als die eine
Hauptaktion 40 px.
"""

from __future__ import annotations

import re

from django.template import engines
from django_cotton.compiler_regex import CottonCompiler

OPTIONEN = [{"value": "", "label": "Alle", "checked": True}, {"value": "12m", "label": "Letzte 12 Monate"}]
REITER = [{"key": "", "label": "Alle", "count": "3", "url": "?q=x", "active": True}]


def render(source: str, **context: object) -> str:
    """Template-String mit Cotton-Kompilierung rendern (wie der Loader)."""
    return engines["django"].from_string(CottonCompiler().process(source)).render(context)


def _klassen(html: str, muster: str) -> str:
    treffer = re.search(muster + r'[^>]*?class="([^"]*)"', html, re.S)
    assert treffer, muster
    return treffer.group(1)


def test_buergerportal_behaelt_seine_masse() -> None:
    facette = render('<c-suche.facette titel="Zeitraum" name="period" typ="radio" :optionen="o" />', o=OPTIONEN)
    assert "min-h-11 px-3.5 rounded-lg" in _klassen(facette, "<summary")
    assert 'name="period" value="12m" form="suche-form"' in facette
    assert "/insight/suche/" in facette

    auswahl = render(
        '<c-suche.auswahl id="a" name="gremium" titel="Gremium" leer="Alle" :optionen="o" adresse="/s/" />', o=OPTIONEN
    )
    assert _klassen(auswahl, "<select").startswith("min-h-11 min-w-0 max-w-[16rem]")

    reiter = render('<c-suche.reiter :tabs="t" />', t=REITER)
    assert "min-h-11 pt-2" in _klassen(reiter, "<a ") and "gap-x-6" in _klassen(reiter, "<ul")

    feld = render(
        '<c-suche.feld adresse="/insight/suche/" :wert="q" platzhalter="Straße, Thema oder Aktenzeichen" />', q="Radweg"
    )
    eingabe = _klassen(feld, '<input id="suche-eingabe"')
    assert eingabe.startswith("w-full min-h-12 pl-11 pr-10 rounded-xl border") and "text-[1.0625rem]" in eingabe
    assert "min-h-12 px-5 rounded-xl" in _klassen(feld, '<button type="submit"')
    assert 'class="flex gap-3 max-w-3xl"' in feld and "autofocus" not in feld
    assert 'hx-get="/insight/suche/" hx-target="#suchergebnis"' in feld


def test_dicht_in_den_massen_einer_arbeitsplattform() -> None:
    leiste = render(
        '<c-suche.filterleiste adresse="/work/x/ris/search/" :zeitraum="o" :art="o" :sortierung="o" :gremien="o" '
        ':frei="frei" dicht />',
        o=OPTIONEN,
        frei={"von": "2025-01-01", "bis": ""},
    )
    assert "min-h-11" not in leiste and "min-h-10" not in leiste
    for muster in ("<summary", '<select id="suche-gremium"', '<select id="suche-sortierung"', '<input id="suche-von"'):
        klassen = _klassen(leiste, muster)
        assert "min-h-8" in klassen and "text-sm" in klassen, muster
    assert "min-h-7 text-sm" in _klassen(leiste, '<a id="filter-period-')

    reiter = render('<c-suche.reiter :tabs="t" dicht />', t=REITER)
    assert "min-h-8" in _klassen(reiter, "<a ") and "text-sm" in _klassen(reiter, "<ul") and "min-h-11" not in reiter

    feld = render('<c-suche.feld adresse="/work/x/ris/search/" :wert="q" platzhalter="Thema" dicht fokus />', q="")
    assert "min-h-10 pl-9 pr-9 rounded-md" in _klassen(feld, '<input id="suche-eingabe"') and "autofocus" in feld
    assert "min-h-10 px-4 rounded-md text-sm" in _klassen(feld, '<button type="submit"')


def test_suchbegriff_nur_einmal_maskiert() -> None:
    feld = render('<c-suche.feld adresse="/s/" :wert="q" platzhalter="Suche" />', q='Rad & "Weg" <b>')
    assert 'value="Rad &amp; &quot;Weg&quot; &lt;b&gt;"' in feld
