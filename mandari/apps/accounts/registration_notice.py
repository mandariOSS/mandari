# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Selbstregistrierung mit einer Adresse, die schon ein Konto hat.

Die Registrierungsseite antwortet für jede Adresse gleich. Dass es das Konto schon gibt, erfährt nur, wer das
Postfach liest: per Mail an die Adresse, mit dem Weg zur Anmeldung und zum Zurücksetzen des Passworts.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.urls import reverse

logger = logging.getLogger(__name__)

TEMPLATE = "accounts/emails/registration_existing_account.html"
#: Mailart neben der Bestätigungsmail der Registrierung (``work.zugang.registration_confirm``): Beide folgen so
#: denselben Mustern in ``MAIL_QUEUE`` und gehen auf demselben Weg raus
KIND = "work.zugang.registration_existing"


def send_existing_account_notice(organization: Any, email: str) -> bool:
    """Hinweis an ``email`` senden (``True`` bei Versand); nie über das SMTP der Organisation."""
    from apps.common import mail
    from apps.common.email import render_email

    site_url = str(getattr(settings, "SITE_URL", "")).rstrip("/")
    beitreten = reverse("accounts:self_register", kwargs={"org_slug": organization.slug})
    context = {
        "organization": organization,
        "login_url": f"{site_url}{reverse('accounts:login')}?next={quote(beitreten)}",
        "reset_url": f"{site_url}{reverse('accounts:password_reset')}",
    }
    try:
        html_body, text_body = render_email(TEMPLATE, context)
        return mail.send(
            kind=KIND,
            subject=f"Ihre Registrierung bei {organization.name}",
            body=text_body,
            html_body=html_body,
            to=[email],
            organization=organization,
            # Die Mail sagt, dass es zu der Adresse ein Konto gibt: nie über fremde Mailserver
            via_organization=False,
            fail_silently=True,
        )
    except Exception:
        logger.exception("Hinweis zur Registrierung bei %s konnte nicht versendet werden", organization.slug)
        return False
