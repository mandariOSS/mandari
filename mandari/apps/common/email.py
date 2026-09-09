# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E-Mail-Werkzeuge für Mandari.

- ``render_email`` rendert ein HTML-Mail-Template (Basis-Layout ``emails/base_email.html``),
  wandelt die CSS-Klassen per ``css_inline`` in ``style=``-Attribute um und liefert die
  Text-Alternative gleich mit: aus dem ``.txt``-Geschwistertemplate, sonst per ``html2text``
  aus dem HTML (Issue #175).
- ``send_email`` / ``send_template_email`` versenden über die SMTP-Konfiguration aus den
  SiteSettings (Fallback: Django-Settings).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import css_inline
import html2text
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
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


def get_email_connection() -> BaseEmailBackend:
    """
    Get an email connection using SiteSettings or Django settings.

    Returns a configured email connection ready for sending.
    """
    from .models import SiteSettings

    config = SiteSettings.get_email_config()

    return get_connection(
        backend=config["EMAIL_BACKEND"],
        host=config["EMAIL_HOST"],
        port=config["EMAIL_PORT"],
        username=config["EMAIL_HOST_USER"],
        password=config["EMAIL_HOST_PASSWORD"],
        use_tls=config["EMAIL_USE_TLS"],
        use_ssl=config["EMAIL_USE_SSL"],
        timeout=config["EMAIL_TIMEOUT"],
    )


def get_from_email() -> str:
    """
    Get the default from email address with name.

    Returns formatted "Name <email>" string.
    """
    from .models import SiteSettings

    site_settings = SiteSettings.get_settings()
    config = SiteSettings.get_email_config()

    email: str = config["DEFAULT_FROM_EMAIL"]
    name: str = site_settings.default_from_name

    if name:
        return f"{name} <{email}>"
    return email


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


# =============================================================================
# Versand
# =============================================================================


def send_email(
    subject: str,
    body: str,
    to: list[str],
    from_email: str | None = None,
    html_body: str | None = None,
    reply_to: list[str] | None = None,
    attachments: list[tuple[str, Any, str]] | None = None,
    fail_silently: bool = False,
    connection: BaseEmailBackend | None = None,
) -> bool:
    """
    Send an email using configured SMTP settings.

    Args:
        subject: Email subject
        body: Plain text body
        to: List of recipient email addresses
        from_email: Override from address (optional)
        html_body: HTML body (optional, for multipart emails)
        reply_to: Reply-to addresses (optional)
        attachments: List of (filename, content, mimetype) tuples
        fail_silently: Don't raise exceptions on errors
        connection: Override email connection (optional) — genutzt vom
            organisationseigenen SMTP-Versand (Issue #65)

    Returns:
        True if email was sent successfully, False otherwise
    """
    try:
        connection = connection or get_email_connection()
        sender = from_email or get_from_email()

        email: EmailMessage
        if html_body:
            # Multipart email (plain + HTML)
            email = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=sender,
                to=to,
                reply_to=reply_to,
                connection=connection,
            )
            email.attach_alternative(html_body, "text/html")
        else:
            # Plain text email
            email = EmailMessage(
                subject=subject,
                body=body,
                from_email=sender,
                to=to,
                reply_to=reply_to,
                connection=connection,
            )

        # Add attachments
        if attachments:
            for filename, content, mimetype in attachments:
                email.attach(filename, content, mimetype)

        # Kein fail_silently an send() – ab Django 6.1 ist die Kombination mit
        # einer expliziten connection ein TypeError (der hier still geschluckt
        # würde: "keine Mail, kein Fehler"). Die fail_silently-Semantik
        # übernimmt der umschließende try/except.
        email.send()
        logger.info(f"Email sent successfully to {', '.join(to)}: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {', '.join(to)}: {e}")
        if not fail_silently:
            raise
        return False


def send_template_email(
    subject: str,
    template_name: str,
    context: dict[str, Any],
    to: list[str],
    from_email: str | None = None,
    reply_to: list[str] | None = None,
    fail_silently: bool = False,
) -> bool:
    """
    Send an email using a Django template.

    ``template_name`` ist der Basisname ohne Endung (z. B. ``emails/contact/confirmation``).
    Gibt es ``<name>.html``, wird es über :func:`render_email` gerendert (Inliner, Text-
    Alternative aus ``<name>.txt`` oder html2text); gibt es nur ``<name>.txt``, geht die Mail
    als reiner Text.

    Args:
        subject: Email subject
        template_name: Base template name (without extension)
        context: Template context dictionary
        to: List of recipient email addresses
        from_email: Override from address (optional)
        reply_to: Reply-to addresses (optional)
        fail_silently: Don't raise exceptions on errors

    Returns:
        True if email was sent successfully, False otherwise
    """
    html_body: str | None
    text_body: str | None
    try:
        html_body, text_body = render_email(f"{template_name}.html", context)
    except TemplateDoesNotExist:
        html_body = None
        try:
            text_body = render_to_string(f"{template_name}.txt", context)
        except TemplateDoesNotExist:
            text_body = None
    except Exception as exc:
        logger.error(f"Failed to render email template {template_name}: {exc}")
        if not fail_silently:
            raise
        return False

    if not text_body:
        logger.error(f"No email templates found for {template_name}")
        if not fail_silently:
            raise ValueError(f"No email templates found for {template_name}")
        return False

    return send_email(
        subject=subject,
        body=text_body,
        to=to,
        from_email=from_email,
        html_body=html_body,
        reply_to=reply_to,
        fail_silently=fail_silently,
    )
