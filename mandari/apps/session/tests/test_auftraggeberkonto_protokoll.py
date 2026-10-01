# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sitzungsgeld: Änderungen am Auftraggeberkonto des SEPA-Exports stehen im Protokoll des Mandanten."""

from __future__ import annotations

import json

import pytest

from apps.session.models import SessionAuditLog, SessionTenant
from apps.session.tests._niederschrift import client, nutzer

pytestmark = pytest.mark.django_db

IBAN_ALT = "DE89370400440532013000"
IBAN_NEU = "DE02120300000000202051"


def _eintraege(tenant: SessionTenant) -> list[SessionAuditLog]:
    return list(SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionTenant", action="update"))


def test_aenderung_des_auftraggeberkontos_wird_protokolliert() -> None:
    tenant = SessionTenant.objects.create(name="Stadt Konto", slug="konto")
    kaemmerei = nutzer(tenant, "kaemmerei", "manage_allowances")
    pfad = f"/session/{tenant.slug}/allowances/debtor/save/"

    client(kaemmerei).post(pfad, {"debtor_name": "Stadtkasse", "debtor_iban": IBAN_ALT, "debtor_bic": ""})
    client(kaemmerei).post(pfad, {"debtor_name": "Stadtkasse", "debtor_iban": IBAN_NEU, "debtor_bic": "BYLADEM1001"})

    eintraege = _eintraege(tenant)
    assert len(eintraege) == 2
    letzter = max(eintraege, key=lambda e: e.seq or 0)
    assert letzter.user == kaemmerei
    assert letzter.changes == {
        "auftraggeberkonto_iban": {"alt": "DE … 3000", "neu": "DE … 2051"},
        "auftraggeberkonto_bic": {"alt": "", "neu": "BYLADEM1001"},
    }
    # Die vollständige IBAN steht nicht im Protokoll
    assert IBAN_ALT not in json.dumps([e.changes for e in eintraege])
    assert IBAN_NEU not in json.dumps([e.changes for e in eintraege])


def test_unveraendertes_konto_ohne_eintrag() -> None:
    tenant = SessionTenant.objects.create(
        name="Stadt Konto", slug="konto", settings={"allowances": {"debtor_name": "Kasse", "debtor_iban": IBAN_ALT}}
    )
    kaemmerei = nutzer(tenant, "kaemmerei", "manage_allowances")

    client(kaemmerei).post(
        f"/session/{tenant.slug}/allowances/debtor/save/",
        {"debtor_name": "Kasse", "debtor_iban": IBAN_ALT, "debtor_bic": ""},
    )

    assert _eintraege(tenant) == []
