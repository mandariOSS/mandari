# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organisationseigener E-Mail-Versand (Issue #65): Prüfung der Einstellungen.

Die Organisation entscheidet in den Einstellungen, ob ihre Mails (Einladungen, Erinnerungen,
Freigabe-Hinweise, Benachrichtigungen) über das eigene SMTP (``mail_sender_mode = "smtp"``) oder den
mandari-Standardversand laufen. Den Weg wählt der Mail-Dienst (``apps.common.mail.config.resolve``);
hier stehen die Grenzen für das eigene SMTP und der Fehler, wenn es ohne Ersatzweg scheitert
(``smtp_fallback_to_mandari``).

SPF/DKIM für die eigene Absender-Domain verantwortet die Organisation —
darauf weist die Einstellungs-UI ausdrücklich hin.
"""

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)


class OrgMailError(Exception):
    """Versand über das organisationseigene SMTP ist fehlgeschlagen (ohne Fallback)."""


#: Mail-Einlieferungsports, die das eigene SMTP nutzen darf (SMTP, SMTPS, Submission, alternativ)
SMTP_PORTS = (25, 465, 587, 2525)


def _oeffentlich(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Liegt die Adresse im öffentlichen Netz (nicht privat, Loopback, Link-Local, reserviert, Multicast)?"""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _adressen(name: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Adressen eines Servers: die angegebene IP-Adresse oder alle, auf die der Name auflöst (``OSError`` ohne)."""
    try:
        return [ipaddress.ip_address(name)]
    except ValueError:
        infos = socket.getaddrinfo(name, None)
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


def check_smtp_server(host: str, port: int) -> None:
    """
    Der eigene SMTP-Server muss öffentlich erreichbar sein und einen Mail-Port nutzen.

    Wirft ``OrgMailError`` für andere Ports sowie für IP-Adressen – direkt angegeben oder
    aufgelöst – in privaten, Loopback-, Link-Local-, reservierten oder Multicast-Netzen.
    Nicht auflösbare Namen bleiben erlaubt; der Versand scheitert dann ohnehin.
    """
    if port not in SMTP_PORTS:
        raise OrgMailError("Der SMTP-Port ist nicht zulässig.")
    name = (host or "").strip().strip("[]")
    if not name:
        return
    try:
        addresses = _adressen(name)
    except (OSError, UnicodeError):
        return
    if not all(_oeffentlich(address) for address in addresses):
        raise OrgMailError("Der SMTP-Server liegt in einem internen Netz.")


def public_address(host: str) -> str:
    """
    Die Adresse, zu der die Verbindung zum eigenen SMTP-Server aufgebaut wird.

    Der Name wird hier aufgelöst und geprüft, und die Verbindung geht genau an die geprüfte Adresse. Löste der
    Verbindungsaufbau den Namen ein zweites Mal auf, könnte die zweite Antwort eine andere sein als die geprüfte.
    Wirft ``OrgMailError`` für interne Adressen und ``OSError`` für Namen, die sich nicht auflösen lassen.
    """
    name = (host or "").strip().strip("[]")
    addresses = _adressen(name)
    if not addresses or not all(_oeffentlich(address) for address in addresses):
        raise OrgMailError("Der SMTP-Server liegt in einem internen Netz.")
    return str(addresses[0])
