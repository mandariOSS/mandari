# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verwaltungsbefehle (``manage.py …``) als Zeitpläne im Worker statt als Host-Cronjobs (Issue #516).

Bisher rief die Crontab des Hosts ``docker exec <anwendung> python manage.py <befehl>`` auf. Jetzt
registriert ``befehl_als_zeitplan`` einen Zeitplan (``apps.events.schedule``), dessen Auftrag
``befehl_ausfuehren`` den Befehl im Worker-Container **als eigenen Prozess** startet, genau wie
vorher der Cron-Eintrag:

- gleiche Befehlszeile, gleiche Singleton-Sperre (``apps.common.einmalig``), gleicher Exit-Code;
- eigener Speicher: Ein Befehl, der viel Speicher braucht (Sitzungsmappen, Geodaten), lässt den
  Worker-Prozess nicht wachsen, und nach dem Ende ist der Speicher wieder frei;
- harte Zeitgrenze: Überschreitet der Befehl ``zeitgrenze``, wird der Prozess beendet. Einen Faden
  könnte Python nicht abbrechen.
- Ausgabe wie bisher im Protokoll des Containers (stdout/stderr des Workers).

Ein Fehlschlag (Exit-Code ungleich 0, Zeitgrenze) wird nicht wiederholt (``PermanentTaskError``):
Der nächste Termin ist die Wiederholung, wie bei Cron. Er erscheint in ``mandari_tasks_dead``.

**Übergabe ohne Doppelläufe:** Solange auf dem Host noch der alte Cron-Eintrag steht, ruft er den
Befehl weiter auf. ``zeitplan_uebernimmt`` sagt ihm, dass ein Worker den Zeitplan bedient (Scheduler
und Runner für seine Warteschlange leben); dann überspringt der Befehl den Aufruf mit Hinweis
(``apps.common.einmalig.EinmaligMixin``). Ohne laufenden Worker läuft er wie bisher. Der Lauf aus
dem Zeitplan erkennt sich an ``AUS_ZEITPLAN_ENV``.

**Abschalten einzelner Zeitpläne:** ``EVENTS_SCHEDULES_DISABLED`` (Namen, kommagetrennt, z. B.
``befehl:build_meeting_packages``); der Scheduler legt dann keine Aufträge an, und der Cron-Eintrag
läuft wieder (Rückweg je Befehl).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from django.conf import settings
from django.tasks import task

from .schedule import Catchup, ScheduleRegistry, autodiscover, cron, disabled_schedules, every, registry
from .tasks_backend import PermanentTaskError

logger = logging.getLogger(__name__)

#: Umgebungsvariable, an der ein Befehl erkennt, dass er aus seinem Zeitplan läuft
AUS_ZEITPLAN_ENV: Final = "MANDARI_AUS_ZEITPLAN"
#: Präfix der Zeitplan-Namen: ``befehl:<name>``
PRAEFIX: Final = "befehl:"
#: Zeitgrenze eines Befehls in Sekunden, wenn nichts angegeben ist
STANDARD_ZEITGRENZE: Final = 1800
#: Längste erlaubte Zeitgrenze; darunter liegt die Zeitgrenze des Runners für ``befehl_ausfuehren``
#: (``TASKS["default"]["OPTIONS"]["tasks"]``, 3660 s), damit der Prozess vor dem Auftrag endet
MAX_ZEITGRENZE: Final = 3600


def schedule_name(befehl: str) -> str:
    return f"{PRAEFIX}{befehl}"


@task
def befehl_ausfuehren(befehl: str, argumente: list[str], zeitgrenze: int = STANDARD_ZEITGRENZE) -> int:
    """Startet ``manage.py <befehl> <argumente>`` als eigenen Prozess; liefert den Exit-Code (0)."""
    manage = Path(settings.BASE_DIR) / "manage.py"
    umgebung = {**os.environ, AUS_ZEITPLAN_ENV: "1"}
    start = time.monotonic()
    try:
        ergebnis = subprocess.run(  # noqa: S603 – feste Befehlszeile aus dem Code, keine Eingaben
            [sys.executable, str(manage), befehl, *argumente],
            env=umgebung,
            cwd=str(manage.parent),
            stdin=subprocess.DEVNULL,
            timeout=min(int(zeitgrenze), MAX_ZEITGRENZE),
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("Zeitplan %s: nach %d s abgebrochen", schedule_name(befehl), zeitgrenze)
        raise PermanentTaskError(f"manage.py {befehl}: Zeitgrenze {zeitgrenze} s überschritten") from None
    dauer = time.monotonic() - start
    if ergebnis.returncode != 0:
        logger.error("Zeitplan %s: Exit-Code %d nach %.1f s", schedule_name(befehl), ergebnis.returncode, dauer)
        raise PermanentTaskError(f"manage.py {befehl}: Exit-Code {ergebnis.returncode}")
    logger.info("Zeitplan %s: beendet nach %.1f s", schedule_name(befehl), dauer)
    return 0


def befehl_als_zeitplan(
    befehl: str,
    *,
    crontab: str | None = None,
    minuten: int | None = None,
    argumente: Sequence[str] = (),
    zeitgrenze: int = STANDARD_ZEITGRENZE,
    catchup: Catchup = Catchup.NACHHOLEN,
    ziel: ScheduleRegistry | None = None,
) -> None:
    """Registriert ``manage.py <befehl>`` als Zeitplan ``befehl:<befehl>`` (Crontab-Ausdruck oder Abstand)."""
    if (crontab is None) == (minuten is None):
        raise ValueError("befehl_als_zeitplan: genau eines von crontab und minuten angeben")
    if not 0 < zeitgrenze <= MAX_ZEITGRENZE:
        raise ValueError(f"befehl_als_zeitplan: Zeitgrenze 1–{MAX_ZEITGRENZE} s")
    args = (befehl, list(argumente), int(zeitgrenze))
    name = schedule_name(befehl)
    if crontab is not None:
        cron(crontab, name=name, args=args, catchup=catchup, registry=ziel)(befehl_ausfuehren)
    else:
        every(minutes=int(minuten or 0), name=name, args=args, catchup=catchup, registry=ziel)(befehl_ausfuehren)


def zeitplan_uebernimmt(befehl: str) -> bool:
    """Läuft ``befehl`` gerade als Zeitplan im Worker?

    Ja, wenn der Zeitplan registriert und nicht abgeschaltet ist und lebende Worker den Scheduler und
    einen Runner für seine Warteschlange stellen. Dann soll ein Aufruf von außen (alter Cron-Eintrag)
    nichts tun. Im Lauf aus dem Zeitplan selbst immer nein.
    """
    if os.environ.get(AUS_ZEITPLAN_ENV) == "1":
        return False
    autodiscover()
    eintrag = registry.get(schedule_name(befehl))
    if eintrag is None or eintrag.name in disabled_schedules():
        return False
    from .presence import live_workers

    queue = eintrag.task.queue_name
    worker = live_workers()
    plant = any("scheduler" in w.roles for w in worker)
    fuehrt_aus = any("tasks" in w.roles and (not w.queues or queue in w.queues) for w in worker)
    return plant and fuehrt_aus
