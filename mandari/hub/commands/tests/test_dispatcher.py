# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dispatcher (Issue #539): Registrierung beim Eigentümer, Transaktion, Protokoll ohne Inhalte, Nebenläufigkeit."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections, transaction

from apps.events.models import IdempotencyKey
from hub.commands import Command, CommandError, Dispatcher, HandlerResult, Receipt, command_handler, get_dispatcher
from hub.commands.dispatcher import scope, valid_idempotency_key
from hub.commands.tests.hilfen import ACTOR, TENANT, json_body, register_mit_testvertraegen

GEHEIM = "Erika Mustermann, Musterweg 1"


def einreichen(command: Command) -> HandlerResult:
    return HandlerResult(reference="A/2026/1")


def _befehl(key: str = "k-1", **kw: Any) -> Command:
    werte: dict[str, Any] = {
        "name": "submission.submit",
        "body": json_body(),
        "idempotency_key": key,
        "tenant_ref": TENANT,
        "actor_ref": ACTOR,
    }
    werte.update(kw)
    return Command(**werte)


@pytest.fixture
def dispatcher(tmp_path: Path) -> Dispatcher:
    return Dispatcher(register_mit_testvertraegen(tmp_path))


# --- Registrierung --------------------------------------------------------------------------------


def test_handler_beim_eigentuemer(dispatcher: Dispatcher) -> None:
    dispatcher.register("submission.submit", 1, einreichen)
    assert dispatcher.has_handler("submission.submit", 1)
    assert dispatcher.handlers == {("submission.submit", 1): einreichen}
    dispatcher.register("submission.submit", 1, einreichen)  # dieselbe Funktion erneut: unkritisch


def test_handler_ausserhalb_des_eigentuemers_wird_abgelehnt(dispatcher: Dispatcher) -> None:
    # str liegt in builtins, nicht im Eigentümer hub.commands.tests
    with pytest.raises(ImproperlyConfigured, match="Eigentümer ist hub.commands.tests"):
        dispatcher.register("submission.submit", 1, str)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "version", "meldung"),
    [
        ("submission.unbekannt", 1, "kein Vertrag"),
        ("submission.submit", 2, "kein Vertrag"),
        ("ris.paper.released", 1, "ist ein Ereignis"),
    ],
)
def test_handler_nur_fuer_vorhandene_befehle(dispatcher: Dispatcher, name: str, version: int, meldung: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=meldung):
        dispatcher.register(name, version, einreichen)


def test_zweiter_handler_fuer_denselben_befehl_wird_abgelehnt(dispatcher: Dispatcher) -> None:
    dispatcher.register("submission.submit", 1, einreichen)

    def noch_einer(command: Command) -> HandlerResult:
        return HandlerResult(reference="x")

    with pytest.raises(ImproperlyConfigured, match="schon ein Handler"):
        dispatcher.register("submission.submit", 1, noch_einer)


def test_dekorator_der_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.commands import dispatcher as modul

    eigener = Dispatcher(register_mit_testvertraegen(tmp_path))
    monkeypatch.setattr(modul, "_DEFAULT", eigener)

    @command_handler("submission.submit")
    def handler(command: Command) -> HandlerResult:
        return HandlerResult(reference="dekoriert")

    assert get_dispatcher() is eigener
    assert eigener.handlers == {("submission.submit", 1): handler}


# --- Transaktion und Protokoll --------------------------------------------------------------------


@pytest.mark.django_db
def test_handler_laeuft_in_einer_transaktion_mit_dem_schluessel(dispatcher: Dispatcher) -> None:
    gesehen: list[bool] = []

    def pruefen(command: Command) -> HandlerResult:
        gesehen.append(transaction.get_connection().in_atomic_block)
        # Der Schlüssel ist in derselben Transaktion schon belegt.
        gesehen.append(IdempotencyKey.objects.filter(key=command.idempotency_key).exists())
        return HandlerResult(reference="A/1", received_at=None)

    dispatcher.register("submission.submit", 1, pruefen)
    dispatcher.dispatch(_befehl())
    assert gesehen == [True, True]
    eintrag = IdempotencyKey.objects.get()
    assert (eintrag.scope, eintrag.label) == (f"{TENANT} {ACTOR}", "submission.submit")
    assert eintrag.response["reference"] == "A/1"


@pytest.mark.django_db
def test_eingangszeit_des_eigentuemers_hat_vorrang(dispatcher: Dispatcher) -> None:
    zeit = datetime(2026, 9, 30, 8, 15, tzinfo=UTC)

    def mit_zeit(command: Command) -> HandlerResult:
        return HandlerResult(reference="A/1", received_at=zeit)

    dispatcher.register("submission.submit", 1, mit_zeit)
    assert dispatcher.dispatch(_befehl()).received_at == zeit


@pytest.mark.django_db
def test_falsches_ergebnis_des_handlers_ist_ein_interner_fehler(dispatcher: Dispatcher) -> None:
    def falsch(command: Command) -> HandlerResult:
        return {"reference": "A/1"}  # type: ignore[return-value]

    dispatcher.register("submission.submit", 1, falsch)
    with pytest.raises(CommandError) as info:
        dispatcher.dispatch(_befehl())
    assert info.value.problem.status == 500
    assert IdempotencyKey.objects.count() == 0


@pytest.mark.django_db
def test_protokoll_nennt_keine_inhalte(dispatcher: Dispatcher, caplog: pytest.LogCaptureFixture) -> None:
    dispatcher.register("submission.submit", 1, einreichen)
    with caplog.at_level(logging.INFO, logger="hub.commands"):
        dispatcher.dispatch(_befehl(body=json_body(title=GEHEIM)))
        dispatcher.dispatch(_befehl(body=json_body(title=GEHEIM)))
        with pytest.raises(CommandError):
            dispatcher.dispatch(_befehl(body={"document": GEHEIM, "title": GEHEIM}))
    assert [record.getMessage() for record in caplog.records] == [
        "Befehl submission.submit v1 ausgeführt",
        "Befehl submission.submit v1 wiederholt",
        "Befehl submission.submit v1 abgelehnt",
    ]
    assert GEHEIM not in caplog.text


def test_bereich_trennt_mandanten_und_ausloeser() -> None:
    assert scope(_befehl()) == f"{TENANT} {ACTOR}"
    assert scope(_befehl(actor_ref=None)) == f"{TENANT} -"


@pytest.mark.parametrize(
    ("schluessel", "gueltig"),
    [
        (str(uuid.uuid4()), True),
        ("a", True),
        ("x" * 255, True),
        ("", False),
        ("x" * 256, False),
        ("mit leerzeichen", False),
        ('mit"anfuehrung', False),
        ("mit\\rueckstrich", False),
        ("umlaut-ä", False),
        (None, False),
    ],
)
def test_gueltige_idempotenzschluessel(schluessel: object, gueltig: bool) -> None:
    assert valid_idempotency_key(schluessel) is gueltig


@pytest.mark.parametrize(
    "abweichung",
    [{"tenant_ref": "session:keine-uuid"}, {"tenant_ref": "user:" + str(uuid.uuid4())}, {"actor_ref": "Erika"}],
)
def test_befehl_braucht_kennungen_wie_die_ereignishuelle(abweichung: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _befehl(**abweichung)


def test_befehl_kopiert_seinen_inhalt() -> None:
    inhalt = json_body()
    befehl = _befehl(body=inhalt)
    inhalt["title"] = "geändert"
    assert befehl.body["title"] == "Mehr Bänke im Park"


def test_quittung_hin_und_zurueck() -> None:
    quittung = Receipt(
        command="submission.submit",
        version=1,
        reference="A/2026/1",
        received_at=datetime(2026, 9, 30, 10, 0, tzinfo=UTC),
        content_hash="0" * 64,
        aggregate_id=uuid.uuid4(),
        data={"status": "submitted"},
    )
    assert Receipt.from_dict(quittung.to_dict()) == quittung


# --- Nebenläufigkeit (PostgreSQL) -----------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gleichzeitige_wiederholungen_fuehren_den_handler_einmal_aus(dispatcher: Dispatcher) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("braucht PostgreSQL (CI): SQLite kennt keine gleichzeitigen Schreibtransaktionen")
    im_handler = threading.Event()
    weiter = threading.Event()
    aufrufe: list[str] = []

    def langsam(command: Command) -> HandlerResult:
        aufrufe.append(command.idempotency_key)
        im_handler.set()
        weiter.wait(timeout=10)
        return HandlerResult(reference=f"A/2026/{len(aufrufe)}")

    dispatcher.register("submission.submit", 1, langsam)
    ergebnisse: list[Receipt] = []
    fehler: list[Exception] = []

    def senden() -> None:
        try:
            ergebnisse.append(dispatcher.dispatch(_befehl("gleichzeitig")))
        except Exception as exc:  # im Test sammeln und unten prüfen
            fehler.append(exc)
        finally:
            connections.close_all()

    erster = threading.Thread(target=senden)
    erster.start()
    assert im_handler.wait(timeout=10)
    zweiter = threading.Thread(target=senden)
    zweiter.start()
    zweiter.join(timeout=1)
    assert zweiter.is_alive(), "der zweite wartet am belegten Schlüssel"
    weiter.set()
    erster.join(timeout=10)
    zweiter.join(timeout=10)
    assert fehler == []
    assert aufrufe == ["gleichzeitig"]
    assert len(ergebnisse) == 2
    assert ergebnisse[0] == ergebnisse[1]
