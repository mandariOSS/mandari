# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``publish()`` (Issue #502): Transaktionsprüfung, Kontext, Formatprüfung der Hülle und Einhängepunkt
für die Vertragsprüfung.

Die Prüfung gegen das echte Vertragsregister testet die Drehscheibe
(``hub/contracts/tests/test_publish.py``); hier steht an ihrer Stelle eine Attrappe.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from apps.accounts.models import User
from apps.common.observability import REQUEST_ID_HEADER, RequestIdMiddleware
from apps.events import (
    CanonicalRef,
    InvalidEventError,
    PublishOutsideTransactionError,
    event_context,
    publish,
    publishing,
    system_ref,
    tenant_ref,
    user_ref,
)
from apps.events.models import Event, Operation, Visibility
from apps.events.tests.hilfen import nur_postgres

TENANT = "org:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
OBJEKT = uuid.UUID("5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c")
KOMMUNE = uuid.UUID("7e8f9a0b-1c2d-5e3f-8a4b-5c6d7e8f9a0b")
KONTO = uuid.UUID("9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a")


def _angaben(**abweichend: Any) -> dict[str, Any]:
    angaben: dict[str, Any] = {
        "version": 1,
        "aggregate": CanonicalRef("Objekt", OBJEKT),
        "tenant": TENANT,
        "visibility": Visibility.INTERN,
        "payload": {"changed": ["status"]},
    }
    angaben.update(abweichend)
    return angaben


def _publish(**abweichend: Any) -> Event:
    return publish("test.objekt.geaendert", **_angaben(**abweichend))


@pytest.fixture
def ohne_vertragspruefung(settings: Any) -> None:
    """Wie im Betrieb: nur die Formatprüfungen der Hülle. Die Testtypen stehen in keinem Register."""
    settings.EVENTS_VALIDATE_CONTRACTS = False


class Pruefer:
    """Attrappe der Vertragsprüfung: merkt sich die Hüllen und lehnt auf Wunsch ab."""

    def __init__(self) -> None:
        self.huellen: list[Mapping[str, Any]] = []
        self.zeilen_beim_aufruf: list[int] = []
        self.ablehnen = False

    def __call__(self, huelle: Mapping[str, Any]) -> None:
        self.huellen.append(huelle)
        self.zeilen_beim_aufruf.append(Event.objects.count())
        if self.ablehnen:
            raise ValueError("$.payload: verletzt „required“")


@pytest.fixture
def pruefer(settings: Any) -> Iterator[Pruefer]:
    settings.EVENTS_VALIDATE_CONTRACTS = True
    attrappe = Pruefer()
    vorher = publishing.set_contract_validator(attrappe)
    yield attrappe
    publishing.set_contract_validator(vorher)


# --- Transaktion ----------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_publish_ausserhalb_von_atomic_wirft() -> None:
    """Der Rahmen, den der Test selbst um sich hat, zählt nicht als Transaktion des Aufrufers."""
    assert connection.in_atomic_block
    with pytest.raises(PublishOutsideTransactionError, match=r"transaction\.atomic\(\)"):
        _publish()
    assert not Event.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_publish_im_autocommit_wirft() -> None:
    assert not connection.in_atomic_block
    with pytest.raises(PublishOutsideTransactionError):
        _publish()
    assert not Event.objects.exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_publish_in_atomic_schreibt_eine_journalzeile() -> None:
    zeit = datetime(2026, 9, 30, 8, 15, tzinfo=UTC)
    korrelation, ausloeser = uuid.uuid4(), uuid.uuid4()
    with transaction.atomic():
        ereignis = publish(
            "test.objekt.geaendert",
            version=2,
            aggregate=CanonicalRef("Objekt", OBJEKT),
            tenant=TENANT,
            body_id=KOMMUNE,
            visibility="nichtoeffentlich",
            operation="delete",
            occurred_at=zeit,
            actor_ref="system:abgleich",
            correlation_id=korrelation,
            causation_id=ausloeser,
            payload={"changed": ["status"]},
        )

    gespeichert = Event.objects.get()
    assert gespeichert.pk == ereignis.pk
    assert gespeichert.event_id == ereignis.event_id
    assert (gespeichert.type, gespeichert.version) == ("test.objekt.geaendert", 2)
    assert (gespeichert.aggregate_type, gespeichert.aggregate_id) == ("Objekt", OBJEKT)
    assert (gespeichert.tenant_ref, gespeichert.body_id) == (TENANT, KOMMUNE)
    assert (gespeichert.visibility, gespeichert.operation) == (Visibility.NICHTOEFFENTLICH, Operation.DELETE)
    assert gespeichert.occurred_at == zeit
    assert gespeichert.actor_ref == "system:abgleich"
    assert (gespeichert.correlation_id, gespeichert.causation_id) == (korrelation, ausloeser)
    assert gespeichert.payload == {"changed": ["status"]}
    # Die Folgenummer vergibt erst der Sequenzierer nach dem Commit.
    assert gespeichert.seq is None
    assert gespeichert.recorded_at is not None


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_standards_der_huelle() -> None:
    with transaction.atomic():
        ereignis = _publish(aggregate=("Objekt", OBJEKT))

    assert ereignis.operation == Operation.UPSERT
    assert ereignis.body_id is None
    assert ereignis.actor_ref is None
    assert ereignis.causation_id is None
    assert isinstance(ereignis.correlation_id, uuid.UUID)
    assert ereignis.occurred_at.utcoffset() is not None


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_ereignis_verschwindet_mit_der_fachlichen_aenderung() -> None:
    """Ereignis und Änderung sind eine Einheit: Rollt die Änderung zurück, gibt es kein Ereignis."""

    class AbbruchError(Exception):
        pass

    with pytest.raises(AbbruchError), transaction.atomic():
        _publish()
        assert Event.objects.count() == 1
        raise AbbruchError

    assert not Event.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_ereignis_traegt_die_transaktion_der_fachlichen_aenderung() -> None:
    nur_postgres()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_current_xact_id()::text")
            transaktion = int(cursor.fetchone()[0])
        with transaction.atomic():  # Sicherungspunkt: Es bleibt die Kennung der Haupttransaktion
            ereignis = _publish()

    ereignis.refresh_from_db()
    assert ereignis.xid == transaktion


# --- Kontext: Anfrage, OpenTelemetry, event_context ---------------------------------------------------


def _anfrage(user: Any = None, request_id: str | None = None) -> tuple[list[Event], HttpResponse]:
    """Eine Anfrage durch die ``RequestIdMiddleware``, deren View zwei Ereignisse veröffentlicht."""
    ereignisse: list[Event] = []

    def view(request: HttpRequest) -> HttpResponse:
        middleware.process_view(request, view, (), {})
        with transaction.atomic():
            ereignisse.append(_publish())
            ereignisse.append(_publish())
        return HttpResponse()

    middleware = RequestIdMiddleware(view)
    headers = {REQUEST_ID_HEADER: request_id} if request_id else {}
    request = RequestFactory().post("/", headers=headers)
    if user is not None:
        request.user = user
    return ereignisse, middleware(request)


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_korrelation_und_ausloeser_aus_der_anfrage() -> None:
    user = User.objects.create(email="erika.mustermann@example.org", first_name="Erika", last_name="Mustermann")

    ereignisse, response = _anfrage(user)

    korrelation = uuid.UUID(response[REQUEST_ID_HEADER])
    assert [e.correlation_id for e in ereignisse] == [korrelation, korrelation]
    # Kennung des Kontos, kein Name und keine Mailadresse
    assert [e.actor_ref for e in ereignisse] == [f"user:{user.pk}"] * 2
    for wert in ("Erika", "Mustermann", "example.org"):
        assert wert not in str(ereignisse[0].actor_ref)


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_anfrage_ohne_anmeldung_hat_keinen_ausloeser() -> None:
    ereignisse, _ = _anfrage()
    assert [e.actor_ref for e in ereignisse] == [None, None]


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_request_kennung_des_proxys_ergibt_eine_feste_korrelation() -> None:
    """Eine Kennung, die keine UUID ist, wird abgeleitet: gleiche Anfrage, gleiche Korrelations-ID."""
    ereignisse, _ = _anfrage(request_id="proxy-2026-09-30.000123")

    erwartet = publishing.correlation_id_for_request("proxy-2026-09-30.000123")
    assert [e.correlation_id for e in ereignisse] == [erwartet, erwartet]
    assert erwartet == publishing.correlation_id_for_request("proxy-2026-09-30.000123")
    assert erwartet != publishing.correlation_id_for_request("proxy-2026-09-30.000124")


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_request_kennung_als_uuid_wird_uebernommen() -> None:
    kennung = "0f4c6f8e-6a1b-4d8e-9c55-2f1a7b3c9d10"
    ereignisse, _ = _anfrage(request_id=kennung)
    assert ereignisse[0].correlation_id == uuid.UUID(kennung)


def _span(trace_id: int) -> Any:
    trace = pytest.importorskip("opentelemetry.trace")
    kontext = trace.SpanContext(
        trace_id=trace_id, span_id=0xB7AD6B7169203331, is_remote=False, trace_flags=trace.TraceFlags(0x01)
    )
    return trace.use_span(trace.NonRecordingSpan(kontext))


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_korrelation_aus_dem_opentelemetry_kontext() -> None:
    trace_id = 0x0AF7651916CD43DD8448EB211C80319C
    with _span(trace_id), transaction.atomic():
        ereignis = _publish()
    assert ereignis.correlation_id == uuid.UUID(int=trace_id)


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_anfrage_geht_dem_opentelemetry_kontext_vor() -> None:
    with _span(0x0AF7651916CD43DD8448EB211C80319C):
        ereignisse, response = _anfrage()
    assert ereignisse[0].correlation_id == uuid.UUID(response[REQUEST_ID_HEADER])


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_ohne_kontext_bekommt_jedes_ereignis_eine_eigene_korrelation() -> None:
    with transaction.atomic():
        erstes, zweites = _publish(), _publish()
    assert erstes.correlation_id != zweites.correlation_id


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_event_context_gilt_fuer_alle_ereignisse_des_blocks() -> None:
    with event_context(actor_ref=system_ref("abgleich")) as kontext, transaction.atomic():
        erstes, zweites = _publish(), _publish()

    assert erstes.correlation_id == zweites.correlation_id == kontext.correlation_id
    assert erstes.actor_ref == zweites.actor_ref == "system:abgleich"
    assert publishing.current_context() is None


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_event_context_geschachtelt_erbt_und_ueberschreibt() -> None:
    aussen_id = uuid.uuid4()
    with event_context(correlation_id=aussen_id, actor_ref=user_ref(KONTO)), transaction.atomic():
        with event_context(actor_ref="system:nacharbeit"):
            innen = _publish()
        aussen = _publish()

    assert (innen.correlation_id, innen.actor_ref) == (aussen_id, "system:nacharbeit")
    assert (aussen.correlation_id, aussen.actor_ref) == (aussen_id, f"user:{KONTO}")


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_folgeereignis_uebernimmt_korrelation_und_nennt_den_ausloeser() -> None:
    with transaction.atomic():
        ursache = _publish(actor_ref=user_ref(KONTO))
        with event_context(caused_by=ursache):
            folge = _publish()

    assert folge.correlation_id == ursache.correlation_id
    assert folge.causation_id == ursache.event_id
    # Den Auslöser des Folgeereignisses bestimmt der Handler, nicht das auslösende Ereignis.
    assert folge.actor_ref is None


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_angaben_am_aufruf_gehen_dem_kontext_vor() -> None:
    eigene = uuid.uuid4()
    with event_context(actor_ref="system:abgleich"), transaction.atomic():
        ereignis = _publish(correlation_id=eigene, actor_ref=user_ref(KONTO))
    assert (ereignis.correlation_id, ereignis.actor_ref) == (eigene, f"user:{KONTO}")


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
def test_event_context_in_einer_anfrage_behaelt_deren_korrelation() -> None:
    ereignisse: list[Event] = []

    def view(request: HttpRequest) -> HttpResponse:
        with event_context(actor_ref="system:import"), transaction.atomic():
            ereignisse.append(_publish())
        return HttpResponse()

    response = RequestIdMiddleware(view)(RequestFactory().post("/"))
    assert ereignisse[0].correlation_id == uuid.UUID(response[REQUEST_ID_HEADER])


# --- Formatprüfung der Hülle (immer, auch im Betrieb) --------------------------------------------------

NAME = "Erika Mustermann"


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
@pytest.mark.parametrize(
    "wert", [NAME, "user:erika.mustermann@example.org", "user:Erika", "system:Erika Mustermann", ""]
)
def test_ausloeser_ist_nie_ein_name(wert: str) -> None:
    with transaction.atomic():
        if wert:
            with pytest.raises(InvalidEventError, match="actor_ref") as info:
                _publish(actor_ref=wert)
            assert wert not in str(info.value)
        else:
            # Leer heißt: nicht angegeben, es gilt der Kontext.
            assert _publish(actor_ref=wert).actor_ref is None
    assert Event.objects.count() == (0 if wert else 1)


def test_event_context_lehnt_namen_als_ausloeser_ab() -> None:
    with pytest.raises(InvalidEventError, match="actor_ref") as info, event_context(actor_ref=NAME):
        pass
    assert NAME not in str(info.value)
    assert publishing.current_context() is None


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
@pytest.mark.parametrize(
    ("abweichung", "feld"),
    [
        ({"tenant": "org:test"}, "tenant"),
        ({"tenant": "Fraktion Musterstadt"}, "tenant"),
        ({"tenant": "kunde:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"}, "tenant"),
        ({"visibility": "geheim"}, "visibility"),
        ({"visibility": ""}, "visibility"),
        ({"operation": "update"}, "operation"),
        ({"version": 0}, "version"),
        ({"version": True}, "version"),
        ({"version": 40000}, "version"),
        ({"aggregate": ("objekt", OBJEKT)}, "aggregate"),
        ({"aggregate": ("Objekt", "keine-uuid")}, "aggregate"),
        ({"body_id": "Musterstadt"}, "body_id"),
        ({"occurred_at": datetime(2026, 9, 30, 8, 15)}, "occurred_at"),  # bewusst ohne Zeitzone
        ({"payload": ["status"]}, "payload"),
        ({"correlation_id": "keine-uuid"}, "correlation_id"),
    ],
)
def test_ungueltige_huelle_wird_abgelehnt(abweichung: dict[str, Any], feld: str) -> None:
    with transaction.atomic(), pytest.raises(InvalidEventError, match=f"^{feld}:") as info:
        _publish(**abweichung)
    for wert in abweichung.values():
        if isinstance(wert, str) and wert:
            assert wert not in str(info.value)
    assert not Event.objects.exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("ohne_vertragspruefung")
@pytest.mark.parametrize(
    "typ", ["Test.Objekt", "test", "test objekt geaendert", "test.objekt.geaendert.v2", "a." * 60 + "b"]
)
def test_ungueltiger_typ_wird_abgelehnt(typ: str) -> None:
    with transaction.atomic(), pytest.raises(InvalidEventError, match="^type:"):
        publish(typ, **_angaben())
    assert not Event.objects.exists()


def test_kennungen_der_huelle() -> None:
    assert tenant_ref("session", OBJEKT) == f"session:{OBJEKT}"
    assert tenant_ref("source", str(OBJEKT).upper()) == f"source:{OBJEKT}"
    assert user_ref(KONTO) == f"user:{KONTO}"
    assert system_ref("ingestor") == "system:ingestor"
    with pytest.raises(InvalidEventError, match="^tenant_ref:"):
        tenant_ref("kunde", OBJEKT)
    with pytest.raises(InvalidEventError, match="^tenant_ref:"):
        tenant_ref("org", "Fraktion Musterstadt")
    with pytest.raises(InvalidEventError, match="^actor_ref:"):
        user_ref("erika.mustermann@example.org")
    with pytest.raises(InvalidEventError, match="^actor_ref:"):
        system_ref(NAME)


# --- Einhängepunkt der Vertragsprüfung -------------------------------------------------------------------


@pytest.mark.django_db
def test_vertragspruefung_bekommt_die_huelle_vor_dem_schreiben(pruefer: Pruefer) -> None:
    with transaction.atomic():
        ereignis = _publish(body_id=KOMMUNE, actor_ref="system:abgleich")

    assert pruefer.zeilen_beim_aufruf == [0]
    (huelle,) = pruefer.huellen
    assert huelle == {
        "event_id": str(ereignis.event_id),
        "type": "test.objekt.geaendert",
        "version": 1,
        "aggregate_type": "Objekt",
        "aggregate_id": str(OBJEKT),
        "tenant_ref": TENANT,
        "body_id": str(KOMMUNE),
        "visibility": "intern",
        "operation": "upsert",
        "occurred_at": ereignis.occurred_at.isoformat(),
        "actor_ref": "system:abgleich",
        "correlation_id": str(ereignis.correlation_id),
        "causation_id": None,
        "payload": {"changed": ["status"]},
    }


@pytest.mark.django_db
def test_abgelehntes_ereignis_wird_nicht_geschrieben(pruefer: Pruefer) -> None:
    pruefer.ablehnen = True
    with transaction.atomic(), pytest.raises(ValueError, match="required"):
        _publish()
    assert len(pruefer.huellen) == 1
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_im_betrieb_bleibt_die_vertragspruefung_aus(pruefer: Pruefer, settings: Any) -> None:
    settings.EVENTS_VALIDATE_CONTRACTS = False
    pruefer.ablehnen = True
    with transaction.atomic():
        _publish()
    assert pruefer.huellen == []
    assert Event.objects.count() == 1


@pytest.mark.django_db
def test_eingeschaltete_pruefung_ohne_pruefer_ist_ein_konfigurationsfehler(pruefer: Pruefer) -> None:
    """Sonst bliebe die Prüfung still aus, etwa wenn ``hub.contracts`` nicht installiert ist."""
    publishing.set_contract_validator(None)
    with transaction.atomic(), pytest.raises(ImproperlyConfigured, match="EVENTS_VALIDATE_CONTRACTS"):
        _publish()
    assert not Event.objects.exists()


@pytest.mark.parametrize(("debug", "erwartet"), [(True, True), (False, False)])
def test_vertragspruefung_folgt_ohne_einstellung_debug(settings: Any, debug: bool, erwartet: bool) -> None:
    del settings.EVENTS_VALIDATE_CONTRACTS
    settings.DEBUG = debug
    assert publishing.contract_validation_active() is erwartet


def test_tests_pruefen_gegen_das_register(settings: Any) -> None:
    """Die Testeinstellungen schalten die Prüfung ein, auch wenn die CI mit ``DEBUG=false`` läuft."""
    assert settings.EVENTS_VALIDATE_CONTRACTS is True
    assert publishing.contract_validation_active()
