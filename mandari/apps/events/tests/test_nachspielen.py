# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nachspielen ab Folgenummer oder Zeitpunkt (``dispatch.rewind``, ``events_dispatch --replay``, Issue #526).

Der Cursor geht nur zurück; danach stellt die Zustellung die Ereignisse erneut zu, der Handler muss
das vertragen.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.events import Delivery, subscriber
from apps.events.dispatch import deliver_batch, ensure_subscription, first_seq_since, rewind
from apps.events.models import Event, Subscription
from apps.events.registry import Subscriber
from apps.events.tests.hilfen import nummeriert

NAME = "test.nachspielen"


def _abonnement(zugestellt: list[int]) -> Subscriber:
    def handler(events: list[Event], delivery: Delivery) -> None:
        zugestellt.extend(ereignis.seq or 0 for ereignis in events)

    subscriber(NAME, types=["test.*"], transactional=False)(handler)
    from apps.events.registry import get

    return get(NAME)


@pytest.mark.django_db
def test_nachspielen_stellt_ab_folgenummer_erneut_zu(leeres_register: dict[str, Subscriber]) -> None:
    zugestellt: list[int] = []
    spec = _abonnement(zugestellt)
    ensure_subscription(spec)
    folgenummern = [nummeriert().seq or 0 for _ in range(4)]
    deliver_batch(spec)
    assert zugestellt == folgenummern

    assert rewind(NAME, folgenummern[2]) == (folgenummern[-1], folgenummern[1])
    deliver_batch(spec)
    assert zugestellt == folgenummern + folgenummern[2:]
    assert Subscription.objects.get(name=NAME).cursor_seq == folgenummern[-1]


@pytest.mark.django_db
def test_nachspielen_nur_rueckwaerts(leeres_register: dict[str, Subscriber]) -> None:
    spec = _abonnement([])
    ensure_subscription(spec)
    erstes = nummeriert().seq or 0
    with pytest.raises(ValueError, match="nur zurück"):
        rewind(NAME, erstes + 1)  # Cursor steht vor dem Ereignis: vorwärts übersprünge es
    with pytest.raises(ValueError):
        rewind(NAME, 0)
    assert rewind("gibt.es.nicht", 1) is None


@pytest.mark.django_db
def test_erste_folgenummer_seit_zeitpunkt() -> None:
    alt = nummeriert()
    Event.objects.filter(pk=alt.pk).update(recorded_at=timezone.now() - timedelta(days=2))
    neu = nummeriert()
    assert first_seq_since(timezone.now() - timedelta(days=1)) == neu.seq
    assert first_seq_since(timezone.now() + timedelta(days=1)) is None


@pytest.mark.django_db
def test_befehl_replay(leeres_register: dict[str, Subscriber]) -> None:
    spec = _abonnement([])
    ensure_subscription(spec)
    erstes, zweites = nummeriert(), nummeriert()
    deliver_batch(spec)

    ausgabe = StringIO()
    call_command("events_dispatch", "--replay", NAME, "--from-seq", str(zweites.seq), stdout=ausgabe)
    assert f"Cursor {zweites.seq} -> {erstes.seq}" in ausgabe.getvalue()

    Event.objects.filter(pk=erstes.pk).update(recorded_at=timezone.now() - timedelta(days=3))
    gestern = (timezone.localtime() - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")
    ausgabe = StringIO()
    call_command("events_dispatch", "--replay", NAME, "--since", gestern, stdout=ausgabe)
    assert f"-> {(erstes.seq or 0) - 1}" in ausgabe.getvalue()

    with pytest.raises(CommandError, match="genau eines"):
        call_command("events_dispatch", "--replay", NAME)
    with pytest.raises(CommandError, match="genau eines"):
        call_command("events_dispatch", "--replay", NAME, "--from-seq", "1", "--since", gestern)
    with pytest.raises(CommandError, match="Format"):
        call_command("events_dispatch", "--replay", NAME, "--since", "gestern")
    with pytest.raises(CommandError, match="Format"):
        call_command("events_dispatch", "--replay", NAME, "--since", "2026-13-01T00:00")
    with pytest.raises(CommandError, match="gibt es nicht"):
        call_command("events_dispatch", "--replay", "unbekannt", "--from-seq", "1")
