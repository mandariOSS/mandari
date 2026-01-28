# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Email utilities for Mandari.

Uses SiteSettings for SMTP configuration with fallback to Django settings.
"""

import logging
from typing import List, Optional

from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.conf import settings

logger = logging.getLogger(__name__)


def get_email_connection():
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

    email = config["DEFAULT_FROM_EMAIL"]
    name = site_settings.default_from_name

    if name:
        return f"{name} <{email}>"
    return email


def send_email(
    subject: str,
    body: str,
    to: List[str],
    from_email: Optional[str] = None,
    html_body: Optional[str] = None,
    reply_to: Optional[List[str]] = None,
    attachments: Optional[List[tuple]] = None,
    fail_silently: bool = False,
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

    Returns:
        True if email was sent successfully, False otherwise
    """
    try:
        connection = get_email_connection()
        sender = from_email or get_from_email()

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

        email.send(fail_silently=fail_silently)
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
    context: dict,
    to: List[str],
    from_email: Optional[str] = None,
    reply_to: Optional[List[str]] = None,
    fail_silently: bool = False,
) -> bool:
    """
    Send an email using a Django template.

    The template should have both .txt and .html versions:
    - emails/example.txt for plain text
    - emails/example.html for HTML

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
    # Render templates
    try:
        text_body = render_to_string(f"{template_name}.txt", context)
    except Exception:
        text_body = None

    try:
        html_body = render_to_string(f"{template_name}.html", context)
    except Exception:
        html_body = None

    if not text_body and not html_body:
        logger.error(f"No email templates found for {template_name}")
        if not fail_silently:
            raise ValueError(f"No email templates found for {template_name}")
        return False

    # Use text body or extract from HTML
    if not text_body and html_body:
        # Simple HTML to text conversion
        import re
        text_body = re.sub(r'<[^>]+>', '', html_body)
        text_body = re.sub(r'\s+', ' ', text_body).strip()

    return send_email(
        subject=subject,
        body=text_body,
        to=to,
        from_email=from_email,
        html_body=html_body,
        reply_to=reply_to,
        fail_silently=fail_silently,
    )
