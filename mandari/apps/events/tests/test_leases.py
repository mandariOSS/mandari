# SPDX-License-Identifier: AGPL-3.0-or-later
"""Leader-Leases ohne sitzungsgebundene Sperren (Issue #503): Übernahme, Erneuerung, Abgrenzung."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from unittest import mock

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.events import leases
from apps.events.models import Lease
from apps.events.tests.hilfen import nur_postgres

NAME = "test-rolle"


@pytest.mark.django_db
def test_erster_prozess_bekommt_die_lease_ein_zweiter_nicht() -> None:
    assert leases.acquire(NAME, "a")
    assert not leases.acquire(NAME, "b")
    assert Lease.objects.get(name=NAME).holder == "a"


@pytest.mark.django_db
def test_inhaber_verlaengert_seine_lease() -> None:
    assert leases.acquire(NAME, "a", ttl=timedelta(seconds=5))
    vorher = Lease.objects.get(name=NAME).expires_at

    assert leases.acquire(NAME, "a", ttl=timedelta(seconds=60))
    assert Lease.objects.get(name=NAME).expires_at > vorher


@pytest.mark.django_db
def test_abgelaufene_lease_darf_ein_anderer_uebernehmen() -> None:
    assert leases.acquire(NAME, "a")
    Lease.objects.filter(name=NAME).update(expires_at=timezone.now() - timedelta(seconds=1))

    assert leases.acquire(NAME, "b")
    assert Lease.objects.get(name=NAME).holder == "b"
    assert not leases.acquire(NAME, "a")


@pytest.mark.django_db
def test_nur_der_inhaber_gibt_frei() -> None:
    assert leases.acquire(NAME, "a")
    leases.release(NAME, "b")
    assert Lease.objects.filter(name=NAME).exists()

    leases.release(NAME, "a")
    assert not Lease.objects.filter(name=NAME).exists()
    assert leases.acquire(NAME, "b")


@pytest.mark.django_db(transaction=True)
def test_fence_braucht_eine_transaktion_und_die_eigene_lease() -> None:
    assert leases.acquire(NAME, "a")
    with pytest.raises(RuntimeError):
        leases.fence(NAME, "a")
    with transaction.atomic():
        leases.fence(NAME, "a")
    with pytest.raises(leases.LeaseLostError), transaction.atomic():
        leases.fence(NAME, "b")


@pytest.mark.django_db
def test_fence_gilt_auch_nach_ablauf_solange_niemand_uebernommen_hat() -> None:
    assert leases.acquire(NAME, "a")
    Lease.objects.filter(name=NAME).update(expires_at=timezone.now() - timedelta(minutes=5))
    with transaction.atomic():
        leases.fence(NAME, "a")


@pytest.mark.django_db
def test_ablauf_richtet_sich_nach_der_datenbankzeit_nicht_nach_der_prozessuhr() -> None:
    """Worker auf mehreren Rechnern: Eine vorgehende Uhr darf keine gültige Lease übernehmen und keine
    Lease weit in die Zukunft verlängern; eine nachgehende nicht an einer abgelaufenen festhalten."""
    assert leases.acquire(NAME, "a")
    echte_zeit = timezone.now()

    with mock.patch("django.utils.timezone.now", return_value=echte_zeit + timedelta(minutes=5)):
        assert not leases.acquire(NAME, "b"), "Uhr von b geht vor: die Lease von a ist noch gültig"
        assert leases.acquire(NAME, "a")
    assert Lease.objects.get(name=NAME).expires_at < timezone.now() + leases.LEASE_TTL + timedelta(seconds=30)

    Lease.objects.filter(name=NAME).update(expires_at=timezone.now() - timedelta(seconds=1))
    with mock.patch("django.utils.timezone.now", return_value=echte_zeit - timedelta(minutes=5)):
        assert leases.acquire(NAME, "b"), "Uhr von b geht nach: die Lease von a ist trotzdem abgelaufen"
    assert Lease.objects.get(name=NAME).holder == "b"


def test_kennung_eines_prozesses_ist_eindeutig() -> None:
    assert leases.new_holder_id() != leases.new_holder_id()


@pytest.mark.django_db(transaction=True)
def test_uebernahme_wartet_auf_die_transaktion_des_bisherigen_inhabers() -> None:
    """Die Abgrenzung sperrt die Lease-Zeile bis zum Commit; eine Übernahme kommt erst danach."""
    nur_postgres()
    assert leases.acquire(NAME, "a")
    Lease.objects.filter(name=NAME).update(expires_at=timezone.now() - timedelta(seconds=1))
    uebernommen = threading.Event()

    def uebernehmen() -> None:
        try:
            if leases.acquire(NAME, "b"):
                uebernommen.set()
        finally:
            connection.close()

    with transaction.atomic():
        leases.fence(NAME, "a")
        faden = threading.Thread(target=uebernehmen)
        faden.start()
        time.sleep(0.5)
        assert not uebernommen.is_set(), "die Übernahme darf nicht in die laufende Transaktion fallen"
    faden.join(timeout=10)

    assert uebernommen.is_set()
    assert Lease.objects.get(name=NAME).holder == "b"
