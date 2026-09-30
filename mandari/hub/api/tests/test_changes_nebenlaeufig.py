# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsfeed unter Nebenläufigkeit (Issue #562), nur gegen PostgreSQL (CI).

Fitnessfunktion aus ``docs/adr/20260929-aenderungsfeed-format.md``: Parallele Schreiber legen in eigenen
Transaktionen Ereignisse an (manche verwerfen sie wieder), der Sequenzierer vergibt die Folgenummern
nach dem Commit, und ein Abnehmer liest währenddessen den Feed seitenweise. Er sieht jede
festgeschriebene öffentliche Änderung seiner Kommune genau einmal und in der Reihenfolge der
Folgenummern – Verworfenes, Nichtöffentliches und die Änderungen anderer Kommunen nie.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.test import Client, override_settings

from apps.events.models import Event
from apps.events.sequencer import Sequencer
from apps.events.tests.hilfen import nur_postgres
from hub.api import changes
from hub.api.tests.ereignisse import huelle, schreiben
from insight_core.models import OParlBody, OParlPaper, OParlSource

#: Frist für alles, was auf den Sequenzierer wartet (Last in der CI)
FRIST = 120.0
RIS = "https://ris.example/oparl"
SCHREIBER = 8
TRANSAKTIONEN = 6


class _VerworfenError(Exception):
    """Bricht die Transaktion eines Schreibers ab."""


@pytest.mark.django_db(transaction=True)
def test_abnehmer_sieht_jede_festgeschriebene_oeffentliche_aenderung_genau_einmal() -> None:
    nur_postgres()
    cache.clear()
    saat = random.randrange(1_000_000)
    hinweis = f"Saat {saat}"
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
    andere = uuid.uuid4()
    pfad = f"/oparl/v1/body/{body.pk}/changes"

    sperre = threading.Lock()
    festgeschrieben: set[str] = set()
    verborgen: set[str] = set()
    fehler: list[BaseException] = []
    start = threading.Barrier(SCHREIBER)

    def schreiben_lassen(nummer: int) -> None:
        zufall = random.Random(saat + nummer)
        try:
            start.wait(FRIST)
            for _ in range(TRANSAKTIONEN):
                oeffentlich: list[str] = []
                nicht_sichtbar: list[str] = []
                verwerfen = zufall.random() < 0.2
                try:
                    with transaction.atomic():
                        for _ in range(zufall.randint(1, 3)):
                            objekt = uuid.uuid4()
                            wurf = zufall.random()
                            if wurf < 0.6:
                                # Wie beim Ingestor: Objekt im Bestand und Ereignis in einer Transaktion
                                OParlPaper.objects.create(id=objekt, external_id=f"{RIS}/paper/{objekt}", body=body)
                                schreiben(huelle("ris.paper.changed", body.pk, objekt), nummeriert=False)
                                oeffentlich.append(str(objekt))
                            elif wurf < 0.8:
                                schreiben(
                                    huelle("ris.paper.changed", body.pk, objekt, sichtbarkeit="nichtoeffentlich"),
                                    nummeriert=False,
                                )
                                nicht_sichtbar.append(str(objekt))
                            else:
                                schreiben(huelle("ris.paper.changed", andere, objekt), nummeriert=False)
                                nicht_sichtbar.append(str(objekt))
                        # Transaktionen überlappen sich und enden in anderer Reihenfolge, als sie begonnen haben
                        time.sleep(zufall.uniform(0.0, 0.15))
                        if verwerfen:
                            raise _VerworfenError
                except _VerworfenError:
                    with sperre:
                        verborgen.update(oeffentlich + nicht_sichtbar)
                else:
                    with sperre:
                        festgeschrieben.update(oeffentlich)
                        verborgen.update(nicht_sichtbar)
                time.sleep(zufall.uniform(0.0, 0.05))
        except BaseException as exc:  # noqa: BLE001 – im Hauptfaden prüfen
            fehler.append(exc)
        finally:
            connection.close()

    stop = threading.Event()
    sequenzierer = Sequencer(batch_size=5)

    def sequenzieren() -> None:
        try:
            while not stop.is_set():
                sequenzierer.drain()
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001 – im Hauptfaden prüfen
            fehler.append(exc)
        finally:
            sequenzierer.release()
            connection.close()

    schreiber = [threading.Thread(target=schreiben_lassen, args=(nummer,)) for nummer in range(SCHREIBER)]
    nummerierer = threading.Thread(target=sequenzieren)
    gesehen: list[str] = []
    # Der Abnehmer beginnt mit dem Cursor einer leeren Kommune, wie nach einem Snapshot vor dem ersten Ereignis.
    # Die Sequenz der Testdatenbank zählt über die Tests hinweg weiter; eine Lücke am Anfang ist kein Aufräumen
    stand = {"after": changes.encode_cursor(body.pk, 0, changes.today())}

    def lesen() -> int:
        """Eine Seite lesen, wie es ein Abnehmer täte; gibt die Zahl der Einträge zurück."""
        antwort = Client().get(pfad, {**stand, "limit": "7"})
        assert antwort.status_code == 200, (antwort.status_code, antwort.content, hinweis)
        seite: dict[str, Any] = antwort.json()
        gesehen.extend(eintrag["id"].rsplit("/", 1)[1] for eintrag in seite["data"])
        stand["after"] = seite["cursor"]
        return len(seite["data"])

    with override_settings(OPARL_CHANGES_ENABLED=True, OPARL_API_RATE_LIMIT=0):
        for faden in [nummerierer, *schreiber]:
            faden.start()
        try:
            # Der Abnehmer liest, während geschrieben und nummeriert wird
            while any(faden.is_alive() for faden in schreiber):
                lesen()
                time.sleep(0.02)
            for faden in schreiber:
                faden.join(timeout=FRIST)
            ende = time.monotonic() + FRIST
            while Event.objects.filter(seq__isnull=True).exists() or lesen():
                assert time.monotonic() < ende, f"nicht alle Ereignisse nummeriert und gelesen ({hinweis})"
                time.sleep(0.05)
        finally:
            stop.set()
            nummerierer.join(timeout=60)

    assert not fehler, (fehler, hinweis)
    assert festgeschrieben, f"der Test soll festgeschriebene Änderungen enthalten ({hinweis})"
    assert len(gesehen) == len(set(gesehen)), f"kein Eintrag doppelt ({hinweis})"
    assert set(gesehen) == festgeschrieben, f"jede festgeschriebene öffentliche Änderung, sonst nichts ({hinweis})"
    assert not set(gesehen) & verborgen, hinweis
    nummern = dict(Event.objects.filter(body_id=body.pk).values_list("aggregate_id", "seq"))
    folge = [nummern[uuid.UUID(kennung)] or 0 for kennung in gesehen]
    assert folge == sorted(folge), f"Reihenfolge der Folgenummern ({hinweis})"
