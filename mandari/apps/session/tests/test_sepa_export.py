# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SEPA-Export für Sitzungsgeld und Monatspauschalen (Issue #428).

- Export-Referenzen sind über beide Arten eindeutig: ein Zähler je Mandant und Jahr, der auf den
  bereits vergebenen Nummern beider Tabellen aufsetzt; bestehende Referenzen bleiben unverändert.
- Der Export läuft in einer Transaktion mit gesperrten Positionen und nimmt nur genehmigte, noch
  nicht exportierte Positionen: Doppelt ausgelöst, gibt er keine Position zweimal aus.
- Der Verwendungszweck folgt der Art (Sitzungsgeld bzw. Monatspauschale); Beträge unverändert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.utils import timezone

from apps.session.models import (
    SessionAllowance,
    SessionAttendance,
    SessionExportCounter,
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

DEBTOR = {"allowances": {"debtor_name": "Stadt Kasse", "debtor_iban": "DE02100100100006820101"}}


@dataclass
class Kasse:
    tenant: SessionTenant
    gremium: SessionOrganization
    client: Client
    heute: date
    monat: date

    @property
    def base(self) -> str:
        return f"/session/{self.tenant.slug}/allowances"

    def person(self, name: str, iban: str | None = "DE02120300000000202051") -> SessionPerson:
        p = SessionPerson.objects.create(tenant=self.tenant, given_name="P", family_name=name)
        if iban is not None:
            cast(Any, p).set_bank_iban_encrypted(iban)
            p.save()
        return p

    def sitzungsgeld(self, person: SessionPerson, betrag: str = "25.00", **felder: Any) -> SessionAllowance:
        start = timezone.make_aware(datetime.combine(self.heute, time(18, 0)))
        sitzung = SessionMeeting.objects.create(tenant=self.tenant, name="Rat", organization=self.gremium, start=start)
        anwesenheit = SessionAttendance.objects.create(meeting=sitzung, person=person, status="present")
        felder.setdefault("status", "approved")
        return SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal(betrag), **felder)

    def pauschale(
        self, person: SessionPerson, betrag: str = "322.50", monat: date | None = None, **felder: Any
    ) -> SessionMonthlyAllowance:
        rate, _ = SessionMonthlyRate.objects.get_or_create(
            tenant=self.tenant, name="Teilpauschale", defaults={"amount": Decimal(betrag)}
        )
        felder.setdefault("status", "approved")
        return SessionMonthlyAllowance.objects.create(
            tenant=self.tenant, person=person, rate=rate, period=monat or self.monat, amount=Decimal(betrag), **felder
        )

    def export_sitzungsgeld(self) -> Any:
        tag = self.heute.isoformat()
        return self.client.post(f"{self.base}/export/sepa/", {"from": tag, "to": tag})

    def export_pauschalen(self, monat: date | None = None) -> Any:
        monat = monat or self.monat
        return self.client.post(f"{self.base}/monthly/export/sepa/", {"year": monat.year, "month": monat.month})


def kasse(slug: str = "kasse") -> Kasse:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug, settings=DEBTOR)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    heute = timezone.localdate()
    return Kasse(
        tenant=tenant,
        gremium=gremium,
        client=client(nutzer(tenant, "kasse", "manage_allowances")),
        heute=heute,
        monat=heute.replace(day=1),
    )


def _xml(response: Any) -> str:
    assert response.status_code == 200, response.status_code
    return cast(bytes, response.content).decode("utf-8")


def _tag(xml: str, name: str) -> list[str]:
    return re.findall(rf"<{name}>([^<]*)</{name}>", xml)


def _meldungen(response: Any) -> str:
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


def _ref(nummer: int) -> str:
    return f"SG-{timezone.localdate().year}-{nummer:04d}"


# =============================================================================
# Doppelte Auslösung
# =============================================================================


class TestDoppelteAusloesung:
    def test_sitzungsgeld_zweimal_hintereinander(self) -> None:
        k = kasse()
        a = k.sitzungsgeld(k.person("Amsel"))
        b = k.sitzungsgeld(k.person("Buche", iban="DE02120300000000202052"))

        erste = _xml(k.export_sitzungsgeld())
        assert _tag(erste, "NbOfTxs") == ["2", "2"]
        zweite = k.export_sitzungsgeld()
        assert zweite.status_code == 302, "zweiter Export liefert keine Datei"
        assert f"Zuletzt exportiert mit Referenz {_ref(1)}" in _meldungen(zweite)

        for position in (a, b):
            position.refresh_from_db()
            assert (position.status, position.export_reference) == ("paid", _ref(1))
        assert SessionExportCounter.objects.get(tenant=k.tenant).value == 1

    def test_pauschalen_zweimal_hintereinander(self) -> None:
        k = kasse()
        posten = k.pauschale(k.person("Amsel"))
        _xml(k.export_pauschalen())
        zweite = k.export_pauschalen()
        assert zweite.status_code == 302
        assert "nichts zu exportieren" in _meldungen(zweite)
        posten.refresh_from_db()
        assert (posten.status, posten.export_reference) == ("paid", _ref(1))

    def test_vorab_geladene_auswahl_wird_unter_sperre_neu_geprueft(self) -> None:
        """Zwei Auslösungen mit derselben, vorab gebildeten Auswahl: die zweite findet nichts mehr."""
        k = kasse()
        k.sitzungsgeld(k.person("Amsel"))
        auswahl = SessionAllowance.objects.filter(attendance__meeting__tenant=k.tenant).select_related(
            "attendance__person", "approved_by__user"
        )
        assert len(list(auswahl)) == 1  # Stand vor dem ersten Export ist geladen
        kwargs: dict[str, Any] = {"debtor_name": "Stadt", "debtor_iban": "DE02100100100006820101"}
        erster = allowance_service.export_sepa(k.tenant, auswahl, kind=allowance_service.KIND_SESSION, **kwargs)
        zweiter = allowance_service.export_sepa(k.tenant, auswahl, kind=allowance_service.KIND_SESSION, **kwargs)
        assert (erster.reference, erster.exported, erster.transaction_count) == (_ref(1), 1, 1)
        assert (zweiter.reference, zweiter.exported, zweiter.xml) == ("", 0, b"")

    def test_nur_noch_nicht_exportierte_positionen(self) -> None:
        k = kasse()
        alt = k.sitzungsgeld(k.person("Amsel"), export_reference="SG-2020-0001")  # genehmigt, aber schon exportiert
        neu = k.sitzungsgeld(k.person("Buche", iban="DE02120300000000202052"), betrag="30.00")
        xml = _xml(k.export_sitzungsgeld())
        assert _tag(xml, "CtrlSum") == ["30.00", "30.00"]
        alt.refresh_from_db()
        neu.refresh_from_db()
        assert (alt.status, alt.export_reference) == ("approved", "SG-2020-0001")
        assert (neu.status, neu.export_reference) == ("paid", _ref(1))


# =============================================================================
# Referenzen
# =============================================================================


class TestReferenzen:
    def test_eindeutig_ueber_beide_arten(self) -> None:
        k = kasse()
        amsel = k.person("Amsel")
        k.sitzungsgeld(amsel)
        k.pauschale(amsel)
        vormonat = date(k.monat.year - 1, 12, 1)
        k.pauschale(amsel, monat=vormonat)

        assert _tag(_xml(k.export_sitzungsgeld()), "MsgId") == [_ref(1)]
        assert _tag(_xml(k.export_pauschalen()), "MsgId") == [_ref(2)]
        k.sitzungsgeld(amsel)
        assert _tag(_xml(k.export_sitzungsgeld()), "MsgId") == [_ref(3)]
        assert _tag(_xml(k.export_pauschalen(vormonat)), "MsgId") == [_ref(4)]

        vergeben = [
            *SessionAllowance.objects.values_list("export_reference", flat=True),
            *SessionMonthlyAllowance.objects.values_list("export_reference", flat=True),
        ]
        assert sorted(set(vergeben)) == [_ref(1), _ref(2), _ref(3), _ref(4)]

    def test_zaehler_setzt_auf_bestehende_referenzen_auf(self) -> None:
        """Bestand aus älteren Ständen (auch doppelt vergebene Pauschalen-Referenzen) bleibt, neu geht es danach weiter."""
        k = kasse()
        amsel = k.person("Amsel")
        alt_sg = k.sitzungsgeld(amsel, status="paid", export_reference=_ref(3))
        alt_p1 = k.pauschale(amsel, monat=date(2025, 1, 1), status="paid", export_reference=_ref(7))
        alt_p2 = k.pauschale(amsel, monat=date(2025, 2, 1), status="paid", export_reference=_ref(7))
        k.pauschale(amsel)
        assert _tag(_xml(k.export_pauschalen()), "MsgId") == [_ref(8)]
        for alt, ref in ((alt_sg, _ref(3)), (alt_p1, _ref(7)), (alt_p2, _ref(7))):
            alt.refresh_from_db()
            assert alt.export_reference == ref

    def test_zaehler_je_mandant(self) -> None:
        nord, sued = kasse("nord"), kasse("sued")
        nord.sitzungsgeld(nord.person("Amsel"))
        sued.sitzungsgeld(sued.person("Buche"))
        assert _tag(_xml(nord.export_sitzungsgeld()), "MsgId") == [_ref(1)]
        assert _tag(_xml(sued.export_sitzungsgeld()), "MsgId") == [_ref(1)]

    def test_ohne_iban_wird_keine_referenz_verbraucht(self) -> None:
        k = kasse()
        ohne = k.sitzungsgeld(k.person("Amsel", iban=None))
        antwort = k.export_sitzungsgeld()
        assert antwort.status_code == 302
        assert "für keine der Personen ist eine IBAN hinterlegt: P Amsel" in _meldungen(antwort)
        ohne.refresh_from_db()
        assert (ohne.status, ohne.export_reference) == ("approved", "")

        k.sitzungsgeld(k.person("Buche"))
        assert _tag(_xml(k.export_sitzungsgeld()), "MsgId") == [_ref(1)]


# =============================================================================
# Verwendungszweck, Beträge, IBAN-Regel
# =============================================================================


class TestInhalt:
    def test_verwendungszweck_sitzungsgeld(self) -> None:
        k = kasse()
        amsel = k.person("Amsel")
        k.sitzungsgeld(amsel)
        k.sitzungsgeld(amsel, betrag="30.00")
        xml = _xml(k.export_sitzungsgeld())
        assert _tag(xml, "Ustrd") == [f"Sitzungsgeld 2 Sitzung(en) {_ref(1)}"]
        assert _tag(xml, "CtrlSum") == ["55.00", "55.00"]

    def test_verwendungszweck_monatspauschale(self) -> None:
        k = kasse()
        amsel = k.person("Amsel")
        k.pauschale(amsel)
        zulage, _ = SessionMonthlyRate.objects.get_or_create(
            tenant=k.tenant, name="Zulage", defaults={"amount": Decimal("874.00")}
        )
        SessionMonthlyAllowance.objects.create(
            tenant=k.tenant, person=amsel, rate=zulage, period=k.monat, amount=Decimal("874.00"), status="approved"
        )
        xml = _xml(k.export_pauschalen())
        assert _tag(xml, "Ustrd") == [f"Monatspauschale {k.monat:%m/%Y} {_ref(1)}"]
        assert "Sitzungsgeld" not in xml
        assert _tag(xml, "CtrlSum") == ["1196.50", "1196.50"]

    def test_iban_nur_aus_leerraum_gilt_bei_pauschalen_als_fehlend(self) -> None:
        k = kasse()
        mit = k.pauschale(k.person("Amsel"))
        leer = k.pauschale(k.person("Buche", iban="   "))
        antwort = k.export_pauschalen()
        assert _tag(_xml(antwort), "NbOfTxs") == ["1", "1"]
        assert "Ohne IBAN übersprungen: P Buche" in _meldungen(antwort)
        mit.refresh_from_db()
        leer.refresh_from_db()
        assert (mit.status, leer.status, leer.export_reference) == ("paid", "approved", "")
