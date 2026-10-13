# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Template-Tag der Einstellungen im neuen Erscheinungsbild (Issue #951): Reiter und Unterpunkte der geöffneten Seite.

    {% load work_einstellungen %}{% einstellungen_navigation "email" as navi %}

Mit ``"true"`` als zweiter Angabe für Detail- und Formularseiten unter einem Unterpunkt (Mitglied, Rolle …).
Daten und Sichtbarkeit: ``apps.work.organization.einstellungen``.
"""

from __future__ import annotations

from typing import Any

from django import template

from apps.work.organization.einstellungen import navigation

register = template.Library()


@register.simple_tag(takes_context=True)
def einstellungen_navigation(context: Any, seite: str, aktuell: str = "page") -> dict[str, Any]:
    return navigation(context["organization"], context.get("membership"), seite, aktuell)
