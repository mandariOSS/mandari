# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Feste Kennungen und Termine der Demo-Umgebung (gemeinsam für ``setup_demo_environment`` und ``setup_demo_work``).

Die Basisdemo (``apps/common/management/commands/setup_demo_environment.py``) legt Kommune, Work-Organisation und
Session-Mandanten an; die Work-Inhalte für das neue Design ergänzt ``setup_demo_work`` im Work-Modul (Issue #884).
Beide brauchen dieselben Kennungen und dieselbe Terminregel. Sie stehen hier, in der Plattform, damit das
Work-Modul sie nutzen kann, ohne den Aufbau des Sitzungsdienstes (Session) mitzuziehen – die Fachmodule kennen
sich nicht (Schichtenmodell, ``[tool.importlinter]``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dt_time

from django.utils import timezone

#: Präfix aller Demo-Kennungen im RIS-Bestand (kollidiert nie mit echten OParl-Quellen)
DEMO_OPARL_PREFIX = "https://demo.mandari.invalid/oparl"
DEMO_ORG_SLUG = "musterfraktion-demo"
DEMO_EMAIL_DOMAIN = "demo.mandari.de"
#: Anträge der Musterfraktion (natürliche Schlüssel in Work). Den Trinkwasser-Antrag hat die Verwaltung in eine
#: Vorlage umgewandelt und auf die kommende Ratssitzung gesetzt (durchgehende Vorführung Work → Session).
DEMO_ANTRAG_TRINKBRUNNEN = "Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt (Demo)"
DEMO_ANTRAG_SITZBAENKE = "Antrag: Mehr Sitzgelegenheiten in der Innenstadt (Demo)"

DEMO_USERS = {
    "vorsitz": {
        "email": f"demo-vorsitz@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Vera",
        "last_name": "Beispiel",
    },
    "mitglied": {
        "email": f"demo-mitglied@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Martin",
        "last_name": "Muster",
    },
    "gast": {
        "email": f"demo-gast@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Greta",
        "last_name": "Gast",
    },
    # Weitere Work-Rollen für die Prüfung des neuen Designs (Issue #884): sachkundige Bürgerin und ein
    # Fraktionsmitglied, das noch nicht vereidigt ist (sieht keine nicht-öffentlichen Inhalte)
    "sachkundig": {
        "email": f"demo-sachkundig@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Sofia",
        "last_name": "Sachkundig",
    },
    "unvereidigt": {
        "email": f"demo-unvereidigt@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Nora",
        "last_name": "Neu",
    },
    "verwaltung": {
        "email": f"demo-verwaltung@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Victor",
        "last_name": "Verwaltung",
    },
    # Weitere Verwaltungsnutzer, um die Rollen des Session-RIS erlebbar zu
    # machen (Sachbearbeitung, Protokollführung, reiner Lesezugriff).
    "sachbearbeitung": {
        "email": f"demo-sachbearbeitung@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Sabine",
        "last_name": "Sachbearbeitung",
    },
    "protokoll": {
        "email": f"demo-protokoll@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Paul",
        "last_name": "Protokoll",
    },
    "lesezugriff": {
        "email": f"demo-lesezugriff@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Lena",
        "last_name": "Lesezugriff",
    },
}


def demo_ext(kind: str, key: str) -> str:
    """Deterministische Demo-Kennung im RIS-Bestand (``external_id``)."""
    return f"{DEMO_OPARL_PREFIX}/{kind}/{key}"


def ostersonntag(jahr: int) -> date:
    """Ostersonntag (gregorianisch, Gaußsche Osterformel in der Fassung von Meeus)."""
    a = jahr % 19
    b, c = divmod(jahr, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    wochentag = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * wochentag) // 451
    monat, tag = divmod(h + wochentag - 7 * m + 114, 31)
    return date(jahr, monat, tag + 1)


def sitzungsfreie_tage(jahr: int) -> set[date]:
    """Bundesweite gesetzliche Feiertage sowie Heiligabend und Silvester – an diesen Tagen tagt die Demo nicht."""
    ostern = ostersonntag(jahr)
    return {
        date(jahr, 1, 1),
        ostern - timedelta(days=2),  # Karfreitag
        ostern + timedelta(days=1),  # Ostermontag
        date(jahr, 5, 1),
        ostern + timedelta(days=39),  # Christi Himmelfahrt
        ostern + timedelta(days=50),  # Pfingstmontag
        date(jahr, 10, 3),
        date(jahr, 12, 24),
        date(jahr, 12, 25),
        date(jahr, 12, 26),
        date(jahr, 12, 31),
    }


def ist_sitzungstag(tag: date) -> bool:
    """Montag bis Freitag und kein sitzungsfreier Tag."""
    return tag.weekday() < 5 and tag not in sitzungsfreie_tage(tag.year)


def termin_am_sitzungstag(bezugstag: date, tage: int, stunde: int = 17) -> datetime:
    """
    Termin ``tage`` Tage nach (negativ: vor) ``bezugstag`` um ``stunde`` Uhr Ortszeit, immer an einem Sitzungstag.

    Fällt der Tag auf ein Wochenende oder einen Feiertag, rückt ein kommender Termin auf den nächsten, ein
    vergangener auf den vorherigen Sitzungstag – kommende Sitzungen bleiben kommend, vergangene vergangen.
    """
    tag = bezugstag + timedelta(days=tage)
    schritt = timedelta(days=-1 if tage < 0 else 1)
    while not ist_sitzungstag(tag):
        tag += schritt
    return timezone.make_aware(datetime.combine(tag, dt_time(stunde, 0)))
