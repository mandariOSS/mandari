# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO: personenbezogene Nutzlasten im Journal neutralisieren (``apps.events.datenschutz``, Issue #511).

Nach einem ``redact`` zu einer Person werden die Journaleinträge mit Sichtbarkeit ``personenbezogen`` zu dieser
Person neutralisiert: Die Kennung bleibt, die Felder werden leer. Welche Felder eine Person nennen, steht in den
Verträgen (``x-person``), eingehängt von ``hub.contracts``.
"""

from __future__ import annotations

import uuid
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction

from apps.events import datenschutz, publish, tenant_ref
from apps.events.models import Event, Operation, Visibility
from apps.events.tests.hilfen import ereignis_anlegen

pytestmark = pytest.mark.django_db

ORG = uuid.UUID("3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d")


def _personenbezogen(typ: str, aggregat: uuid.UUID, **nutzlast: Any) -> Event:
    return ereignis_anlegen(
        type=typ,
        version=1,
        aggregate_type="Objekt",
        aggregate_id=aggregat,
        visibility=Visibility.PERSONENBEZOGEN,
        payload=nutzlast,
    )


def _nutzlasten() -> dict[uuid.UUID, dict[str, Any]]:
    return dict(Event.objects.values_list("event_id", "payload"))


def test_personenfelder_kommen_aus_den_vertraegen() -> None:
    felder = datenschutz.person_fields()
    assert felder[("core.user.registered", 1)] == ("user",)
    assert felder[("attendance.response_recorded", 1)] == ("person",)


def test_neutralisiert_nur_personenbezogene_eintraege_zu_dieser_person() -> None:
    person, andere = uuid.uuid4(), uuid.uuid4()
    sitzung = uuid.uuid4()
    konto = _personenbezogen("core.user.registered", person, user=str(person), step="created")
    zusage = _personenbezogen(
        "attendance.response_recorded", sitzung, meeting=str(sitzung), person=str(person), response="declined"
    )
    fremde_zusage = _personenbezogen(
        "attendance.response_recorded", sitzung, meeting=str(sitzung), person=str(andere), response="confirmed"
    )
    # Nennt die Person, ist aber nicht personenbezogen (keine Grundlage im Vertrag): bleibt
    intern = ereignis_anlegen(payload={"user": str(person), "changed": ["status"]})
    # Nennt die Person in einem Feld, das kein Personenfeld ist: bleibt
    sitzung_der_person = _personenbezogen(
        "attendance.response_recorded", uuid.uuid4(), meeting=str(person), person=str(andere), response="confirmed"
    )
    vorher = _nutzlasten()

    assert datenschutz.count(person) == 2
    assert datenschutz.neutralize(person, batch=1) == 2

    nachher = _nutzlasten()
    assert nachher[konto.event_id] == {} and nachher[zusage.event_id] == {}
    for unveraendert in (fremde_zusage, intern, sitzung_der_person):
        assert nachher[unveraendert.event_id] == vorher[unveraendert.event_id]
    # Die Kennung bleibt
    konto_neu = Event.objects.get(pk=konto.pk)
    assert (konto_neu.event_id, konto_neu.aggregate_id, konto_neu.type, konto_neu.seq) == (
        konto.event_id,
        person,
        "core.user.registered",
        konto.seq,
    )
    # Wiederholbar
    assert datenschutz.neutralize(person) == 0 and datenschutz.count(person) == 0


def test_ohne_vertraege_nur_das_objekt(monkeypatch: pytest.MonkeyPatch) -> None:
    person, sitzung = uuid.uuid4(), uuid.uuid4()
    konto = _personenbezogen("core.user.registered", person, user=str(person), step="created")
    zusage = _personenbezogen(
        "attendance.response_recorded", sitzung, meeting=str(sitzung), person=str(person), response="declined"
    )
    vorher = datenschutz.set_person_fields_provider(None)
    try:
        assert datenschutz.neutralize(person) == 1
    finally:
        datenschutz.set_person_fields_provider(vorher)
    assert _nutzlasten()[konto.event_id] == {} and _nutzlasten()[zusage.event_id] != {}


def _redact(person: uuid.UUID) -> None:
    with transaction.atomic():
        publish(
            "core.user.registered",
            version=1,
            aggregate=("User", person),
            tenant=tenant_ref("org", ORG),
            visibility=Visibility.PERSONENBEZOGEN,
            payload={"user": str(person), "step": "confirmed"},
            operation=Operation.REDACT,
        )


def test_redact_neutralisiert_nur_mit_schalter(settings: Any) -> None:
    person = uuid.uuid4()
    alt = _personenbezogen("core.user.registered", person, user=str(person), step="created")

    settings.EVENTS_REDACT_NEUTRALIZE = False
    _redact(person)
    assert _nutzlasten()[alt.event_id] != {}

    settings.EVENTS_REDACT_NEUTRALIZE = True
    _redact(person)  # Tests führen Aufträge sofort aus (settings_test.TASKS)
    assert all(nutzlast == {} for nutzlast in _nutzlasten().values())


def test_redact_legt_den_auftrag_in_derselben_transaktion_an(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.EVENTS_REDACT_NEUTRALIZE = True
    eingereiht: list[tuple[Any, ...]] = []

    class Auftrag:
        @staticmethod
        def enqueue(*args: Any) -> None:
            eingereiht.append(args)

    monkeypatch.setattr(datenschutz, "journal_neutralisieren", Auftrag())
    person = uuid.uuid4()
    with pytest.raises(RuntimeError), transaction.atomic():
        publish(
            "core.user.registered",
            version=1,
            aggregate=("User", person),
            tenant=tenant_ref("org", ORG),
            visibility=Visibility.PERSONENBEZOGEN,
            payload={"user": str(person), "step": "confirmed"},
            operation=Operation.REDACT,
        )
        assert eingereiht == [(str(person),)]
        raise RuntimeError("Rücknahme der fachlichen Änderung")
    assert not Event.objects.exists()


def test_befehl_mit_probelauf_und_sicherheitsprotokoll() -> None:
    from apps.accounts.models import SecurityAuditLog

    person = uuid.uuid4()
    konto = _personenbezogen("core.user.registered", person, user=str(person), step="created")

    ausgabe = StringIO()
    call_command("events_neutralize", "--person", str(person), "--dry-run", stdout=ausgabe)
    assert "Probelauf: 1 Journaleinträge" in ausgabe.getvalue()
    assert _nutzlasten()[konto.event_id] != {}
    assert not SecurityAuditLog.objects.filter(event="betrieb").exists()

    ausgabe = StringIO()
    call_command("events_neutralize", "--person", str(person), stdout=ausgabe)
    assert "1 Journaleinträge neutralisiert" in ausgabe.getvalue()
    assert _nutzlasten()[konto.event_id] == {}
    eintrag = SecurityAuditLog.objects.get(event="betrieb")
    assert eintrag.details == {
        "aktion": "journal_neutralisiert",
        "quelle": "kommandozeile",
        "befehl": "events_neutralize",
        "person": str(person),
        "anzahl": 1,
    }

    with pytest.raises(CommandError, match="UUID"):
        call_command("events_neutralize", "--person", "keine-kennung")
