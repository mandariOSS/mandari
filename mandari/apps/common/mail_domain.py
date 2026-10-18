# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Domainname für Message-ID und EHLO beim Mailversand (Issue #957).

Django bildet den Teil hinter dem @ der Message-ID und den Namen, mit dem sich das SMTP-Backend beim
Mailserver meldet (EHLO/HELO), aus ``socket.getfqdn()``. In einem Container ist das die Container-ID,
kein vollständiger Domainname; Spamfilter werten das ab (rspamd ``MID_RHS_NOT_FQDN``).

``apply`` setzt beim Start (``CommonConfig.ready``) in jedem Prozess – Anwendung, Worker,
Verwaltungsbefehle – denselben Namen:

1. ``EMAIL_MESSAGE_ID_DOMAIN``, wenn gesetzt und ein vollständiger Domainname (sonst Warnung ``common.W001``),
2. sonst die Domain von ``DEFAULT_FROM_EMAIL``, wenn ausdrücklich gesetzt – nie der eingebaute Rückfall
   ``DEFAULT_FROM_EMAIL_FALLBACK`` (``noreply@mandari.de``), damit sich keine fremde Installation mit mandari.de
   meldet,
3. sonst der Host aus ``SITE_URL``,
4. liefert keiner davon einen vollständigen Domainnamen: eine IP-Adresse als Adressliteral nach RFC 5321
   (``[192.0.2.10]``, ``[IPv6:2001:db8::1]``), sonst ``localhost``. Ein Rechnername mit Unterstrich oder ohne
   Punkt wäre als EHLO-Name ungültig oder nichtssagend. Die Systemprüfung warnt (``common.W002``), außer bei
   einer rein lokalen Installation (``SITE_URL`` auf ``localhost`` oder Loopback).

Der Name gilt für alle Versandwege, weil alle über Djangos ``EmailMessage`` und dessen SMTP-Backend laufen:
Systemeinstellungen, Umgebung (``SiteSettingsEmailBackend``), eigenes SMTP einer Organisation
(``apps.common.mail_backends.build_backend``). Djangos SMTP-Backend übergibt den Namen beim Verbindungsaufbau
selbst als ``local_hostname``; ein eigenes Backend muss ihn nicht setzen.

Django kennt dafür keine Einstellung. ``DNS_NAME`` (``django.core.mail.utils``) merkt sich den Namen nach der
ersten Abfrage im internen Attribut ``_fqdn``; ``apply`` belegt es vorab. Ändert Django das, schlagen die
Tests in ``apps/common/tests/test_mail_domain.py`` fehl.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from email.utils import parseaddr
from typing import Any, Final
from urllib.parse import urlsplit

from django.conf import settings
from django.core import checks
from django.core.mail.utils import DNS_NAME
from django.utils.encoding import punycode

logger = logging.getLogger(__name__)

#: Ein Label eines Domainnamens (nach Punycode): Buchstaben, Ziffern, Bindestrich, nicht am Rand
_LABEL: Final = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
#: Letzter Ausweg, wenn weder Einstellung noch Absender noch SITE_URL einen Namen liefern
NOTNAME: Final = "localhost"


def normalize(value: str) -> str:
    """Kleingeschrieben, ohne Leerraum und abschließenden Punkt, Umlautdomains als Punycode; ungültig → ``""``."""
    name = (value or "").strip().rstrip(".").lower()
    if not name:
        return ""
    try:
        return str(punycode(name))
    except UnicodeError:
        return ""


def is_fqdn(name: str) -> bool:
    """Vollständiger Domainname: mindestens zwei Labels, keine IP-Adresse, höchstens 253 Zeichen."""
    if not name or len(name) > 253 or "." not in name:
        return False
    labels = name.split(".")
    return all(_LABEL.match(label) for label in labels) and not labels[-1].isdigit()


def domain_of(address: str) -> str:
    """Domain einer Adresse wie ``noreply@example.org`` oder ``Name <noreply@example.org>``; sonst ``""``."""
    _name, addr = parseaddr(address or "")
    return normalize(addr.rpartition("@")[2]) if "@" in addr else ""


def address_literal(value: str) -> str:
    """IP-Adresse als Adressliteral nach RFC 5321 (``[192.0.2.10]``, ``[IPv6:2001:db8::1]``); sonst ``""``.

    Gilt so auch in der Message-ID (RFC 5322 ``no-fold-literal``). Eine Zonenangabe (``%eth0``) entfällt.
    """
    roh = (value or "").strip().removeprefix("[").removesuffix("]")
    try:
        adresse = ipaddress.ip_address(roh)
    except ValueError:
        return ""
    if adresse.version == 6:
        return f"[IPv6:{ipaddress.IPv6Address(int(adresse)).compressed}]"
    return f"[{adresse}]"


def sender_domain() -> str:
    """Domain von ``DEFAULT_FROM_EMAIL``, wenn ausdrücklich gesetzt; beim eingebauten Rückfall ``""``."""
    absender = str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
    rueckfall = str(getattr(settings, "DEFAULT_FROM_EMAIL_FALLBACK", "") or "").strip()
    if rueckfall and absender.lower() == rueckfall.lower():
        return ""
    return domain_of(absender)


def site_host() -> str:
    """Host aus ``SITE_URL`` (ohne Port und Klammern), unverändert; ``""`` ohne Host."""
    return urlsplit(str(getattr(settings, "SITE_URL", "") or "")).hostname or ""


def explicit_domain() -> str:
    """``EMAIL_MESSAGE_ID_DOMAIN`` normalisiert (leer, wenn nicht gesetzt oder nicht darstellbar)."""
    return normalize(str(getattr(settings, "EMAIL_MESSAGE_ID_DOMAIN", "") or ""))


def derived_domain() -> str:
    """Name ohne Einstellung: Domain eines ausdrücklich gesetzten ``DEFAULT_FROM_EMAIL``, sonst Host aus ``SITE_URL``.

    Ist keiner von beiden ein vollständiger Domainname, gilt eine IP-Adresse als Adressliteral, sonst
    ``localhost`` (die Systemprüfung ``common.W002`` meldet das) – nie der Rechnername des Containers und nie
    ein ungültiger EHLO-Name wie ``mandari_web``.
    """
    kandidaten = [sender_domain(), site_host()]
    for kandidat in kandidaten:
        name = normalize(kandidat)
        if is_fqdn(name):
            return name
    return next((literal for literal in map(address_literal, kandidaten) if literal), NOTNAME)


def configured_domain() -> str:
    """Domain für Message-ID und EHLO: ``EMAIL_MESSAGE_ID_DOMAIN``, wenn gültig, sonst ``derived_domain``."""
    explicit = explicit_domain()
    if is_fqdn(explicit):
        return explicit
    return derived_domain()


def apply() -> str:
    """Setzt den Namen für Djangos Message-ID und EHLO (``DNS_NAME``) und liefert ihn zurück."""
    domain = configured_domain()
    if _explicit_invalid():
        # Kein Abbruch: Die Systemprüfung (common.W001) meldet es, versendet wird mit dem abgeleiteten Namen
        logger.warning("EMAIL_MESSAGE_ID_DOMAIN ist kein vollständiger Domainname; verwendet wird %s", domain)
    # Internes Attribut von django.core.mail.utils.CachedDnsName (Django 6.1), siehe Moduldokumentation
    DNS_NAME._fqdn = domain
    return domain


def _explicit_invalid() -> bool:
    roh = str(getattr(settings, "EMAIL_MESSAGE_ID_DOMAIN", "") or "").strip()
    return bool(roh) and not is_fqdn(explicit_domain())


def _local_site() -> bool:
    """``SITE_URL`` zeigt auf ``localhost`` oder eine Loopback-Adresse (Entwicklung, CI)."""
    host = site_host().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@checks.register(checks.Tags.compatibility)
def check_message_id_domain(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    """Warnt, wenn ``EMAIL_MESSAGE_ID_DOMAIN`` ungültig ist oder kein vollständiger Domainname gilt.

    Bewusst nur Warnungen: Fehler der Systemprüfung hielten jeden Verwaltungsbefehl an, auch die Worker.
    """
    meldungen: list[checks.CheckMessage] = []
    domain = configured_domain()
    if _explicit_invalid():
        meldungen.append(
            checks.Warning(
                f"EMAIL_MESSAGE_ID_DOMAIN ist kein vollständiger Domainname; verwendet wird „{domain}“.",
                hint="Einen Namen wie example.org eintragen (mit Punkt, ohne @, keine IP-Adresse) oder leeren.",
                id="common.W001",
            )
        )
    # Rein lokal (Entwicklung, CI) ist localhost erwartet; einen ungültigen Wert der Einstellung meldet W001
    if not is_fqdn(domain) and not _local_site():
        meldungen.append(
            checks.Warning(
                f"Message-ID und EHLO verwenden „{domain}“, keinen vollständigen Domainnamen; Spamfilter werten das ab.",
                hint=(
                    "EMAIL_MESSAGE_ID_DOMAIN setzen, DEFAULT_FROM_EMAIL mit vollständiger Domain eintragen oder "
                    "SITE_URL mit Domainnamen statt IP-Adresse."
                ),
                id="common.W002",
            )
        )
    return meldungen
