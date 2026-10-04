# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lasttest der Ereignistechnik: Normalbetrieb und nächtlicher Vollabgleich (Issue #514).

Der Worker läuft als eigener Prozess (``prozess.py``) mit Sequenzierer, Zustellung und den
Abonnements ``probe.sicht`` (Datenbank-Sicht) und ``probe.extern`` (externer Effekt), Batch 200.
Geschrieben wird wie vom Ingestor: eine Transaktion je Objekt mit einer Anweisung, mehrere Schreiber
gleichzeitig. Gemessen wird auf der Uhr der Datenbank vom Erfassen im Journal (``recorded_at``) bis
zum Effekt; Ergebnisse und Einordnung in ``docs/EREIGNISTECHNIK_NACHWEISE.md``.

1. **Normalbetrieb:** 100 Ereignisse im Abstand von 50 ms. Zusage: Latenz p95 ≤ 5 s.
2. **Vollabgleich:** ``EVENTS_LAST_EREIGNISSE`` Ereignisse (Standard 10 000) so schnell wie möglich
   von ``EVENTS_LAST_SCHREIBER`` Schreibern (Standard 4), während der Worker läuft. Zusage: Latenz
   p95 ≤ 60 s.
3. **Rückstau:** Beide Abonnements pausiert, noch einmal so viele Ereignisse, dann fortgesetzt. Die
   Zeit bis zum Abbau ergibt die Leistung der Zustellung unabhängig vom Tempo der Schreiber. Zusage:
   Durchsatz ≥ 100 Ereignisse/s je Abonnement. Wie groß ein Rückstau höchstens sein darf, damit die
   Latenz unter 60 s bleibt, folgt daraus (Durchsatz × 60 s).

``EVENTS_LAST_BERICHT`` nennt eine Datei, in die der Bericht als JSON geschrieben wird (vor den
Prüfungen, damit auch ein gescheiterter Lauf seine Zahlen behält).
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from apps.events.dispatch import set_state
from apps.events.models import SubscriptionState
from apps.events.tests.prozess import EXTERN, EXTERN_ABO, SICHT, SICHT_ABO, Probe

pytestmark = pytest.mark.django_db(transaction=True)

EREIGNISSE = int(os.environ.get("EVENTS_LAST_EREIGNISSE", "10000"))
SCHREIBER = int(os.environ.get("EVENTS_LAST_SCHREIBER", "4"))
#: Zusagen (ADR Ereignistechnik, Issue #514)
LATENZ_NORMAL = 5.0
LATENZ_RUECKSTAU = 60.0
DURCHSATZ = 100.0


def _quantil(werte: list[float], anteil: float) -> float:
    if len(werte) < 2:
        return werte[0] if werte else 0.0
    return statistics.quantiles(werte, n=100, method="inclusive")[round(anteil * 100) - 1]


def _messung(probe: Probe, tabelle: str, kennungen: list[uuid.UUID]) -> dict[str, float]:
    """Latenz je Ereignis (erster Effekt) und Durchsatz über die Spanne der Effekte."""
    zeilen = probe.abfragen(
        f"""SELECT extract(epoch FROM min(t.zugestellt) - e.recorded_at),
                  extract(epoch FROM min(t.zugestellt))
             FROM {tabelle} t JOIN events_event e ON e.event_id = t.event_id
            WHERE t.event_id = ANY(%s)
            GROUP BY e.event_id, e.recorded_at""",
        [kennungen],
    )
    latenzen = [float(z[0]) for z in zeilen]
    zeiten = [float(z[1]) for z in zeilen]
    spanne = max(zeiten) - min(zeiten) if zeiten else 0.0
    return {
        "anzahl": len(zeilen),
        "latenz_p50_s": round(_quantil(latenzen, 0.50), 3),
        "latenz_p95_s": round(_quantil(latenzen, 0.95), 3),
        "latenz_max_s": round(max(latenzen, default=0.0), 3),
        "durchsatz_je_s": round(len(zeilen) / spanne, 1) if spanne > 0 else float("inf"),
    }


def _alle_da(probe: Probe) -> bool:
    """Alles nummeriert und beide Cursor am Ende (im Testjournal stehen nur Ereignisse der Probe)."""
    offen, hoechste = probe.abfragen(
        "SELECT count(*) FILTER (WHERE seq IS NULL), coalesce(max(seq), 0) FROM events_event"
    )[0]
    cursor = probe.abfragen("SELECT coalesce(min(cursor_seq), 0) FROM events_subscription WHERE name LIKE 'probe.%%'")
    return bool(offen == 0 and cursor[0][0] >= hoechste)


def _vollabgleich(probe: Probe) -> tuple[list[uuid.UUID], float]:
    ergebnisse: list[list[uuid.UUID]] = [[] for _ in range(SCHREIBER)]
    fehler: list[BaseException] = []

    def schreiben(nummer: int) -> None:
        try:
            anteil = EREIGNISSE // SCHREIBER + (1 if nummer < EREIGNISSE % SCHREIBER else 0)
            ergebnisse[nummer] = probe.schreiben(anteil, objekte=max(1, anteil // 3))
        except BaseException as exc:  # noqa: BLE001 – im Testfaden festhalten, im Test melden
            fehler.append(exc)

    beginn = time.monotonic()
    faeden = [threading.Thread(target=schreiben, args=(nummer,)) for nummer in range(SCHREIBER)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join()
    assert not fehler, fehler
    return [kennung for teil in ergebnisse for kennung in teil], time.monotonic() - beginn


def _tabelle(bericht: dict[str, Any]) -> str:
    zeilen = [
        f"### Lasttest Ereignistechnik ({bericht['ereignisse']} Ereignisse, {bericht['cpu']} CPU)",
        "",
        "| Abonnement | Normal p95 | Vollabgleich p95 | Rückstau Durchsatz | Rückstau für 60 s |",
        "|---|---|---|---|---|",
    ]
    for abo in (SICHT_ABO, EXTERN_ABO):
        zeilen.append(
            f"| {abo} | {bericht['normal'][abo]['latenz_p95_s']} s | {bericht['vollabgleich'][abo]['latenz_p95_s']} s "
            f"| {bericht['rueckstau'][abo]['durchsatz_je_s']}/s | {bericht['rueckstau'][abo]['rueckstau_fuer_60_s']} |"
        )
    return "\n".join(zeilen) + "\n\n"


def test_normalbetrieb_und_naechtlicher_vollabgleich(probe: Probe) -> None:
    groesse_vorher = int(probe.abfragen("SELECT pg_total_relation_size('events_event')")[0][0])
    probe.worker(PROBE_BATCH="200")
    probe.warten(
        lambda: probe.anzahl("events_subscription") >= 2 and bool(probe.abfragen("SELECT 1 FROM events_lease")),
        120,
        "Worker bereit",
    )

    normal = probe.schreiben(100, objekte=100, abstand=0.05)
    probe.warten(lambda: _alle_da(probe), 60, "Normalbetrieb zugestellt")

    voll, schreibdauer = _vollabgleich(probe)
    nachlauf = probe.warten(lambda: _alle_da(probe), 3600, "Vollabgleich zugestellt")
    groesse_nachher = int(probe.abfragen("SELECT pg_total_relation_size('events_event')")[0][0])

    for abo in (SICHT_ABO, EXTERN_ABO):
        set_state(abo, SubscriptionState.PAUSIERT)
    rueckstau, _ = _vollabgleich(probe)
    probe.warten(lambda: not probe.abfragen("SELECT 1 FROM events_event WHERE seq IS NULL"), 600, "nummeriert")
    abbau: dict[str, float] = {}
    for abo in (SICHT_ABO, EXTERN_ABO):
        set_state(abo, SubscriptionState.AKTIV)
    beginn = time.monotonic()
    while len(abbau) < 2:
        hoechste = probe.abfragen("SELECT max(seq) FROM events_event")[0][0]
        for name, cursor in probe.abfragen(
            "SELECT name, cursor_seq FROM events_subscription WHERE name LIKE 'probe.%%'"
        ):
            if cursor >= hoechste and name not in abbau:
                abbau[name] = time.monotonic() - beginn
        assert time.monotonic() - beginn < 3600, "Rückstau nicht abgebaut"
        time.sleep(0.1)

    bericht: dict[str, Any] = {
        "ereignisse": EREIGNISSE,
        "schreiber": SCHREIBER,
        "batch": 200,
        "cpu": os.cpu_count(),
        "schreiben_je_s": round(EREIGNISSE / schreibdauer, 1),
        "nachlauf_nach_schreibende_s": round(nachlauf, 1),
        "journal_bytes_je_ereignis": round((groesse_nachher - groesse_vorher) / (len(normal) + len(voll))),
        "normal": {SICHT_ABO: _messung(probe, SICHT, normal), EXTERN_ABO: _messung(probe, EXTERN, normal)},
        "vollabgleich": {SICHT_ABO: _messung(probe, SICHT, voll), EXTERN_ABO: _messung(probe, EXTERN, voll)},
        "rueckstau": {
            abo: {
                "anzahl": len(rueckstau),
                "abbau_s": round(abbau[abo], 1),
                "durchsatz_je_s": round(len(rueckstau) / abbau[abo], 1),
                "rueckstau_fuer_60_s": round(len(rueckstau) / abbau[abo] * LATENZ_RUECKSTAU),
            }
            for abo in (SICHT_ABO, EXTERN_ABO)
        },
    }
    print(json.dumps(bericht, indent=2, ensure_ascii=False))
    ziel = os.environ.get("EVENTS_LAST_BERICHT")
    if ziel:
        Path(ziel).write_text(json.dumps(bericht, indent=2, ensure_ascii=False), encoding="utf-8")
    zusammenfassung = os.environ.get("GITHUB_STEP_SUMMARY")
    if zusammenfassung:  # in der CI: Zahlen jedes Laufs in der Zusammenfassung des Jobs
        with Path(zusammenfassung).open("a", encoding="utf-8") as datei:
            datei.write(_tabelle(bericht))

    for abo in (SICHT_ABO, EXTERN_ABO):
        assert bericht["normal"][abo]["latenz_p95_s"] <= LATENZ_NORMAL, abo
        assert bericht["vollabgleich"][abo]["latenz_p95_s"] <= LATENZ_RUECKSTAU, abo
        assert bericht["rueckstau"][abo]["durchsatz_je_s"] >= DURCHSATZ, abo
