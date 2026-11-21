# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Deklaratives WebMCP an den lesenden Formularen des Bürgerportals (Issue #1309).

KI-Agenten im Browser (Chrome, Origin-Trial) lesen Formulare mit ``toolname`` und ``tooldescription`` als Werkzeug;
die Felder werden zu Parametern (``name``, Beschreibung aus ``toolparamdescription``). ``toolautosubmit`` erlaubt dem
Agenten das Absenden, weil die Formulare nur lesen (GET-Suche). Konto-, Kontakt- und Abo-Formulare bekommen kein
Werkzeug. Je Seite steht jedes Werkzeug höchstens einmal: Die Kopfsuche fehlt auf Übersicht und Suchseite, die ihr
eigenes Suchformular haben; der Kommunenwechsel im Dialog bleibt ohne Werkzeug.

Browser ohne WebMCP übergehen die Attribute. Chrome meldet die Werkzeuge während des Origin-Trials nur mit Token an
(``WEBMCP_ORIGIN_TRIAL_TOKEN``).
"""

from __future__ import annotations

from django import template
from django.utils.html import format_html
from django.utils.safestring import SafeString

register = template.Library()

#: Werkzeuge: Beschreibung (was es tut und wann ein Agent es nimmt, ohne Felder aufzuzählen) und ihre Felder
WERKZEUGE: dict[str, dict[str, object]] = {
    "ratsinfo_suchen": {
        "beschreibung": (
            "Durchsucht die öffentlichen Ratsinformationen dieser Kommune: Vorgänge und Vorlagen, Sitzungen, "
            "Personen und Gremien. Für Fragen, was der Rat zu einem Thema, einer Straße oder einem Ort berät "
            "oder beschlossen hat."
        ),
        "felder": {"q": "Suchbegriff, etwa ein Thema, eine Straße oder ein Aktenzeichen"},
    },
    "kommune_finden": {
        "beschreibung": (
            "Findet eine Kommune im Bürgerportal und öffnet ihre Ratsinformationen. Zuerst nutzen, wenn eine "
            "Frage eine bestimmte Stadt, Gemeinde oder einen Kreis betrifft."
        ),
        "felder": {"q": "Name der Kommune oder Postleitzahl"},
    },
}


@register.simple_tag
def webmcp_werkzeug(name: str) -> SafeString:
    """Attribute am ``<form>``: ``toolname``, ``tooldescription``, ``toolautosubmit``."""
    werkzeug = WERKZEUGE[name]
    return format_html('toolname="{}" tooldescription="{}" toolautosubmit', name, werkzeug["beschreibung"])


@register.simple_tag
def webmcp_feld(name: str, feld: str) -> SafeString:
    """Attribut an einem Feld eines Werkzeugs: ``toolparamdescription``."""
    felder = WERKZEUGE[name]["felder"]
    assert isinstance(felder, dict)
    return format_html('toolparamdescription="{}"', felder[feld])
