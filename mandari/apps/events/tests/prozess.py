# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Worker als eigener Prozess für Absturz- und Lasttests (Issue #514).

Ein Absturz lässt sich im selben Prozess nicht nachstellen: Ein Faden lässt sich nicht hart beenden,
seine Verbindung bliebe offen. Deshalb startet ``Probe.worker`` den Befehl ``events_worker`` als
eigenen Prozess gegen die Testdatenbank, und ``WorkerProzess.abschiessen`` beendet ihn hart
(SIGKILL bzw. ``TerminateProcess``). Zurück bleiben, wie nach einem OOM-Kill oder Stromausfall, eine
offene Transaktion (PostgreSQL rollt sie zurück, sobald die Verbindung abreißt), Leases, die nicht
freigegeben wurden, und Aufträge mit Sperre.

Der Kindprozess (``python -m apps.events.tests.prozess <Argumente von events_worker>``) registriert
zwei Abonnements auf ``probe.*``, bevor er den Befehl ausführt:

- ``probe.sicht`` – Datenbank-Sicht (``transactional=True``): schreibt je Ereignis eine Zeile in
  ``events_probe_sicht`` in der Transaktion der Zustellung. Ohne eindeutigen Schlüssel, damit ein
  doppelter Effekt zählbar ist und nicht am Handler scheitert.
- ``probe.extern`` – externer Effekt (``transactional=False``): schreibt über eine eigene Verbindung
  mit Autocommit in ``events_probe_extern``. Was dort steht, bleibt wie bei einem Suchindex auch nach
  einem Absturz bestehen.

Beide halten die Zeit der Zustellung fest (``clock_timestamp()``); mit ``recorded_at`` des Journals
ergibt das die Latenz vom Commit bis zur Sicht auf derselben Uhr. Steuerung über die Umgebung:
``PROBE_BATCH`` (Batchgröße), ``PROBE_PAUSE`` (Sekunden je Batch nach den Effekten) und
``PROBE_HALT`` (Datei: Solange sie besteht, schreibt ein Handler nach seinen Effekten
``<datei>.sicht`` bzw. ``<datei>.extern`` und hält an, bis sie verschwindet – so landet ein Abschuss
sicher mitten im Batch). Auftrag für Absturztests: ``auftraege.probe_ausfuehrung``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import psycopg
import pytest

if TYPE_CHECKING:
    from apps.events.models import Event
    from apps.events.registry import Delivery

SICHT = "events_probe_sicht"
EXTERN = "events_probe_extern"
AUSFUEHRUNG = "events_probe_auftrag"
SICHT_ABO = "probe.sicht"
EXTERN_ABO = "probe.extern"
TYP = "probe.objekt.geaendert"

_TABELLEN = {
    SICHT: "ord bigserial PRIMARY KEY, event_id uuid NOT NULL, aggregate_id uuid NOT NULL, seq bigint NOT NULL, "
    "zugestellt timestamptz NOT NULL DEFAULT clock_timestamp()",
    EXTERN: "ord bigserial PRIMARY KEY, event_id uuid NOT NULL, aggregate_id uuid NOT NULL, seq bigint NOT NULL, "
    "zugestellt timestamptz NOT NULL DEFAULT clock_timestamp()",
    AUSFUEHRUNG: "ord bigserial PRIMARY KEY, kennung text NOT NULL, versuch integer NOT NULL, phase text NOT NULL, "
    "zeit timestamptz NOT NULL DEFAULT clock_timestamp()",
}

#: Ereignisse wie vom Ingestor: eine Anweisung je Transaktion, Pflichtfelder mit neutralen Werten
_EINFUEGEN = f"""
    INSERT INTO events_event
        (event_id, type, version, aggregate_type, aggregate_id, tenant_ref, visibility, occurred_at, correlation_id,
         payload)
    SELECT e, '{TYP}', 1, 'Objekt', a, 'org:probe', 'intern', now(), e, '{{}}'::jsonb
      FROM unnest(%s::uuid[], %s::uuid[]) AS t(e, a)
"""


# --- Kindprozess -------------------------------------------------------------------------------


def _verbindungsdaten() -> str:
    """Verbindung des Kindprozesses für Effekte am Django-Zugang vorbei (Testdatenbank)."""
    return os.environ["DATABASE_URL"]


def _anhalten(abo: str) -> None:
    """Nach den Effekten, vor dem Festschreiben: optional pausieren oder bis zum Abschuss anhalten."""
    pause = float(os.environ.get("PROBE_PAUSE", "0"))
    if pause:
        time.sleep(pause)
    halt = os.environ.get("PROBE_HALT")
    if halt and Path(halt).exists():
        Path(f"{halt}.{abo}").write_text("drin", encoding="utf-8")
        while Path(halt).exists():
            time.sleep(0.05)


def _spalten(events: list[Event]) -> list[list[Any]]:
    return [[e.event_id for e in events], [e.aggregate_id for e in events], [e.seq for e in events]]


def sicht_handler(events: list[Event], delivery: Delivery) -> None:
    """Datenbank-Sicht: Zeilen in der Transaktion der Zustellung."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {SICHT} (event_id, aggregate_id, seq) "
            "SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[])",
            _spalten(events),
        )
    _anhalten("sicht")


_extern = threading.local()


def extern_handler(events: list[Event], delivery: Delivery) -> None:
    """Externer Effekt: eigene Verbindung mit Autocommit, bleibt auch nach einem Absturz bestehen."""
    verbindung: psycopg.Connection[Any] | None = getattr(_extern, "verbindung", None)
    if verbindung is None or verbindung.closed:
        verbindung = psycopg.connect(_verbindungsdaten(), autocommit=True, prepare_threshold=None)
        _extern.verbindung = verbindung
    verbindung.execute(
        f"INSERT INTO {EXTERN} (event_id, aggregate_id, seq) SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[])",
        _spalten(events),
    )
    _anhalten("extern")


def registrieren() -> None:
    from apps.events.registry import subscriber

    batch = int(os.environ.get("PROBE_BATCH", "200"))
    subscriber(SICHT_ABO, types=["probe.*"], batch=batch, from_beginning=True)(sicht_handler)
    subscriber(EXTERN_ABO, types=["probe.*"], batch=batch, transactional=False, from_beginning=True)(extern_handler)


def main(argumente: list[str]) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mandari.settings_test")
    import django

    django.setup()
    from django.core.management import call_command
    from django.test.utils import override_settings

    from apps.events.tests import prozess
    from apps.events.tests.auftraege import journal_einstellungen

    # Aufträge über das Journal wie in Produktion (die Testeinstellungen führen sie sofort aus)
    override_settings(TASKS=journal_einstellungen()).enable()
    prozess.registrieren()
    call_command("events_worker", *argumente)


# --- Testprozess -------------------------------------------------------------------------------


#: Absturz- und Lasttests messen Zeiten. Mit parallelen Testprozessen (pytest-xdist) halten deren offene
#: Transaktionen den Sequenzierer clusterweit auf (``xmin``); in der CI laufen sie deshalb im Job
#: „Ereignistechnik hinter PgBouncer“ ohne parallele Prozesse.
nur_ohne_parallele_tests = pytest.mark.skipif(
    bool(os.environ.get("PYTEST_XDIST_WORKER")),
    reason="Zeitmessung nur ohne parallele Testprozesse (clusterweites xmin); in der CI im Job hinter PgBouncer",
)


def testdatenbank_url() -> str:
    """Adresse der Testdatenbank für den Kindprozess (``DATABASE_URL``)."""
    from django.db import connection

    daten = connection.settings_dict
    benutzer = quote(str(daten.get("USER") or ""), safe="")
    passwort = quote(str(daten.get("PASSWORD") or ""), safe="")
    anmeldung = f"{benutzer}:{passwort}@" if passwort else (f"{benutzer}@" if benutzer else "")
    host = daten.get("HOST") or "localhost"
    port = daten.get("PORT") or 5432
    return f"postgresql://{anmeldung}{host}:{port}/{quote(str(daten['NAME']), safe='')}"


@dataclass
class WorkerProzess:
    prozess: subprocess.Popen[bytes]
    protokoll: Path

    def abschiessen(self) -> None:
        """Hart beenden: kein Signal-Handler, kein Festschreiben, keine Freigabe der Leases."""
        if self.prozess.poll() is None:
            self.prozess.kill()
        self.prozess.wait(30)

    def beenden(self, frist: float = 60.0) -> None:
        """Wie ``docker stop``: SIGTERM, danach hart (unter Windows gleich hart)."""
        if self.prozess.poll() is not None:
            return
        if os.name == "posix":
            self.prozess.send_signal(signal.SIGTERM)
            try:
                self.prozess.wait(frist)
                return
            except subprocess.TimeoutExpired:
                pass
        self.abschiessen()

    def laeuft(self) -> bool:
        return self.prozess.poll() is None

    def ausgabe(self) -> str:
        return self.protokoll.read_text(encoding="utf-8", errors="replace")[-4000:]


@dataclass
class Probe:
    """Tabellen, Schreiber und Worker-Prozesse eines Tests; ``aufraeumen`` beendet alles."""

    verzeichnis: Path
    prozesse: list[WorkerProzess] = field(default_factory=list)
    _lesen: psycopg.Connection[Any] | None = None

    def anlegen(self) -> None:
        with self.verbindung() as v:
            for tabelle, spalten in _TABELLEN.items():
                v.execute(f"DROP TABLE IF EXISTS {tabelle}")
                v.execute(f"CREATE TABLE {tabelle} ({spalten})")

    def aufraeumen(self) -> None:
        for worker in self.prozesse:
            worker.abschiessen()
        if self._lesen is not None:
            self._lesen.close()
        with self.verbindung() as v:
            for tabelle in _TABELLEN:
                v.execute(f"DROP TABLE IF EXISTS {tabelle}")

    def verbindung(self, *, autocommit: bool = True) -> psycopg.Connection[Any]:
        """
        Verbindung zur Testdatenbank, ohne vorbereitete Anweisungen: Hinter PgBouncer im Transaktionsmodus
        wechselt die Serververbindung, eine vorbereitete Anweisung gäbe es dort nicht oder schon.
        """
        return psycopg.connect(testdatenbank_url(), autocommit=autocommit, prepare_threshold=None)

    def worker(self, *argumente: str, **umgebung: str) -> WorkerProzess:
        """Startet ``events_worker`` als eigenen Prozess (Standard: Sequenzierer und Zustellung der Probe)."""
        from django.conf import settings

        argumente = argumente or (
            "--roles",
            "sequencer,dispatch",
            "--subscription",
            SICHT_ABO,
            "--subscription",
            EXTERN_ABO,
        )
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "mandari.settings_test",
            "DATABASE_URL": testdatenbank_url(),
            # Direktverbindung für LISTEN, von conftest.py schon auf die Testdatenbank gesetzt
            "EVENTS_DB_DIRECT_URL": str(getattr(settings, "EVENTS_DB_DIRECT_URL", "") or ""),
            "PYTHONUNBUFFERED": "1",
            **umgebung,
        }
        protokoll = self.verzeichnis / f"worker-{len(self.prozesse) + 1}.log"
        with protokoll.open("wb") as ausgabe:
            prozess = subprocess.Popen(  # noqa: S603 – eigener Interpreter, feste Argumente
                [sys.executable, "-m", "apps.events.tests.prozess", *argumente, "--metrics-port", "0"],
                cwd=settings.BASE_DIR,
                env=env,
                stdout=ausgabe,
                stderr=subprocess.STDOUT,
            )
        worker = WorkerProzess(prozess, protokoll)
        self.prozesse.append(worker)
        return worker

    def schreiben(self, anzahl: int, *, objekte: int, je_transaktion: int = 1, abstand: float = 0.0) -> list[uuid.UUID]:
        """
        Schreibt ``anzahl`` Ereignisse über ``objekte`` Objekte, eine Anweisung je Transaktion, mit
        ``abstand`` Sekunden zwischen zwei Transaktionen.
        """
        objekt_ids = [uuid.uuid4() for _ in range(objekte)]
        kennungen: list[uuid.UUID] = []
        with self.verbindung(autocommit=False) as v:
            for start in range(0, anzahl, je_transaktion):
                if start and abstand:
                    time.sleep(abstand)
                teil = [uuid.uuid4() for _ in range(min(je_transaktion, anzahl - start))]
                aggregate = [objekt_ids[(start + i) % objekte] for i in range(len(teil))]
                v.execute(_EINFUEGEN, [teil, aggregate])
                v.commit()
                kennungen.extend(teil)
        return kennungen

    def abfragen(self, sql: str, parameter: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Liest über eine Verbindung für den ganzen Test (Abfragen in kurzen Abständen)."""
        if self._lesen is None or self._lesen.closed:
            self._lesen = self.verbindung()
        return self._lesen.execute(sql, parameter or []).fetchall()

    def anzahl(self, tabelle: str) -> int:
        return int(self.abfragen(f"SELECT count(*) FROM {tabelle}")[0][0])

    @property
    def halt(self) -> Path:
        """Haltedatei für ``PROBE_HALT``; ``halt.<abo>`` zeigt, dass ein Handler angehalten hat."""
        return self.verzeichnis / "halt"

    def warten(self, bedingung: Callable[[], bool], frist: float, hinweis: str = "") -> float:
        """Wartet, bis ``bedingung`` gilt; gibt die Wartezeit zurück, scheitert nach ``frist``."""
        beginn = time.monotonic()
        while not bedingung():
            if time.monotonic() - beginn > frist:
                protokolle = "\n".join(w.ausgabe() for w in self.prozesse)
                raise AssertionError(f"Frist {frist:.0f} s überschritten: {hinweis}\n{protokolle}")
            time.sleep(0.1)
        return time.monotonic() - beginn


if __name__ == "__main__":
    main(sys.argv[1:])
