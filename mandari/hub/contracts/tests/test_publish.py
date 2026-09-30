# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``apps.events.publish()`` prüft gegen das Vertragsregister (Issue #502, Folgepunkt aus #517).

Die Plattform importiert die Drehscheibe nicht; ``hub.contracts`` hängt seine Prüfung beim Start ein.
Hier laufen ``publish()`` und das ausgelieferte Register zusammen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import transaction

from apps.events import CanonicalRef, publish, publishing
from apps.events.models import Event, Operation, Visibility
from hub.contracts import (
    EVENT,
    ContractViolationError,
    UnknownContractError,
    envelope_schema,
    get_registry,
    validate_envelope,
)
from hub.contracts.publishing import validate_published_event
from hub.contracts.tests.hilfen import PAPER_ID, TENANT_REF

PAPER = CanonicalRef("Paper", uuid.UUID(PAPER_ID))
GEHEIM = "Erika Mustermann, Musterweg 1"


def _publish(event_type: str = "ris.paper.changed", **abweichend: Any) -> Event:
    angaben: dict[str, Any] = {
        "version": 1,
        "aggregate": PAPER,
        "tenant": TENANT_REF,
        "visibility": "oeffentlich",
        "payload": {"paper": PAPER_ID, "changed": ["name", "paperType"]},
    }
    angaben.update(abweichend)
    return publish(event_type, **angaben)


def test_register_ist_beim_start_eingehaengt() -> None:
    assert publishing._contract_validator is validate_published_event
    assert publishing.contract_validation_active()


@pytest.mark.django_db
def test_gueltiges_ereignis_wird_geschrieben() -> None:
    with transaction.atomic():
        ereignis = _publish(actor_ref="system:ingestor", body_id=uuid.uuid4())

    assert Event.objects.get().event_id == ereignis.event_id
    # Was publish() prüfen lässt, ist eine gültige Hülle.
    validate_envelope(publishing.envelope(ereignis))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "stelle"),
    [
        ({"paper": PAPER_ID}, "Pflichtfeld fehlt (changed)"),
        ({"paper": "keine-uuid", "changed": ["name"]}, "$.paper"),
        ({"paper": PAPER_ID, "changed": ["name"], "title": GEHEIM}, "nicht vorgesehene Felder (title)"),
        ({"paper": PAPER_ID, "changed": [GEHEIM]}, "$.changed[0]"),
    ],
)
def test_nutzlast_gegen_den_vertrag(payload: dict[str, Any], stelle: str) -> None:
    with transaction.atomic(), pytest.raises(ContractViolationError) as info:
        _publish(payload=payload)

    assert stelle in str(info.value)
    # Meldungen nennen Stelle und Regel, nie Werte.
    assert GEHEIM not in str(info.value)
    assert not Event.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(("typ", "version"), [("ris.paper.verschoben", 1), ("ris.paper.changed", 2)])
def test_typ_oder_version_ohne_vertrag(typ: str, version: int) -> None:
    with transaction.atomic(), pytest.raises(UnknownContractError):
        _publish(typ, version=version)
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_sichtbarkeit_muss_im_vertrag_vorgesehen_sein() -> None:
    assert get_registry().get("ris.paper.changed", 1).visibility == {"oeffentlich", "nichtoeffentlich"}
    with transaction.atomic(), pytest.raises(ContractViolationError, match="Sichtbarkeit intern ist nicht vorgesehen"):
        _publish(visibility="intern")
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_befehl_ist_kein_ereignis() -> None:
    with transaction.atomic(), pytest.raises(ContractViolationError, match="ist ein Befehl"):
        _publish("submission.submit", visibility="nichtoeffentlich", payload={})
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_im_betrieb_ohne_vertragspruefung(settings: Any) -> None:
    """Im Betrieb (``EVENTS_VALIDATE_CONTRACTS`` aus) gelten nur die Formatprüfungen der Hülle."""
    settings.EVENTS_VALIDATE_CONTRACTS = False
    with transaction.atomic():
        _publish(payload={"paper": PAPER_ID})
    assert Event.objects.count() == 1


@pytest.mark.django_db
def test_jedes_beispiel_der_ausgelieferten_vertraege_laesst_sich_veroeffentlichen() -> None:
    anzahl = 0
    with transaction.atomic():
        for vertrag in get_registry().contracts(EVENT):
            for beispiel in vertrag.examples:
                for sichtbarkeit in sorted(vertrag.visibility):
                    publish(
                        vertrag.name,
                        version=vertrag.version,
                        aggregate=PAPER,
                        tenant=TENANT_REF,
                        visibility=sichtbarkeit,
                        payload=beispiel,
                    )
                    anzahl += 1
    assert anzahl >= len(get_registry().contracts(EVENT)) > 0
    assert Event.objects.count() == anzahl


def test_formatpruefung_der_plattform_entspricht_der_huelle() -> None:
    """``publish()`` prüft die Hülle auch im Betrieb, ohne das Schema zu lesen: gleiche Muster, gleiche Grenzen."""
    eigenschaften = envelope_schema()["properties"]
    assert eigenschaften["type"]["pattern"] == publishing.EVENT_TYPE_PATTERN
    assert eigenschaften["type"]["maxLength"] == publishing.MAX_EVENT_TYPE_LENGTH
    assert eigenschaften["version"]["maximum"] == publishing.MAX_VERSION
    assert eigenschaften["aggregate_type"]["pattern"] == publishing.AGGREGATE_TYPE_PATTERN
    assert eigenschaften["aggregate_type"]["maxLength"] == publishing.MAX_AGGREGATE_TYPE_LENGTH
    assert eigenschaften["tenant_ref"]["pattern"] == publishing.TENANT_REF_PATTERN
    assert eigenschaften["actor_ref"]["anyOf"][0]["pattern"] == publishing.ACTOR_REF_PATTERN
    assert set(eigenschaften["visibility"]["enum"]) == set(Visibility.values)
    assert set(eigenschaften["operation"]["enum"]) == set(Operation.values)
    ereignis = Event(aggregate_id=PAPER.id, correlation_id=uuid.uuid4(), occurred_at=datetime.now(UTC), payload={})
    assert set(publishing.envelope(ereignis)) == set(eigenschaften)
