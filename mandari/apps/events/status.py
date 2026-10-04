# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statusprüfungen des Workers für die Statusseite (Issue #574): ``/health/worker/``.

Vier Prüfungen mit festen Schwellen, gemessen in der Datenbank (wie ``/metrics/``), sodass jede
Anwendung sie beantworten kann, auch wenn der Worker selbst steht:

- ``lebenszeichen``: Bedienen lebende Worker alle Rollen und Warteschlangen, die die Installation
  braucht (``apps.events.presence``)? Der Worker meldet sich nur, solange jede seiner Rollen arbeitet.
- ``rueckstau``: Sequenzierer (ältestes Ereignis ohne Folgenummer, Stau durch offene Transaktionen),
  Zustellung (ältestes nicht zugestelltes Ereignis je nicht pausiertem Abonnement) jeweils höchstens
  ``MAX_EVENT_LAG`` (5 min), ältester fälliger Auftrag höchstens ``MAX_TASK_WAIT`` (15 min).
- ``fehlerquote``: In der letzten Stunde beendete Aufträge, davon gescheitert (fehlgeschlagen oder
  tot) höchstens ``MAX_ERROR_RATE`` (20 %), sobald mindestens ``MIN_FINISHED`` beendet sind.
- ``gescheitert``: endgültig gescheiterte Aufträge der letzten 24 Stunden und tote Ereignisse (nach
  allen Versuchen geparkt) – jeweils keiner. Wie ``mandari_tasks_dead`` erlischt die Meldung eines
  Auftrags nach einem Tag; tote Ereignisse bleiben, bis sie im Admin erneut versucht oder verworfen
  sind.

Fachmodule ergänzen eigene Prüfungen über ``register_check``: ``texterkennung`` (``insight_core``, Issue #817)
meldet hängende und nach wiederholtem Abbruch aufgegebene Dateien des OCR-Workers.

Die Antworten enthalten nur Zahlen, Rollen- und Warteschlangennamen, keine Namen von Aufträgen oder
Abonnements: Der Endpunkt ist wie ``/health/ready/`` ohne Anmeldung erreichbar. Einzelheiten
stehen im Admin (Ereignisse, Aufträge) und in ``/metrics/``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from django.db.models import Count, Min, Q
from django.db.models.functions import Now
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Rückstand von Sequenzierer und Zustellung, ab dem die Prüfung scheitert (wie die Alarmregeln)
MAX_EVENT_LAG: Final = 300.0
#: Wartezeit des ältesten fälligen Auftrags, ab der die Prüfung scheitert
MAX_TASK_WAIT: Final = 900.0
#: Zeitfenster und Grenzen der Fehlerquote
ERROR_WINDOW: Final = timedelta(hours=1)
MAX_ERROR_RATE: Final = 0.2
MIN_FINISHED: Final = 5
#: Zeitfenster für endgültig gescheiterte Aufträge (wie ``mandari_tasks_dead``)
FAILED_WINDOW: Final = timedelta(hours=24)


@dataclass(frozen=True)
class Check:
    """Ergebnis einer Prüfung; ``detail`` ist ein fester Text ohne Inhalte."""

    ok: bool
    detail: str


def _sekunden(wert: float) -> str:
    return f"{wert:.0f} s"


def check_heartbeat() -> Check:
    from .presence import required_roles, worker_status

    bedarf = required_roles()
    if not bedarf:
        return Check(True, "nicht erforderlich")
    stand = worker_status(required=bedarf)
    if stand.degraded:
        return Check(False, f"kein Worker für {stand.missing_summary()}")
    return Check(True, f"{len(stand.workers)} Worker ({', '.join(sorted(stand.roles))})")


def check_backlog() -> Check:
    from .metrics import sequencer_backlog, subscription_lags
    from .models import SubscriptionState, Task, TaskStatus

    teile: list[str] = []
    ok = True
    stau = sequencer_backlog()
    if stau is not None:
        sequenzierer = max(stau.lag_seconds, stau.blocked_seconds)
        ok &= sequenzierer <= MAX_EVENT_LAG
        teile.append(f"Sequenzierer {_sekunden(sequenzierer)}")
    zustellung = max(
        (
            lag.lag_seconds
            for lag in subscription_lags()
            if lag.lag_seconds is not None and lag.state != SubscriptionState.PAUSIERT
        ),
        default=0.0,
    )
    ok &= zustellung <= MAX_EVENT_LAG
    teile.append(f"Zustellung {_sekunden(zustellung)}")

    aeltester = Task.objects.filter(status=TaskStatus.WARTEND, run_after__lte=Now()).aggregate(seit=Min("run_after"))
    wartet = max((timezone.now() - aeltester["seit"]).total_seconds(), 0.0) if aeltester["seit"] else 0.0
    ok &= wartet <= MAX_TASK_WAIT
    teile.append(f"Aufträge {_sekunden(wartet)}")
    return Check(ok, ", ".join(teile))


def check_error_rate() -> Check:
    from .models import Task, TaskStatus

    gescheitert = [TaskStatus.FEHLGESCHLAGEN, TaskStatus.TOT]
    zahlen = Task.objects.filter(
        status__in=[TaskStatus.ERLEDIGT, *gescheitert], finished_at__gte=timezone.now() - ERROR_WINDOW
    ).aggregate(beendet=Count("id"), gescheitert=Count("id", filter=Q(status__in=gescheitert)))
    beendet, fehler = int(zahlen["beendet"] or 0), int(zahlen["gescheitert"] or 0)
    if beendet == 0:
        return Check(True, "keine Aufträge in der letzten Stunde")
    quote = fehler / beendet
    ok = beendet < MIN_FINISHED or quote <= MAX_ERROR_RATE
    return Check(ok, f"{fehler} von {beendet} Aufträgen der letzten Stunde gescheitert ({quote:.0%})")


def check_failed() -> Check:
    from .models import ParkedEvent, ParkedState, Task, TaskStatus

    auftraege = Task.objects.filter(
        status__in=[TaskStatus.FEHLGESCHLAGEN, TaskStatus.TOT], finished_at__gte=timezone.now() - FAILED_WINDOW
    ).count()
    ereignisse = ParkedEvent.objects.filter(state=ParkedState.TOT).count()
    return Check(
        auftraege == 0 and ereignisse == 0,
        f"{auftraege} gescheiterte Aufträge (24 h), {ereignisse} tote Ereignisse",
    )


CHECKS: dict[str, Callable[[], Check]] = {
    "lebenszeichen": check_heartbeat,
    "rueckstau": check_backlog,
    "fehlerquote": check_error_rate,
    "gescheitert": check_failed,
}


def register_check(name: str, check: Callable[[], Check]) -> None:
    """
    Prüfung eines Fachmoduls aufnehmen, das die Plattform nicht kennen darf (Schichtenmodell), etwa
    ``texterkennung`` aus ``insight_core`` (Issue #817). Aufruf aus ``AppConfig.ready``.
    """
    CHECKS[name] = check


def run_checks() -> dict[str, Check]:
    """Alle Prüfungen; eine, die selbst scheitert (Datenbank weg, Migration ausstehend), gilt als nicht in Ordnung."""
    ergebnisse: dict[str, Check] = {}
    for name, pruefung in CHECKS.items():
        try:
            ergebnisse[name] = pruefung()
        except Exception as exc:  # noqa: BLE001 – jede Ausnahme ist hier ein Prüfergebnis
            logger.warning("Worker-Prüfung %s nicht möglich", name, exc_info=True)
            ergebnisse[name] = Check(False, f"nicht prüfbar ({type(exc).__name__})")
    return ergebnisse
