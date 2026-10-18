# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``insight.verortung`` und Auftrag ``verortung_vorgang`` (Issue #919, ADR Dokumentkette, Abschnitt 9):
neuer Text stößt die Verortung seines Vorgangs an, statt erst in der Abfrage alle 15 Minuten aufzufallen.

- Markiert werden automatisch verortete Vorgänge (nie solche mit KI-Ergebnis) in Kommunen mit Straßenverzeichnis.
- Eingereiht wird nur mit ``TASKS_BACKEND=journal``; im Handler wird nie gerechnet.
- Doppelte Zustellung reiht nichts doppelt ein; Schatten zählt nur.
- Der Auftrag verortet genau einen Vorgang wie der Zeitplan; Zeitplan und Auftrag beanspruchen nur aus ``pending``.

Die Ereignisse laufen über die echte Zustellung (``deliver_batch``). Nur erfundene Namen.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from itertools import count
from typing import Any

import pytest
from django.tasks import task_backends
from prometheus_client import REGISTRY

from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent, Subscription, SubscriptionState, Task, TaskStatus
from apps.events.registry import Subscriber, get
from apps.events.tasks_backend import JournalBackend
from apps.events.tests.hilfen import nummeriert
from insight_core import subscribers
from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource, Street
from insight_core.services import georef_abonnement, georef_runner

pytestmark = pytest.mark.django_db

_nummer = count()
TYP = "ris.file.text_extracted"
TEXT = "Sanierung des Spielplatzes am Hafenweg"


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


@pytest.fixture
def journal(settings: Any) -> JournalBackend:
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    return backend


@pytest.fixture
def mit_strassen(geo_body: OParlBody, make_street: Callable[..., Street]) -> OParlBody:
    make_street(geo_body, "Hafenweg", 51.94, 7.63)
    return geo_body


def _abonnement(settings: Any, modus: str = "aktiv") -> Subscriber:
    settings.GEOREF_SUBSCRIPTION = modus
    assert subscribers.register_verortung()
    spec = get(georef_abonnement.NAME)
    ensure_subscription(spec)
    return spec


def _vorgang(body: OParlBody, status: str = "pending", **felder: Any) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"https://ris.beispielstadt.example/oparl/papers/{next(_nummer)}",
        body=body,
        name="Vorlage",
        georef_status=status,
        **felder,
    )


def _anlage(body: OParlBody, paper: OParlPaper | None, text: str = TEXT) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"https://ris.beispielstadt.example/oparl/files/{next(_nummer)}",
        body=body,
        paper=paper,
        text_content=text,
        text_extraction_status="completed",
    )


def _text_erkannt(datei: OParlFile | uuid.UUID) -> Event:
    kennung = datei.pk if isinstance(datei, OParlFile) else datei
    return nummeriert(
        type=TYP,
        aggregate_type="File",
        aggregate_id=kennung,
        tenant_ref=f"source:{uuid.uuid4()}",
        visibility="intern",
        payload={"file": str(kennung), "method": "pypdf", "characters": len(TEXT)},
    )


def _stand() -> dict[uuid.UUID, str]:
    return dict(OParlPaper.objects.values_list("pk", "georef_status"))


def _auftraege() -> list[tuple[str, str | None]]:
    return sorted(
        (str(zeile.args["args"][0]), zeile.idempotency_key)
        for zeile in Task.objects.filter(
            task_path=georef_runner.verortung_vorgang.module_path, status=TaskStatus.WARTEND
        )
    )


def _zaehler(target: str, result: str) -> float:
    labels = {"target": target, "result": result}
    return REGISTRY.get_sample_value("mandari_georef_subscription_total", labels) or 0.0


# --- Schalter -----------------------------------------------------------------------------------


def test_schalter_aus_registriert_nichts(settings: Any, leeres_register: dict[str, Subscriber]) -> None:
    settings.GEOREF_SUBSCRIPTION = "aus"
    assert not subscribers.register_verortung()
    assert georef_abonnement.NAME not in leeres_register


def test_schalter_registriert_transaktional_mit_schatten(settings: Any, leeres_register: dict[str, Subscriber]) -> None:
    spec = _abonnement(settings, "schatten")
    assert spec.transactional and spec.shadow
    assert spec.types == ("ris.file.text_extracted",)
    assert Subscription.objects.get(name=georef_abonnement.NAME).state == SubscriptionState.SCHATTEN


# --- Markieren und Einreihen --------------------------------------------------------------------


def test_neuer_text_markiert_und_reiht_ein(
    settings: Any, leeres_register: dict[str, Subscriber], mit_strassen: OParlBody, journal: JournalBackend
) -> None:
    spec = _abonnement(settings)
    wartend = _vorgang(mit_strassen, "pending")
    verortet = _vorgang(mit_strassen, "completed", georef_method="gazetteer")
    ohne_ort = _vorgang(mit_strassen, "ai_needed", georef_method="none")
    ereignisse = [_text_erkannt(_anlage(mit_strassen, paper)) for paper in (wartend, verortet, ohne_ort)]

    assert deliver_batch(spec).delivered == 3

    assert _stand() == {wartend.pk: "pending", verortet.pk: "pending", ohne_ort.pk: "pending"}
    erwartet = sorted(
        (str(paper.pk), f"{georef_runner.verortung_vorgang.module_path}:{paper.pk}:{ereignis.event_id}")
        for paper, ereignis in zip((wartend, verortet, ohne_ort), ereignisse, strict=True)
    )
    assert _auftraege() == erwartet
    # Im Handler wird nicht gerechnet: Noch keine Orte
    assert set(OParlPaper.objects.values_list("locations", flat=True)) == {None}
    assert not ParkedEvent.objects.exists()


def test_ki_ergebnisse_und_laufende_bleiben_unberuehrt(
    settings: Any, leeres_register: dict[str, Subscriber], mit_strassen: OParlBody, journal: JournalBackend
) -> None:
    """Der automatische Lauf ersetzte Orte der KI; die KI läuft nie automatisch."""
    spec = _abonnement(settings)
    vorgaenge = {
        _vorgang(mit_strassen, "completed", georef_method="gazetteer+ai").pk: "completed",
        _vorgang(mit_strassen, "no_locations", georef_method="none").pk: "no_locations",
        _vorgang(mit_strassen, "failed", georef_method="ai").pk: "failed",
        _vorgang(mit_strassen, "processing").pk: "processing",
    }
    for paper in OParlPaper.objects.all():
        _text_erkannt(_anlage(mit_strassen, paper))

    deliver_batch(spec)

    assert _stand() == vorgaenge
    assert _auftraege() == []


def test_nur_kommunen_mit_strassenverzeichnis_und_nicht_geloeschte(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    mit_strassen: OParlBody,
    journal: JournalBackend,
) -> None:
    spec = _abonnement(settings)
    quelle = OParlSource.objects.create(name="Andere", url="https://ris.anderswo.example/oparl/system")
    ohne_strassen = OParlBody.objects.create(
        external_id="https://ris.anderswo.example/oparl/bodies/1", source=quelle, name="Anderswo"
    )
    fremd = _vorgang(ohne_strassen, "completed", georef_method="gazetteer")
    geloescht = _vorgang(mit_strassen, "completed", georef_method="gazetteer", deleted=True)
    _text_erkannt(_anlage(ohne_strassen, fremd))
    _text_erkannt(_anlage(mit_strassen, geloescht))
    _text_erkannt(_anlage(mit_strassen, None))  # Sitzungsdokument ohne Vorgang
    _text_erkannt(uuid.uuid4())  # unbekannte Datei

    assert deliver_batch(spec).delivered == 4

    assert _stand() == {fremd.pk: "completed", geloescht.pk: "completed"}
    assert _auftraege() == []
    assert not ParkedEvent.objects.exists()


def test_ohne_journal_nur_markieren(
    settings: Any, leeres_register: dict[str, Subscriber], mit_strassen: OParlBody
) -> None:
    """Mit dem sofort ausführenden Backend liefe der Auftrag in der Zustellung; dann verortet der Zeitplan."""
    spec = _abonnement(settings)
    paper = _vorgang(mit_strassen, "completed", georef_method="gazetteer")
    _text_erkannt(_anlage(mit_strassen, paper))

    deliver_batch(spec)

    assert _stand() == {paper.pk: "pending"}
    assert not Task.objects.exists()
    assert OParlPaper.objects.get(pk=paper.pk).locations is None, "im Handler nicht gerechnet"


def test_automatische_verortung_aus_aendert_nichts(
    settings: Any, leeres_register: dict[str, Subscriber], mit_strassen: OParlBody, journal: JournalBackend
) -> None:
    settings.GEOREF_AUTO_ENABLED = False
    spec = _abonnement(settings)
    paper = _vorgang(mit_strassen, "completed", georef_method="gazetteer")
    _text_erkannt(_anlage(mit_strassen, paper))

    deliver_batch(spec)

    assert _stand() == {paper.pk: "completed"}
    assert _auftraege() == []


def test_schatten_zaehlt_nur(
    settings: Any, leeres_register: dict[str, Subscriber], mit_strassen: OParlBody, journal: JournalBackend
) -> None:
    spec = _abonnement(settings, "schatten")
    paper = _vorgang(mit_strassen, "completed", georef_method="gazetteer")
    vorher = (_zaehler("schatten", "markiert"), _zaehler("schatten", "eingereiht"))
    _text_erkannt(_anlage(mit_strassen, paper))

    deliver_batch(spec)

    assert _stand() == {paper.pk: "completed"}
    assert _auftraege() == []
    assert (_zaehler("schatten", "markiert") - vorher[0], _zaehler("schatten", "eingereiht") - vorher[1]) == (1, 1)


def test_doppelte_zustellung_reiht_einmal_ein(
    settings: Any, leeres_register: dict[str, Subscriber], mit_strassen: OParlBody, journal: JournalBackend
) -> None:
    """Zustellung mindestens einmal: Nachspielen ändert keinen Stand und reiht nichts doppelt ein."""
    spec = _abonnement(settings)
    erster = _vorgang(mit_strassen, "completed", georef_method="gazetteer")
    zweiter = _vorgang(mit_strassen, "pending")
    erstes = _text_erkannt(_anlage(mit_strassen, erster))
    _text_erkannt(_anlage(mit_strassen, erster))  # zweite Anlage desselben Vorgangs im selben Batch
    _text_erkannt(_anlage(mit_strassen, zweiter))
    assert deliver_batch(spec).delivered == 3
    stand, auftraege = _stand(), _auftraege()
    assert stand == {erster.pk: "pending", zweiter.pk: "pending"}
    assert len(auftraege) == 2, "ein Auftrag je Vorgang und Batch"

    rewind(georef_abonnement.NAME, erstes.seq or 1)
    assert deliver_batch(spec).delivered == 3

    assert _stand() == stand
    assert _auftraege() == auftraege
    assert not ParkedEvent.objects.exists()


# --- Auftrag ------------------------------------------------------------------------------------


def test_auftrag_verortet_den_vorgang(mit_strassen: OParlBody) -> None:
    paper = _vorgang(mit_strassen, "pending")
    _anlage(mit_strassen, paper)

    assert georef_runner.verortung_vorgang.call(str(paper.pk)) == georef_runner.VERORTET

    paper.refresh_from_db()
    assert paper.georef_status == "completed"
    assert [loc["name"] for loc in paper.locations or []] == ["Hafenweg"]
    # Erneut: schon verortet, nichts zu tun
    assert georef_runner.verortung_vorgang.call(str(paper.pk)) == georef_runner.NICHT_ZU_TUN


def test_auftrag_ohne_text_ohne_strassen_oder_beansprucht(
    mit_strassen: OParlBody, make_street: Callable[..., Street]
) -> None:
    ohne_text = _vorgang(mit_strassen, "pending")
    beansprucht = _vorgang(mit_strassen, "processing")
    _anlage(mit_strassen, beansprucht)
    quelle = OParlSource.objects.create(name="Andere", url="https://ris.anderswo.example/oparl/system")
    anderswo = OParlBody.objects.create(
        external_id="https://ris.anderswo.example/oparl/bodies/1", source=quelle, name="Anderswo"
    )
    ohne_strassen = _vorgang(anderswo, "pending")
    _anlage(anderswo, ohne_strassen)

    assert georef_runner.verortung_vorgang.call(str(ohne_text.pk)) == georef_runner.NICHT_ZU_TUN
    assert georef_runner.verortung_vorgang.call(str(beansprucht.pk)) == georef_runner.NICHT_ZU_TUN
    assert georef_runner.verortung_vorgang.call(str(ohne_strassen.pk)) == georef_runner.OHNE_STRASSEN
    assert _stand() == {ohne_text.pk: "pending", beansprucht.pk: "processing", ohne_strassen.pk: "pending"}


def test_zeitplan_verortet_keinen_schon_beanspruchten_vorgang(mit_strassen: OParlBody) -> None:
    """Hat der Auftrag den Vorgang zwischen Auswahl und Bearbeitung beansprucht, verortet ihn der Zeitplan nicht."""
    paper = _vorgang(mit_strassen, "pending")
    _anlage(mit_strassen, paper)
    stats = {"processed": 0, "completed": 0, "failed": 0}
    OParlPaper.objects.filter(pk=paper.pk).update(georef_status="processing")

    assert not georef_runner._georef_one(paper, stats)

    assert stats["processed"] == 0
    assert OParlPaper.objects.get(pk=paper.pk).locations is None
