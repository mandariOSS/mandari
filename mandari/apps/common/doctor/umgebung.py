# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abgleich der Umgebungsvariablen für ``manage.py doctor`` (Issue #964).

Zwei Prüfungen:

- ``umgebung``: Die Pflichtvariablen sind im Prozess angekommen, keine trägt noch den Platzhalter aus
  ``.env.example``, der Hauptschlüssel ist ein gültiger Schlüssel.
- ``umgebung-datei``: Mit ``--env-datei`` (auch ``-`` für die Standardeingabe) jede Zuweisung der ``.env`` gegen den
  Prozess. Im Container gibt es die Datei nicht, deshalb kommt sie beim Aufruf mit. Gemeldet werden nur Namen.

Die Listen hier gleicht ``apps/common/tests/test_doctor.py`` mit ``.env.example`` und ``docker-compose.yml`` ab
(dieselben Hilfen wie ``test_umgebung_compose.py``): Wer dort etwas ändert, erfährt es im Test.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlsplit

from django.conf import settings

from .kern import Befund, Kontext, fehler, ok, pruefung, uebersprungen, warnung

#: Ohne sie läuft keine Installation richtig. ``docker-compose.yml`` setzt alle fünf für die Anwendung und die
#: Worker: Schlüssel aus der ``.env``, Adressen von Datenbank, Redis und Website aus deren Angaben.
PFLICHT: Final = ("SECRET_KEY", "ENCRYPTION_MASTER_KEY", "DATABASE_URL", "REDIS_URL", "SITE_URL")

#: So beginnen die Platzhalter der Pflichtwerte in ``.env.example``
PLATZHALTER_BEGINN: Final = "CHANGE_ME"

#: Variablen aus ``.env.example``, deren Wert ``docker-compose.yml`` im Container selbst festlegt (Block
#: „Entwicklung“) oder je Dienst anders setzt (``worker-heavy``): Hier darf der Prozess von der Datei abweichen.
SETZT_COMPOSE_SELBST: Final = frozenset(
    {"DEBUG", "DATABASE_URL", "REDIS_URL", "ELASTICSEARCH_URL", "OCR_MEMORY_LIMIT_MB"}
)

#: Namen, deren Wert als geheim gilt (zweites Netz der Ausgabe, ``kern.schwaerze``)
_GEHEIMER_NAME: Final = re.compile(r"PASSWORD|PASSWORT|PASSPHRASE|SECRET|TOKEN|CREDENTIAL|SCHLUESSEL|(?:^|_)KEY(?:_|$)")
#: Nur solche Namen aus ``--env-datei`` erscheinen in Meldungen; alles andere könnte ein verrutschter Wert sein
_NAME: Final = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
#: So viele Namen nennt eine Meldung höchstens
_HOECHSTENS: Final = 20
#: Größte ``--env-datei`` in Zeichen
ENV_DATEI_MAX: Final = 1_000_000


def ist_platzhalter(wert: str) -> bool:
    return wert.strip().startswith(PLATZHALTER_BEGINN)


def _kennwort_aus_adresse(wert: str) -> str:
    """Kennwort einer Adresse der Form ``schema://name:kennwort@host``; sonst leer."""
    if "://" not in wert or "@" not in wert:
        return ""
    try:
        return urlsplit(wert).password or ""
    except ValueError:
        return ""


def geheime_werte(*quellen: Mapping[str, str]) -> frozenset[str]:
    """
    Werte, die in keiner Ausgabe stehen dürfen: Variablen mit geheimem Namen und Kennwörter aus Adressen
    (``DATABASE_URL``, ``REDIS_URL``), aus den genannten Quellen und aus den geladenen Einstellungen.
    """
    werte: set[str] = set()
    for quelle in quellen:
        for name, wert in quelle.items():
            if not wert:
                continue
            if _GEHEIMER_NAME.search(name.upper()):
                werte.add(wert)
            werte.add(_kennwort_aus_adresse(wert))
    werte.add(str(getattr(settings, "SECRET_KEY", "") or ""))
    for datenbank in settings.DATABASES.values():
        werte.add(str(datenbank.get("PASSWORD") or ""))
    werte.add(str(dict(getattr(settings, "SMTP_FALLBACK", {}) or {}).get("password") or ""))
    werte.discard("")
    return frozenset(werte)


def lies_env_datei(text: str) -> dict[str, str]:
    """
    Zuweisungen einer ``.env`` (Name → Wert) wie python-dotenv sie liest, das auch die Anwendung außerhalb der
    Container nutzt. Zeilen ohne Zuweisung, leere Werte (leer heißt Vorgabe) und Namen, die keine Variablennamen
    sind, entfallen.
    """
    from dotenv import dotenv_values

    roh = dotenv_values(stream=io.StringIO(text[:ENV_DATEI_MAX]), interpolate=True)
    return {name: wert for name, wert in roh.items() if wert and _NAME.fullmatch(name)}


def _namen(namen: list[str]) -> str:
    """Aufzählung für eine Meldung, gekürzt nach ``_HOECHSTENS`` Namen."""
    sortiert = sorted(namen)
    rest = len(sortiert) - _HOECHSTENS
    return ", ".join(sortiert[:_HOECHSTENS]) + (f" und {rest} weitere" if rest > 0 else "")


@pruefung("umgebung", "Umgebung: Pflichtvariablen", rang=10)
def pruefe_pflichtvariablen(kontext: Kontext) -> Befund:
    from apps.common.encryption import decode_master_key

    fehlt = [name for name in PFLICHT if not os.environ.get(name, "").strip()]
    platzhalter = [name for name in PFLICHT if ist_platzhalter(os.environ.get(name, ""))]
    ungueltig: list[str] = []
    hauptschluessel = os.environ.get("ENCRYPTION_MASTER_KEY", "").strip()
    if hauptschluessel and not ist_platzhalter(hauptschluessel):
        try:
            decode_master_key(hauptschluessel)
        except ValueError:
            ungueltig.append("ENCRYPTION_MASTER_KEY")

    teile: list[str] = []
    if fehlt:
        teile.append(f"nicht gesetzt: {_namen(fehlt)}")
    if platzhalter:
        teile.append(f"noch der Platzhalter aus .env.example: {_namen(platzhalter)}")
    if ungueltig:
        teile.append("ENCRYPTION_MASTER_KEY ist kein Schlüssel (Base64 von genau 32 Byte)")
    if not teile:
        return ok(f"{len(PFLICHT)} von {len(PFLICHT)} gesetzt")
    meldung = "; ".join(teile)
    hinweis = (
        "SECRET_KEY und ENCRYPTION_MASTER_KEY stehen in der .env (erzeugen: .env.example, „Generating Secure "
        "Values“; einen benutzten ENCRYPTION_MASTER_KEY nie ersetzen). DATABASE_URL, REDIS_URL und SITE_URL setzt "
        "docker-compose.yml aus POSTGRES_*, REDIS_PASSWORD und DOMAIN. Danach: docker compose up -d."
    )
    if settings.DEBUG and not platzhalter and not ungueltig:
        # Entwicklung: Für nicht gesetzte Werte gelten die Vorgaben aus settings.py (lokale Datenbank, Cache im Prozess)
        return warnung(meldung, hinweis)
    return fehler(meldung, hinweis)


@pruefung("umgebung-datei", "Umgebung: Abgleich mit der .env", rang=11)
def pruefe_env_datei(kontext: Kontext) -> Befund:
    if kontext.env_datei is None:
        return uebersprungen(
            "ohne --env-datei kein Abgleich",
            "docker compose exec -T mandari python manage.py doctor --env-datei - < .env",
        )
    datei = kontext.env_datei
    if not datei:
        return warnung("die Datei enthält keine Zuweisung mit Wert")
    fehlt = [name for name in datei if name not in os.environ]
    weicht_ab = [
        name
        for name, wert in datei.items()
        if name in os.environ and name not in SETZT_COMPOSE_SELBST and os.environ[name] != wert
    ]
    if fehlt:
        meldung = f"kommt im Prozess nicht an: {_namen(fehlt)}"
        if weicht_ab:
            meldung += f"; anderer Wert als in der Datei: {_namen(weicht_ab)}"
        return fehler(
            meldung,
            "Der Dienst liest die .env nicht (env_file: .env in docker-compose.yml, seit Issue #962 für mandari "
            "und die Worker) oder wurde seit der Änderung nicht neu erstellt: docker compose up -d.",
        )
    if weicht_ab:
        return warnung(
            f"anderer Wert als in der Datei: {_namen(weicht_ab)}",
            "Nach einer Änderung der .env die Dienste neu erstellen (docker compose up -d); ein restart liest die "
            "Datei nicht neu. Sonderzeichen wie $ im Wert in einfache Anführungszeichen setzen.",
        )
    return ok(f"{len(datei)} Zuweisungen der Datei sind im Prozess angekommen")
