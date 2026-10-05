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

import logging

logger = logging.getLogger(__name__)


class OrgMailError(Exception):
    """Versand über das organisationseigene SMTP ist fehlgeschlagen (ohne Fallback)."""


#: Mail-Einlieferungsports, die das eigene SMTP nutzen darf (SMTP, SMTPS, Submission, alternativ)
SMTP_PORTS = (25, 465, 587, 2525)


def check_smtp_server(host: str, port: int) -> None:
    """
    Der eigene SMTP-Server muss öffentlich erreichbar sein und einen Mail-Port nutzen.

    Wirft ``OrgMailError`` für andere Ports sowie für IP-Adressen – direkt angegeben oder
    aufgelöst – in privaten, Loopback-, Link-Local-, reservierten oder Multicast-Netzen.
    Nicht auflösbare Namen bleiben erlaubt; der Versand scheitert dann ohnehin.
    """
    import ipaddress
    import socket

    if port not in SMTP_PORTS:
        raise OrgMailError("Der SMTP-Port ist nicht zulässig.")
    name = (host or "").strip().strip("[]")
    if not name:
        return
    try:
        addresses = [ipaddress.ip_address(name)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(name, None)
        except (OSError, UnicodeError):
            return
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise OrgMailError("Der SMTP-Server liegt in einem internen Netz.")
