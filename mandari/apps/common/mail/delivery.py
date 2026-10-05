# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Versand einer Mail über einen Weg (Issue #528) – sofort, im Aufruf.

``deliver`` ist die einzige Stelle, an der der Mail-Dienst eine Verbindung aufbaut und sendet. Der
Postausgang ruft sie im Auftrag auf, ``send(..., sofort=True)`` und der Rückfall ohne Warteschlange in
der Anfrage. Gezählt wird in ``mandari_mail_total`` (Art, Weg, Ergebnis) und wie bisher in
``mandari_emails_total``. Protokolle nennen Art, Weg, Fehlerklasse und SMTP-Codes, nie Empfänger oder
Inhalt: SMTP-Ausnahmen tragen Empfängeradressen in sich, deshalb weder Stacktrace noch Meldungstext
(``fehlercode``).
"""

from __future__ import annotations

import logging
import smtplib

from apps.common.mail_backends import send_with
from apps.common.metrics import EMAILS, MAILS
from apps.common.org_email import OrgMailError

from . import config
from .message import Mail

logger = logging.getLogger("apps.common.mail")


def fehlercode(exc: BaseException) -> str:
    """Fehlerklasse mit SMTP-Codes, ohne Meldungstext und Adressen, z. B. ``SMTPRecipientsRefused 451``."""
    name = type(exc).__name__
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = sorted({str(antwort[0]) for antwort in exc.recipients.values()})
        return f"{name} {','.join(codes)}".strip()
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"{name} {exc.smtp_code}"
    return name


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
    als ``OrgMailError`` (nur mit der Fehlerklasse, ohne die ursprüngliche Ausnahme).
    """
    ersatz = route.fallback
    try:
        _send(mail, route, kind)
    except Exception as exc:
        code = fehlercode(exc)
        if ersatz is None:
            logger.warning("Mail (%s) über den Weg %s fehlgeschlagen: %s", kind, route.name, code)
            if route.name == config.ORGANISATION:
                raise OrgMailError(code) from None
            raise
        logger.warning(
            "Mail (%s) über das eigene SMTP der Organisation %s fehlgeschlagen (%s), Versand über die Plattform",
            kind,
            route.organization_ref,
            code,
        )
    else:
        logger.info("Mail (%s) über den Weg %s versendet", kind, route.name)
        return route.name
    # Ersatzweg außerhalb des except-Blocks: Die gescheiterte Ausnahme hängt nicht an der nächsten
    _send(mail, ersatz, kind)
    logger.info("Mail (%s) über den Weg %s versendet", kind, ersatz.name)
    return ersatz.name
