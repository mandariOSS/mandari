# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigener SMTP-Versand: Zugangsdaten gehen nur an den Server, für den sie eingegeben wurden.

- Ändert sich Server, Port oder Benutzer ohne neues Passwort, verfällt das gespeicherte Passwort.
- Server in internen Netzen (privat, Loopback, Link-Local, reserviert) sind nicht möglich –
  weder beim Speichern noch beim Versand; erlaubt sind die Mail-Ports 25, 465, 587 und 2525.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from apps.common.org_email import OrgMailError, get_organization_connection
from apps.work.organization import services
from apps.work.organization.services import ServiceError


def _daten(**felder: Any) -> services.EmailSettingsInput:
    werte: dict[str, Any] = {
        "mail_sender_mode": "smtp",
        "smtp_fallback_to_mandari": False,
        "smtp_host": "mail.example.org",
        "smtp_port_raw": "587",
        "smtp_username": "fraktion",
        "smtp_use_tls": True,
        "smtp_from_email": "info@example.org",
        "smtp_from_name": "Fraktion",
        "smtp_password": "",
        "smtp_password_clear": False,
    }
    werte.update(felder)
    return services.EmailSettingsInput(**werte)


@pytest.fixture
def oeffentliche_aufloesung(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """DNS ohne Netz: Namen auf feste Adressen abbilden."""
    tabelle = {
        "mail.example.org": "93.184.216.34",
        "mail.example.net": "93.184.216.35",
        "intern.example.org": "10.0.0.5",
    }

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        if host not in tabelle:
            raise socket.gaierror("unbekannt")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (tabelle[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return tabelle


@pytest.mark.django_db
@pytest.mark.parametrize(
    "aenderung",
    [{"smtp_host": "mail.example.net"}, {"smtp_port_raw": "465"}, {"smtp_username": "jemand-anderes"}],
)
def test_passwort_verfaellt_bei_geaendertem_server(
    org: Any, oeffentliche_aufloesung: dict[str, str], aenderung: dict[str, str]
) -> None:
    services.save_email_settings(org, _daten(smtp_password="geheim-1234"))
    org.refresh_from_db()
    assert org.get_smtp_password() == "geheim-1234"

    services.save_email_settings(org, _daten(**aenderung))

    org.refresh_from_db()
    assert org.get_smtp_password() == ""


@pytest.mark.django_db
def test_passwort_bleibt_bei_unveraenderter_verbindung(org: Any, oeffentliche_aufloesung: dict[str, str]) -> None:
    services.save_email_settings(org, _daten(smtp_password="geheim-1234"))
    services.save_email_settings(org, _daten(smtp_from_name="Neuer Name"))

    org.refresh_from_db()
    assert org.get_smtp_password() == "geheim-1234"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost-literal", "169.254.169.254", "10.1.2.3", "[::1]", "intern.example.org"]
)
def test_interne_server_sind_nicht_moeglich(org: Any, oeffentliche_aufloesung: dict[str, str], host: str) -> None:
    oeffentliche_aufloesung["localhost-literal"] = "127.0.0.1"

    with pytest.raises(ServiceError):
        services.save_email_settings(org, _daten(smtp_host=host, smtp_password="geheim-1234"))

    org.refresh_from_db()
    assert org.smtp_host == ""


@pytest.mark.django_db
@pytest.mark.parametrize("port", ["6379", "22", "99999"])
def test_nur_mail_ports(org: Any, oeffentliche_aufloesung: dict[str, str], port: str) -> None:
    with pytest.raises(ServiceError):
        services.save_email_settings(org, _daten(smtp_port_raw=port))


@pytest.mark.django_db
def test_versand_verbindet_nicht_ins_interne_netz(org: Any, oeffentliche_aufloesung: dict[str, str]) -> None:
    # Bestand aus der Zeit vor der Prüfung
    org.mail_sender_mode = "smtp"
    org.smtp_host = "intern.example.org"
    org.smtp_port = 587
    org.save()

    verbindung: Any = get_organization_connection
    with pytest.raises(OrgMailError):
        verbindung(org)

    org.smtp_host = "mail.example.org"
    org.smtp_port = 6379
    org.save()
    with pytest.raises(OrgMailError):
        verbindung(org)
