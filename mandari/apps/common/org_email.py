# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organisationseigener E-Mail-Versand (Issue #65).

Die Organisation entscheidet in den Einstellungen, ob Fraktions-Mails
(Einladungen, Erinnerungen, Freigabe-Hinweise) über das eigene SMTP oder
den mandari-Standardversand laufen:

- ``mail_sender_mode = "mandari"`` (Default): Versand wie bisher über
  :func:`apps.common.email.send_email` (SiteSettings/Django-Settings).
- ``mail_sender_mode = "smtp"``: Versand über die SMTP-Felder am
  Organization-Model. Das Passwort liegt tenant-verschlüsselt vor und
  wird ausschließlich über ``get_smtp_password()`` gelesen.

Fehlerverhalten konfigurierbar (``smtp_fallback_to_mandari``):
- True (Default): Bei SMTP-Fehlern wird auf den mandari-Versand
  zurückgefallen (mit Log-Warnung).
- False: Der Versand schlägt sichtbar fehl (:class:`OrgMailError` bzw.
  ``False`` bei ``fail_silently=True``).

SPF/DKIM für die eigene Absender-Domain verantwortet die Organisation —
darauf weist die Einstellungs-UI ausdrücklich hin.
"""

import logging

logger = logging.getLogger(__name__)


class OrgMailError(Exception):
    """Versand über das organisationseigene SMTP ist fehlgeschlagen (ohne Fallback)."""


def organization_uses_own_smtp(organization) -> bool:
    """Ist der Versand über das eigene SMTP aktiv und konfiguriert?"""
    if organization is None:
        return False
    return organization.mail_sender_mode == "smtp" and bool(organization.smtp_host)


def get_organization_connection(organization):
    """
    SMTP-Verbindung aus den Organisations-Feldern aufbauen.

    Das Passwort wird über den Accessor entschlüsselt (tenant-spezifische
    AES-256-GCM-Ablage) und niemals geloggt.
    """
    from django.core.mail import get_connection

    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=organization.smtp_host,
        port=organization.smtp_port or 587,
        username=organization.smtp_username,
        password=organization.get_smtp_password(),
        use_tls=organization.smtp_use_tls,
        timeout=15,
    )


def get_organization_from_email(organization) -> str | None:
    """Absender-Adresse der Organisation ("Name <adresse>") oder None."""
    if not organization.smtp_from_email:
        return None
    name = organization.smtp_from_name or organization.name
    if name:
        return f"{name} <{organization.smtp_from_email}>"
    return organization.smtp_from_email


def send_org_email(
    organization,
    *,
    subject: str,
    body: str,
    to: list[str],
    html_body: str | None = None,
    reply_to: list[str] | None = None,
    attachments: list[tuple] | None = None,
    fail_silently: bool = False,
) -> bool:
    """
    E-Mail über den konfigurierten Versandweg der Organisation senden.

    Returns:
        True bei Erfolg (bzw. erfolgreichem Fallback), sonst False
        (nur mit fail_silently=True — sonst OrgMailError).

    Raises:
        OrgMailError: SMTP-Fehler ohne Fallback und fail_silently=False.
    """
    from apps.common.email import send_email

    if not organization_uses_own_smtp(organization):
        return send_email(
            subject=subject,
            body=body,
            to=to,
            html_body=html_body,
            reply_to=reply_to,
            attachments=attachments,
            fail_silently=fail_silently,
        )

    try:
        connection = get_organization_connection(organization)
        return send_email(
            subject=subject,
            body=body,
            to=to,
            html_body=html_body,
            reply_to=reply_to,
            attachments=attachments,
            fail_silently=False,
            connection=connection,
            from_email=get_organization_from_email(organization),
        )
    except Exception as exc:
        if organization.smtp_fallback_to_mandari:
            logger.warning(
                "Organisations-SMTP fehlgeschlagen (org=%s) — Fallback auf mandari-Versand: %s",
                organization.slug,
                exc,
            )
            return send_email(
                subject=subject,
                body=body,
                to=to,
                html_body=html_body,
                reply_to=reply_to,
                attachments=attachments,
                fail_silently=fail_silently,
            )

        logger.error(
            "Organisations-SMTP fehlgeschlagen (org=%s, kein Fallback konfiguriert): %s",
            organization.slug,
            exc,
        )
        if fail_silently:
            return False
        raise OrgMailError(f"Versand über das eigene SMTP fehlgeschlagen: {exc}") from exc
