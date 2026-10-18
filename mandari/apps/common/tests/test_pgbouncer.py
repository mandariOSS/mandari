# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PgBouncer für die Webanwendung (Issue #1060).

Hinter PgBouncer im Transaktionsmodus bekommt jede Transaktion, ohne Transaktion jede Anweisung, irgendeine
freie Serververbindung. Zwei Teile:

- **Einstellungen:** ``DB_PGBOUNCER=true`` schaltet serverseitige Cursor ab und legt ``prepare_threshold = None``
  fest, der Pool der Anwendung bleibt. Ohne Schalter bleibt alles wie bisher. ``migrate``, ``safemigrate`` und
  ``events_after_restore`` nehmen mit ``DB_DIRECT_URL`` die Direktverbindung.
- **Echter Lauf hinter PgBouncer** (nur mit ``MANDARI_TEST_PGBOUNCER=1``, in der CI im Job „Ereignistechnik hinter
  PgBouncer“): ``.iterator()`` außerhalb einer Transaktion und eine oft wiederholte Abfrage, während eine zweite
  Verbindung die zuletzt benutzte Serververbindung festhält, wie unter Last. Jeder ``.iterator()`` der Anwendung
  läuft über denselben Weg in Django (``SQLCompiler.execute_sql(chunked_fetch=True)``), einer genügt also. Je ein
  Kontrollversuch zeigt, dass dieselbe Lage ohne die Einstellungen scheitert; sonst bewiese der Lauf nichts.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

import psycopg
import pytest

POOLER = "postgres://u:p@pgbouncer:5432/db"
DIREKT = "postgres://u:p@postgres:5432/db"
#: Variablen, die die Prüfungen selbst setzen; der CI-Job setzt DB_PGBOUNCER für den echten Lauf
_EIGENE = ("DB_PGBOUNCER", "DB_DIRECT_URL", "DB_POOL", "DB_POOL_MAX")


def lade_einstellungen(umgebung: dict[str, str], argv: list[str] | None = None) -> Any:
    """settings-Modul mit genau dieser Umgebung (und Befehlszeile) neu auswerten."""
    with mock.patch.dict(os.environ, {"DEBUG": "false"}), mock.patch.object(sys, "argv", argv or ["pytest"]):
        for name in _EIGENE:
            os.environ.pop(name, None)
        os.environ.update(umgebung)
        return importlib.reload(importlib.import_module("mandari.settings"))


#: Dienst der Anwendung in den Skripten
_ANWENDUNG = {"mandari", "$APP_SERVICE", "${APP_SERVICE}"}


def befehle_in_einmalcontainern(text: str) -> set[str]:
    """Verwaltungsbefehle, die ein Skript in einem Einmalcontainer der Anwendung startet (``run --no-deps``).

    Erkannt werden ``docker compose run``/``$DC run`` mit ``--no-deps`` in beliebiger Reihenfolge und mit weiteren
    Optionen, die Hüllfunktionen ``volume_container <name> <dienst> …`` (backup.sh) und ``run_manage <protokoll>
    <befehl>`` (deploy.sh, immer der Dienst der Anwendung), Fortsetzungszeilen und Tabs. Nicht gemeint sind
    Kommentare, Texte in Anführungszeichen mit Leerzeichen (Hinweise wie ``warn "  docker compose run …"``,
    Beschriftungen von ``run_step``) und ``docker exec`` in laufenden Containern.
    """
    import re

    befehle: set[str] = set()
    for zeile in re.sub(r"\\[ \t]*\r?\n", " ", text).splitlines():
        zeile = " ".join(zeile.split())
        if not zeile or zeile.startswith("#"):
            continue
        # Paarweise von links: "$APP_SERVICE" bleibt, Hinweistexte (mit Leerzeichen) fallen weg
        zeile = re.sub(r"\"[^\"]*\"|'[^']*'", lambda m: '""' if re.search(r"\s", m.group(0)) else m.group(0), zeile)
        befehle |= set(re.findall(r"\brun_manage \S+ (\w+)", zeile))
        einmal = (re.search(r"\brun\b", zeile) and "--no-deps" in zeile) or "volume_container " in zeile
        if einmal:
            for dienst, befehl in re.findall(r"(\S+) python manage\.py (\w+)", zeile):
                if dienst.strip("\"'") in _ANWENDUNG:
                    befehle.add(befehl)
    return befehle


@pytest.fixture
def _einstellungen_wiederherstellen() -> Iterator[None]:
    yield
    importlib.reload(importlib.import_module("mandari.settings"))


@pytest.mark.usefixtures("_einstellungen_wiederherstellen")
class TestSchalter:
    def test_ohne_schalter_wie_bisher(self) -> None:
        s = lade_einstellungen({"DATABASE_URL": POOLER})
        db = s.DATABASES["default"]
        assert s.DB_PGBOUNCER is False
        assert db["DISABLE_SERVER_SIDE_CURSORS"] is False, "Vorgabe von dj-database-url"
        assert set(db["OPTIONS"]) == {"pool"}, "kein prepare_threshold, keine weiteren Optionen"
        assert db["OPTIONS"]["pool"]["max_size"] == 10
        assert db["CONN_MAX_AGE"] == 0

    def test_schalter_aus_aendert_nichts(self) -> None:
        """``DB_PGBOUNCER=false`` und eine Direktverbindung ohne Schalter wirken nicht, auch nicht für migrate."""
        ohne = lade_einstellungen({"DATABASE_URL": POOLER}).DATABASES
        aus = lade_einstellungen(
            {"DATABASE_URL": POOLER, "DB_PGBOUNCER": "false", "DB_DIRECT_URL": DIREKT}, ["manage.py", "migrate"]
        ).DATABASES
        assert aus == ohne

    def test_mit_schalter(self) -> None:
        s = lade_einstellungen({"DATABASE_URL": POOLER, "DB_PGBOUNCER": "true"})
        db = s.DATABASES["default"]
        assert db["DISABLE_SERVER_SIDE_CURSORS"] is True, "sonst scheitert .iterator() außerhalb einer Transaktion"
        assert "prepare_threshold" in db["OPTIONS"]
        assert db["OPTIONS"]["prepare_threshold"] is None, "keine vorbereiteten Anweisungen"
        assert "server_side_binding" not in db["OPTIONS"], "Parameter bindet weiter der Client"
        assert db["HOST"] == "pgbouncer"

    def test_pool_der_anwendung_bleibt(self) -> None:
        s = lade_einstellungen({"DATABASE_URL": POOLER, "DB_PGBOUNCER": "true", "DB_POOL_MAX": "30"})
        db = s.DATABASES["default"]
        assert db["OPTIONS"]["pool"]["max_size"] == 30
        assert db["CONN_MAX_AGE"] == 0, "Django verlangt CONN_MAX_AGE=0 neben dem Pool"

    def test_auch_ohne_pool(self) -> None:
        s = lade_einstellungen({"DATABASE_URL": POOLER, "DB_PGBOUNCER": "true", "DB_POOL": "false"})
        db = s.DATABASES["default"]
        assert "pool" not in db["OPTIONS"]
        assert db["DISABLE_SERVER_SIDE_CURSORS"] is True
        assert db["OPTIONS"]["prepare_threshold"] is None

    def test_sqlite_unberuehrt(self) -> None:
        """sqlite3.connect kennt prepare_threshold nicht; der Schalter wirkt nur bei PostgreSQL."""
        s = lade_einstellungen({"DATABASE_URL": "sqlite:///tmp/test.sqlite3", "DB_PGBOUNCER": "true"})
        db = s.DATABASES["default"]
        assert db["DISABLE_SERVER_SIDE_CURSORS"] is False
        assert "prepare_threshold" not in db.get("OPTIONS", {})

    @pytest.mark.parametrize(
        ("befehl", "argumente"),
        [
            ("migrate", ["--noinput"]),
            ("safemigrate", ["--noinput"]),
            # backup.sh, Wiederherstellung: Nur postgres läuft, PgBouncer noch nicht
            ("events_after_restore", ["--apply"]),
        ],
    )
    def test_direkt_an_postgres(self, befehl: str, argumente: list[str]) -> None:
        """update.sh, install.sh, deploy.sh und backup.sh starten diese Befehle im Container der Anwendung."""
        s = lade_einstellungen(
            {"DATABASE_URL": POOLER, "DB_PGBOUNCER": "true", "DB_DIRECT_URL": DIREKT},
            ["manage.py", befehl, *argumente],
        )
        db = s.DATABASES["default"]
        assert db["HOST"] == "postgres", "am Pooler vorbei"
        assert s.DB_PGBOUNCER is False
        assert db["DISABLE_SERVER_SIDE_CURSORS"] is False, "direkt verbunden gehen serverseitige Cursor"
        assert "prepare_threshold" not in db["OPTIONS"]

    @pytest.mark.parametrize(
        ("zeilen", "erwartet"),
        [
            ("docker compose run --rm --no-deps mandari python manage.py safemigrate --noinput", {"safemigrate"}),
            ('docker compose run --no-deps --rm -T -e A=b "$APP_SERVICE" python manage.py migrate', {"migrate"}),
            ("docker compose run --rm --no-deps \\\n\t\tmandari\tpython manage.py migrate", {"migrate"}),
            ('IMAGE_TAG="$T" $DC run --rm --no-deps -T "${APP_SERVICE}" python manage.py check', {"check"}),
            (
                'volume_container folge "$APP_SERVICE" \\\n    python manage.py events_after_restore --apply',
                {"events_after_restore"},
            ),
            ("run_manage /tmp/plan.log migrate --plan", {"migrate"}),
            # nicht gemeint: andere Dienste, laufende Container, Hinweise, Kommentare, die Hüllfunktion selbst
            ("docker compose run --rm --no-deps website python manage.py migrate", set()),
            ('docker exec "$APP_CONTAINER" python manage.py migrate --noinput', set()),
            ('warn "  docker compose run --rm --no-deps mandari python manage.py migrate"', set()),
            ("# docker compose run --rm --no-deps mandari python manage.py migrate", set()),
            ('$DC run --rm --no-deps -T "$APP_SERVICE" python manage.py "$@"', set()),
            ("run_manage() {", set()),
        ],
    )
    def test_erkennung_der_einmalcontainer(self, zeilen: str, erwartet: set[str]) -> None:
        assert befehle_in_einmalcontainern(zeilen) == erwartet

    def test_einmalcontainer_der_skripte_gehen_direkt(self) -> None:
        """Befehle, die update.sh, install.sh, backup.sh und deploy.sh in einem Einmalcontainer der Anwendung starten.

        Dort läuft PgBouncer nicht unbedingt: Bei der Wiederherstellung ist nach ``down`` nur postgres gestartet.
        Der Test findet die Schreibweisen aus ``befehle_in_einmalcontainern``; eine ganz andere Schreibweise in einem
        Skript fände er nicht.
        """
        from pathlib import Path

        from mandari import settings as einstellungen

        wurzel = Path(__file__).resolve().parents[4]
        befehle: set[str] = set()
        for skript in ("update.sh", "backup.sh", "install.sh", "deploy/scripts/deploy.sh"):
            befehle |= befehle_in_einmalcontainern((wurzel / skript).read_text(encoding="utf-8"))
        # check (Systemprüfung ohne Datenbankprüfungen) startet deploy.sh vor dem Deploy, während die Installation
        # und damit PgBouncer läuft; es darf über den Pooler gehen
        assert befehle == {"safemigrate", "events_after_restore", "migrate", "check"}, "neu: direkt verbinden?"
        assert befehle - {"check"} <= set(einstellungen.DB_DIRECT_COMMANDS)

    def test_andere_befehle_ueber_den_pooler(self) -> None:
        s = lade_einstellungen(
            {"DATABASE_URL": POOLER, "DB_PGBOUNCER": "true", "DB_DIRECT_URL": DIREKT}, ["manage.py", "shell"]
        )
        assert s.DATABASES["default"]["HOST"] == "pgbouncer"
        assert s.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True

    def test_migration_ohne_direktverbindung_ueber_den_pooler(self) -> None:
        """Fehlt DB_DIRECT_URL, bleibt es beim Pooler, dann aber mit den Einstellungen für den Transaktionsmodus."""
        s = lade_einstellungen({"DATABASE_URL": POOLER, "DB_PGBOUNCER": "true"}, ["manage.py", "migrate"])
        assert s.DATABASES["default"]["HOST"] == "pgbouncer"
        assert s.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True


# ---------------------------------------------------------------------------------------------------------------
# Echter Lauf hinter PgBouncer (pool_mode = transaction)
# ---------------------------------------------------------------------------------------------------------------

HINTER_PGBOUNCER = os.environ.get("MANDARI_TEST_PGBOUNCER") == "1"
NAMEN = [f"pgbouncer-{i}" for i in range(1, 6)]


def _url() -> str:
    """Testdatenbank über denselben Weg wie die Anwendung (DATABASE_URL zeigt im Job auf PgBouncer)."""
    from django.db import connection
    from psycopg.conninfo import make_conninfo

    daten = connection.settings_dict
    teile = {
        "dbname": daten["NAME"],
        "user": daten.get("USER"),
        "password": daten.get("PASSWORD"),
        "host": daten.get("HOST"),
        "port": daten.get("PORT"),
    }
    return make_conninfo("", **{k: v for k, v in teile.items() if v not in (None, "")})


def _serverprozess(verbindung: psycopg.Connection[Any]) -> int:
    """Serverprozess hinter PgBouncer, auf dem *verbindung* gerade ihre Anweisung ausführt."""
    zeile = verbindung.execute("SELECT pg_backend_pid()").fetchone()
    assert zeile is not None
    return int(zeile[0])


def _pid() -> int:
    """Serverprozess, auf dem die Anwendung gerade ihre Anweisung ausführt."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


@contextmanager
def _serververbindung_festhalten(erwartet: int) -> Iterator[None]:
    """Hält die Serververbindung, die eben noch die Anwendung hatte, in einer offenen Transaktion fest.

    PgBouncer vergibt freie Serververbindungen zuletzt benutzt zuerst (``server_round_robin = 0``, Vorgabe): Eine
    zweite Verbindung bekommt die eben freigegebene. Solange sie hier in einer Transaktion steckt, muss die Anwendung
    ihre nächste Anweisung auf einer anderen Serververbindung ausführen. Danach räumt sie auf, was die Kontrollversuche
    dort liegen lassen (Cursor WITH HOLD, vorbereitete Anweisungen).
    """
    with psycopg.connect(_url(), prepare_threshold=None) as halter:
        pid = _serverprozess(halter)  # beginnt die Transaktion
        assert pid == erwartet, "Vorbedingung: PgBouncer vergibt die zuletzt benutzte Serververbindung zuerst"
        try:
            yield
        finally:
            halter.execute("CLOSE ALL")
            halter.execute("DEALLOCATE ALL")
            halter.commit()


def _gruppen() -> None:
    from django.contrib.auth.models import Group

    Group.objects.bulk_create([Group(name=name) for name in NAMEN])


def _iterator() -> Iterator[str]:
    from django.contrib.auth.models import Group

    return Group.objects.filter(name__in=NAMEN).order_by("name").values_list("name", flat=True).iterator(chunk_size=1)


@pytest.mark.skipif(not HINTER_PGBOUNCER, reason="nur hinter PgBouncer (MANDARI_TEST_PGBOUNCER=1, Job in der CI)")
@pytest.mark.django_db(transaction=True)
class TestHinterPgBouncer:
    def test_job_laeuft_mit_dem_schalter(self) -> None:
        from django.db import connection

        assert connection.settings_dict.get("DISABLE_SERVER_SIDE_CURSORS") is True, "Job setzt DB_PGBOUNCER=true"
        assert connection.settings_dict["OPTIONS"].get("pool"), "der Pool der Anwendung bleibt an"

    def test_iterator_ausserhalb_einer_transaktion(self) -> None:
        from django.db import connection

        _gruppen()
        assert connection.get_autocommit(), "außerhalb einer Transaktion"
        zeilen = _iterator()
        erste = next(zeilen)
        with _serververbindung_festhalten(_pid()):
            rest = list(zeilen)
        assert [erste, *rest] == NAMEN

    def test_wiederholte_abfrage(self) -> None:
        """psycopg bereitet eine Anweisung ab der sechsten Ausführung vor (``prepare_threshold`` 5)."""
        from django.contrib.auth.models import Group

        _gruppen()
        for _ in range(6):
            assert Group.objects.filter(name=NAMEN[0]).count() == 1
        with _serververbindung_festhalten(_pid()):
            for _ in range(6):
                assert Group.objects.filter(name=NAMEN[0]).count() == 1

    def test_kontrollversuch_serverseitiger_cursor_scheitert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ohne DISABLE_SERVER_SIDE_CURSORS holt der nächste Block von einer Serververbindung ohne den Cursor.

        Django meldet den Fehler beim FETCH; das anschließende CLOSE im ``finally`` von ``cursor_iter`` scheitert
        ebenso und kommt als Fehler von psycopg obenauf. Beide nennen den fehlenden Cursor.
        """
        from django.db import DatabaseError, connection

        _gruppen()
        monkeypatch.setitem(connection.settings_dict, "DISABLE_SERVER_SIDE_CURSORS", False)
        zeilen = _iterator()
        next(zeilen)
        with (
            _serververbindung_festhalten(_pid()),
            pytest.raises((DatabaseError, psycopg.Error), match=r'cursor "_django_curs_\w+" does not exist'),
        ):
            list(zeilen)

    def test_kontrollversuch_vorbereitete_anweisung_scheitert(self) -> None:
        """psycopg mit seinen Vorgaben (Parameter auf dem Server gebunden, ``prepare_threshold`` 5) scheitert hier.

        Django bindet Parameter auf der Clientseite und bereitet nie vor; dieser Versuch zeigt, dass die Lage im
        Test vorbereitete Anweisungen wirklich bricht.
        """
        abfrage = "SELECT count(*) FROM auth_group WHERE name = %s"
        with psycopg.connect(_url(), autocommit=True, prepare_threshold=None) as aufraeumen:
            # Die Serververbindung, die gleich die Probe bekommt, frei von vorbereiteten Anweisungen anderer Clients
            aufraeumen.execute("DEALLOCATE ALL")
        with psycopg.connect(_url(), autocommit=True, prepare_threshold=5) as probe:
            for _ in range(6):
                probe.execute(abfrage, [NAMEN[0]]).fetchone()
            with (
                _serververbindung_festhalten(_serverprozess(probe)),
                pytest.raises((psycopg.errors.InvalidSqlStatementName, psycopg.errors.DuplicatePreparedStatement)),
            ):
                probe.execute(abfrage, [NAMEN[0]]).fetchone()
