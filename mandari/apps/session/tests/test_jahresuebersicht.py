# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Jahresübersicht je Person (Grundlage der Steuerbescheinigung).

Sie weist Sitzungsgeld und Monatspauschalen getrennt aus und rechnet beide zusammen – mit derselben
Statuslogik (ausgezahlt, genehmigt, sonst ausstehend) und ohne stornierte Positionen. Sitzungsgeld
zählt im Jahr der Sitzung, Pauschalen im Jahr des Abrechnungsmonats. Seite und CSV zeigen dieselben
Werte.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.session.models import (
    SessionAllowance,
    SessionAttendance,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import allowance_service
from apps.session.tests._niederschrift import client, nutzer

pytestmark = pytest.mark.django_db

JAHR = 2025


@dataclass
class Kasse:
    tenant: SessionTenant
    gremium: SessionOrganization
    rate: SessionMonthlyRate

    def person(self, name: str) -> SessionPerson:
        return SessionPerson.objects.create(tenant=self.tenant, given_name="P", family_name=name)

    def sitzungsgeld(self, person: SessionPerson, betrag: str, status: str, tag: date | None = None) -> None:
        start = timezone.make_aware(datetime.combine(tag or date(JAHR, 3, 10), time(18, 0)))
        sitzung = SessionMeeting.objects.create(tenant=self.tenant, name="Rat", organization=self.gremium, start=start)
        anwesenheit = SessionAttendance.objects.create(meeting=sitzung, person=person, status="present")
        SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal(betrag), status=status)

    def pauschale(self, person: SessionPerson, betrag: str, status: str, monat: int, jahr: int = JAHR) -> None:
        SessionMonthlyAllowance.objects.create(
            tenant=self.tenant,
            person=person,
            rate=self.rate,
            period=date(jahr, monat, 1),
            amount=Decimal(betrag),
            status=status,
        )


def kasse(slug: str = "jahr") -> Kasse:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    rate = SessionMonthlyRate.objects.create(tenant=tenant, name="Teilpauschale", amount=Decimal("300.00"))
    return Kasse(tenant=tenant, gremium=gremium, rate=rate)


def _zeile(rows: list[dict[str, Any]], person: SessionPerson) -> dict[str, Any]:
    return next(row for row in rows if row["person"].pk == person.pk)


def _csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


def test_pauschalen_getrennt_ausgewiesen_und_mitgerechnet() -> None:
    k = kasse()
    amsel = k.person("Amsel")
    k.sitzungsgeld(amsel, "25.00", "paid")
    k.sitzungsgeld(amsel, "25.00", "approved")
    k.pauschale(amsel, "300.00", "paid", monat=1)
    k.pauschale(amsel, "300.00", "approved", monat=2)
    k.pauschale(amsel, "300.00", "pending", monat=3)

    zeile = _zeile(allowance_service.year_summary(k.tenant, JAHR), amsel)

    assert zeile["session"]["count"] == 2 and zeile["session"]["total"] == Decimal("50.00")
    assert zeile["monthly"]["count"] == 3 and zeile["monthly"]["total"] == Decimal("900.00")
    assert zeile["count"] == 5
    assert zeile["total"] == Decimal("950.00")
    assert (zeile["paid"], zeile["approved"], zeile["pending"]) == (
        Decimal("325.00"),
        Decimal("325.00"),
        Decimal("300.00"),
    )


def test_person_nur_mit_pauschalen_erscheint() -> None:
    k = kasse()
    buche = k.person("Buche")
    k.pauschale(buche, "150.00", "paid", monat=6)

    zeile = _zeile(allowance_service.year_summary(k.tenant, JAHR), buche)

    assert zeile["session"]["total"] == Decimal("0.00")
    assert zeile["monthly"]["total"] == zeile["total"] == zeile["paid"] == Decimal("150.00")


def test_storno_anderes_jahr_und_anderer_mandant_zaehlen_nicht() -> None:
    k = kasse()
    fremd = kasse("fremd")
    ceder = k.person("Ceder")
    k.pauschale(ceder, "300.00", "approved", monat=12)
    k.pauschale(ceder, "300.00", "cancelled", monat=11)
    k.pauschale(ceder, "300.00", "paid", monat=1, jahr=JAHR + 1)
    k.sitzungsgeld(ceder, "25.00", "cancelled")
    k.sitzungsgeld(ceder, "25.00", "paid", tag=date(JAHR - 1, 12, 1))
    fremd.pauschale(fremd.person("Fremd"), "999.00", "paid", monat=5)

    rows = allowance_service.year_summary(k.tenant, JAHR)

    assert [row["person"].pk for row in rows] == [ceder.pk]
    assert rows[0]["total"] == rows[0]["monthly"]["total"] == Decimal("300.00")
    assert rows[0]["session"]["count"] == 0


def test_seite_und_csv_zeigen_pauschalen() -> None:
    k = kasse()
    dachs = k.person("Dachs")
    k.sitzungsgeld(dachs, "25.00", "paid")
    k.pauschale(dachs, "300.00", "approved", monat=4)
    kaemmerei: Client = client(nutzer(k.tenant, "kaemmerei", "manage_allowances"))
    url = f"/session/{k.tenant.slug}/allowances/year/"

    seite = kaemmerei.get(url, {"year": JAHR})
    assert seite.status_code == 200
    totals = seite.context["totals"]
    assert totals["session"]["total"] == Decimal("25.00")
    assert totals["monthly"]["total"] == Decimal("300.00")
    assert totals["total"] == Decimal("325.00")
    assert "Monatspauschalen" in seite.content.decode()

    antwort = kaemmerei.get(url, {"year": str(JAHR), "format": "csv"})
    assert antwort.status_code == 200
    kopf, zeile = _csv(antwort.content.decode("utf-8"))
    werte = dict(zip(kopf, zeile, strict=True))
    assert werte["Summe"] == "325,00"
    assert (werte["Ausgezahlt"], werte["Genehmigt"], werte["Ausstehend"]) == ("25,00", "300,00", "0,00")
    assert (werte["Sitzungsgeld Positionen"], werte["Sitzungsgeld"]) == ("1", "25,00")
    assert (werte["Monatspauschalen Positionen"], werte["Monatspauschalen"]) == ("1", "300,00")
