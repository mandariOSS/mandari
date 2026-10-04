# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Absturztests mit dem Worker als eigenem Prozess (Issue #514, ``docs/EREIGNISTECHNIK_NACHWEISE.md``).

Der Worker wird mitten in der Arbeit hart beendet (SIGKILL, wie ein OOM-Kill), ein neuer Prozess
startet sofort, wie es die Neustartregel des Containers tut. Geprüft werden die Zusagen der
Ereignistechnik: kein Verlust, Datenbank-Sichten ohne doppelten Effekt, externe Effekte höchstens
für den Batch im Flug doppelt, Wiederanlauf ohne Handarbeit innerhalb von 60 Sekunden. Die Tests
messen mit den Fristen des Betriebs (Leases 30 s, Sperre eines Auftrags 60 s) und dauern deshalb
jeweils rund eine Minute.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from typing import Any

import pytest

from apps.events import leases
from apps.events.models import Lease, ParkedEvent, Subscription
from apps.events.models import Task as TaskRow
from apps.events.task_runner import LOCK_TTL, MAINTENANCE_INTERVAL
from apps.events.tests import auftraege
from apps.events.tests.auftraege import journal_einstellungen
from apps.events.tests.prozess import AUSFUEHRUNG, EXTERN, EXTERN_ABO, SICHT, SICHT_ABO, Probe

pytestmark = pytest.mark.django_db(transaction=True)

#: Zusage (ADR Ereignistechnik, Qualitätsziele): Wiederanlauf nach einem Absturz ohne Handarbeit
WIEDERANLAUF = 60.0
#: Batchgröße der Probe; doppelt zugestellt wird höchstens der Batch im Flug
BATCH = 50


def _cursor(name: str) -> int:
    return Subscription.objects.values_list("cursor_seq", flat=True).get(name=name)


def _effekte(probe: Probe, tabelle: str) -> Counter[uuid.UUID]:
    return Counter(zeile[0] for zeile in probe.abfragen(f"SELECT event_id FROM {tabelle}"))


def _folgenummern(probe: Probe, kennungen: list[uuid.UUID]) -> dict[uuid.UUID, int | None]:
    return dict(probe.abfragen("SELECT event_id, seq FROM events_event WHERE event_id = ANY(%s)", [kennungen]))


def _alles_zugestellt(probe: Probe, kennungen: list[uuid.UUID]) -> bool:
    erwartet = set(kennungen)
    return erwartet <= set(_effekte(probe, SICHT)) and erwartet <= set(_effekte(probe, EXTERN))


def test_absturz_mitten_im_batch_ohne_verlust_und_ohne_doppelte_sichteffekte(probe: Probe) -> None:
    vorher = probe.schreiben(600, objekte=40)
    erster = probe.worker(PROBE_BATCH=str(BATCH), PROBE_PAUSE="0.3", PROBE_HALT=str(probe.halt))
    probe.warten(lambda: probe.anzahl(SICHT) >= 3 * BATCH, 120, "drei Batches der Sicht")

    # Der nächste Batch beider Abonnements schreibt seine Effekte und hält vor dem Festschreiben an
    probe.halt.write_text("", encoding="utf-8")
    probe.warten(
        lambda: probe.halt.with_suffix(".sicht").exists() and probe.halt.with_suffix(".extern").exists(),
        60,
        "beide Handler mitten im Batch",
    )
    erster.abschiessen()
    abgestuerzt = time.monotonic()
    probe.halt.unlink()

    # Stand nach dem Absturz: Die Sicht enthält genau die Ereignisse bis zu ihrem Cursor, der offene
    # Batch ist mit der Verbindung zurückgerollt. Der externe Effekt des offenen Batches besteht.
    cursor_sicht, cursor_extern = _cursor(SICHT_ABO), _cursor(EXTERN_ABO)
    seq_von = _folgenummern(probe, vorher)
    bis_cursor = {k for k, s in seq_von.items() if s is not None and s <= cursor_sicht}
    assert set(_effekte(probe, SICHT)) == bis_cursor
    im_flug = {k for k in _effekte(probe, EXTERN) if (seq_von[k] or 0) > cursor_extern}
    assert 0 < len(im_flug) <= BATCH, "Der Abschuss traf den externen Handler mitten im Batch"
    verwaist = set(Lease.objects.values_list("name", flat=True))
    assert {"sequencer", f"dispatch:{SICHT_ABO}"} <= verwaist, "Leases des toten Prozesses bestehen noch"

    # Während der Worker tot ist, schreibt die Anwendung weiter; der neue Prozess startet sofort
    danach = probe.schreiben(100, objekte=10)
    probe.worker(PROBE_BATCH=str(BATCH))
    alle = vorher + danach
    probe.warten(lambda: _alles_zugestellt(probe, alle), 2 * WIEDERANLAUF, "Wiederanlauf")
    wiederanlauf = time.monotonic() - abgestuerzt

    sicht, extern = _effekte(probe, SICHT), _effekte(probe, EXTERN)
    assert set(sicht) == set(alle) and set(sicht.values()) == {1}, "Sicht: jedes Ereignis genau einmal"
    assert set(extern) == set(alle), "extern: jedes Ereignis mindestens einmal"
    doppelt = {k for k, n in extern.items() if n > 1}
    assert doppelt <= im_flug, "doppelt höchstens der Batch im Flug"
    assert not ParkedEvent.objects.exists()

    # Reihenfolge je Objekt in der Sicht: aufsteigende Folgenummern in der Reihenfolge der Effekte
    je_objekt: dict[uuid.UUID, list[int]] = {}
    for aggregat, seq in probe.abfragen(f"SELECT aggregate_id, seq FROM {SICHT} ORDER BY ord"):
        je_objekt.setdefault(aggregat, []).append(seq)
    assert all(folge == sorted(folge) for folge in je_objekt.values())

    print(
        f"Absturz mitten im Batch: Wiederanlauf {wiederanlauf:.1f} s (Lease {leases.LEASE_TTL.total_seconds():.0f} s), "
        f"doppelt extern {len(doppelt)} von {len(alle)}, Sicht ohne Doppel"
    )
    assert wiederanlauf <= WIEDERANLAUF


def _ausfuehrungen(probe: Probe, kennung: str) -> list[tuple[Any, ...]]:
    return probe.abfragen(f"SELECT versuch, phase FROM {AUSFUEHRUNG} WHERE kennung = %s ORDER BY ord", [kennung])


def test_absturz_mitten_im_auftrag_wird_ohne_handarbeit_wiederholt(probe: Probe, settings: Any) -> None:
    settings.TASKS = journal_einstellungen()
    rollen = ("--roles", "tasks", "--queues", "default")
    unterbrochen = auftraege.probe_ausfuehrung.enqueue("unterbrochen", 600.0)
    erster = probe.worker(*rollen)
    probe.warten(lambda: bool(_ausfuehrungen(probe, "unterbrochen")), 60, "Auftrag läuft")
    erster.abschiessen()
    abgestuerzt = time.monotonic()

    neu = auftraege.probe_ausfuehrung.enqueue("neu", 0.0)
    probe.worker(*rollen)
    probe.warten(lambda: (1, "ende") in _ausfuehrungen(probe, "neu"), WIEDERANLAUF, "neuer Auftrag")
    neuer_auftrag = time.monotonic() - abgestuerzt
    frist = (LOCK_TTL.total_seconds() + MAINTENANCE_INTERVAL) * 1.5
    probe.warten(lambda: (2, "ende") in _ausfuehrungen(probe, "unterbrochen"), frist, "Wiederholung")
    wiederholt = time.monotonic() - abgestuerzt

    assert _ausfuehrungen(probe, "unterbrochen") == [(1, "beginn"), (2, "beginn"), (2, "ende")]
    zeile = TaskRow.objects.get(pk=unterbrochen.id)
    assert (zeile.status, zeile.attempts) == ("erledigt", 2)
    assert TaskRow.objects.get(pk=neu.id).status == "erledigt"
    print(
        f"Absturz mitten im Auftrag: neuer Auftrag nach {neuer_auftrag:.1f} s, unterbrochener wiederholt nach "
        f"{wiederholt:.1f} s (Sperre {LOCK_TTL.total_seconds():.0f} s, Freigabe alle {MAINTENANCE_INTERVAL:.0f} s)"
    )
    assert neuer_auftrag <= WIEDERANLAUF
    assert wiederholt <= LOCK_TTL.total_seconds() + MAINTENANCE_INTERVAL + 10
