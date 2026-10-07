# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zuordnung (Issue #915): TOP-Nummer → Tagesordnungspunkt, gelesener Name → Person (eindeutig, unsicher, keine)."""

from __future__ import annotations

from datetime import date

import pytest

from hub.live.models import SpeechAssignment
from hub.live.tests.conftest import Welt, mitglied, person
from hub.live.zuordnung import (
    aehnlichkeit,
    normalisiere_nummer,
    person_zuordnen,
    tagesordnungspunkt,
    vergleichsname,
)
from insight_core.models import OParlOrganization

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("roh", "nummer"),
    [("5", "5"), ("5.", "5"), ("Ö 5", "5"), ("TOP 5", "5"), ("1.1", "1.1"), (" 12 ", "12"), ("Ö", None), (None, None)],
)
def test_nummer_normalisieren(roh: str | None, nummer: str | None) -> None:
    assert normalisiere_nummer(roh) == nummer


def test_tagesordnungspunkt_der_sitzung(welt: Welt) -> None:
    assert tagesordnungspunkt(welt.sitzung.pk, "5") == welt.top5, "„Ö 5“ im RIS"
    assert tagesordnungspunkt(welt.sitzung.pk, "5.1") == welt.top51
    assert tagesordnungspunkt(welt.sitzung.pk, "9") is None
    welt.top5.public = False
    welt.top5.save()
    assert tagesordnungspunkt(welt.sitzung.pk, "5") is None, "nichtöffentliche TOPs nie"


def test_titelaehnlichkeit() -> None:
    assert aehnlichkeit("Neubau einer Grundschule", "Neubau einer Grundschule am Musterweg") == 0.83
    assert aehnlichkeit(None, "x") is None
    assert (aehnlichkeit("Haushalt", "Spielplatz sanieren") or 0) < 0.5


def test_vergleichsname_ohne_titel() -> None:
    assert vergleichsname("Dr. Erika Muster") == "erika muster"
    assert vergleichsname("Prof. Dr.-Ing. Max Beispiel") == "max beispiel"
    assert vergleichsname("Jörg Groß") == "jorg gross"


def test_eindeutig_unter_den_mitgliedern(welt: Welt) -> None:
    treffer = person_zuordnen("Erika Muster", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (welt.muster, SpeechAssignment.EINDEUTIG)
    treffer = person_zuordnen("Dr. Erika Muster", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert treffer.person == welt.muster


def test_ehemaliges_mitglied_zaehlt_nicht_im_gremium_aber_in_der_kommune(welt: Welt) -> None:
    ehemalig = person(welt.body, "Paula", "Probe")
    mitglied(ehemalig, welt.rat, end_date=date(2019, 12, 31))
    treffer = person_zuordnen("Paula Probe", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (ehemalig, SpeechAssignment.EINDEUTIG), "über die Kommune"


def test_namensgleiche_sind_unsicher(welt: Welt) -> None:
    zweite = person(welt.body, "Max", "Beispiel")
    mitglied(zweite, welt.rat)
    treffer = person_zuordnen("Max Beispiel", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (None, SpeechAssignment.UNSICHER)


def test_lesefehler_ist_unsicher(welt: Welt) -> None:
    treffer = person_zuordnen("Erika Mustor", meeting=welt.sitzung, organization_id=welt.rat.pk)
    assert (treffer.person, treffer.zuordnung) == (welt.muster, SpeechAssignment.UNSICHER)


def test_unbekannt_und_andere_kommune(welt: Welt) -> None:
    assert person_zuordnen("Gisela Gast", meeting=welt.sitzung, organization_id=welt.rat.pk).zuordnung == (
        SpeechAssignment.KEINE
    )
    andere = OParlOrganization.objects.create(
        external_id="https://andere.example/o/1", body=welt.body, name="Ausschuss"
    )
    treffer = person_zuordnen("Erika Muster", meeting=welt.sitzung, organization_id=andere.pk)
    assert treffer.person == welt.muster, "ohne Mitgliedschaft über die Kommune"
    assert person_zuordnen("ab", meeting=welt.sitzung, organization_id=welt.rat.pk).zuordnung == SpeechAssignment.KEINE
