# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigenes SMTP einer Organisation: Grenzen für Server, die nicht der Betreiber gewählt hat.

- Eine Antwort des Servers ist höchstens 64 KiB lang, auch der Gruß.
- Die ganze Verbindung hat eine Frist, nicht nur der einzelne Lesevorgang. Die Übertragung der Mail zählt nicht mit.
- Die Verbindung geht an die geprüften Adressen (alle der Reihe nach), nicht an eine zweite Auflösung des Namens.

Ohne Netz: ``socket.create_connection`` liefert eine Leitung mit vorbereiteten Antworten.
"""

from __future__ import annotations

import io
import smtplib
import socket
import time
from typing import Any, cast

import pytest

from apps.common.mail import config
from apps.common.mail_backends import BegrenzterSMTP, BegrenztesSMTPBackend
from apps.common.org_email import OrgMailError

OEFFENTLICH = "93.184.216.34"
OEFFENTLICH_V6 = "2606:2800:220:1:248:1893:25c8:1946"
GRUSS = b"220 bereit\r\n"


class Leitung:
    """
    Socket ohne Netz: vorbereitete Antworten, zeichnet Gesendetes und die gesetzten Zeitgrenzen auf.

    ``stueck``: so viele Bytes liefert ein Empfang höchstens; ``pause``: so lange dauert er (ein Server, der seine
    Antwort streckt). Sind die Antworten aufgebraucht und ``schweigt`` gesetzt, wartet der Empfang die Zeitgrenze ab
    und scheitert wie ein echter Socket mit ``TimeoutError``. ``sendedauer``: so lange dauert ein Sendevorgang (ein
    Server, der langsam abnimmt).
    """

    def __init__(self, antworten: bytes = b"", *, stueck: int = 4096, pause: float = 0.0, schweigt: bool = False):
        self.antworten = io.BytesIO(antworten)
        self.stueck = stueck
        self.pause = pause
        self.schweigt = schweigt
        self.sendedauer = 0.0
        self.gelesen = 0
        self.gesendet = b""
        self.zeitgrenzen: list[float] = []
        self.geschlossen = False

    def settimeout(self, wert: float) -> None:
        self.zeitgrenzen.append(wert)

    def _zeitgrenze(self) -> float:
        return self.zeitgrenzen[-1] if self.zeitgrenzen else 60.0

    def recv(self, anzahl: int) -> bytes:
        if self.pause:
            if self.pause > self._zeitgrenze():
                time.sleep(self._zeitgrenze())
                raise TimeoutError("timed out")
            time.sleep(self.pause)
        daten = self.antworten.read(min(anzahl, self.stueck))
        if not daten and self.schweigt:
            time.sleep(self._zeitgrenze())
            raise TimeoutError("timed out")
        self.gelesen += len(daten)
        return daten

    def sendall(self, daten: bytes) -> None:
        if self.sendedauer:
            time.sleep(self.sendedauer)
        self.gesendet += daten

    def close(self) -> None:
        self.geschlossen = True


@pytest.fixture
def netz(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Namensauflösung und Verbindungsaufbau ohne Netz.

    ``adressen``: je Auflösung eine Antwort (eine Adresse oder mehrere), die letzte gilt weiter. ``gestoert``:
    Adressen, zu denen keine Verbindung zustande kommt. ``ziele`` nennt, wohin verbunden wurde, ``wartezeiten`` mit
    welcher Zeitgrenze.
    """
    zustand: dict[str, Any] = {
        "adressen": [OEFFENTLICH],
        "gestoert": set(),
        "ziele": [],
        "wartezeiten": [],
        "leitung": Leitung(GRUSS),
    }

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        antwort = zustand["adressen"].pop(0) if len(zustand["adressen"]) > 1 else zustand["adressen"][0]
        adressen = [antwort] if isinstance(antwort, str) else list(antwort)
        # Wie das Betriebssystem: je Adresse mehrere Einträge (Stream, Datagramm)
        return [
            (socket.AF_INET6 if ":" in adresse else socket.AF_INET, art, 0, "", (adresse, 0))
            for adresse in adressen
            for art in (socket.SOCK_STREAM, socket.SOCK_DGRAM)
        ]

    def create_connection(ziel: tuple[str, int], timeout: float, *args: Any, **kwargs: Any) -> Leitung:
        zustand["ziele"].append(ziel)
        zustand["wartezeiten"].append(timeout)
        if ziel[0] in zustand["gestoert"]:
            raise OSError(101, "Network is unreachable")
        return cast(Leitung, zustand["leitung"])

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return zustand


# --- Größe der Antwort -----------------------------------------------------------------------------------------


def test_zu_lange_antwort_beendet_die_verbindung(netz: dict[str, Any]) -> None:
    """Ein Gruß aus sehr vielen Fortsetzungszeilen wird nicht eingesammelt: Nach 64 KiB ist Schluss."""
    netz["leitung"] = Leitung(b"220-warten\r\n" * 100_000 + GRUSS)

    with pytest.raises(smtplib.SMTPResponseException):
        BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert netz["leitung"].gelesen <= BegrenzterSMTP.max_antwort + BegrenzterSMTP.max_zeile
    assert netz["leitung"].geschlossen


def test_zu_lange_zeile_beendet_die_verbindung(netz: dict[str, Any]) -> None:
    netz["leitung"] = Leitung(b"220 " + b"x" * 100_000 + b"\r\n")

    with pytest.raises(smtplib.SMTPResponseException):
        BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert netz["leitung"].gelesen <= 2 * BegrenzterSMTP.max_zeile


def test_mehrzeilige_antwort_unter_der_grenze_gilt(netz: dict[str, Any]) -> None:
    netz["leitung"] = Leitung(
        b"220 bereit\r\n250-mail.example.org\r\n250-SIZE 52428800\r\n250-8BITMIME\r\n250 STARTTLS\r\n221 ende\r\n"
    )

    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=5)
    code, _text = verbindung.ehlo("absender.example")

    assert code == 250
    assert verbindung.has_extn("starttls")
    assert verbindung.has_extn("size")
    assert verbindung.quit()[0] == 221
    assert netz["leitung"].gesendet == b"ehlo absender.example\r\nquit\r\n"
    assert verbindung.sock is None and netz["leitung"].geschlossen


def test_antwort_in_einzelnen_bytes_wird_zusammengesetzt(netz: dict[str, Any]) -> None:
    """Der eigene Zeilenleser liest wie die gepufferte Datei von ``smtplib``, auch über viele Empfänge hinweg."""
    netz["leitung"] = Leitung(GRUSS + b"250-mail.example.org\r\n250 SIZE 1000\r\n", stueck=1)

    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert verbindung.ehlo("absender.example") == (250, b"mail.example.org\nSIZE 1000")


def test_vor_starttls_gepuffertes_zaehlt_danach_nicht(netz: dict[str, Any]) -> None:
    """
    ``smtplib`` verwirft die Leseseite nach STARTTLS (``file = None``). Was der Server vorher zu viel geschickt hat,
    gilt danach nicht als Antwort: Sonst käme eine Antwort aus der unverschlüsselten Verbindung in die verschlüsselte.
    """
    netz["leitung"] = Leitung(GRUSS + b"250 untergeschoben\r\n")
    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=5)

    verbindung.file = None  # wie smtplib.SMTP.starttls nach dem Wechsel des Sockets

    with pytest.raises(smtplib.SMTPServerDisconnected):
        verbindung.getreply()


# --- Frist der ganzen Verbindung -------------------------------------------------------------------------------


def test_frist_beendet_einen_server_der_schweigt(netz: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Zeitgrenze des Sockets ist höchstens der Rest der Frist, auch wenn sie selbst länger wäre."""
    monkeypatch.setattr(BegrenzterSMTP, "gesamtfrist", 0.2)
    netz["leitung"] = Leitung(schweigt=True)

    beginn = time.monotonic()
    with pytest.raises(smtplib.SMTPServerDisconnected):
        BegrenzterSMTP("mail.example.org", 587, timeout=30)

    assert time.monotonic() - beginn < 5
    assert netz["wartezeiten"] and max(netz["wartezeiten"]) <= 0.2
    assert netz["leitung"].zeitgrenzen and max(netz["leitung"].zeitgrenzen) <= 0.2
    assert netz["leitung"].geschlossen


def test_frist_beendet_einen_server_der_seine_antwort_streckt(
    netz: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Jeder Empfang bleibt unter der Zeitgrenze des Sockets, die Antwort käme aber erst nach vielen Sekunden zusammen.
    Die Frist zählt über alle Empfänge: Die Verbindung endet, lange bevor die Zeile vollständig ist.
    """
    monkeypatch.setattr(BegrenzterSMTP, "gesamtfrist", 0.3)
    netz["leitung"] = Leitung(b"220-" + b"x" * 2000 + b"\r\n" + GRUSS, stueck=1, pause=0.02)

    beginn = time.monotonic()
    with pytest.raises(smtplib.SMTPServerDisconnected):
        BegrenzterSMTP("mail.example.org", 587, timeout=30)

    assert time.monotonic() - beginn < 5
    assert 0 < netz["leitung"].gelesen < 200, "abgebrochen nach wenigen der 2000 Bytes"
    assert netz["leitung"].geschlossen


def test_abgelaufene_frist_sendet_nichts_mehr(netz: dict[str, Any]) -> None:
    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=30)
    verbindung._frist_ende = time.monotonic() - 1

    with pytest.raises(smtplib.SMTPServerDisconnected):
        verbindung.noop()

    assert netz["leitung"].gesendet == b""
    assert verbindung.sock is None and netz["leitung"].geschlossen


def test_uebertragung_der_mail_zaehlt_nicht_gegen_die_frist(netz: dict[str, Any]) -> None:
    """
    Die Zeit, in der mandari sendet (ein Anhang an einen Server, der langsam abnimmt), verlängert die Frist um genau
    diese Dauer: Die Frist für Aufbau und Antworten bricht die Übertragung nicht ab.
    """
    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=15)
    vorher = verbindung._frist_ende
    assert vorher - time.monotonic() <= BegrenzterSMTP.gesamtfrist
    netz["leitung"].sendedauer = 0.2

    verbindung.send(b"x" * 1024)

    assert 0.15 <= verbindung._frist_ende - vorher < 5
    assert len(netz["leitung"].gesendet) == 1024
    # Der einzelne Sendevorgang wartet weiter höchstens die Zeitgrenze des Sockets
    assert max(netz["leitung"].zeitgrenzen) <= 15


def test_gewoehnliche_mail_mit_anhang_geht_innerhalb_der_frist_raus(netz: dict[str, Any]) -> None:
    """Der ganze Ablauf einer Mail mit 5 MiB Anhang über den begrenzten Client."""
    netz["leitung"] = Leitung(
        GRUSS + b"250-mail.example.org\r\n250 SIZE 52428800\r\n250 ok\r\n250 ok\r\n354 weiter\r\n250 angenommen\r\n"
        b"221 ende\r\n"
    )
    anhang = b"Subject: Einladung\r\n\r\n" + b"QUJD" * (5 * 1024 * 1024 // 4) + b"\r\n"

    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=15)
    verbindung.ehlo("absender.example")
    abgelehnt = verbindung.sendmail("absender@example.org", ["person@example.org"], anhang)
    verbindung.quit()

    assert abgelehnt == {}
    assert anhang in netz["leitung"].gesendet


# --- Geprüfte Adressen -----------------------------------------------------------------------------------------


def test_verbindung_geht_an_die_gepruefte_adresse(netz: dict[str, Any]) -> None:
    BegrenzterSMTP("mail.example.org", 587, timeout=5).close()

    assert netz["ziele"] == [(OEFFENTLICH, 587)]
    assert netz["wartezeiten"] == [5]


def test_alle_geprueften_adressen_werden_der_reihe_nach_versucht(netz: dict[str, Any]) -> None:
    """Server mit IPv6 und IPv4, der Weg über IPv6 ist gestört: Die Verbindung kommt über IPv4 zustande."""
    netz["adressen"] = [[OEFFENTLICH_V6, OEFFENTLICH]]
    netz["gestoert"] = {OEFFENTLICH_V6}

    verbindung = BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert netz["ziele"] == [(OEFFENTLICH_V6, 587), (OEFFENTLICH, 587)], "jede Adresse einmal, in der Reihenfolge"
    assert verbindung.sock is netz["leitung"]


def test_ohne_erreichbare_adresse_scheitert_der_aufbau_mit_dem_letzten_fehler(netz: dict[str, Any]) -> None:
    netz["adressen"] = [[OEFFENTLICH_V6, OEFFENTLICH]]
    netz["gestoert"] = {OEFFENTLICH_V6, OEFFENTLICH}

    with pytest.raises(OSError, match="unreachable"):
        BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert len(netz["ziele"]) == 2


def test_eine_interne_unter_mehreren_adressen_verbindet_gar_nicht(netz: dict[str, Any]) -> None:
    netz["adressen"] = [[OEFFENTLICH, "10.0.0.5"]]

    with pytest.raises(OrgMailError):
        BegrenzterSMTP("mail.example.org", 587, timeout=5)

    assert netz["ziele"] == []


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
