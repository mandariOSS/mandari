# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusagen der Ereignistechnik, die keine eigene Datei haben (Issue #514,
``docs/EREIGNISTECHNIK_NACHWEISE.md``): Reihenfolge je Objekt bei verschachtelten Transaktionen und
ein Doppelzustellungstest für jedes Abonnement.
"""

from __future__ import annotations

import importlib
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.conf import settings as django_settings

from apps.events import registry
from apps.events.dispatch import deliver_batch
from apps.events.models import Event
from apps.events.registry import Delivery, Subscriber, subscriber
from apps.events.sequencer import Sequencer
from apps.events.tests.hilfen import Sicht, folgenummern, nur_postgres, roh_einfuegen

#: Je Abonnement der Test, der jedes Ereignis zweimal zustellt und denselben Zustand verlangt
#: (``<Pfad>::<Testname>``, Pfad relativ zu ``mandari/``). Ein neues Abonnement braucht hier einen
#: Eintrag; Zustellung mindestens einmal heißt, jeder Handler muss eine Wiederholung vertragen.
DOPPELZUSTELLUNG = {
    "benachrichtigung": (
        "apps/work/notifications/tests/test_benachrichtigung_abonnement.py::test_doppelte_zustellung_benachrichtigt_einmal"
    ),
    "ris.session_projektor": (
        "apps/session/tests/test_ris_projektor.py::test_doppelte_zustellung_aendert_die_schatten_quelle_nicht"
    ),
    "suchindex": "insight_search/tests/test_suchindex_abonnement.py::test_wiederholung_und_veralteter_stand_schaden_nicht",
}
#: Schalter, unter denen sich Abonnements registrieren (alle eingeschaltet, damit keines fehlt)
SCHALTER = {
    "SEARCH_INDEX_SUBSCRIPTION": "aktiv",
    "WORK_NOTIFICATION_SUBSCRIPTION": "aktiv",
    "RIS_SESSION_PROJECTOR": "schatten",
}


@pytest.mark.django_db(transaction=True)
def test_verschachtelte_transaktionen_halten_die_reihenfolge_je_objekt(
    pg_verbindungen: Any, leeres_register: dict[str, Subscriber], sicht: Sicht
) -> None:
    """
    Sicherungspunkte gehören zur äußeren Transaktion: Ihre Ereignisse bekommen deren Kennung und
    damit deren Platz, auch wenn eine jüngere Transaktion dasselbe Objekt vorher festschreibt.
    Zurückgerollte Sicherungspunkte hinterlassen nichts.
    """
    nur_postgres()
    objekt = uuid.uuid4()
    aussen, juenger = pg_verbindungen(autocommit=False), pg_verbindungen(autocommit=False)

    erstes = roh_einfuegen(aussen, objekt)
    aussen.execute("SAVEPOINT verworfen")
    verworfen = roh_einfuegen(aussen, objekt)
    aussen.execute("ROLLBACK TO SAVEPOINT verworfen")
    aussen.execute("SAVEPOINT innen")
    aussen.execute("SAVEPOINT tiefer")
    tief = roh_einfuegen(aussen, objekt)
    aussen.execute("RELEASE SAVEPOINT tiefer")
    innen = roh_einfuegen(aussen, objekt)
    aussen.execute("RELEASE SAVEPOINT innen")
    spaeter = roh_einfuegen(juenger, objekt)
    juenger.commit()  # vor der äußeren festgeschrieben

    sequenzierer = Sequencer()
    sequenzierer.drain()
    assert folgenummern([spaeter]) == [None], "wartet auf die ältere, noch offene Transaktion"
    aussen.commit()
    # In der CI halten offene Transaktionen paralleler Testprozesse die Grenze clusterweit kurz auf
    ende = time.monotonic() + 60
    while None in folgenummern([erstes, tief, innen, spaeter]):
        assert time.monotonic() < ende, "nicht alle Ereignisse nummeriert"
        sequenzierer.drain()
        time.sleep(0.05)
    sequenzierer.release()

    nummern = folgenummern([erstes, tief, innen, spaeter, verworfen])
    assert nummern[-1] is None and not Event.objects.filter(event_id=verworfen).exists()
    assert nummern[:4] == sorted(n or 0 for n in nummern[:4]), "Schreibreihenfolge, die jüngere danach"

    def handler(events: list[Event], delivery: Delivery) -> None:
        sicht.schreiben(events)

    subscriber("test.verschachtelt", types=["test.*"], from_beginning=True)(handler)
    while deliver_batch(registry.get("test.verschachtelt")).more:
        pass
    reihenfolge = {seq: kennung for kennung, seq in zip([erstes, tief, innen, spaeter], nummern, strict=False)}
    assert [reihenfolge[seq] for seq in sicht.seqs()] == [erstes, tief, innen, spaeter]


def test_jedes_abonnement_hat_einen_doppelzustellungstest(
    leeres_register: dict[str, Subscriber], settings: Any
) -> None:
    for name, wert in SCHALTER.items():
        setattr(settings, name, wert)
    for app in apps.get_app_configs():
        try:
            modul = importlib.import_module(f"{app.name}.subscribers")
        except ModuleNotFoundError as exc:
            if exc.name != f"{app.name}.subscribers":
                raise
            continue
        importlib.reload(modul)  # registriert unter den Schaltern neu (eigenes Register des Tests)

    namen = {spec.name for spec in registry.registered()}
    assert "suchindex" in namen, "Schalter greifen nicht; die Prüfung sähe keine Abonnements"
    fehlend = sorted(namen - set(DOPPELZUSTELLUNG))
    assert not fehlend, f"Doppelzustellungstest fehlt in DOPPELZUSTELLUNG für: {fehlend}"
    basis = Path(django_settings.BASE_DIR)
    for name, verweis in DOPPELZUSTELLUNG.items():
        datei, test = verweis.split("::")
        assert f"def {test}(" in (basis / datei).read_text(encoding="utf-8"), f"{name}: {verweis} fehlt"
