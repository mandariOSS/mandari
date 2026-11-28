# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Filter für die Module der Installation (Issue #1594, ``apps.common.module``).

Verweise in ein anderes Modul stehen nur, wenn es eingeschaltet ist; sonst gäbe es seine Adresse nicht::

    {% load modul_tags %}
    {% if "portal"|modul_aktiv %}<a href="{% url 'insight_core:insight:portal_home' %}">…</a>{% endif %}

Ein Filter statt eines Kontextprozessors: Die Fehlerseiten werden ohne Kontextprozessoren gerendert.
"""

from __future__ import annotations

from django import template

from apps.common import module

register = template.Library()


@register.filter
def modul_aktiv(kennung: str) -> bool:
    """Ist das Modul ``kennung`` (session, work, portal) eingeschaltet?"""
    return module.aktiv(kennung)
