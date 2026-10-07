# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionen aus den Wortmeldungen der Live-Übertragungen (Issue #915, #916): Abonnement ``insight.fraktionen_live``.

- Registriert nur mit ``LIVE_UEBERTRAGUNG_AKTIV``.
- Eine eindeutig zugeordnete, zweimal gleich gelesene Wortmeldung ergibt über die echte Zustellung
  (``deliver_batch``) eine bestätigte Zuordnung, sonst einen Vorschlag; Funktionen und Wortmeldungen ohne Person
  ergeben nichts.
- Doppelte Zustellung und Nachholen zählen eine Wortmeldung nur einmal.
- ``manage.py fraktionen_aus_wortmeldungen`` verbucht vorhandene Wortmeldungen (eingespielte Protokolle).

Nur erfundene Namen (Musterstadt, „Fraktion A“).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db.models import Max

from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent
from apps.events.registry import Delivery, Subscriber, get
from hub.live import services
from hub.live.lesung import Lesung
from hub.live.models import Broadcast, BroadcastSpeech, BroadcastStatus, SpeechAssignment
from hub.live.profil import lade_profil, vorlage
from hub.live.tests.conftest import Welt, jetzt, welt  # noqa: F401 – Fixtures der Live-Übertragungen
from insight_core import subscribers
from insight_core.models import PersonFraktion, PersonFraktionBeleg
from insight_core.services import fraktionen_live
from insight_core.services.fraktionen_live import Ergebnis

pytestmark = pytest.mark.django_db

PROFIL = lade_profil(vorlage("balken_unten_dreizeilig"))
TYP = "ris.broadcast.speaker_changed"


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


@pytest.fixture
def laufend(welt: Welt, settings: Any) -> Broadcast:  # noqa: F811 – Fixture aus hub.live
    settings.LIVE_UEBERTRAGUNG_AKTIV = True
    return Broadcast.objects.create(
        source=welt.quelle, meeting=welt.sitzung, status=BroadcastStatus.LIVE, started_at=welt.jetzt
    )


def _abonnement() -> Subscriber:
    assert subscribers.register()
    spec = get(fraktionen_live.NAME)
    ensure_subscription(spec)
    return spec


def _nummerieren() -> None:
    """Folgenummern wie der Sequenzierer vergeben (in der Reihenfolge der Erfassung)."""
    hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
    for event in Event.objects.filter(seq__isnull=True).order_by("id"):
        hoechste += 1
        Event.objects.filter(pk=event.pk).update(seq=hoechste)


def _zustellen(spec: Subscriber) -> int:
    _nummerieren()
    return deliver_batch(spec).delivered


def _am_wort(welt: Welt, laufend: Broadcast, name: str, fraktion: str, sekunde: int) -> BroadcastSpeech:  # noqa: F811
    """Zwei gleiche Lesungen in Folge: eine neue Wortmeldung samt Ereignis ``speaker_changed``."""
    lesung = Lesung(balken=True, top="5", titel="Neubau einer Grundschule", name=name, fraktion=fraktion)
    services.lesung_verarbeiten(laufend.pk, lesung, welt.jetzt + timedelta(seconds=sekunde), PROFIL)
    ergebnis = services.lesung_verarbeiten(laufend.pk, lesung, welt.jetzt + timedelta(seconds=sekunde + 10), PROFIL)
    assert ergebnis.neue_wortmeldung is not None
    return ergebnis.neue_wortmeldung


def _wortmeldung(welt: Welt, laufend: Broadcast, **felder: Any) -> BroadcastSpeech:  # noqa: F811
    """Wortmeldung ohne Ereignis, wie sie ein eingespieltes Protokoll anlegt."""
    werte: dict[str, Any] = {
        "broadcast": laufend,
        "person": welt.muster,
        "name_read": "Erika Muster",
        "faction_read": "Fraktion A",
        "started_at": welt.jetzt,
        "assignment": SpeechAssignment.EINDEUTIG,
        "readings": 2,
    }
    werte.update(felder)
    return BroadcastSpeech.objects.create(**werte)


def test_abonnement_nur_mit_schalter(leeres_register: dict[str, Subscriber], settings: Any) -> None:
    settings.LIVE_UEBERTRAGUNG_AKTIV = False
    assert not subscribers.register()
    assert fraktionen_live.NAME not in leeres_register
    settings.LIVE_UEBERTRAGUNG_AKTIV = True
    assert subscribers.register()
    spec = leeres_register[fraktionen_live.NAME]
    assert spec.types == (TYP,) and spec.transactional and not spec.shadow


def test_wortmeldung_ergibt_bestaetigte_fraktion(
    leeres_register: dict[str, Subscriber],
    welt: Welt,  # noqa: F811
    laufend: Broadcast,
) -> None:
    spec = _abonnement()
    wortmeldung = _am_wort(welt, laufend, "Erika Muster", "Fraktion A", 0)
    assert Event.objects.filter(type=TYP).count() == 1

    assert _zustellen(spec) == 1

    zuordnung = PersonFraktion.objects.get(person=welt.muster)
    assert (zuordnung.bezeichnung, zuordnung.status, zuordnung.quelle) == ("Fraktion A", "bestaetigt", "einblendung")
    assert (zuordnung.body_id, zuordnung.belege) == (welt.body.pk, 1)
    assert zuordnung.zuletzt_gesehen == wortmeldung.started_at
    beleg = PersonFraktionBeleg.objects.get()
    assert (beleg.wortmeldung, beleg.zuordnung) == (wortmeldung.pk, zuordnung)
    assert not ParkedEvent.objects.exists()


def test_doppelte_zustellung_verbucht_einmal(
    leeres_register: dict[str, Subscriber],
    welt: Welt,  # noqa: F811
    laufend: Broadcast,
) -> None:
    """Zustellung mindestens einmal: Nachspielen zählt keine Wortmeldung doppelt."""
    spec = _abonnement()
    _am_wort(welt, laufend, "Erika Muster", "Fraktion A", 0)
    _am_wort(welt, laufend, "Max Beispiel", "Fraktion B", 60)
    _am_wort(welt, laufend, "Erika Muster", "Fraktion A", 120)
    assert _zustellen(spec) == 3
    vorher = sorted(PersonFraktion.objects.values_list("person_id", "bezeichnung", "status", "belege"))
    assert sorted((b, s, n) for _, b, s, n in vorher) == [
        ("Fraktion A", "bestaetigt", 2),
        ("Fraktion B", "bestaetigt", 1),
    ]

    erstes = Event.objects.filter(type=TYP).order_by("seq").first()
    assert erstes is not None and erstes.seq is not None
    rewind(fraktionen_live.NAME, erstes.seq)
    assert deliver_batch(spec).delivered == 3

    assert sorted(PersonFraktion.objects.values_list("person_id", "bezeichnung", "status", "belege")) == vorher
    assert PersonFraktionBeleg.objects.count() == 3
    assert not ParkedEvent.objects.exists()


def test_unsicher_oder_einmal_gelesen_ergibt_vorschlag(welt: Welt, laufend: Broadcast) -> None:  # noqa: F811
    unsicher = _wortmeldung(welt, laufend, assignment=SpeechAssignment.UNSICHER, readings=3)
    assert fraktionen_live.aus_wortmeldung(unsicher.pk) == Ergebnis.VERBUCHT
    zuordnung = PersonFraktion.objects.get(person=welt.muster)
    assert (zuordnung.status, zuordnung.belege) == ("vorschlag", 1)

    einmal = _wortmeldung(welt, laufend, person=welt.beispiel, name_read="Max Beispiel", readings=1)
    assert fraktionen_live.aus_wortmeldung(einmal.pk) == Ergebnis.VERBUCHT
    assert PersonFraktion.objects.get(person=welt.beispiel).status == "vorschlag"


def test_funktion_oder_ohne_person_ergibt_nichts(welt: Welt, laufend: Broadcast) -> None:  # noqa: F811
    funktion = _wortmeldung(welt, laufend, faction_read="", function_read="Bürgermeisterin")
    ohne_person = _wortmeldung(welt, laufend, person=None, assignment=SpeechAssignment.KEINE)
    # Die Einblendung zeigt eine Funktion an der Stelle der Fraktion: die Prüfung in fraktionen fängt sie ab
    verlesen = _wortmeldung(welt, laufend, faction_read="Oberbürgermeisterin")

    assert fraktionen_live.aus_wortmeldung(funktion.pk) == Ergebnis.OHNE_FRAKTION
    assert fraktionen_live.aus_wortmeldung(ohne_person.pk) == Ergebnis.OHNE_FRAKTION
    assert fraktionen_live.aus_wortmeldung(verlesen.pk) == Ergebnis.OHNE_FRAKTION
    assert fraktionen_live.aus_wortmeldung("00000000-0000-0000-0000-000000000000") == Ergebnis.FEHLT
    assert not PersonFraktion.objects.exists()


def test_schatten_verbucht_nichts(welt: Welt, laufend: Broadcast) -> None:  # noqa: F811
    wortmeldung = _wortmeldung(welt, laufend)
    event = Event(type=TYP, payload={"speech": str(wortmeldung.pk)})

    fraktionen_live.fraktionen_live([event], Delivery(subscription=fraktionen_live.NAME, shadow=True))
    assert not PersonFraktion.objects.exists()

    fraktionen_live.fraktionen_live([event], Delivery(subscription=fraktionen_live.NAME, shadow=False))
    assert PersonFraktion.objects.get().belege == 1


def test_befehl_leitet_fraktionen_aus_vorhandenen_wortmeldungen_ab(welt: Welt, laufend: Broadcast) -> None:  # noqa: F811
    """Eingespielte Protokolle legen Wortmeldungen ohne Ereignis an; der Befehl holt die Fraktionen nach."""
    _wortmeldung(welt, laufend)
    _wortmeldung(welt, laufend, started_at=welt.jetzt + timedelta(minutes=5))
    _wortmeldung(welt, laufend, person=welt.beispiel, name_read="Max Beispiel", faction_read="Fraktion B")
    _wortmeldung(welt, laufend, person=None, name_read="Gisela Gast", assignment=SpeechAssignment.KEINE)
    ausgabe = StringIO()

    call_command("fraktionen_aus_wortmeldungen", "--meeting", str(welt.sitzung.pk), "--probelauf", stdout=ausgabe)
    assert "3 Wortmeldungen mit Person und Fraktion: 3 verbucht" in ausgabe.getvalue()
    assert "Probelauf" in ausgabe.getvalue()
    assert not PersonFraktion.objects.exists() and not PersonFraktionBeleg.objects.exists()

    call_command("fraktionen_aus_wortmeldungen", "--body", str(welt.body.pk), stdout=ausgabe)
    erika = PersonFraktion.objects.get(person=welt.muster)
    assert (erika.bezeichnung, erika.status, erika.belege) == ("Fraktion A", "bestaetigt", 2)
    assert PersonFraktion.objects.get(person=welt.beispiel).bezeichnung == "Fraktion B"

    ausgabe = StringIO()
    call_command("fraktionen_aus_wortmeldungen", stdout=ausgabe)
    assert "0 verbucht, 3 schon verbucht" in ausgabe.getvalue()
    erika.refresh_from_db()
    assert erika.belege == 2, "ein erneuter Aufruf zählt nichts doppelt"


def test_befehl_prueft_kennungen() -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="UUID"):
        call_command("fraktionen_aus_wortmeldungen", "--meeting", "keine-uuid")
