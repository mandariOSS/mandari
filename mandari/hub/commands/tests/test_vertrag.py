# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Vertragssuite für ``InProcessClient`` und ``HttpClient`` (Issue #539).

Jeder Test läuft gegen beide Clients: gleiche Quittung, gleiche Idempotenz, gleiche Probleme nach
RFC 9457 (``docs/adr/20260929-befehle-synchron.md``, Prüfung „Vertragstestsuite“).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from apps.events.models import IdempotencyKey, Lease
from hub.commands import (
    Command,
    CommandClient,
    CommandError,
    Dispatcher,
    HandlerResult,
    HttpClient,
    InProcessClient,
    content_hash,
)
from hub.commands import dispatcher as dispatcher_modul
from hub.commands.dispatcher import INTERNAL, UNREPRESENTABLE
from hub.commands.problems import PROBLEM_TYPE_BASE
from hub.commands.tests.hilfen import (
    ACTOR,
    BASE_URL,
    TENANT,
    TOKEN,
    DjangoTransport,
    json_body,
    protokolltext,
    register_mit_testvertraegen,
)

pytestmark = [pytest.mark.django_db, pytest.mark.urls("hub.commands.tests.urls")]

GEHEIM = "Erika Mustermann, Musterweg 1"
KONFLIKT = "KONFLIKT"
ABSTURZ = "ABSTURZ"
AUFRUFE: list[str] = []


def einreichen(command: Command) -> HandlerResult:
    """Test-Handler beim „Eigentümer“: schreibt einen Datensatz und vergibt eine Eingangsnummer."""
    AUFRUFE.append(command.idempotency_key)
    Lease.objects.create(name=f"antrag-{len(AUFRUFE)}", holder=command.idempotency_key, expires_at=timezone.now())
    title = str(command.body["title"])
    if title.startswith(KONFLIKT):
        raise CommandError.of(409, "konflikt", "Zu diesem Dokument gibt es schon eine Einreichung.")
    if title.startswith(ABSTURZ):
        raise RuntimeError(f"Absturz mit Inhalt: {title}")
    return HandlerResult(
        reference=f"A/2026/{len(AUFRUFE)}",
        aggregate_id=uuid.uuid5(uuid.NAMESPACE_URL, str(command.body["document"])),
        data={"status": "submitted"},
    )


@pytest.fixture
def dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    AUFRUFE.clear()
    dispatcher = Dispatcher(register_mit_testvertraegen(tmp_path))
    dispatcher.register("submission.submit", 1, einreichen)
    # Der HTTP-Weg nutzt den Dispatcher der Installation.
    monkeypatch.setattr(dispatcher_modul, "_DEFAULT", dispatcher)
    return dispatcher


@pytest.fixture(params=["in_process", "http"])
def sender(request: pytest.FixtureRequest, dispatcher: Dispatcher) -> CommandClient:
    if request.param == "in_process":
        return InProcessClient(dispatcher)
    return HttpClient(BASE_URL, TOKEN, transport=DjangoTransport())


def befehl(
    body: dict[str, Any] | None = None, *, key: str = "schluessel-1", name: str = "submission.submit", **kw: Any
) -> Command:
    return Command(
        name=name,
        body=json_body() if body is None else body,
        idempotency_key=key,
        tenant_ref=TENANT,
        actor_ref=ACTOR,
        **kw,
    )


def _problem(sender: CommandClient, command: Command) -> dict[str, Any]:
    with pytest.raises(CommandError) as info:
        sender.send(command)
    return info.value.problem.to_dict()


# --- Quittung und Idempotenz --------------------------------------------------------------------


def test_quittung_mit_eingangsnummer_zeit_und_inhalts_hash(sender: CommandClient) -> None:
    vorher = timezone.now()
    quittung = sender.send(befehl())
    assert quittung.command == "submission.submit"
    assert quittung.version == 1
    assert quittung.reference == "A/2026/1"
    assert vorher - timedelta(seconds=1) <= quittung.received_at <= timezone.now() + timedelta(seconds=1)
    assert quittung.received_at.utcoffset() is not None
    assert quittung.content_hash == content_hash(json_body())
    assert len(quittung.content_hash) == 64
    assert quittung.aggregate_id == uuid.uuid5(uuid.NAMESPACE_URL, json_body()["document"])
    assert quittung.data == {"status": "submitted"}
    assert Lease.objects.count() == 1


def test_gleiche_anfrage_mit_gleichem_schluessel_ergibt_dieselbe_quittung(sender: CommandClient) -> None:
    erste = sender.send(befehl())
    zweite = sender.send(befehl())
    assert zweite == erste
    assert AUFRUFE == ["schluessel-1"]
    assert Lease.objects.count() == 1
    assert IdempotencyKey.objects.count() == 1


def test_schluessel_als_structured_field_string(sender: CommandClient) -> None:
    """Anführungszeichen gehören zur Schreibweise im Header, nicht zum Schlüssel."""
    problem = _problem(sender, befehl(key='"in-anfuehrungszeichen"'))
    assert (problem["status"], problem["type"]) == (400, f"{PROBLEM_TYPE_BASE}idempotenzschluessel-fehlt")


def test_gleicher_schluessel_mit_anderem_inhalt_wird_abgelehnt(sender: CommandClient) -> None:
    sender.send(befehl())
    problem = _problem(sender, befehl(json_body(title="Anderer Titel")))
    assert (problem["status"], problem["type"]) == (422, f"{PROBLEM_TYPE_BASE}idempotenzschluessel-wiederverwendet")
    assert AUFRUFE == ["schluessel-1"]


def test_gleicher_schluessel_fuer_anderen_befehl_wird_abgelehnt(sender: CommandClient, dispatcher: Dispatcher) -> None:
    dispatcher.register("submission.withdraw", 1, zuruecknehmen)
    sender.send(befehl())
    problem = _problem(sender, befehl({"document": json_body()["document"]}, name="submission.withdraw"))
    assert problem["status"] == 422
    assert problem["type"].endswith("idempotenzschluessel-wiederverwendet")


def zuruecknehmen(command: Command) -> HandlerResult:
    return HandlerResult(reference="zurueckgenommen")


def test_neuer_schluessel_ergibt_neue_quittung(sender: CommandClient) -> None:
    erste = sender.send(befehl())
    zweite = sender.send(befehl(key="schluessel-2"))
    assert (erste.reference, zweite.reference) == ("A/2026/1", "A/2026/2")
    assert zweite.content_hash == erste.content_hash


@pytest.mark.parametrize("schluessel", ["", "mit leerzeichen", "ä-umlaut", "x" * 256])
def test_fehlender_oder_ungueltiger_schluessel_ergibt_400(sender: CommandClient, schluessel: str) -> None:
    problem = _problem(sender, befehl(key=schluessel))
    assert problem["status"] == 400
    assert problem["type"] == f"{PROBLEM_TYPE_BASE}idempotenzschluessel-fehlt"
    assert problem["title"] == "Ungültige Anfrage"
    assert AUFRUFE == []


# --- Fehler nach RFC 9457 ------------------------------------------------------------------------


def test_ungueltiger_inhalt_ergibt_422_ohne_werte(sender: CommandClient) -> None:
    problem = _problem(sender, befehl({"document": GEHEIM, "title": "x", GEHEIM: 1}))
    assert problem["status"] == 422
    assert problem["type"] == f"{PROBLEM_TYPE_BASE}validierung"
    assert {"pointer": "/document", "detail": "verletzt „format“"} in problem["errors"]
    assert {"pointer": "", "detail": "nicht vorgesehene Felder (<Feld>)"} in problem["errors"]
    assert GEHEIM not in str(problem)
    assert AUFRUFE == []


def test_fehlendes_pflichtfeld(sender: CommandClient) -> None:
    problem = _problem(sender, befehl({"document": json_body()["document"]}))
    assert problem["errors"] == [{"pointer": "", "detail": "Pflichtfeld fehlt (title)"}]


@pytest.mark.parametrize(
    ("name", "version"), [("submission.unbekannt", 1), ("submission.submit", 2), ("ris.paper.released", 1)]
)
def test_unbekannter_befehl_ergibt_404(sender: CommandClient, name: str, version: int) -> None:
    problem = _problem(sender, befehl(name=name, version=version))
    assert (problem["status"], problem["type"]) == (404, f"{PROBLEM_TYPE_BASE}befehl-unbekannt")


def test_befehl_ohne_handler_ergibt_501(sender: CommandClient) -> None:
    problem = _problem(sender, befehl({"document": json_body()["document"]}, name="submission.withdraw"))
    assert (problem["status"], problem["type"]) == (501, f"{PROBLEM_TYPE_BASE}befehl-nicht-verfuegbar")


def test_fachliche_ablehnung_rollt_zurueck_und_darf_wiederholt_werden(sender: CommandClient) -> None:
    problem = _problem(sender, befehl(json_body(title=f"{KONFLIKT} {GEHEIM}")))
    assert {key: problem[key] for key in ("type", "title", "status", "detail")} == {
        "type": f"{PROBLEM_TYPE_BASE}konflikt",
        "title": "Konflikt",
        "status": 409,
        "detail": "Zu diesem Dokument gibt es schon eine Einreichung.",
    }
    assert GEHEIM not in str(problem)
    assert Lease.objects.count() == 0
    assert IdempotencyKey.objects.count() == 0
    # Derselbe Schlüssel mit demselben Inhalt wird erneut ausgeführt (keine gespeicherte Ablehnung).
    with pytest.raises(CommandError):
        sender.send(befehl(json_body(title=f"{KONFLIKT} {GEHEIM}")))
    assert len(AUFRUFE) == 2


def test_unerwarteter_fehler_ergibt_500_mit_festem_text(
    sender: CommandClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        problem = _problem(sender, befehl(json_body(title=f"{ABSTURZ} {GEHEIM}")))
    assert (problem["status"], problem["type"], problem["detail"]) == (
        500,
        f"{PROBLEM_TYPE_BASE}interner-fehler",
        INTERNAL,
    )
    assert GEHEIM not in str(problem)
    assert Lease.objects.count() == 0
    assert IdempotencyKey.objects.count() == 0
    # Der Handler nennt den Titel in seiner Ausnahme; das Log nennt nur Typ und Aufrufstelle.
    gescheitert = [r for r in caplog.records if r.name == "hub.commands" and r.levelno == logging.ERROR]
    assert len(gescheitert) == 1
    assert "gescheitert: RuntimeError" in gescheitert[0].getMessage()
    assert "in einreichen" in gescheitert[0].getMessage()
    assert not gescheitert[0].exc_info
    assert GEHEIM not in protokolltext(caplog)
    assert ABSTURZ not in protokolltext(caplog)


@pytest.mark.parametrize(
    "inhalt",
    [
        {"title": "a\ud800b"},
        {"title": "Titel", "anzahl": 10**21},
        {"title": "Titel", "anzahl": 2**53},
    ],
    ids=["surrogat", "zehn-hoch-21", "zwei-hoch-53"],
)
def test_inhalt_ohne_kanonische_darstellung_ergibt_422(sender: CommandClient, inhalt: dict[str, Any]) -> None:
    """Kein 500: Ein Inhalt, für den es keinen Inhalts-Hash gibt, ist ein Fehler des Aufrufers."""
    problem = _problem(sender, befehl(json_body(**inhalt)))
    assert (problem["status"], problem["type"]) == (422, f"{PROBLEM_TYPE_BASE}validierung")
    assert problem["errors"] == [{"pointer": "", "detail": UNREPRESENTABLE.removeprefix("$: ")}]
    assert AUFRUFE == []
    assert IdempotencyKey.objects.count() == 0


def test_problem_ist_rfc_9457(sender: CommandClient) -> None:
    problem = _problem(sender, befehl(name="submission.unbekannt"))
    assert {"type", "title", "status", "detail"} <= set(problem)
    assert problem["type"].startswith("https://")
