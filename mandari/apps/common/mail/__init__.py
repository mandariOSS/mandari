# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mail-Dienst der Plattform (Issue #528): der einzige Einstieg für ausgehende Mails.

    from apps.common import mail

    mail.send(
        kind="work.fraktion.einladung",       # Mailart: Metrik, Schalter, Protokoll
        subject="Einladung", body=text, html_body=html, to=[adresse],
        organization=org,                      # Versandweg und Mandantenschlüssel (optional)
        attachments=[("sitzung.ics", ics, "text/calendar")],
        idempotency_key=f"{ereignis}:{empfaenger}",   # optional: dieselbe Mail nur einmal
    )

- **Vorlagen:** ``render_email`` (Basis-Layout, Inliner, Textfassung) bzw. ``send_template``.
- **Konfiguration:** ``apps.common.mail.config`` – Plattform (Systemeinstellungen, sonst Umgebung) und
  eigenes SMTP der Organisation mit Ersatzweg.
- **Versand als Auftrag:** Passt die Mailart zu ``MAIL_QUEUE`` und läuft das Backend ``journal``,
  landet die Mail verschlüsselt im Postausgang und ein Auftrag der Warteschlange ``mail`` versendet sie
  mit Wiederholung (``apps.common.mail.outbox``). Sonst – und mit ``sofort=True``, etwa für Testmails,
  deren Ergebnis die Oberfläche anzeigt – geht sie wie bisher im Aufruf raus. Ebenfalls sofort geht eine
  Mail über das eigene SMTP einer Organisation, die keinen Ersatzweg erlaubt: Ihr Scheitern muss der
  Auslöser sehen (#65), im Auftrag stünde es nur im Protokoll. ``MAIL_QUEUE`` leeren ist der Rückweg; was
  schon im Postausgang liegt, versendet der Worker trotzdem.
- **Metrik:** ``mandari_mail_total{kind, route, result}`` (``sent``, ``failed``, ``queued``, ``expired``),
  dazu wie bisher ``mandari_emails_total{result}``.

Rückgabe ``True``: versendet bzw. zum Versand angenommen. Fehler: Ausnahme, mit ``fail_silently=True``
stattdessen ``False`` (protokolliert, ohne Empfänger und Inhalt).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Any, Final

from django.conf import settings
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

from apps.common.email import render_email

from . import config, delivery, outbox
from .message import Attachment, Mail, check_kind

__all__ = ["Attachment", "Mail", "queue_enabled", "render_email", "send", "send_template"]

logger = logging.getLogger("apps.common.mail")

#: Größere Mails gehen sofort raus, statt im Postausgang zu liegen (Bytes, Texte und Anhänge)
DEFAULT_QUEUE_MAX_BYTES: Final = 20 * 1024 * 1024


def queue_enabled(kind: str) -> bool:
    """Geht eine Mail dieser Art als Auftrag raus? ``MAIL_QUEUE`` passt und das Backend ist ``journal``."""
    muster = getattr(settings, "MAIL_QUEUE", ()) or ()
    if not any(fnmatchcase(kind, str(m)) for m in muster):
        return False
    from django.tasks import task_backends

    from apps.events.tasks_backend import JournalBackend

    return isinstance(task_backends[outbox.deliver_mail.backend], JournalBackend)


def send(
    *,
    kind: str,
    subject: str,
    body: str,
    to: Iterable[str],
    html_body: str | None = None,
    from_email: str | None = None,
    reply_to: Iterable[str] | None = None,
    attachments: Iterable[Any] | None = None,
    organization: Any = None,
    via_organization: bool = True,
    idempotency_key: str | None = None,
    fail_silently: bool = False,
    sofort: bool = False,
) -> bool:
    """Eine Mail senden bzw. zum Versand in den Postausgang legen (siehe Moduldokumentation).

    ``organization`` wählt ihren Versandweg (eigenes SMTP, sonst Plattform) und verschlüsselt den
    Postausgang mit ihrem Mandantenschlüssel; ``via_organization=False`` erzwingt den Weg der Plattform
    (Links zum Setzen eines Passworts). ``from_email`` gilt nur auf dem Weg der Plattform.
    """
    check_kind(kind)
    if isinstance(to, str) or isinstance(reply_to, str):
        raise TypeError("to und reply_to sind Listen von Adressen, keine Zeichenkette")
    empfaenger = tuple(adresse for adresse in to if adresse)
    if not empfaenger:
        return False
    try:
        nachricht = Mail(
            subject=subject,
            body=body,
            to=empfaenger,
            html_body=html_body or None,
            from_email=from_email or None,
            reply_to=tuple(adresse for adresse in reply_to or () if adresse),
            attachments=tuple(Attachment.of(anhang) for anhang in attachments or ()),
        )
        grenze = int(getattr(settings, "MAIL_QUEUE_MAX_BYTES", DEFAULT_QUEUE_MAX_BYTES))
        weg = config.resolve(organization, via_organization=via_organization)
        # Eigenes SMTP ohne Ersatzweg: Scheitern muss sichtbar sein (#65), also im Aufruf
        sichtbar_scheitern = weg.name == config.ORGANISATION and weg.fallback is None
        if not sofort and not sichtbar_scheitern and queue_enabled(kind) and nachricht.size <= grenze:
            outbox.put(
                nachricht,
                kind=kind,
                organization=organization,
                via_organization=via_organization,
                idempotency_key=idempotency_key,
            )
            return True
        delivery.deliver(nachricht, weg, kind=kind)
        return True
    except Exception as exc:
        if not fail_silently:
            raise
        # Ohne Stacktrace: SMTP-Ausnahmen nennen Empfängeradressen
        logger.warning("Mail (%s) nicht versendet (%s)", kind, delivery.fehlercode(exc))
        return False


def send_template(
    *,
    kind: str,
    subject: str,
    template_name: str,
    context: Mapping[str, Any],
    to: Iterable[str],
    fail_silently: bool = False,
    **optionen: Any,
) -> bool:
    """Mail aus Vorlage ``<template_name>.html`` (Basis-Layout, Textfassung) bzw. nur ``.txt`` senden.

    ``template_name`` ohne Endung, z. B. ``emails/contact/confirmation``. Weitere Angaben wie bei ``send``.
    """
    html_body: str | None
    text_body: str | None
    try:
        html_body, text_body = render_email(f"{template_name}.html", dict(context))
    except TemplateDoesNotExist:
        html_body = None
        try:
            text_body = render_to_string(f"{template_name}.txt", dict(context))
        except TemplateDoesNotExist:
            text_body = None
    except Exception:
        logger.exception("Mailvorlage %s konnte nicht gerendert werden", template_name)
        if not fail_silently:
            raise
        return False
    if not text_body:
        logger.error("Keine Mailvorlage für %s gefunden", template_name)
        if not fail_silently:
            raise ValueError(f"Keine Mailvorlage für {template_name} gefunden")
        return False
    return send(
        kind=kind,
        subject=subject,
        body=text_body,
        html_body=html_body,
        to=to,
        fail_silently=fail_silently,
        **optionen,
    )
