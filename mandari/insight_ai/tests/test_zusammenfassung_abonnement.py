# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``insight.zusammenfassung`` (Issue #919, ADR Dokumentkette, Abschnitt 9): neuer Text verwirft die
KI-Zusammenfassung seines Vorgangs, erzeugt aber keine neue.

Die Ereignisse laufen über die echte Zustellung (``deliver_batch``). Nur erfundene Namen.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from itertools import count
from typing import Any

import pytest
from prometheus_client import REGISTRY

from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent, Subscription, SubscriptionState
from apps.events.registry import Subscriber, get
from apps.events.tests.hilfen import nummeriert
from insight_ai import abonnement, subscribers
from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

_nummer = count()
TYP = "ris.file.text_extracted"


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


@pytest.fixture
def body() -> OParlBody:
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    return OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt")


def _abonnement(settings: Any, modus: str = "aktiv") -> Subscriber:
    settings.SUMMARY_SUBSCRIPTION = modus
    assert subscribers.register()
    spec = get(abonnement.NAME)
    ensure_subscription(spec)
    return spec


def _vorgang(body: OParlBody, summary: str | None = "Kurzfassung") -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"https://ris.example.org/oparl/paper/{next(_nummer)}", body=body, name="Vorlage", summary=summary
    )


def _anlage(body: OParlBody, paper: OParlPaper | None = None, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"https://ris.example.org/oparl/file/{next(_nummer)}",
        body=body,
        paper=paper,
        text_content="Neuer Text",
        text_extraction_status="completed",
        **felder,
    )


def _text_erkannt(datei: OParlFile | uuid.UUID) -> Event:
    kennung = datei.pk if isinstance(datei, OParlFile) else datei
    return nummeriert(
        type=TYP,
        aggregate_type="File",
        aggregate_id=kennung,
        tenant_ref=f"source:{uuid.uuid4()}",
        visibility="intern",
        payload={"file": str(kennung), "method": "pypdf", "characters": 10},
    )


def _zusammenfassungen() -> dict[uuid.UUID, str | None]:
    return dict(OParlPaper.objects.values_list("pk", "summary"))


def _zaehler(target: str) -> float:
    labels = {"target": target, "result": "verworfen"}
    return REGISTRY.get_sample_value("mandari_summary_subscription_total", labels) or 0.0


def test_schalter_aus_registriert_nichts(settings: Any, leeres_register: dict[str, Subscriber]) -> None:
    settings.SUMMARY_SUBSCRIPTION = "aus"
    assert not subscribers.register()
    assert abonnement.NAME not in leeres_register


def test_schalter_registriert_transaktional_mit_schatten(settings: Any, leeres_register: dict[str, Subscriber]) -> None:
    spec = _abonnement(settings, "schatten")
    assert spec.transactional and spec.shadow
    assert spec.types == ("ris.file.text_extracted",)
    assert Subscription.objects.get(name=abonnement.NAME).state == SubscriptionState.SCHATTEN


def test_neuer_text_verwirft_nur_die_zusammenfassung_seines_vorgangs(
    settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody
) -> None:
    spec = _abonnement(settings)
    betroffen, andere = _vorgang(body), _vorgang(body)
    datei = _anlage(body, betroffen)
    _anlage(body, andere)
    vorher = _zaehler("live")
    _text_erkannt(datei)

    assert deliver_batch(spec).delivered == 1

    assert _zusammenfassungen() == {betroffen.pk: None, andere.pk: "Kurzfassung"}
    assert _zaehler("live") - vorher == 1
    # Verworfen, nicht neu erzeugt; der Text der Anlage bleibt unberührt
    assert OParlFile.objects.get(pk=datei.pk).text_content == "Neuer Text"
    assert not ParkedEvent.objects.exists()


def test_ohne_vorgang_oder_unbekannt_ohne_wirkung(
    settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody
) -> None:
    spec = _abonnement(settings)
    paper = _vorgang(body)
    from django.utils import timezone

    sitzung = OParlMeeting.objects.create(
        external_id="https://ris.example.org/oparl/meeting/1", body=body, name="Rat", start=timezone.now()
    )
    _text_erkannt(_anlage(body, None, meeting=sitzung))
    _text_erkannt(uuid.uuid4())

    assert deliver_batch(spec).delivered == 2
    assert _zusammenfassungen() == {paper.pk: "Kurzfassung"}
    assert not ParkedEvent.objects.exists()


def test_schatten_zaehlt_nur(settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody) -> None:
    spec = _abonnement(settings, "schatten")
    paper = _vorgang(body)
    vorher = _zaehler("schatten")
    _text_erkannt(_anlage(body, paper))

    deliver_batch(spec)

    assert _zusammenfassungen() == {paper.pk: "Kurzfassung"}
    assert _zaehler("schatten") - vorher == 1


def test_schalter_aktiv_verwirft_nicht_solange_die_datenbank_schatten_sagt(
    settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody
) -> None:
    """Wie beim Suchindex: Erst Schalter und Zustand in der Datenbank zusammen machen das Abonnement wirksam."""
    spec = _abonnement(settings, "schatten")
    settings.SUMMARY_SUBSCRIPTION = "aktiv"
    paper = _vorgang(body)
    _text_erkannt(_anlage(body, paper))

    deliver_batch(spec)

    assert _zusammenfassungen() == {paper.pk: "Kurzfassung"}


def test_doppelte_zustellung_verwirft_einmal(
    settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody
) -> None:
    """Zustellung mindestens einmal: Nachspielen ändert nichts mehr und parkt nichts."""
    spec = _abonnement(settings)
    erster, zweiter, ohne = _vorgang(body), _vorgang(body), _vorgang(body, summary=None)
    erstes = _text_erkannt(_anlage(body, erster))
    _text_erkannt(_anlage(body, zweiter))
    _text_erkannt(_anlage(body, erster))
    _text_erkannt(_anlage(body, ohne))
    assert deliver_batch(spec).delivered == 4
    vorher = _zusammenfassungen()
    assert vorher == {erster.pk: None, zweiter.pk: None, ohne.pk: None}
    zaehler = _zaehler("live")

    rewind(abonnement.NAME, erstes.seq or 1)
    assert deliver_batch(spec).delivered == 4

    assert _zusammenfassungen() == vorher
    assert _zaehler("live") == zaehler
    assert not ParkedEvent.objects.exists()
