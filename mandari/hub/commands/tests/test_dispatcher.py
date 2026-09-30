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
from django.db import IntegrityError, connection, connections, transaction

from apps.events import CanonicalRef, publish
from apps.events.idempotency import NestedTransactionError
from apps.events.models import Event, IdempotencyKey
from apps.events.publishing import current_context
from hub.commands import Command, CommandError, Dispatcher, HandlerResult, Receipt, command_handler, get_dispatcher
from hub.commands.dispatcher import call_sites, error_types, scope, valid_idempotency_key
from hub.commands.tests.hilfen import (
    ACTOR,
    DOCUMENT,
    TENANT,
    json_body,
    protokolltext,
    register_mit_testvertraegen,
)
from hub.commands.types import MAX_DEPTH, exceeds_depth

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
def test_ereignisse_des_handlers_tragen_korrelation_und_ausloeser_des_befehls(dispatcher: Dispatcher) -> None:
    def einreichen_und_melden(command: Command) -> HandlerResult:
        publish(
            "ris.paper.released",
            version=1,
            aggregate=CanonicalRef("Paper", uuid.UUID(DOCUMENT)),
            tenant=command.tenant_ref,
            visibility="nichtoeffentlich",
            payload={"paper": DOCUMENT},
        )
        return HandlerResult(reference="A/1")

    dispatcher.register("submission.submit", 1, einreichen_und_melden)
    befehl = _befehl()
    dispatcher.dispatch(befehl)

    ereignis = Event.objects.get()
    assert ereignis.correlation_id == befehl.correlation_id
    assert ereignis.actor_ref == ACTOR
    assert ereignis.tenant_ref == TENANT
    # Der Kontext gilt nur für die Dauer des Handlers.
    assert current_context() is None


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
    assert GEHEIM not in protokolltext(caplog)


class _DriverError(Exception):
    """Wie der Fehler des Datenbanktreibers: fester Code, Meldung mit Werten."""

    sqlstate = "23505"


def absturz_mit_inhalt(command: Command) -> HandlerResult:
    titel = command.body["title"]
    try:
        raise _DriverError(f"duplicate key value: Key (title)=({titel}) already exists")
    except _DriverError as exc:
        raise IntegrityError(f"Eintrag mit Titel {titel} gibt es schon") from exc


@pytest.mark.django_db
def test_protokoll_nennt_auch_beim_absturz_keine_inhalte(
    dispatcher: Dispatcher, caplog: pytest.LogCaptureFixture
) -> None:
    """Meldungen von Ausnahmen zitieren Werte; das Log nennt nur Typ, SQLSTATE und Aufrufstellen."""
    dispatcher.register("submission.submit", 1, absturz_mit_inhalt)
    befehl = _befehl(body=json_body(title=GEHEIM))
    with caplog.at_level(logging.DEBUG), pytest.raises(CommandError) as info:
        dispatcher.dispatch(befehl)
    assert info.value.problem.status == 500
    (eintrag,) = [record for record in caplog.records if record.name == "hub.commands"]
    meldung = eintrag.getMessage()
    assert eintrag.levelno == logging.ERROR
    assert meldung.startswith(
        "Befehl submission.submit v1 gescheitert: IntegrityError <- _DriverError[23505] "
        f"(correlation_id={befehl.correlation_id})"
    )
    assert "in absturz_mit_inhalt" in meldung
    assert "test_dispatcher.py" in meldung
    assert not eintrag.exc_info and not eintrag.exc_text
    assert (eintrag.command, eintrag.tenant_ref, eintrag.error_type) == (  # type: ignore[attr-defined]
        "submission.submit",
        TENANT,
        "IntegrityError",
    )
    assert GEHEIM not in protokolltext(caplog)
    assert "already exists" not in protokolltext(caplog)


def test_typen_und_aufrufstellen_einer_ausnahme_ohne_meldung() -> None:
    def innen() -> None:
        raise ValueError(GEHEIM)

    try:
        try:
            innen()
        except ValueError:
            raise KeyError(GEHEIM)  # noqa: B904 – die Kette über __context__ ist hier Gegenstand
    except KeyError as exc:
        assert error_types(exc) == "KeyError <- ValueError"
        stellen = call_sites(exc)
    assert "in test_typen_und_aufrufstellen_einer_ausnahme_ohne_meldung" in stellen
    assert GEHEIM not in stellen
    assert "raise" not in stellen, "kein Quelltext"


def test_ursachenkette_ist_begrenzt_und_endet_bei_einem_kreis() -> None:
    erste, zweite = ValueError("a"), KeyError("b")
    erste.__cause__, zweite.__cause__ = zweite, erste
    assert error_types(erste) == "ValueError <- KeyError"
    kette: BaseException = RuntimeError("0")
    for nummer in range(20):
        naechste = RuntimeError(str(nummer))
        naechste.__cause__ = kette
        kette = naechste
    assert error_types(kette).count("RuntimeError") == 6


# --- Eigene, festgeschriebene Transaktion ---------------------------------------------------------


@pytest.mark.django_db
def test_dispatch_in_offener_transaktion_ist_ein_programmierfehler(dispatcher: Dispatcher) -> None:
    """
    Sonst lägen Fachdaten und Schlüssel nur in einem Sicherungspunkt: Die Quittung wäre schon
    zurückgegeben, ein späteres Rückrollen außen nähme beides wieder weg (anders als über HTTP).
    """
    aufrufe: list[str] = []

    def zaehlen(command: Command) -> HandlerResult:
        aufrufe.append(command.idempotency_key)
        return HandlerResult(reference="A/1")

    dispatcher.register("submission.submit", 1, zaehlen)
    with transaction.atomic(), pytest.raises(NestedTransactionError, match="offenen Transaktion"):
        dispatcher.dispatch(_befehl())
    assert aufrufe == []
    assert IdempotencyKey.objects.count() == 0
    # Außerhalb einer Transaktion des Aufrufers gelingt derselbe Befehl.
    assert dispatcher.dispatch(_befehl()).reference == "A/1"


@pytest.mark.django_db(transaction=True)
def test_quittung_ist_festgeschrieben_wenn_der_aufrufer_sie_erhaelt(dispatcher: Dispatcher) -> None:
    dispatcher.register("submission.submit", 1, einreichen)
    quittung = dispatcher.dispatch(_befehl())
    assert not connection.in_atomic_block
    # Ein Rückrollen des Aufrufers nach der Quittung nimmt nichts mehr weg.
    transaction.set_autocommit(False)
    try:
        transaction.rollback()
    finally:
        transaction.set_autocommit(True)
    assert IdempotencyKey.objects.get().response["reference"] == quittung.reference


def test_bereich_trennt_mandanten_und_ausloeser() -> None:
    assert scope(_befehl()) == f"{TENANT} {ACTOR}"
    assert scope(_befehl(actor_ref=None)) == f"{TENANT} -"


@pytest.mark.parametrize(
    ("schluessel", "gueltig"),
    [
        ("8e03978e-40d5-43e8-bc93-6894a57f9324", True),
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
    [
        {"tenant_ref": "session:keine-uuid"},
        {"tenant_ref": "user:9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"},
        {"actor_ref": "Erika"},
    ],
)
def test_befehl_braucht_kennungen_wie_die_ereignishuelle(abweichung: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _befehl(**abweichung)


@pytest.mark.parametrize(
    ("ebenen", "zu_tief"),
    [(1, False), (2, False), (MAX_DEPTH, False), (MAX_DEPTH + 1, True), (5_000, True)],
)
def test_inhalt_ist_hoechstens_64_ebenen_tief(ebenen: int, zu_tief: bool) -> None:
    inhalt: Any = "blatt"
    for nummer in range(ebenen - 1):
        inhalt = [inhalt] if nummer % 2 else {"k": inhalt}
    inhalt = {"title": inhalt}
    assert exceeds_depth(inhalt) is zu_tief
    if zu_tief:
        with pytest.raises(ValueError, match="zu tief verschachtelt"):
            _befehl(body=inhalt)
    else:
        assert _befehl(body=inhalt).body == inhalt


def test_tiefe_zaehlt_den_tiefsten_zweig() -> None:
    flach = {"a": [1, 2, {"b": "x"}], "c": {"d": {"e": [True, None]}}}
    assert not exceeds_depth(flach, 4)
    assert exceeds_depth(flach, 3)
    assert not exceeds_depth("kein Behälter", 0)
    assert exceeds_depth({}, 0)


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
