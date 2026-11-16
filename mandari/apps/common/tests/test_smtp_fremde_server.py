# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigenes SMTP einer Organisation: Grenzen für Server, die nicht der Betreiber gewählt hat.

- Eine Antwort des Servers ist höchstens 64 KiB lang, auch der Gruß.
- Die ganze Verbindung hat eine Frist, nicht nur der einzelne Lesevorgang.
- Die Verbindung geht an die geprüfte Adresse, nicht an eine zweite Auflösung des Namens.

Ohne Netz: ``socket.create_connection`` liefert eine Leitung mit vorbereiteten Antworten.
"""

from __future__ import annotations

import io
import smtplib
import socket
import threading
import time
from typing import Any, cast

import pytest

from apps.common.mail import config
from apps.common.mail_backends import BegrenzterSMTP, BegrenztesSMTPBackend
from apps.common.org_email import OrgMailError

OEFFENTLICH = "93.184.216.34"
#: So lange wartet eine hängende Leitung höchstens, bevor sie doch antwortet (hält einen Lauf ohne Frist nicht auf)
HAENGT_HOECHSTENS = 5.0


class Lesedatei:
    """Leseseite der Leitung, wie ``socket.makefile("rb")``."""

    def __init__(self, leitung: Leitung) -> None:
        self.leitung = leitung

    def readline(self, limit: int = -1) -> bytes:
        if self.leitung.haengt:
            # Der Server schweigt, bis die Verbindung von außen beendet wird
            if self.leitung.beendet.wait(HAENGT_HOECHSTENS):
                return b""
            return b"220 bereit\r\n"
        zeile = self.leitung.antworten.readline(limit)
        self.leitung.gelesen += len(zeile)
        return zeile

    def close(self) -> None:
        return None


class Leitung:
    """Socket ohne Netz: vorbereitete Antworten, zeichnet Gesendetes und das Ende der Verbindung auf."""

    def __init__(self, antworten: bytes = b"", *, haengt: bool = False) -> None:
        self.antworten = io.BytesIO(antworten)
        self.haengt = haengt
        self.gelesen = 0
        self.gesendet = b""
        self.beendet = threading.Event()

    def makefile(self, mode: str) -> Lesedatei:
        return Lesedatei(self)

    def sendall(self, daten: bytes) -> None:
        self.gesendet += daten

    def dup(self) -> Leitung:
        return self

    def shutdown(self, how: int) -> None:
        self.beendet.set()

    def close(self) -> None:
        return None


@pytest.fixture
def netz(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Namensauflösung und Verbindungsaufbau ohne Netz; ``ziele`` nennt, wohin verbunden wurde."""
    zustand: dict[str, Any] = {"adressen": [OEFFENTLICH], "ziele": [], "leitung": Leitung(b"220 bereit\r\n")}

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        # Jede Auflösung verbraucht eine vorbereitete Antwort; die letzte gilt weiter
        adresse = zustand["adressen"].pop(0) if len(zustand["adressen"]) > 1 else zustand["adressen"][0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (adresse, 0))]

    def create_connection(ziel: tuple[str, int], *args: Any, **kwargs: Any) -> Leitung:
        zustand["ziele"].append(ziel)
        return cast(Leitung, zustand["leitung"])

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return zustand


def test_zu_lange_antwort_beendet_die_verbindung(netz: dict[str, Any]) -> None:
    """Ein Gruß aus sehr vielen Fortsetzungszeilen wird nicht eingesammelt: Nach 64 KiB ist Schluss."""
    netz["leitung"] = Leitung(b"220-warten\r\n" * 100_000 + b"220 bereit\r\n")

    with pytest.raises(smtplib.SMTPResponseException):
        BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert netz["leitung"].gelesen <= BegrenzterSMTP.max_antwort + BegrenzterSMTP.max_zeile


def test_mehrzeilige_antwort_unter_der_grenze_gilt(netz: dict[str, Any]) -> None:
    netz["leitung"] = Leitung(
        b"220 bereit\r\n250-mail.example.org\r\n250-SIZE 52428800\r\n250-8BITMIME\r\n250 STARTTLS\r\n221 ende\r\n"
    )

    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=5)
    code, _text = verbindung.ehlo("absender.example")

    assert code == 250
    assert verbindung.has_extn("starttls")
    assert verbindung.has_extn("size")
    verbindung.quit()
    assert verbindung._zeitgeber is None, "die Frist endet mit der Verbindung"


def test_frist_gilt_fuer_die_ganze_verbindung(netz: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Schweigt oder tröpfelt der Server, endet die Verbindung nach der Gesamtfrist."""
    monkeypatch.setattr(BegrenzterSMTP, "gesamtfrist", 0.2)
    netz["leitung"] = Leitung(haengt=True)

    beginn = time.monotonic()
    with pytest.raises(smtplib.SMTPServerDisconnected):
        BegrenzterSMTP("mail.example.org", 587, timeout=30)

    assert time.monotonic() - beginn < HAENGT_HOECHSTENS
    assert netz["leitung"].beendet.is_set()


def test_verbindung_geht_an_die_gepruefte_adresse(netz: dict[str, Any]) -> None:
    BegrenzterSMTP("mail.example.org", 587, timeout=5).close()

    assert netz["ziele"] == [(OEFFENTLICH, 587)]


@pytest.mark.django_db
def test_zweite_aufloesung_ins_interne_netz_verbindet_nicht(org: Any, netz: dict[str, Any]) -> None:
    """Antwortet der Name beim Verbinden anders als bei der Prüfung, entsteht keine Verbindung."""
    org.mail_sender_mode = "smtp"
    org.smtp_host = "mail.example.org"
    org.smtp_port = 587
    org.smtp_use_tls = False
    org.save()
    netz["adressen"] = [OEFFENTLICH, "127.0.0.1"]

    backend = cast(Any, config.organization_backend(org))
    assert isinstance(backend, BegrenztesSMTPBackend)
    assert backend.connection_class is BegrenzterSMTP
    with pytest.raises(OrgMailError):
        backend.open()

    assert netz["ziele"] == []


@pytest.mark.django_db
def test_plattform_behaelt_den_gewoehnlichen_client(settings: Any) -> None:
    """Den Server der Plattform wählt der Betreiber: dort gelten die Grenzen nicht."""
    settings.MAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.SMTP_FALLBACK = {**settings.SMTP_FALLBACK, "host": "smtp.example.org"}

    backend = cast(Any, config.platform_config().connect())

    assert not isinstance(backend, BegrenztesSMTPBackend)
