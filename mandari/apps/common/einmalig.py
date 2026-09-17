# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Singleton-Schutz für zeitgesteuerte Jobs (#55).

Läuft mandari auf mehreren Servern, kann derselbe Cron- oder Daemon-Job versehentlich
auf zwei Knoten gleichzeitig starten (doppeltes ``worker``-Profil, überlappende Läufe).
``Sperre`` legt dafür einen Schlüssel im gemeinsamen Cache (Redis) an: ``cache.add`` ist
atomar, nur ein Aufrufer bekommt den Zuschlag. Die Sperre verfällt nach ``ttl`` Sekunden
von selbst, falls ein Prozess abstürzt; nach regulärem Ende wird sie sofort freigegeben.

Verwendung im Management-Command::

    class Command(EinmaligMixin, BaseCommand):
        sperre = "check_source_health"
        sperre_ttl = 3600

Ein zweiter Aufruf während der Laufzeit endet mit einer Meldung auf stderr und Exit-Code 0
(nichts zu tun) – Cron-Mails bleiben damit aus. ``--ohne-sperre`` erzwingt den Lauf.

Mit dem lokalen Speicher-Cache (Entwicklung, Tests) schützt die Sperre nur innerhalb eines
Prozesses; der Mehr-Server-Betrieb setzt den Redis-Cache voraus (docs/MEHR_SERVER_BETRIEB.md).
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from typing import Any

from django.core.cache import cache
from django.core.management.base import OutputWrapper

logger = logging.getLogger(__name__)

PRAEFIX = "einmalig:"


class Sperre:
    """Kontextmanager: ``with Sperre("name", ttl=600) as erhalten: ...``."""

    def __init__(self, name: str, ttl: int = 3600) -> None:
        self.name = name
        self.ttl = max(1, int(ttl))
        self.schluessel = f"{PRAEFIX}{name}"
        self.inhaber = f"{socket.gethostname()}:{os.getpid()}"
        self.erhalten = False

    def erwerben(self) -> bool:
        self.erhalten = bool(cache.add(self.schluessel, self.inhaber, self.ttl))
        return self.erhalten

    def verlaengern(self) -> bool:
        """Laufzeit verlängern (lange Daemons); nur, solange wir selbst Inhaber sind."""
        if not self.erhalten or cache.get(self.schluessel) != self.inhaber:
            self.erhalten = False
            return False
        cache.set(self.schluessel, self.inhaber, self.ttl)
        return True

    def freigeben(self) -> None:
        if self.erhalten and cache.get(self.schluessel) == self.inhaber:
            cache.delete(self.schluessel)
        self.erhalten = False

    def andere_instanz(self) -> str:
        return str(cache.get(self.schluessel) or "unbekannt")

    def __enter__(self) -> bool:
        return self.erwerben()

    def __exit__(self, *exc: object) -> None:
        self.freigeben()


class EinmaligMixin:
    """Management-Command nur einmal gleichzeitig ausführen (vor ``BaseCommand`` einreihen)."""

    sperre: str = ""
    sperre_ttl: int = 3600

    def create_parser(self, prog_name: str, subcommand: str, **kwargs: Any) -> Any:
        # Nicht über add_arguments: Das überschreiben die meisten Commands ohne super()-Aufruf,
        # und die Option wäre still verschwunden. create_parser bleibt bei BaseCommand.
        parser = super().create_parser(prog_name, subcommand, **kwargs)  # type: ignore[misc]
        parser.add_argument("--ohne-sperre", action="store_true", help="Singleton-Sperre ignorieren (Notfall).")
        return parser

    def execute(self, *args: Any, **options: Any) -> Any:
        name = self.sperre or getattr(self, "_command_name", "") or type(self).__module__.rsplit(".", 1)[-1]
        if options.pop("ohne_sperre", False):
            return super().execute(*args, **options)  # type: ignore[misc]
        sperre = Sperre(name, self.sperre_ttl)
        if not sperre.erwerben():
            meldung = f"{name}: läuft bereits auf {sperre.andere_instanz()} – übersprungen."
            logger.info(meldung)
            # BaseCommand.execute() hat stderr noch nicht gesetzt – direkt aus den Optionen nehmen
            OutputWrapper(options.get("stderr") or sys.stderr).write(meldung)
            return None
        self.sperre_objekt = sperre
        try:
            return super().execute(*args, **options)  # type: ignore[misc]
        finally:
            sperre.freigeben()
