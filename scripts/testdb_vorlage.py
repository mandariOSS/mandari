# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migrierte PostgreSQL-Vorlage für die Testdatenbanken der pytest-Worker (Issue #935).

    python scripts/testdb_vorlage.py [--name NAME] [--coverage DATEI]

Legt die Datenbank NAME (Standard: mandari_testvorlage) auf dem Server aus DATABASE_URL neu an, migriert
sie einmal mit den Test-Settings (``mandari.settings_test``, ``migrate --run-syncdb`` wie pytest-django für
jede Testdatenbank) und sperrt sie danach für Verbindungen. Mit ``MANDARI_TEST_DB_VORLAGE=NAME`` legt pytest
jede Testdatenbank als Kopie an (``CREATE DATABASE … TEMPLATE NAME``, siehe ``mandari/conftest.py``), statt
in jedem xdist-Worker alle Migrationen abzuspielen (in der CI rund 150 Sekunden je Worker).

Die Sperre (``ALLOW_CONNECTIONS false``) verhindert, dass eine offene Verbindung zur Vorlage das Kopieren
blockiert ("source database is being accessed by other users").

``--coverage DATEI`` misst das Migrieren mit coverage (dieselben Quellen wie der Job "Test"). Die CI führt
die Daten mit denen der Testteile zusammen; so zählt das Migrieren wie früher, als jeder Worker selbst
migrierte.

Lokal, mit einem PostgreSQL-Server:

    DATABASE_URL=postgresql://nutzer:passwort@localhost:5432/mandari_test python scripts/testdb_vorlage.py
    cd mandari && MANDARI_TEST_DB_VORLAGE=mandari_testvorlage DATABASE_URL=… pytest -n auto
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

WURZEL = Path(__file__).resolve().parent.parent
PROJEKT = WURZEL / "mandari"
# wie --cov im Job "Test" (.github/workflows/pr-check.yml)
QUELLEN = "apps,hub,insight_core"


def _mit_datenbank(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--name", default="mandari_testvorlage", help="Name der Vorlage")
    parser.add_argument("--coverage", metavar="DATEI", help="Migrieren mit coverage messen, Daten nach DATEI")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        print("FEHLER: DATABASE_URL muss auf einen PostgreSQL-Server zeigen", file=sys.stderr)
        return 2
    vorlage = sql.Identifier(args.name)
    wartung = _mit_datenbank(url, "postgres")

    with psycopg.connect(wartung, autocommit=True) as verbindung:
        if verbindung.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.name,)).fetchone():
            verbindung.execute(
                sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE false ALLOW_CONNECTIONS true").format(vorlage)
            )
            verbindung.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(vorlage))
        verbindung.execute(sql.SQL("CREATE DATABASE {}").format(vorlage))

    befehl = [sys.executable]
    if args.coverage:
        daten = Path(args.coverage).resolve()
        daten.parent.mkdir(parents=True, exist_ok=True)
        befehl += ["-m", "coverage", "run", f"--data-file={daten}", f"--source={QUELLEN}"]
    befehl += ["manage.py", "migrate", "--run-syncdb", "--noinput", "--verbosity", "0"]
    umgebung = {
        **os.environ,
        "DATABASE_URL": _mit_datenbank(url, args.name),
        "DJANGO_SETTINGS_MODULE": "mandari.settings_test",
    }
    beginn = time.monotonic()
    subprocess.run(befehl, cwd=PROJEKT, env=umgebung, check=True)  # noqa: S603 – fester Aufruf

    with psycopg.connect(wartung, autocommit=True) as verbindung:
        verbindung.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false IS_TEMPLATE true").format(vorlage))
    print(f"Vorlage {args.name} migriert ({time.monotonic() - beginn:.0f} s); MANDARI_TEST_DB_VORLAGE={args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
