# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anzeige von Mitgliedschaften in Templates (Issue #420).

Inhalte der Organisation bleiben erhalten, wenn ein Mitglied entfernt wird; der Verweis auf die
Person ist dann leer. Diese Filter zeigen dafür „Ehemaliges Mitglied“ statt einer leeren Stelle.

Usage:
    {% load member_tags %}
    {{ motion.author|member_name }}
    {{ comment.author|member_initials }}
"""

from typing import Any

from django import template

from apps.common.formatting import member_initials as _member_initials
from apps.common.formatting import member_name as _member_name

register = template.Library()


@register.filter
def member_name(membership: Any) -> str:
    """Anzeigename der Mitgliedschaft oder „Ehemaliges Mitglied“."""
    return _member_name(membership)


@register.filter
def member_initials(membership: Any) -> str:
    """Initialen der Mitgliedschaft oder „EM“."""
    return _member_initials(membership)
