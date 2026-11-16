# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mail-Backends zur Laufzeit aufbauen (Django ≥ 6.1, Issue #80).

Django 6.1 ersetzt ``get_connection()`` und das ``connection``-Argument durch die
``MAILERS``-Konfiguration mit festen Aliassen. mandari braucht daneben Backends mit
Zugangsdaten aus der Datenbank (SiteSettings, organisationseigenes SMTP), die erst zur
Laufzeit bekannt sind. Dafür wird die Backend-Klasse direkt instanziiert – das ist die
von Django vorgesehene, nicht deprecatete Schnittstelle (``BaseEmailBackend`` mit
``send_messages``).
"""

from __future__ import annotations

import contextlib
import smtplib
import socket
import threading
from typing import Any, ClassVar

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend
from django.utils.module_loading import import_string

SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
#: Backend für Server, die eine Organisation selbst eingetragen hat (``BegrenztesSMTPBackend``)
BEGRENZTES_SMTP_BACKEND = "apps.common.mail_backends.BegrenztesSMTPBackend"
#: Alias für zur Laufzeit aufgebaute Backends (SiteSettings, Organisations-SMTP)
LAUFZEIT_ALIAS = "laufzeit"

# Djangos SMTP-Backend liest für jede Option, die None ist, das zugehörige EMAIL_*-Setting –
# und das wirft neben MAILERS einen AttributeError. Deshalb bekommt jedes SMTP-Backend
# vollständige, nie leere Optionen (Deploy-Rückfall 18.09.2026).
SMTP_DEFAULTS: dict[str, Any] = {
    "port": 587,
    "username": "",
    "password": "",
    "use_tls": True,
    "use_ssl": False,
    "timeout": 30,
    "ssl_keyfile": "",
    "ssl_certfile": "",
}


def smtp_options(**options: Any) -> dict[str, Any]:
    """Vollständiger Optionssatz für ein SMTP-Backend; ``host`` ist Pflicht, None-Werte werden ersetzt."""
    voll = dict(SMTP_DEFAULTS)
    voll.update({k: v for k, v in options.items() if v is not None})
    if not voll.get("host"):
        raise ValueError("SMTP-Host fehlt: weder SiteSettings noch EMAIL_HOST gesetzt")
    return voll


def build_backend(backend: str, **options: Any) -> BaseEmailBackend:
    """
    Backend-Klasse laden und mit den Optionen (host, port, username, …) instanziieren.

    SMTP-Optionen gehen nur an SMTP-Backends: Django 6.1 warnt (7.0: Fehler), wenn ein
    Backend wie locmem oder console unbekannte Argumente bekommt.
    """
    if getattr(settings, "DEMO_INSTANCE", False):
        # Öffentliche Demo (Issue #99): auch Zugänge aus SiteSettings oder Organisation versenden nichts.
        backend = "django.core.mail.backends.locmem.EmailBackend"
    klasse = import_string(backend)
    if issubclass(klasse, SMTPEmailBackend):
        options = smtp_options(**options)
    else:
        options = {k: v for k, v in options.items() if k == "fail_silently"}
    # Mit Alias läuft Django ≥ 6.1 im MAILERS-Modus: keine EMAIL_*-Settings, keine Deprecation.
    options.setdefault("alias", LAUFZEIT_ALIAS)
    instanz: BaseEmailBackend = klasse(**options)
    return instanz


class BegrenzterSMTP(smtplib.SMTP):
    """
    SMTP-Client für Server, die nicht der Betreiber gewählt hat (eigenes SMTP einer Organisation).

    Der Versand läuft in Prozessen, die alle Mandanten teilen. Deshalb gelten für einen fremden Server drei Grenzen,
    die ``smtplib`` nicht kennt:

    - Eine Antwort (auch der Gruß) ist höchstens ``max_antwort`` Bytes lang. ``smtplib`` begrenzt nur die einzelne
      Zeile und sammelt beliebig viele Fortsetzungszeilen im Speicher.
    - Die ganze Verbindung dauert höchstens ``gesamtfrist`` Sekunden. Die Zeitgrenze des Sockets beginnt mit jedem
      Lesevorgang neu und hält einen Server nicht auf, der seine Antwort in kleinen Stücken streckt.
    - Die Verbindung geht an die Adresse, die ``public_address`` geprüft hat, nicht an eine zweite Auflösung
      desselben Namens. Für TLS gilt weiter der eingetragene Name.
    """

    #: Antworten eines Mailservers sind wenige hundert Bytes lang, mit vielen Erweiterungen wenige KiB
    max_antwort: ClassVar[int] = 64 * 1024
    #: Längste Zeile einer Antwort (wie ``smtplib``)
    max_zeile: ClassVar[int] = 8192
    #: Sekunden für Aufbau, Anmeldung und Übergabe einer Mail zusammen
    gesamtfrist: ClassVar[float] = 60.0

    _zeitgeber: threading.Timer | None = None
    _notaus: socket.socket | None = None

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        from apps.common.org_email import public_address

        sock = socket.create_connection((public_address(host), port), timeout, self.source_address)
        # Die Frist wirkt über ein Duplikat: STARTTLS ersetzt das Socket-Objekt, die Verbindung bleibt dieselbe
        try:
            self._notaus = sock.dup()
        except OSError:
            sock.close()
            raise
        self._zeitgeber = threading.Timer(self.gesamtfrist, self._abbrechen)
        self._zeitgeber.daemon = True
        self._zeitgeber.start()
        return sock

    def _abbrechen(self) -> None:
        """Frist abgelaufen: Verbindung in beide Richtungen beenden; der wartende Lesevorgang endet damit."""
        notaus = self._notaus
        if notaus is not None:
            with contextlib.suppress(OSError):
                notaus.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        zeitgeber, self._zeitgeber = self._zeitgeber, None
        if zeitgeber is not None:
            zeitgeber.cancel()
        notaus, self._notaus = self._notaus, None
        if notaus is not None:
            with contextlib.suppress(OSError):
                notaus.close()
        super().close()

    def getreply(self) -> tuple[int, bytes]:
        """Antwort lesen wie ``smtplib``, aber nur bis ``max_antwort`` Bytes; darüber endet die Verbindung."""
        if self.sock is None:
            raise smtplib.SMTPServerDisconnected("please run connect() first")
        if self.file is None:
            self.file = self.sock.makefile("rb")
        zeilen: list[bytes] = []
        gelesen = 0
        while True:
            try:
                zeile = self.file.readline(self.max_zeile + 1)
            except OSError as exc:
                self.close()
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed") from exc
            if not zeile:
                self.close()
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            gelesen += len(zeile)
            if len(zeile) > self.max_zeile or gelesen > self.max_antwort:
                self.close()
                raise smtplib.SMTPResponseException(500, "Reply too long.")
            zeilen.append(zeile[4:].strip(b" \t\r\n"))
            try:
                code = int(zeile[:3])
            except ValueError:
                code = -1
                break
            if zeile[3:4] != b"-":
                break
        return code, b"\n".join(zeilen)


class BegrenztesSMTPBackend(SMTPEmailBackend):
    """SMTP-Backend für das eigene SMTP einer Organisation: Verbindungen über :class:`BegrenzterSMTP`."""

    @property
    def connection_class(self) -> type[smtplib.SMTP]:
        return BegrenzterSMTP


def send_with(backend: BaseEmailBackend, message: Any) -> int:
    """Eine Nachricht über ein bestimmtes Backend senden; liefert die Anzahl gesendeter Mails."""
    with backend:
        return int(backend.send_messages([message]) or 0)
