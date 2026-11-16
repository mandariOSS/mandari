# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechtegrenzen zwischen Anwesenheit, Abrechnung und Niederschrift.

- Vier-Augen-Prinzip der Niederschrift: Auch wer nur Einzelstimmen ändert (Summen gleich), hat die Niederschrift
  bearbeitet und genehmigt sie nicht selbst.
- Eine Anwesenheitszeile mit genehmigtem, exportiertem oder ausgezahltem Sitzungsgeld bleibt bestehen; die Position
  lässt sich nur in der Abrechnung stornieren.
- Die Abrechnung nennt den Namen einer nichtöffentlichen Sitzung nur mit dem Sichtrecht dafür.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAttendance,
    SessionMeeting,
    SessionProtocol,
)
from apps.session.services import allowance_service, four_eyes_service, voting_service
from apps.session.services.four_eyes_service import PROCESS_PROTOCOL, ApprovalError
from apps.session.tests._niederschrift import Welt, client, nutzer, welt

pytestmark = pytest.mark.django_db


# --- Vier-Augen-Prinzip und Einzelstimmen ------------------------------------------------------------------


def _namentlich(w: Welt) -> SessionAgendaItem:
    item = SessionAgendaItem.objects.get(pk=w.top.pk)
    item.voting_method = "roll_call"
    item.save()
    a, b, c = w.stimmberechtigt
    voting_service.capture_votes(item, {a: "yes", b: "yes", c: "no"}, recorded_by=w.protokollant)
    # Ausgangslage: Niemand außer dem Ersteller hat die Niederschrift bearbeitet
    SessionProtocol.objects.filter(pk=w.protokoll.pk).update(content_edited_by=None)
    return SessionAgendaItem.objects.get(pk=item.pk)


def test_stimmentausch_bei_gleichen_summen_zaehlt_als_bearbeitung() -> None:
    w = welt(status="review", four_eyes=True)
    item = _namentlich(w)
    a, _b, c = w.stimmberechtigt
    protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
    assert four_eyes_service.authorize(PROCESS_PROTOCOL, protokoll, w.zweite) is None

    # Zwei Stimmen tauschen: Die Summen bleiben bei 2 Ja und 1 Nein
    voting_service.capture_votes(item, {a: "no", c: "yes"}, recorded_by=w.zweite)

    item.refresh_from_db()
    assert (item.votes_yes, item.votes_no) == (2, 1)
    protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
    assert protokoll.content_edited_by_id == w.zweite.pk
    with pytest.raises(ApprovalError):
        four_eyes_service.authorize(PROCESS_PROTOCOL, protokoll, w.zweite)
    # Ein anderes Konto mit Genehmigungsrecht darf weiter genehmigen
    assert four_eyes_service.authorize(PROCESS_PROTOCOL, protokoll, w.genehmiger) is None


def test_unveraenderter_lauf_zaehlt_nicht_als_bearbeitung() -> None:
    w = welt(status="review", four_eyes=True)
    item = _namentlich(w)
    a, b, c = w.stimmberechtigt

    voting_service.capture_votes(item, {a: "yes", b: "yes", c: "no"}, recorded_by=w.zweite)

    assert SessionProtocol.objects.get(pk=w.protokoll.pk).content_edited_by_id is None


def test_genehmigte_niederschrift_bekommt_keinen_bearbeitungsvermerk() -> None:
    w = welt(status="approved", four_eyes=True)

    four_eyes_service.mark_protocol_edited(w.sitzung.pk, w.zweite)
    four_eyes_service.mark_protocol_edited(w.sitzung.pk, None)

    assert SessionProtocol.objects.get(pk=w.protokoll.pk).content_edited_by_id is None


# --- Anwesenheit mit Sitzungsgeld -----------------------------------------------------------------------------


def _entfernen(w: Welt, anwesenheit: SessionAttendance) -> Any:
    dienst = nutzer(w.tenant, "dienst", "view_meetings", "edit_meetings", "manage_attendance")
    url = reverse("session:attendance_delete", kwargs={"tenant_slug": w.tenant.slug, "attendance_id": anwesenheit.pk})
    return client(dienst).post(url)


@pytest.mark.parametrize(
    ("status", "referenz"),
    [("paid", ""), ("approved", ""), ("pending", "EXPORT-2026-11"), ("paid", "EXPORT-2026-11")],
)
def test_anwesenheit_mit_geprueftem_sitzungsgeld_bleibt(status: str, referenz: str) -> None:
    w = welt(status="review")
    anwesenheit = SessionAttendance.objects.get(meeting=w.sitzung, person=w.gast)
    position = SessionAllowance.objects.create(
        attendance=anwesenheit, amount=Decimal("25.00"), status=status, export_reference=referenz
    )

    antwort = _entfernen(w, anwesenheit)

    assert SessionAttendance.objects.filter(pk=anwesenheit.pk).exists()
    assert SessionAllowance.objects.filter(pk=position.pk, status=status).exists()
    meldungen = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert meldungen == [allowance_service.ATTENDANCE_HAS_ALLOWANCE]


@pytest.mark.parametrize("status", ["pending", "cancelled", None])
def test_anwesenheit_ohne_gepruefte_position_laesst_sich_entfernen(status: str | None) -> None:
    w = welt(status="review")
    anwesenheit = SessionAttendance.objects.get(meeting=w.sitzung, person=w.gast)
    if status is not None:
        SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("25.00"), status=status)

    _entfernen(w, anwesenheit)

    assert not SessionAttendance.objects.filter(pk=anwesenheit.pk).exists()


# --- Abrechnung und nichtöffentliche Sitzungen ------------------------------------------------------------------


def _abrechnung(w: Welt) -> dict[str, str]:
    """Je eine ausgezahlte Position für eine öffentliche und eine nichtöffentliche Sitzung; Zeitraum als Parameter."""
    geheim = SessionMeeting.objects.create(
        tenant=w.tenant,
        name="GEHEIMSITZUNG Personalangelegenheit",
        organization=w.gremium,
        start=timezone.now() - timedelta(days=3),
        is_public=False,
    )
    person = w.stimmberechtigt[0]
    offen = SessionAttendance.objects.get(meeting=w.sitzung, person=person)
    verdeckt = SessionAttendance.objects.create(meeting=geheim, person=person, status="present")
    for anwesenheit in (offen, verdeckt):
        SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("25.00"), status="paid")
    heute = timezone.localdate()
    return {"from": (heute - timedelta(days=10)).isoformat(), "to": heute.isoformat()}


@pytest.mark.parametrize("seite", ["session:allowance_export_csv", "session:allowances"])
def test_abrechnung_nennt_nichtoeffentliche_sitzung_nur_mit_sichtrecht(seite: str) -> None:
    w = welt(status="review")
    zeitraum = _abrechnung(w)
    url = reverse(seite, kwargs={"tenant_slug": w.tenant.slug})
    kasse = nutzer(w.tenant, "kasse", "manage_allowances")
    kasse_noe = nutzer(w.tenant, "kasse-noe", "manage_allowances", "view_non_public_meetings")

    ohne = client(kasse).get(url, zeitraum)
    mit = client(kasse_noe).get(url, zeitraum)

    assert ohne.status_code == 200 and mit.status_code == 200
    text_ohne = ohne.content.decode("utf-8-sig")
    text_mit = mit.content.decode("utf-8-sig")
    # Die Abrechnung bleibt vollständig: Beide Positionen stehen darin, auch die der nichtöffentlichen Sitzung
    assert "GEHEIMSITZUNG" not in text_ohne
    assert allowance_service.NON_PUBLIC_MEETING_LABEL in text_ohne
    assert "GEHEIMSITZUNG Personalangelegenheit" in text_mit
    assert allowance_service.NON_PUBLIC_MEETING_LABEL not in text_mit
    for text in (text_ohne, text_mit):
        assert "Bauausschuss" in text


def test_sitzungsname_ohne_annotation_ist_der_name_der_sitzung() -> None:
    w = welt(status="review")
    anwesenheit = SessionAttendance.objects.get(meeting=w.sitzung, person=w.gast)
    position = SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("25.00"))

    assert allowance_service.meeting_label(position) == w.sitzung.name
