# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO: personenbezogene Nutzlasten im Journal neutralisieren (``apps.events.datenschutz``, Issue #511).

Nach einem ``redact`` werden die Journaleinträge mit Sichtbarkeit ``personenbezogen`` zu den Personen
neutralisiert, die das Ereignis nennt (Objekt vom Typ ``User`` oder Personenfeld laut Vertrag): Die Kennung
bleibt, die Felder werden leer. Welche Felder eine Person nennen, steht in den Verträgen (``x-person``),
eingehängt von ``hub.contracts``. Der Auftrag landet immer in ``events_task``, nie sofort in der Anfrage.
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
from apps.events.models import Event, Operation, Task, Visibility
from apps.events.task_runner import run_pending
from apps.events.tests.hilfen import ereignis_anlegen

pytestmark = pytest.mark.django_db

ORG = uuid.UUID("3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d")


def _personenbezogen(typ: str, aggregat: uuid.UUID, aggregat_typ: str = "Meeting", **nutzlast: Any) -> Event:
    return ereignis_anlegen(
        type=typ,
        version=1,
        aggregate_type=aggregat_typ,
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
    konto = _personenbezogen("core.user.registered", person, "User", user=str(person), step="created")
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
    konto = _personenbezogen("core.user.registered", person, "User", user=str(person), step="created")
    zusage = _personenbezogen(
        "attendance.response_recorded", sitzung, meeting=str(sitzung), person=str(person), response="declined"
    )
    # Dieselbe Kennung als Objekt eines anderen Typs ist keine Person
    gleiche_kennung = _personenbezogen(
        "attendance.response_recorded", person, meeting=str(person), person=str(uuid.uuid4()), response="confirmed"
    )
    vorher = datenschutz.set_person_fields_provider(None)
    try:
        assert datenschutz.neutralize(person) == 1
    finally:
        datenschutz.set_person_fields_provider(vorher)
    nachher = _nutzlasten()
    assert nachher[konto.event_id] == {}
    assert nachher[zusage.event_id] != {} and nachher[gleiche_kennung.event_id] != {}


def _publish_redact(typ: str, aggregat: tuple[str, uuid.UUID], nutzlast: dict[str, Any]) -> Event:
    return publish(
        typ,
        version=1,
        aggregate=aggregat,
        tenant=tenant_ref("org", ORG),
        visibility=Visibility.PERSONENBEZOGEN,
        payload=nutzlast,
        operation=Operation.REDACT,
    )


def _auftraege() -> list[Any]:
    """Argumente der eingereihten Aufträge zum Neutralisieren."""
    pfad = datenschutz.journal_neutralisieren.module_path
    return list(Task.objects.filter(task_path=pfad).order_by("pk").values_list("args", flat=True))


def _ereignis(typ: str, aggregat_typ: str, aggregat: uuid.UUID, **nutzlast: Any) -> Event:
    return Event(type=typ, version=1, aggregate_type=aggregat_typ, aggregate_id=aggregat, payload=nutzlast)


def test_personen_eines_redact_stehen_im_ereignis() -> None:
    person, sitzung = uuid.uuid4(), uuid.uuid4()
    konto = _ereignis("core.user.registered", "User", person, user=str(person), step="confirmed")
    assert datenschutz.persons_of(konto) == [person]
    absage = _ereignis(
        "attendance.response_recorded",
        "Meeting",
        sitzung,
        meeting=str(sitzung),
        person=str(person),
        response="declined",
    )
    assert datenschutz.persons_of(absage) == [person], "die Sitzung ist keine Person"
    ris = _ereignis(
        "ris.object.depublished", "Person", person, object_type="Person", object=str(person), reason="datenschutz"
    )
    assert datenschutz.persons_of(ris) == [], "öffentliche Daten: kein Personenfeld, kein Konto"
    kaputt = _ereignis("attendance.response_recorded", "Meeting", sitzung, meeting=str(sitzung), person="keine-kennung")
    assert datenschutz.persons_of(kaputt) == []


def test_redact_einer_rueckmeldung_neutralisiert_nur_die_person_nicht_die_sitzung(settings: Any) -> None:
    """Aggregat der Rückmeldung ist die Sitzung: Andere Personen derselben Sitzung bleiben unberührt."""
    settings.EVENTS_REDACT_NEUTRALIZE = True
    person_a, person_b, sitzung = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    konto_a = _personenbezogen("core.user.registered", person_a, "User", user=str(person_a), step="created")
    zusage_a = _personenbezogen(
        "attendance.response_recorded", sitzung, meeting=str(sitzung), person=str(person_a), response="confirmed"
    )
    zusage_b = _personenbezogen(
        "attendance.response_recorded", sitzung, meeting=str(sitzung), person=str(person_b), response="confirmed"
    )
    vorher = _nutzlasten()

    with transaction.atomic():
        absage = _publish_redact(
            "attendance.response_recorded",
            ("Meeting", sitzung),
            {"meeting": str(sitzung), "person": str(person_a), "response": "declined"},
        )
    assert _auftraege() == [{"args": [str(person_a)], "kwargs": {}}]
    assert run_pending(["default"]) == 1

    nachher = _nutzlasten()
    assert nachher[konto_a.event_id] == {} and nachher[zusage_a.event_id] == {} and nachher[absage.event_id] == {}
    assert nachher[zusage_b.event_id] == vorher[zusage_b.event_id], "fremde Person derselben Sitzung bleibt"


def test_redact_ohne_person_legt_keinen_auftrag_an(settings: Any) -> None:
    settings.EVENTS_REDACT_NEUTRALIZE = True
    person = uuid.uuid4()
    with transaction.atomic():
        publish(
            "ris.object.depublished",
            version=1,
            aggregate=("Person", person),
            tenant=tenant_ref("source", uuid.uuid4()),
            visibility=Visibility.OEFFENTLICH,
            payload={"object_type": "Person", "object": str(person), "reason": "datenschutz"},
            operation=Operation.REDACT,
        )
    assert _auftraege() == []


def test_redact_ueber_andere_datenbank_legt_keinen_auftrag_an(settings: Any) -> None:
    settings.EVENTS_REDACT_NEUTRALIZE = True
    person = uuid.uuid4()
    konto = _ereignis("core.user.registered", "User", person, user=str(person), step="confirmed")
    assert datenschutz.after_redact(konto, "andere") == 0
    assert _auftraege() == []


def _redact(person: uuid.UUID) -> None:
    with transaction.atomic():
        _publish_redact("core.user.registered", ("User", person), {"user": str(person), "step": "confirmed"})


def test_redact_neutralisiert_nur_mit_schalter_und_nur_im_worker(settings: Any) -> None:
    person = uuid.uuid4()
    alt = _personenbezogen("core.user.registered", person, "User", user=str(person), step="created")

    settings.EVENTS_REDACT_NEUTRALIZE = False
    _redact(person)
    assert _auftraege() == [] and _nutzlasten()[alt.event_id] != {}

    settings.EVENTS_REDACT_NEUTRALIZE = True
    # Die Tests führen Aufträge sonst sofort aus (settings_test.TASKS, wie ohne TASKS_BACKEND in Produktion);
    # das Neutralisieren landet trotzdem in events_task und läuft nicht in der Anfrage
    _redact(person)
    assert _auftraege() == [{"args": [str(person)], "kwargs": {}}]
    assert _nutzlasten()[alt.event_id] != {}
    assert run_pending(["default"]) == 1
    assert all(nutzlast == {} for nutzlast in _nutzlasten().values())


def test_redact_legt_den_auftrag_in_derselben_transaktion_an(settings: Any) -> None:
    settings.EVENTS_REDACT_NEUTRALIZE = True
    person = uuid.uuid4()
    with pytest.raises(RuntimeError), transaction.atomic():
        _publish_redact("core.user.registered", ("User", person), {"user": str(person), "step": "confirmed"})
        assert _auftraege() == [{"args": [str(person)], "kwargs": {}}]
        raise RuntimeError("Rücknahme der fachlichen Änderung")
    assert not Event.objects.exists() and _auftraege() == []


def test_befehl_mit_probelauf_und_sicherheitsprotokoll() -> None:
    from apps.accounts.models import SecurityAuditLog

    person = uuid.uuid4()
    konto = _personenbezogen("core.user.registered", person, "User", user=str(person), step="created")

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
