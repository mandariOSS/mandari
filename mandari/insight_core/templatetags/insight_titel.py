# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seitentitel des Bürgerportals: höchstens ``TITEL_MAX`` Zeichen (Issue #939, Bing: „Title too long“).

Detailseiten und Stadtseiten bauen ihren Titel schon passend (``seo.seitentitel``). Der Filter sichert den Rest ab –
Listen im Bürgerportal einer Kommune mit langem Namen, Suchanfragen, Rückfälle ohne SEO-Kontext.
"""

from __future__ import annotations

import html

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString

from ..seo import TITEL_MAX, kuerzen

register = template.Library()

#: Trenner vor dem Zusatz des Rahmens („Sitzungen | mandari Insight“)
ZUSATZ_TRENNER = " | "


@register.filter(is_safe=True)
def seitentitel(gerendert: str) -> SafeString:
    """Gerenderten Inhalt von ``<title>`` kürzen: erst fällt der Zusatz nach dem letzten „ | “ weg, dann wird gekürzt.

    Gemessen wird der entschlüsselte Text („&amp;“ zählt als ein Zeichen); die Ausgabe ist wieder maskiert.
    """
    text = " ".join(html.unescape(str(gerendert)).split())
    if len(text) > TITEL_MAX and ZUSATZ_TRENNER in text:
        text = text.rsplit(ZUSATZ_TRENNER, 1)[0]
    return escape(kuerzen(text, TITEL_MAX))
