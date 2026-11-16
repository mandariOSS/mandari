# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anzeige von Mitgliedschaften in Templates (Issue #420).

Inhalte der Organisation bleiben erhalten, wenn ein Mitglied entfernt wird; der Verweis auf die
Person ist dann leer. Diese Filter zeigen dafür „Ehemaliges Mitglied“ statt einer leeren Stelle.

Usage:
    {% load member_tags %}
    {{ motion.author|member_name }}
    {{ comment.author|member_initials }}
    {{ member.user|team_name }}
    {{ member.user|team_initial }}
"""

from typing import Any

from django import template

from apps.common.formatting import member_initials as _member_initials
from apps.common.formatting import member_name as _member_name
from apps.common.formatting import team_display_name

register = template.Library()


@register.filter
def member_name(membership: Any) -> str:
    """Anzeigename der Mitgliedschaft oder „Ehemaliges Mitglied“."""
    return _member_name(membership)


@register.filter
def member_initials(membership: Any) -> str:
    """Initialen der Mitgliedschaft oder „EM“."""
    return _member_initials(membership)


@register.filter
def team_name(user: Any) -> str:
    """Name im Teamverzeichnis: ohne Namen die Adresse nur, wenn die Person sie zeigt (``team_display_name``)."""
    return team_display_name(user)


@register.filter
def team_initial(user: Any) -> str:
    """Anfangsbuchstabe für den Platzhalter des Profilbilds, aus demselben Namen wie ``team_name``."""
    return team_display_name(user)[:1].upper()
