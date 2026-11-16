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

import math
import smtplib
import socket
import time
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


class _Zeilenleser:
    """
    Leseseite der Verbindung wie ``socket.makefile("rb")``, aber mit der Gesamtfrist vor jedem einzelnen Empfang.

    Eine gepufferte Datei ruft ``recv`` für eine Zeile beliebig oft auf, jedes Mal mit der vollen Zeitgrenze des
    Sockets: Ein Server, der eine Zeile byteweise streckt, hielte die Verbindung so fast beliebig lange offen.
    """

    def __init__(self, verbindung: BegrenzterSMTP) -> None:
        self._verbindung = verbindung
        self._puffer = bytearray()

    def readline(self, limit: int) -> bytes:
        """Die nächste Zeile samt Zeilenende, höchstens ``limit`` Bytes; ``b""`` am Ende der Verbindung."""
        while b"\n" not in self._puffer and len(self._puffer) < limit:
            sock = self._verbindung.sock
            if sock is None:
                break
            self._verbindung._frist_setzen()
            stueck = sock.recv(4096)
            if not stueck:
                break
            self._puffer += stueck
        zeilenende = self._puffer.find(b"\n")
        laenge = min(limit, zeilenende + 1 if zeilenende != -1 else len(self._puffer))
        zeile = bytes(self._puffer[:laenge])
        del self._puffer[:laenge]
        return zeile

    def close(self) -> None:
        self._puffer.clear()


class BegrenzterSMTP(smtplib.SMTP):
    """
    SMTP-Client für Server, die nicht der Betreiber gewählt hat (eigenes SMTP einer Organisation).

    Der Versand läuft in Prozessen, die alle Mandanten teilen. Deshalb gelten für einen fremden Server drei Grenzen,
    die ``smtplib`` nicht kennt:

    - Eine Antwort (auch der Gruß) ist höchstens ``max_antwort`` Bytes lang. ``smtplib`` begrenzt nur die einzelne
      Zeile und sammelt beliebig viele Fortsetzungszeilen im Speicher.
    - Die ganze Verbindung hat eine Frist (``gesamtfrist``; die Zeit, in der mandari selbst sendet, zählt nicht
      dagegen). Die Zeitgrenze des Sockets beginnt mit jedem Lesevorgang neu und hält einen Server nicht auf, der
      seine Antwort in kleinen Stücken streckt. Die Frist braucht keinen eigenen Faden: Vor jedem Verbinden,
      Empfangen und Senden bekommt der Socket als Zeitgrenze höchstens den Rest der Frist; ist sie abgelaufen, endet
      die Verbindung. Das gilt auch nach STARTTLS, weil immer der aktuelle Socket gesetzt wird.
    - Die Verbindung geht nur an Adressen, die ``public_addresses`` geprüft hat, nicht an eine zweite Auflösung
      desselben Namens. Für TLS gilt weiter der eingetragene Name.

    Der Mail-Dienst baut je Mail eine Verbindung auf (``send_with``); die Frist gilt damit je Mail.
    """

    #: Antworten eines Mailservers sind wenige hundert Bytes lang, mit vielen Erweiterungen wenige KiB
    max_antwort: ClassVar[int] = 64 * 1024
    #: Längste Zeile einer Antwort (wie ``smtplib``)
    max_zeile: ClassVar[int] = 8192
    #: Sekunden für Aufbau, STARTTLS, Anmeldung und alle Antworten des Servers zusammen. Jeder einzelne Schritt wartet
    #: ohnehin höchstens die Zeitgrenze des Sockets (15 s beim eigenen SMTP). Ein gewöhnlicher Server braucht für alle
    #: Schritte zusammen wenige Sekunden, mit Virenprüfung eines Anhangs und einer nicht erreichbaren ersten Adresse
    #: einige zehn; zwei Minuten brechen deshalb keine gewöhnliche Mail ab. Die Übertragung der Mail selbst zählt
    #: nicht mit (siehe ``send``): Für sie gilt wie bisher allein die Zeitgrenze des Sockets, auch bei großem Anhang.
    #: Ein Server, der hängt oder Antworten streckt, bindet den Prozess höchstens so lange statt unbegrenzt.
    gesamtfrist: ClassVar[float] = 120.0

    #: Zeitpunkt (``time.monotonic``), an dem die Verbindung spätestens endet; gesetzt beim Verbindungsaufbau
    _frist_ende: float = math.inf

    def _wartezeit(self) -> float:
        """Sekunden, die der nächste Schritt warten darf: die Zeitgrenze des Sockets, höchstens der Rest der Frist."""
        rest = self._frist_ende - time.monotonic()
        if rest <= 0:
            self.close()
            raise smtplib.SMTPServerDisconnected("Connection deadline exceeded")
        grenze = self.timeout
        if isinstance(grenze, int | float) and grenze > 0:
            return min(float(grenze), rest)
        return rest

    def _frist_setzen(self) -> None:
        """Zeitgrenze des aktuellen Sockets für den nächsten Schritt setzen (auch nach STARTTLS der richtige)."""
        wartezeit = self._wartezeit()
        if self.sock is None:
            return
        try:
            self.sock.settimeout(wartezeit)
        except OSError as exc:
            self.close()
            raise smtplib.SMTPServerDisconnected("Server not connected") from exc

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        from apps.common.org_email import public_addresses

        self._frist_ende = time.monotonic() + self.gesamtfrist
        fehler: OSError | None = None
        # Wie ``socket.create_connection`` alle Adressen des Namens der Reihe nach (etwa IPv6 gestört, IPv4
        # erreichbar), aber nur die geprüften und ohne zweite Auflösung
        for adresse in public_addresses(host):
            try:
                return socket.create_connection((adresse, port), self._wartezeit(), self.source_address)
            except OSError as exc:
                fehler = exc
        raise fehler if fehler is not None else OSError("SMTP-Server ohne Adresse")

    def send(self, s: Any) -> None:
        """
        Senden wie ``smtplib``, mit dem Rest der Frist als Zeitgrenze.

        Die Dauer des Sendens zählt nicht gegen die Frist: Eine Mail mit Anhang an einen langsamen Server soll nicht
        an der Frist für Aufbau und Antworten scheitern. Unbegrenzt wird die Verbindung dadurch nicht, denn ein
        Sendevorgang dauert als Ganzes höchstens die Zeitgrenze des Sockets, und nur so viel kommt je Vorgang dazu.
        """
        if self.sock is not None:
            self._frist_setzen()
        beginn = time.monotonic()
        try:
            super().send(s)
        finally:
            self._frist_ende += time.monotonic() - beginn

    def getreply(self) -> tuple[int, bytes]:
        """Antwort lesen wie ``smtplib``, aber nur bis ``max_antwort`` Bytes; darüber endet die Verbindung."""
        if self.sock is None:
            raise smtplib.SMTPServerDisconnected("please run connect() first")
        if self.file is None:
            # ``smtplib`` verwirft die Leseseite nach STARTTLS (``file = None``): Vor TLS Gepuffertes zählt nicht
            self.file = _Zeilenleser(self)
        zeilen: list[bytes] = []
        gelesen = 0
        while True:
            try:
                zeile = self.file.readline(self.max_zeile + 1)
            except smtplib.SMTPServerDisconnected:
                # Frist abgelaufen: Die Verbindung ist schon beendet
                raise
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
