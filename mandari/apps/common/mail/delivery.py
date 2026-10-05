# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Versand einer Mail über einen Weg (Issue #528) – sofort, im Aufruf.

``deliver`` ist die einzige Stelle, an der der Mail-Dienst eine Verbindung aufbaut und sendet. Der
Postausgang ruft sie im Auftrag auf, ``send(..., sofort=True)`` und der Rückfall ohne Warteschlange in
der Anfrage. Gezählt wird in ``mandari_mail_total`` (Art, Weg, Ergebnis) und wie bisher in
``mandari_emails_total``. Protokolle nennen Art, Weg und Fehlerklasse, nie Empfänger oder Inhalt.
"""

from __future__ import annotations

import logging

from apps.common.mail_backends import send_with
from apps.common.metrics import EMAILS, MAILS
from apps.common.org_email import OrgMailError

from . import config
from .message import Mail

logger = logging.getLogger("apps.common.mail")


def _sender(mail: Mail, route: config.Route) -> str:
    if route.name == config.ORGANISATION:
        return route.from_email() or mail.from_email or config.platform_from_email()
    return mail.from_email or route.from_email() or config.platform_from_email()


def _send(mail: Mail, route: config.Route, kind: str) -> None:
    try:
        send_with(route.connect(), mail.to_email_message(_sender(mail, route)))
    except Exception:
        EMAILS.labels(result="failed").inc()
        MAILS.labels(kind=kind, route=route.name, result="failed").inc()
        raise
    EMAILS.labels(result="sent").inc()
    MAILS.labels(kind=kind, route=route.name, result="sent").inc()


def deliver(mail: Mail, route: config.Route, *, kind: str) -> str:
    """Sendet ``mail`` über ``route``, bei Bedarf über den Ersatzweg; liefert den genutzten Weg.

    Scheitert der Weg ohne Ersatzweg, wirft ``deliver`` die Ausnahme weiter; beim Weg der Organisation
    als ``OrgMailError``.
    """
    try:
        _send(mail, route, kind)
    except Exception as exc:
        if route.fallback is None:
            logger.warning(
                "Mail (%s) über den Weg %s fehlgeschlagen: %s", kind, route.name, type(exc).__name__, exc_info=True
            )
            if route.name == config.ORGANISATION:
                raise OrgMailError(type(exc).__name__) from exc
            raise
        logger.warning(
            "Mail (%s) über das eigene SMTP der Organisation %s fehlgeschlagen (%s), Versand über die Plattform",
            kind,
            route.organization_ref,
            type(exc).__name__,
        )
        _send(mail, route.fallback, kind)
        return route.fallback.name
    logger.info("Mail (%s) über den Weg %s versendet", kind, route.name)
    return route.name
