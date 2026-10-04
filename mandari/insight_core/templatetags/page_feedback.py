# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template-Tag für die Rückmeldung am Seitenende (``partials/page_feedback.html``)."""

from __future__ import annotations

from typing import Any

from django import template

from ..services.page_feedback import PageContext, page_context

register = template.Library()


@register.simple_tag(takes_context=True)
def page_feedback_context(context: Any) -> PageContext | None:
    """Seitentyp, Pfad und Kommune der aktuellen Seite; ``None`` auf Seiten ohne Rückmeldung."""
    request = context.get("request")
    if request is None:
        return None
    return page_context(request, obj=context.get("object"), active_body=context.get("active_body"))
