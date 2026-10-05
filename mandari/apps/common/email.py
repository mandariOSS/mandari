# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E-Mail-Werkzeuge für Mandari.

- ``render_email`` rendert ein HTML-Mail-Template (Basis-Layout ``emails/base_email.html``),
  wandelt die CSS-Klassen per ``css_inline`` in ``style=``-Attribute um und liefert die
  Text-Alternative gleich mit: aus dem ``.txt``-Geschwistertemplate, sonst per ``html2text``
  aus dem HTML (Issue #175).

Versendet wird über den Mail-Dienst ``apps.common.mail`` (``mail.send``, ``mail.send_template``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import css_inline
import html2text
from django.http import HttpRequest
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# Bereiche, die nur im HTML sinnvoll sind (Preheader, Wortmarke), werden im Basis-Layout mit
# diesen Markern umschlossen und fallen aus der Textfassung heraus.
_TEXT_SKIP_RE = re.compile(r"<!--\s*text:skip\s*-->.*?<!--\s*/text:skip\s*-->", re.DOTALL)
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# html2text maskiert Markdown-Sonderzeichen (z. B. "\-"); in einer reinen Textmail stören die Backslashes.
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!])")


def get_from_email() -> str:
    """Absender der Plattform als ``Name <adresse>`` (``apps.common.mail.config.platform_from_email``)."""
    from apps.common.mail.config import platform_from_email

    return platform_from_email()


# =============================================================================
# Rendering: Basis-Layout, Inliner, Text-Alternative
# =============================================================================


def inline_css(html: str) -> str:
    """CSS aus dem ``<style>``-Block des Basis-Layouts in ``style=``-Attribute schreiben.

    Der ``<style>``-Block bleibt zusätzlich erhalten, damit Clients mit CSS-Unterstützung
    Media-Queries (schmale Viewports) weiterhin anwenden können. Externe Stylesheets werden
    nie nachgeladen.
    """
    return css_inline.inline(html, keep_style_tags=True, load_remote_stylesheets=False)


def html_to_text(html: str) -> str:
    """Text-Alternative aus HTML erzeugen (Links bleiben als ``[Text](URL)`` erhalten)."""
    converter = html2text.HTML2Text()
    converter.body_width = 0  # kein Zeilenumbruch, Links bleiben intakt
    converter.ignore_images = True
    converter.ignore_emphasis = True
    converter.ignore_tables = True  # Layout-Tabellen: nur der Inhalt
    converter.unicode_snob = True
    converter.protect_links = False
    converter.wrap_links = False
    text = _MD_ESCAPE_RE.sub(r"\1", converter.handle(_TEXT_SKIP_RE.sub("", html)))
    lines = [line.rstrip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip() + "\n"


def _text_template_for(template_name: str) -> str:
    return template_name[: -len(".html")] + ".txt" if template_name.endswith(".html") else template_name + ".txt"


def render_email(
    template_name: str,
    context: dict[str, Any] | None = None,
    request: HttpRequest | None = None,
    *,
    text_template_name: str | None = None,
) -> tuple[str, str]:
    """Rendert eine E-Mail und liefert ``(html, text)``.

    ``template_name`` ist das HTML-Template (z. B. ``emails/contact/confirmation.html``); es
    erweitert ``emails/base_email.html``. Das HTML wird durch den Inliner geschickt. Die
    Textfassung kommt aus ``text_template_name`` bzw. dem gleichnamigen ``.txt``-Template,
    sofern vorhanden, sonst per html2text aus dem gerenderten HTML.
    """
    context = dict(context or {})
    html = render_to_string(template_name, context, request=request)
    text_name = text_template_name or _text_template_for(template_name)
    text: str
    try:
        text = str(render_to_string(text_name, context, request=request))
    except TemplateDoesNotExist:
        text = html_to_text(html)
    return inline_css(html), text
