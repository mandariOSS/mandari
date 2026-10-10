# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Tags des neuen Rahmens von Work (Issue #852).

``{% merken "name" %}…{% endmerken %}`` gibt seinen Inhalt aus und legt ihn zusätzlich als Variable ``name`` ab
(Leerraum zusammengefasst). ``work/base_work_neu.html`` merkt sich so den Seitentitel aus dem Block ``page_title``
und zeigt ihn als letzte Brotkrume, wenn der Rahmen die Seite nicht selbst kennt (Detail- und Formularseiten). Die
rund 70 Seiten brauchen dafür keine eigene Angabe; wer eine andere Krume will, überschreibt ``brotkrumen_aktuell``.
"""

from __future__ import annotations

from typing import Any

from django import template
from django.template.base import NodeList, Parser, Token
from django.utils.safestring import SafeString, mark_safe

register = template.Library()


class MerkenNode(template.Node):
    """Gibt den Inhalt aus und legt ihn als Variable im Kontext der Vorlage ab."""

    child_nodelists = ("nodelist",)

    def __init__(self, nodelist: NodeList, name: str) -> None:
        self.nodelist = nodelist
        self.name = name

    def render(self, context: Any) -> SafeString:
        inhalt = self.nodelist.render(context)
        # Der Inhalt ist bereits gerendert und maskiert; nur Leerraum und Zeilenumbrüche werden zusammengefasst
        context[self.name] = mark_safe(" ".join(inhalt.split()))
        return inhalt


@register.tag("merken")
def merken(parser: Parser, token: Token) -> MerkenNode:
    """``{% merken "seitentitel" %}{% block page_title %}…{% endblock %}{% endmerken %}``"""
    teile = token.split_contents()
    if len(teile) != 2 or teile[1][:1] not in {'"', "'"} or teile[1][-1:] != teile[1][:1]:
        raise template.TemplateSyntaxError('Aufruf: {% merken "name" %}…{% endmerken %}')
    nodelist = parser.parse(("endmerken",))
    parser.delete_first_token()
    return MerkenNode(nodelist, teile[1][1:-1])
