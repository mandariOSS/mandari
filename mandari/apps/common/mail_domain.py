# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Domainname für Message-ID und EHLO beim Mailversand (Issue #957).

Django bildet den Teil hinter dem @ der Message-ID und den Namen, mit dem sich das SMTP-Backend beim
Mailserver meldet (EHLO/HELO), aus ``socket.getfqdn()``. In einem Container ist das die Container-ID,
kein vollständiger Domainname; Spamfilter werten das ab (rspamd ``MID_RHS_NOT_FQDN``).

``apply`` setzt beim Start (``CommonConfig.ready``) in jedem Prozess – Anwendung, Worker,
Verwaltungsbefehle – denselben Namen:

1. ``EMAIL_MESSAGE_ID_DOMAIN``, wenn gesetzt und ein vollständiger Domainname (sonst Warnung ``common.W001``),
2. sonst die Domain von ``DEFAULT_FROM_EMAIL``,
3. sonst der Host aus ``SITE_URL`` (ist auch der kein vollständiger Domainname: Warnung ``common.W002``).

Der Name gilt für alle Versandwege, weil alle über Djangos ``EmailMessage`` und dessen SMTP-Backend laufen:
Systemeinstellungen, Umgebung (``SiteSettingsEmailBackend``), eigenes SMTP einer Organisation
(``apps.common.mail_backends.build_backend``). Djangos SMTP-Backend übergibt den Namen beim Verbindungsaufbau
selbst als ``local_hostname``; ein eigenes Backend muss ihn nicht setzen.

Django kennt dafür keine Einstellung. ``DNS_NAME`` (``django.core.mail.utils``) merkt sich den Namen nach der
ersten Abfrage im internen Attribut ``_fqdn``; ``apply`` belegt es vorab. Ändert Django das, schlagen die
Tests in ``apps/common/tests/test_mail_domain.py`` fehl.
"""

from __future__ import annotations

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


def explicit_domain() -> str:
    """``EMAIL_MESSAGE_ID_DOMAIN`` normalisiert (leer, wenn nicht gesetzt oder nicht darstellbar)."""
    return normalize(str(getattr(settings, "EMAIL_MESSAGE_ID_DOMAIN", "") or ""))


def derived_domain() -> str:
    """Name ohne Einstellung: Domain von ``DEFAULT_FROM_EMAIL``, sonst Host aus ``SITE_URL``.

    Ist keiner von beiden ein vollständiger Domainname, gilt der erste vorhandene (die Systemprüfung
    ``common.W002`` meldet das), zuletzt ``localhost`` – nie der Rechnername des Containers.
    """
    kandidaten = [
        domain_of(str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "")),
        normalize(urlsplit(str(getattr(settings, "SITE_URL", "") or "")).hostname or ""),
    ]
    for kandidat in kandidaten:
        if is_fqdn(kandidat):
            return kandidat
    return next((kandidat for kandidat in kandidaten if kandidat), NOTNAME)


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
    if not is_fqdn(domain):
        meldungen.append(
            checks.Warning(
                f"Message-ID und EHLO verwenden „{domain}“, keinen vollständigen Domainnamen; Spamfilter werten das ab.",
                hint="EMAIL_MESSAGE_ID_DOMAIN setzen oder DEFAULT_FROM_EMAIL mit vollständiger Domain eintragen.",
                id="common.W002",
            )
        )
    return meldungen
