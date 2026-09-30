# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tabellen und Datenbankobjekte der Ereignistechnik (Issue #484).

- Spaltenstandards liegen in der Datenbank, damit auch der Ingestor ohne Django schreiben kann.
- ``xid`` ist die Kennung der schreibenden (Haupt-)Transaktion, ``seq`` bleibt beim Schreiben leer.
- Der Weckruf-Trigger meldet sich erst beim Commit, einmal je Transaktion, nach Rollback nie.
- Die Migration lässt sich zurück- und wieder einspielen.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, cast

import psycopg
import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Field
from django.db.models.functions import Now

from apps.events.models import (
    NOTIFY_CHANNEL,
    SEQUENCE_NAME,
    Event,
    Lease,
    Operation,
    ParkedEvent,
    ParkedState,
    Subscription,
    SubscriptionState,
    Task,
    TaskStatus,
)
from apps.events.tests.hilfen import ereignis_anlegen, ereignis_daten, nur_postgres

Verbindungen = Callable[..., psycopg.Connection[Any]]


def _wert(sql: str, *params: Any) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, list(params))
        zeile = cursor.fetchone()
    assert zeile is not None
    return zeile[0]


# --- Tabellen und Standards (SQLite und PostgreSQL) -------------------------------------------


@pytest.mark.django_db
def test_neues_ereignis_bekommt_standards_der_datenbank_und_keine_folgenummer() -> None:
    with transaction.atomic():
        ereignis = ereignis_anlegen()

    ereignis.refresh_from_db()
    assert ereignis.operation == Operation.UPSERT
    assert ereignis.recorded_at is not None
    assert ereignis.seq is None
    assert isinstance(ereignis.xid, int)
    assert isinstance(ereignis.event_id, uuid.UUID)


@pytest.mark.django_db
def test_rohes_insert_ohne_optionale_spalten_nutzt_die_standards() -> None:
    """So schreibt später der Ingestor: nur Pflichtfelder, Standards setzt die Datenbank."""
    daten = ereignis_daten(event_id=uuid.uuid4())
    felder = ["event_id", "type", "version", "aggregate_type", "aggregate_id", "tenant_ref", "visibility"]
    felder += ["occurred_at", "correlation_id", "payload"]
    werte = [
        cast("Field[Any, Any]", Event._meta.get_field(name)).get_db_prep_save(daten[name], connection)
        for name in felder
    ]
    platzhalter = ", ".join(["%s"] * len(felder))
    with connection.cursor() as cursor:
        cursor.execute(f"INSERT INTO events_event ({', '.join(felder)}) VALUES ({platzhalter})", werte)

    ereignis = Event.objects.get()
    assert ereignis.operation == Operation.UPSERT
    assert ereignis.recorded_at is not None
    assert ereignis.seq is None


@pytest.mark.django_db
def test_gespeichertes_ereignis_laesst_sich_aendern_und_nach_xid_finden() -> None:
    """Neutralisieren der Nutzlast (DSGVO) speichert ein vorhandenes Ereignis erneut, samt ``xid``."""
    with transaction.atomic():
        ereignis = ereignis_anlegen()
    ereignis.refresh_from_db()
    xid = ereignis.xid

    ereignis.payload = {}
    ereignis.save()

    ereignis.refresh_from_db()
    assert ereignis.payload == {}
    assert ereignis.xid == xid
    assert Event.objects.filter(xid=xid).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("feld", "wert"),
    [("visibility", "geheim"), ("operation", "ersetzen"), ("version", 0)],
)
def test_ungueltige_werte_lehnt_die_datenbank_ab(feld: str, wert: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ereignis_anlegen(**{feld: wert})


@pytest.mark.django_db
def test_folgenummer_und_ereignis_id_sind_eindeutig() -> None:
    erstes = ereignis_anlegen()
    Event.objects.filter(pk=erstes.pk).update(seq=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        ereignis_anlegen(event_id=erstes.event_id)
    zweites = ereignis_anlegen()
    with pytest.raises(IntegrityError), transaction.atomic():
        Event.objects.filter(pk=zweites.pk).update(seq=1)


@pytest.mark.django_db
def test_abonnement_beginnt_aktiv_bei_cursor_null() -> None:
    abonnement = Subscription.objects.create(name="suchindex")
    abonnement.refresh_from_db()

    assert abonnement.cursor_seq == 0
    assert abonnement.state == SubscriptionState.AKTIV
    assert abonnement.updated_at is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        Subscription.objects.filter(pk="suchindex").update(state="angehalten")


@pytest.mark.django_db
def test_ereignis_wird_je_abonnement_nur_einmal_geparkt() -> None:
    objekt = uuid.uuid4()
    ParkedEvent.objects.create(
        subscription="suchindex", event_seq=7, aggregate_id=objekt, state=ParkedState.WIEDERHOLEN
    )
    ParkedEvent.objects.create(subscription="feed", event_seq=7, aggregate_id=objekt, state=ParkedState.TOT)

    with pytest.raises(IntegrityError), transaction.atomic():
        ParkedEvent.objects.create(
            subscription="suchindex", event_seq=7, aggregate_id=objekt, state=ParkedState.BLOCKIERT
        )
    assert ParkedEvent.objects.get(subscription="suchindex").attempts == 0


@pytest.mark.django_db
def test_auftrag_standards_und_eindeutiger_idempotenzschluessel() -> None:
    auftrag = Task.objects.create(queue="mail", task_path="apps.beispiel.tasks.senden", args={"id": 1})
    auftrag.refresh_from_db()

    assert auftrag.status == TaskStatus.WARTEND
    assert (auftrag.priority, auftrag.attempts, auftrag.max_attempts) == (50, 0, 8)
    assert auftrag.run_after is not None and auftrag.created_at is not None
    # Ohne Schlüssel beliebig viele, mit Schlüssel höchstens einer
    Task.objects.create(queue="mail", task_path="apps.beispiel.tasks.senden")
    Task.objects.create(queue="mail", task_path="apps.beispiel.tasks.senden", idempotency_key="e1:empfaenger")
    with pytest.raises(IntegrityError), transaction.atomic():
        Task.objects.create(queue="mail", task_path="apps.beispiel.tasks.senden", idempotency_key="e1:empfaenger")


@pytest.mark.django_db
def test_lease_ist_eindeutig_je_rolle() -> None:
    Lease.objects.create(name="sequencer", holder="worker-1", expires_at=Now())
    with pytest.raises(IntegrityError), transaction.atomic():
        Lease.objects.create(name="sequencer", holder="worker-2", expires_at=Now())


# --- Nur PostgreSQL: Transaktionskennung, Sequenz, Weckruf ------------------------------------


@pytest.mark.django_db
def test_xid_ist_die_kennung_der_haupttransaktion_auch_im_sicherungspunkt() -> None:
    nur_postgres()
    with transaction.atomic():
        aussen = ereignis_anlegen()
        with transaction.atomic():  # Sicherungspunkt
            innen = ereignis_anlegen()
        eigene = int(_wert("SELECT pg_current_xact_id()::text"))

    aussen.refresh_from_db()
    innen.refresh_from_db()
    assert aussen.xid == innen.xid == eigene
    assert (
        _wert(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'events_event'::regclass AND attname = 'xid'"
        )
        == "xid8"
    )


@pytest.mark.django_db
def test_sequenz_fuer_die_folgenummer_existiert_und_gehoert_keiner_spalte() -> None:
    nur_postgres()
    assert _wert("SELECT to_regclass(%s)::text", SEQUENCE_NAME) == SEQUENCE_NAME
    # Kein OWNED BY: Ein Umbau des Journals darf den Zählerstand nicht mitlöschen
    assert _wert("SELECT count(*) FROM pg_depend WHERE objid = %s::regclass AND deptype = 'a'", SEQUENCE_NAME) == 0
    erste = _wert("SELECT nextval(%s)", SEQUENCE_NAME)
    assert _wert("SELECT nextval(%s)", SEQUENCE_NAME) > erste


def _meldungen(zuhoerer: psycopg.Connection[Any], wartezeit: float = 0.5) -> list[str]:
    return [meldung.payload for meldung in zuhoerer.notifies(timeout=wartezeit) if meldung.channel == NOTIFY_CHANNEL]


@pytest.mark.django_db(transaction=True)
def test_weckruf_kommt_beim_commit_einmal_je_transaktion(pg_verbindungen: Verbindungen) -> None:
    zuhoerer = pg_verbindungen()
    zuhoerer.execute(f"LISTEN {NOTIFY_CHANNEL}")

    with transaction.atomic():
        ereignis_anlegen()
        ereignis_anlegen()
        Event.objects.bulk_create([Event(**ereignis_daten()) for _ in range(3)])
        assert _meldungen(zuhoerer, 0.3) == [], "vor dem Commit darf nichts ankommen"

    assert _meldungen(zuhoerer) == [""], "genau eine Meldung für drei Anweisungen in einer Transaktion"
    assert Event.objects.count() == 5


@pytest.mark.django_db(transaction=True)
def test_nach_rollback_kommt_kein_weckruf(pg_verbindungen: Verbindungen) -> None:
    zuhoerer = pg_verbindungen()
    zuhoerer.execute(f"LISTEN {NOTIFY_CHANNEL}")

    with pytest.raises(RuntimeError), transaction.atomic():
        ereignis_anlegen()
        raise RuntimeError("fachlicher Fehler")

    assert _meldungen(zuhoerer) == []
    assert not Event.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_weckruf_je_transaktion_bei_mehreren_schreibern(pg_verbindungen: Verbindungen) -> None:
    zuhoerer = pg_verbindungen()
    zuhoerer.execute(f"LISTEN {NOTIFY_CHANNEL}")

    with transaction.atomic():
        ereignis_anlegen()
    with transaction.atomic():
        ereignis_anlegen()

    assert _meldungen(zuhoerer) == ["", ""]


# --- Migration ---------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_migration_laesst_sich_zurueck_und_wieder_einspielen() -> None:
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("events", None)])
        tabellen = connection.introspection.table_names()
        assert "events_event" not in tabellen
        if connection.vendor == "postgresql":
            assert _wert("SELECT to_regclass(%s)", SEQUENCE_NAME) is None
            assert _wert("SELECT count(*) FROM pg_proc WHERE proname = 'events_event_notify'") == 0

        executor = MigrationExecutor(connection)
        executor.migrate([("events", "0001_initial")])
        assert "events_event" in connection.introspection.table_names()
        if connection.vendor == "postgresql":
            assert _wert("SELECT to_regclass(%s)::text", SEQUENCE_NAME) == SEQUENCE_NAME
            assert (
                _wert("SELECT count(*) FROM pg_trigger WHERE tgname = 'events_event_notify' AND NOT tgisinternal") == 1
            )
    finally:
        # Zurück auf den neuesten Stand, sonst laufen spätere Tests desselben Workers ohne spätere Migrationen
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
    assert "events_schedule" in connection.introspection.table_names()
    with connection.cursor() as cursor:
        indizes = connection.introspection.get_constraints(cursor, "events_parked")
    assert "events_parked_chain" in indizes
    assert "events_parked_aggregate" not in indizes
