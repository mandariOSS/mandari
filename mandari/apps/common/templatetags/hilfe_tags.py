# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template-Tag für Links in die Anwenderdokumentation (Issue #589, siehe ``apps.common.hilfe``)."""

from __future__ import annotations

from django import template

from apps.common.hilfe import docs_url

register = template.Library()


@register.simple_tag
def hilfe_url(schluessel: str) -> str:
    """``{% hilfe_url "konto" %}`` → Adresse der Seite „Konto und Sicherheit“ in der Dokumentation."""
    return docs_url(schluessel)
