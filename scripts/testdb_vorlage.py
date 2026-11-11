# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migrierte PostgreSQL-Vorlage für die Testdatenbanken der pytest-Worker (Issue #935).

    python scripts/testdb_vorlage.py [--name NAME] [--coverage DATEI] [--aus-abbild DATEI] [--abbild-schreiben DATEI]

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

Abbild (Issue #1315): ``--abbild-schreiben DATEI`` legt die migrierte Vorlage zusätzlich als ``pg_dump``-Abbild ab,
daneben ``DATEI.migrations`` mit dem Fingerabdruck der Migrationsdateien (wie die Smoke-Vorlage,
``scripts/_smoke_db.py``). ``--aus-abbild DATEI`` spielt ein solches Abbild ein, statt zu migrieren (rund 10 statt
100 Sekunden), wenn der Fingerabdruck zu den Migrationsdateien des Arbeitsstands passt; fehlt die Datei, passt
der Fingerabdruck nicht oder scheitert das Einspielen, wird wie sonst migriert. Die CI holt das Abbild aus dem
Vorrat, den ``.github/workflows/testvorlage.yml`` bei jeder Änderung der Migrationen auf dev erneuert.

``pg_dump`` und ``pg_restore`` müssen zur Version des Servers passen: Ein Abbild aus ``pg_dump`` 17 lässt sich in
einen Server der Version 16 nicht einspielen. Mit ``MANDARI_PG_CONTAINER=<Container des Servers>`` laufen beide
deshalb im Container des Servers (``docker exec``), in der CI der Dienst-Container des Jobs. Ohne die Variable
laufen die Programme des Rechners.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _smoke_db import FINGERPRINT_SUFFIX, migration_fingerprint  # noqa: E402

WURZEL = Path(__file__).resolve().parent.parent
PROJEKT = WURZEL / "mandari"
# wie --cov im Job "Test" (.github/workflows/pr-check.yml)
QUELLEN = "apps,hub,insight_core"


def _mit_datenbank(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def _fingerabdruck_datei(abbild: Path) -> Path:
    return abbild.with_suffix(abbild.suffix + FINGERPRINT_SUFFIX)


def _abbild_passt(abbild: Path) -> bool:
    """Abbild samt Fingerabdruck vorhanden und aus genau den Migrationsdateien des Arbeitsstands entstanden."""
    fingerabdruck = _fingerabdruck_datei(abbild)
    if not abbild.is_file() or not fingerabdruck.is_file():
        return False
    return fingerabdruck.read_text(encoding="utf-8").strip() == migration_fingerprint(PROJEKT)


def _neu_anlegen(wartung: str, vorlage: sql.Identifier, name: str) -> None:
    with psycopg.connect(wartung, autocommit=True) as verbindung:
        if verbindung.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            verbindung.execute(
                sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE false ALLOW_CONNECTIONS true").format(vorlage)
            )
            verbindung.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(vorlage))
        verbindung.execute(sql.SQL("CREATE DATABASE {}").format(vorlage))


def _pg_befehl(programm: str, optionen: list[str], ziel: str) -> list[str]:
    """Aufruf von ``pg_dump``/``pg_restore`` für die Datenbank ``ziel``: im Container des Servers oder auf dem Rechner.

    Das Abbild geht in beiden Fällen über die Standardein- und -ausgabe, damit es keinen Pfad im Container braucht.
    """
    container = os.environ.get("MANDARI_PG_CONTAINER", "").strip()
    if not container:
        return [programm, *optionen, "--dbname", ziel]
    teile = urlsplit(ziel)
    nutzer = teile.username or "postgres"
    # Im Container über den lokalen Socket, mit dem Namen der Datenbank statt der Adresse des Rechners
    return ["docker", "exec", "-i", container, programm, *optionen, "-U", nutzer, "--dbname", teile.path.lstrip("/")]


def _einspielen(abbild: Path, ziel: str) -> bool:
    """Abbild in die leere Vorlage einspielen; ``False`` bei jedem Fehler (dann migriert der Aufrufer)."""
    befehl = _pg_befehl("pg_restore", ["--no-owner", "--no-acl", "--exit-on-error"], ziel)
    try:
        with abbild.open("rb") as eingabe:
            ergebnis = subprocess.run(befehl, stdin=eingabe, capture_output=True, timeout=300, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as fehler:
        print(f"Abbild nicht einspielbar ({type(fehler).__name__}), migriere stattdessen", file=sys.stderr)
        return False
    if ergebnis.returncode != 0:
        meldung = ergebnis.stderr.decode("utf-8", "replace")[-2000:]
        print(f"pg_restore scheiterte, migriere stattdessen:\n{meldung}", file=sys.stderr)
        return False
    return True


def _abbild_schreiben(abbild: Path, ziel: str) -> None:
    abbild.parent.mkdir(parents=True, exist_ok=True)
    befehl = _pg_befehl("pg_dump", ["--format=custom", "--no-owner", "--no-acl"], ziel)
    with abbild.open("wb") as ausgabe:
        subprocess.run(befehl, stdout=ausgabe, check=True, timeout=300)  # noqa: S603 – fester Aufruf
    _fingerabdruck_datei(abbild).write_text(migration_fingerprint(PROJEKT), encoding="utf-8")
    print(f"Abbild {abbild} ({abbild.stat().st_size / 1024:.0f} KB)")


def _migrieren(coverage: str | None, ziel: str) -> None:
    befehl = [sys.executable]
    if coverage:
        daten = Path(coverage).resolve()
        daten.parent.mkdir(parents=True, exist_ok=True)
        befehl += ["-m", "coverage", "run", f"--data-file={daten}", f"--source={QUELLEN}"]
    befehl += ["manage.py", "migrate", "--run-syncdb", "--noinput", "--verbosity", "0"]
    umgebung = {**os.environ, "DATABASE_URL": ziel, "DJANGO_SETTINGS_MODULE": "mandari.settings_test"}
    subprocess.run(befehl, cwd=PROJEKT, env=umgebung, check=True)  # noqa: S603 – fester Aufruf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--name", default="mandari_testvorlage", help="Name der Vorlage")
    parser.add_argument("--coverage", metavar="DATEI", help="Migrieren mit coverage messen, Daten nach DATEI")
    parser.add_argument("--aus-abbild", metavar="DATEI", help="passendes pg_dump-Abbild einspielen statt zu migrieren")
    parser.add_argument("--abbild-schreiben", metavar="DATEI", help="migrierte Vorlage als pg_dump-Abbild ablegen")
    parser.add_argument(
        "--abbild-pflicht",
        action="store_true",
        help="mit --aus-abbild: scheitern statt zu migrieren (Probe des Abbilds)",
    )
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        print("FEHLER: DATABASE_URL muss auf einen PostgreSQL-Server zeigen", file=sys.stderr)
        return 2
    vorlage = sql.Identifier(args.name)
    wartung = _mit_datenbank(url, "postgres")

    ziel = _mit_datenbank(url, args.name)
    beginn = time.monotonic()
    _neu_anlegen(wartung, vorlage, args.name)

    eingespielt = False
    if args.aus_abbild:
        abbild = Path(args.aus_abbild)
        if _abbild_passt(abbild):
            eingespielt = _einspielen(abbild, ziel)
            if not eingespielt:
                # Ein halb eingespieltes Abbild darf nicht als Vorlage dienen
                _neu_anlegen(wartung, vorlage, args.name)
        else:
            print(f"Kein passendes Abbild unter {abbild}, migriere")
        if args.abbild_pflicht and not eingespielt:
            print("FEHLER: Das Abbild ließ sich nicht einspielen (--abbild-pflicht)", file=sys.stderr)
            return 1
    if not eingespielt:
        _migrieren(args.coverage, ziel)
    if args.abbild_schreiben:
        _abbild_schreiben(Path(args.abbild_schreiben), ziel)

    with psycopg.connect(wartung, autocommit=True) as verbindung:
        verbindung.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false IS_TEMPLATE true").format(vorlage))
    weg = "aus dem Abbild eingespielt" if eingespielt else "migriert"
    print(f"Vorlage {args.name} {weg} ({time.monotonic() - beginn:.0f} s); MANDARI_TEST_DB_VORLAGE={args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
