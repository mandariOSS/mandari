# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft, dass die Installation mit ihrem Profil läuft und kein Code ein abgeschaltetes Modul erreicht (Issue #1594).

    MODUL_WORK=aus python manage.py installationsprofil_pruefen

Die CI ruft den Befehl je Profil nach ``migrate`` auf (Job „Installationsprofile“). Geprüft wird gegen die migrierte
Datenbank, geändert wird nichts außer Sitzungen der Probeabrufe:

1. Adressen (HTTP und WebSocket): keine setzt ein abgeschaltetes Modul voraus (wie die Systemprüfung).
2. Zeitpläne und Abonnements: keiner bzw. keines eines abgeschalteten Moduls ist registriert.
3. Probeabrufe: gemeinsame Seiten (Gesundheit, Anmeldung, Schnittstelle, OParl, robots.txt) und die Probeadresse
   jedes eingeschalteten Moduls antworten ohne Fehler, die jedes abgeschalteten Moduls mit 404. Keine
   abgerufene Seite verweist auf eine Adresse eines abgeschalteten Moduls, und der Einstieg der Schnittstelle nennt
   keinen Bereich eines abgeschalteten Moduls.
4. Danach ist kein Einstieg (Adressen, Ansichten, WebSocket, Zeitpläne, Abonnements, Befehle, Schnittstellen)
   eines abgeschalteten Moduls geladen.

Beendet sich mit 1, sobald es einen Befund gibt.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.http import HttpResponseBase
from django.test import Client
from django.urls import Resolver404, resolve
from django.utils.module_loading import import_string

from apps.common import module
from apps.common.module import Befund
from apps.events import registry, schedule

#: Seiten, die jedes Profil hat, mit den erlaubten Statuscodes
GEMEINSAME_SEITEN: tuple[tuple[str, frozenset[int]], ...] = (
    ("/health/live/", frozenset({200})),
    ("/accounts/login/", frozenset({200})),
    ("/api/", frozenset({200})),
    ("/oparl/", frozenset({200})),
    ("/robots.txt", frozenset({200})),
)
#: Ein eingeschaltetes Modul antwortet beim Probeabruf mit einer Seite oder leitet weiter (etwa zur Anmeldung)
EINSTIEG_OK: frozenset[int] = frozenset({200, 301, 302})
#: Interne Verweise in Seiten (href, src, action) und in JSON (Werte, die mit / beginnen)
_VERWEIS = re.compile(r"""(?:href|src|action)\s*=\s*["'](/[^"'#?\s]*)""")


def _host() -> str:
    """Ein Host, den ``ALLOWED_HOSTS`` zulässt (ohne Platzhalter)."""
    for host in settings.ALLOWED_HOSTS:
        if host and host != "*" and not host.startswith("."):
            return str(host)
    return "localhost"


def _verweise(inhalt: str) -> set[str]:
    return set(_VERWEIS.findall(inhalt))


def _verweis_befunde(seite: str, verweise: Iterable[str]) -> list[Befund]:
    """Verweise auf Adressen abgeschalteter Module, die es in diesem Profil nicht gibt."""
    befunde: list[Befund] = []
    for verweis in sorted(verweise):
        for modul in module.abgeschaltete():
            if not verweis.lstrip("/").startswith(modul.pfade):
                continue
            try:
                resolve(verweis)
            except Resolver404:
                befunde.append(Befund(modul.kennung, "Verweis", f"{verweis} auf {seite}"))
    return befunde


def _bereiche_befunde(antwort: HttpResponseBase) -> list[Befund | str]:
    """Bereiche im Einstieg der Schnittstelle (``/api/``), deren Einstieg einem abgeschalteten Modul gehört."""
    try:
        daten: Any = json.loads(getattr(antwort, "content", b""))
    except ValueError:
        return ["Einstieg der Schnittstelle /api/ liefert kein JSON"]
    befunde: list[Befund | str] = []
    for bereich in daten.get("areas", []) if isinstance(daten, dict) else []:
        einstieg = urlsplit(str(bereich.get("entry", ""))).path if isinstance(bereich, dict) else ""
        for modul in module.abgeschaltete():
            if einstieg.lstrip("/").startswith(modul.pfade):
                befunde.append(Befund(modul.kennung, "Bereich der Schnittstelle", einstieg))
    return befunde


class Command(BaseCommand):
    help = (
        "Prüft das Installationsprofil (MODUL_SESSION, MODUL_WORK, MODUL_PORTAL): Adressen, Zeitpläne, Abonnements, "
        "Probeabrufe und geladene Einstiege. Exit-Code 1, sobald ein abgeschaltetes Modul erreichbar ist."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        aktiv = sorted(module.aktive())
        aus = [modul.kennung for modul in module.abgeschaltete()]
        self.stdout.write(f"Profil: eingeschaltet {', '.join(aktiv) or '–'}; abgeschaltet {', '.join(aus) or '–'}")

        befunde: list[Befund | str] = []
        befunde += module.adressen_pruefen()
        befunde += module.adressen_pruefen(import_string(module.WEBSOCKET_ROUTEN)(), art="WebSocket")
        schedule.autodiscover()
        befunde += module.registrierte_pruefen(
            "Zeitplan", ((eintrag.name, eintrag.task.module_path) for eintrag in schedule.registry)
        )
        befunde += module.registrierte_pruefen(
            "Abonnement", ((spec.name, module.code_von(spec.handler)) for spec in registry.load_subscribers())
        )
        befunde += self._probeabrufe()
        befunde += module.einstiege_geladen(sys.modules)

        if befunde:
            for befund in befunde:
                self.stderr.write(f"  {befund}")
            raise CommandError(f"{len(befunde)} Befund(e): Das Profil erreicht abgeschaltete Module.")
        self.stdout.write(self.style.SUCCESS("Profil in Ordnung: kein abgeschaltetes Modul erreichbar."))

    def _abruf(self, client: Client, pfad: str) -> HttpResponseBase | str:
        """Antwort oder Beschreibung der Ausnahme, die die Seite geworfen hat."""
        try:
            antwort: HttpResponseBase = client.get(pfad, secure=True)
        except Exception as fehler:  # noqa: BLE001 – jeder Fehler einer Seite ist ein Befund, nicht der Abbruch
            return f"Seite {pfad}: {type(fehler).__name__}: {fehler}"
        return antwort

    def _probeabrufe(self) -> list[Befund | str]:
        client = Client(HTTP_HOST=_host())
        befunde: list[Befund | str] = []
        seiten = [(pfad, erlaubt) for pfad, erlaubt in GEMEINSAME_SEITEN]
        seiten += [(modul.probe, EINSTIEG_OK) for modul in module.MODULE.values() if module.aktiv(modul.kennung)]
        seiten += [(modul.probe, frozenset({404})) for modul in module.abgeschaltete()]
        for pfad, erlaubt in seiten:
            antwort = self._abruf(client, pfad)
            if isinstance(antwort, str):
                befunde.append(antwort)
                continue
            self.stdout.write(f"  GET {pfad} -> {antwort.status_code}")
            if antwort.status_code not in erlaubt:
                befunde.append(f"Seite {pfad}: Status {antwort.status_code}, erwartet {sorted(erlaubt)}")
            inhalt = getattr(antwort, "content", b"").decode("utf-8", errors="replace")
            befunde += _verweis_befunde(pfad, _verweise(inhalt))
            if pfad == "/api/":
                befunde += _bereiche_befunde(antwort)
        return befunde
