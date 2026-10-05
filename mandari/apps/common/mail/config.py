# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Konfigurationsauflösung des Mail-Dienstes (Issue #528): die einzige Stelle, die festlegt, über welchen
Weg und mit welchem Absender eine Mail das System verlässt.

**Plattform:** Ist in den Systemeinstellungen (Admin) ein SMTP-Server eingetragen, gelten dessen Werte;
leere Felder für Benutzername und Passwort fallen auf die Umgebung zurück (``EMAIL_HOST_USER``,
``EMAIL_HOST_PASSWORD``). Sonst gelten Backend und Zugang aus der Umgebung (``EMAIL_BACKEND``,
``SMTP_FALLBACK``). Absender: Absenderadresse der Systemeinstellungen, sonst ``DEFAULT_FROM_EMAIL``,
mit dem Absendernamen der Systemeinstellungen.

**Organisation (Work):** Hat sie den Versand über ihr eigenes SMTP gewählt und einen Server eingetragen,
geht die Mail darüber, mit ihrem Absender. Scheitert er und erlaubt die Organisation den Ersatzweg
(``smtp_fallback_to_mandari``), geht sie über die Plattform. Links, mit denen sich ein Passwort setzen
lässt, laufen nie über fremde Server (``via_organization=False``).

Die Systemeinstellungen (``SiteSettings.get_email_config``) und das Backend für Djangos eigenen
Versand (``SiteSettingsEmailBackend``) lesen dieselbe Auflösung.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

from apps.common.mail_backends import SMTP_BACKEND, build_backend

#: Name der Wege in Protokoll und Metrik
PLATTFORM: Final = "plattform"
ORGANISATION: Final = "organisation"
#: Ersatzweg: Die Organisation erlaubt den Versand über die Plattform, wenn ihr eigenes SMTP scheitert
ERSATZWEG: Final = "ersatzweg"

#: Zeitgrenze für das eigene SMTP einer Organisation (Sekunden)
ORGANIZATION_TIMEOUT: Final = 15


@dataclass(frozen=True)
class SmtpConfig:
    """Zugang eines Versandweges; ``backend`` ist ein Importpfad (SMTP, im Test locmem, lokal console)."""

    backend: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    use_tls: bool
    use_ssl: bool
    timeout: int
    from_address: str

    def connect(self) -> BaseEmailBackend:
        return build_backend(
            self.backend,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_tls=self.use_tls,
            use_ssl=self.use_ssl,
            timeout=self.timeout,
        )


def platform_config() -> SmtpConfig:
    """Zugang der Plattform: Systemeinstellungen, sonst Umgebung."""
    from apps.common.models import SiteSettings

    site = SiteSettings.get_settings()
    fallback: dict[str, Any] = dict(getattr(settings, "SMTP_FALLBACK", {}) or {})
    from_address = site.default_from_email or str(getattr(settings, "DEFAULT_FROM_EMAIL", ""))
    if site.email_host:
        return SmtpConfig(
            backend=SMTP_BACKEND,
            host=site.email_host,
            port=int(site.email_port),
            username=site.email_host_user or str(fallback.get("username", "")),
            password=site.get_email_host_password() or str(fallback.get("password", "")),
            use_tls=bool(site.email_use_tls),
            use_ssl=bool(site.email_use_ssl),
            timeout=int(site.email_timeout),
            from_address=from_address,
        )
    return SmtpConfig(
        backend=str(getattr(settings, "MAIL_BACKEND", SMTP_BACKEND)),
        host=str(fallback.get("host", "")),
        port=int(fallback.get("port", 587)),
        username=str(fallback.get("username", "")),
        password=str(fallback.get("password", "")),
        use_tls=bool(fallback.get("use_tls", True)),
        use_ssl=bool(fallback.get("use_ssl", False)),
        timeout=int(fallback.get("timeout", 30)),
        from_address=from_address,
    )


def platform_from_email() -> str:
    """Absender der Plattform als ``Name <adresse>`` (ohne Namen nur die Adresse)."""
    from apps.common.models import SiteSettings

    address = platform_config().from_address
    name = SiteSettings.get_settings().default_from_name
    return f"{name} <{address}>" if name else address


def organization_uses_own_smtp(organization: Any) -> bool:
    """Hat die Organisation den Versand über ihr eigenes SMTP gewählt und einen Server eingetragen?"""
    if organization is None:
        return False
    return getattr(organization, "mail_sender_mode", "") == "smtp" and bool(getattr(organization, "smtp_host", ""))


def organization_backend(organization: Any) -> BaseEmailBackend:
    """Verbindung über das eigene SMTP der Organisation.

    Das Passwort wird nur hier über den Accessor entschlüsselt (Mandantenschlüssel) und nie
    protokolliert. Vor dem Aufbau wird der Server geprüft (``check_smtp_server``), auch für ältere
    Einstellungen.
    """
    from apps.common.org_email import check_smtp_server

    port = organization.smtp_port or 587
    check_smtp_server(organization.smtp_host, port)
    return build_backend(
        SMTP_BACKEND,
        host=organization.smtp_host,
        port=port,
        username=organization.smtp_username,
        password=organization.get_smtp_password(),
        use_tls=organization.smtp_use_tls,
        timeout=ORGANIZATION_TIMEOUT,
    )


def organization_from_email(organization: Any) -> str | None:
    """Absender der Organisation (``Name <adresse>``) oder None, wenn sie keine Adresse eingetragen hat."""
    address = getattr(organization, "smtp_from_email", "")
    if not address:
        return None
    name = organization.smtp_from_name or organization.name
    return f"{name} <{address}>" if name else address


@dataclass(frozen=True)
class Route:
    """Ein Versandweg: Verbindung, Absender und gegebenenfalls Ersatzweg."""

    name: str
    connect: Callable[[], BaseEmailBackend] = field(compare=False)
    #: Absender des Weges; ``None`` heißt: der Absender der Nachricht bzw. der Plattform
    from_email: Callable[[], str | None] = field(compare=False)
    fallback: Route | None = None
    #: Kennung der Organisation für das Protokoll (nie der Name)
    organization_ref: str = ""


def platform_route() -> Route:
    return Route(name=PLATTFORM, connect=lambda: platform_config().connect(), from_email=platform_from_email)


def resolve(organization: Any = None, *, via_organization: bool = True) -> Route:
    """Versandweg einer Mail: der Organisation, falls gewählt und erlaubt, sonst der Plattform."""
    if not via_organization or not organization_uses_own_smtp(organization):
        return platform_route()
    ersatz = platform_route() if organization.smtp_fallback_to_mandari else None
    return Route(
        name=ORGANISATION,
        connect=lambda: organization_backend(organization),
        from_email=lambda: organization_from_email(organization),
        fallback=Route(ERSATZWEG, ersatz.connect, ersatz.from_email) if ersatz else None,
        organization_ref=str(organization.slug),
    )
