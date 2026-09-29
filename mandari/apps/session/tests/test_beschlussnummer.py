# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussnummern-Vergabe (B/<Jahr>/<lfd>) bei doppelter Auslösung.

Zwei Ausfertigungen mit demselben, noch nummernlosen Stand eines TOP (Doppelklick, zwei Fenster)
dürfen dem TOP keine zweite Nummer geben: Ob schon eine Nummer vergeben ist, entscheidet die
Datenbank unter der Sperre. Gespeichert wird nur die Nummer; ein veralteter Stand des Aufrufers
überschreibt nichts.
"""

from __future__ import annotations

from datetime import datetime, time

import pytest
from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant
from apps.session.services import resolution_service

pytestmark = pytest.mark.django_db


def _sitzung() -> SessionMeeting:
    tenant = SessionTenant.objects.create(name="Stadt Nummer", slug="nummer")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    start = timezone.make_aware(datetime.combine(timezone.localdate(), time(17, 0)))
    return SessionMeeting.objects.create(tenant=tenant, name="Ratssitzung", organization=gremium, start=start)


def _top(sitzung: SessionMeeting, nummer: str) -> SessionAgendaItem:
    return SessionAgendaItem.objects.create(
        meeting=sitzung, number=nummer, order=int(nummer), name=f"Beschluss {nummer}", vote_result="approved"
    )


def _frisch(item: SessionAgendaItem) -> SessionAgendaItem:
    return SessionAgendaItem.objects.select_related("meeting").get(pk=item.pk)


def _nummer(sitzung: SessionMeeting, lfd: int) -> str:
    return f"B/{timezone.localtime(sitzung.start).year}/{lfd:04d}"


def test_doppelte_ausloesung_vergibt_keine_zweite_nummer() -> None:
    sitzung = _sitzung()
    top = _top(sitzung, "1")
    erste, zweite = _frisch(top), _frisch(top)  # zwei Anfragen, beide noch ohne Nummer

    assert resolution_service.assign_resolution_number(erste) is True
    assert resolution_service.assign_resolution_number(zweite) is False

    assert _frisch(top).resolution_number == _nummer(sitzung, 1)
    assert zweite.resolution_number == _nummer(sitzung, 1), "der Aufrufer erhält die vergebene Nummer"


def test_naechster_beschluss_luekenlos_nach_doppelter_ausloesung() -> None:
    sitzung = _sitzung()
    top, weiterer = _top(sitzung, "1"), _top(sitzung, "2")
    veraltet = _frisch(top)
    resolution_service.assign_resolution_number(_frisch(top))
    resolution_service.assign_resolution_number(veraltet)

    assert resolution_service.ensure_numbers_for_meeting(sitzung) == 1
    assert _frisch(weiterer).resolution_number == _nummer(sitzung, 2)
    assert _frisch(top).resolution_number == _nummer(sitzung, 1)


def test_veralteter_stand_ueberschreibt_keine_aenderung() -> None:
    sitzung = _sitzung()
    top = _top(sitzung, "1")
    veraltet = _frisch(top)
    aktuell = _frisch(top)
    aktuell.name = "Beschluss 1 (berichtigter Betreff)"
    aktuell.implementation_status = "done"
    aktuell.save()

    assert resolution_service.assign_resolution_number(veraltet) is True

    gespeichert = _frisch(top)
    assert gespeichert.resolution_number == _nummer(sitzung, 1)
    assert gespeichert.name == "Beschluss 1 (berichtigter Betreff)"
    assert gespeichert.implementation_status == "done"
