# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsgeld, Monatspauschalen und Auswertungen: Korrekturen aus der Fehlerjagd im Session-RIS.

- Sitzungsgeld: Stornierung (von Hand und automatisch nach Absage oder korrigierter Anwesenheit),
  Wiederaufnahme, Prüfung von Gremium, Jahr, Betrag, IBAN und BIC
- Monatspauschalen: Katalog ändern und deaktivieren, Stornierung, ungültiger Monat, Rücksprung mit Monat,
  Protokoll beim gescheiterten Löschen
- Berichte (Durchlaufzeit ab Freigabe, CSV-Beträge), Archiv und Wahlperiode, Suche, Dashboard,
  Leitstelle, API-Übersicht
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAllowance,
    SessionAllowanceRate,
    SessionApplication,
    SessionAttendance,
    SessionAuditLog,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
)
from apps.session.services import allowance_service, leitstelle_service, report_service

pytestmark = pytest.mark.django_db

BERLIN = ZoneInfo("Europe/Berlin")
#: Gültige Beispiel-IBAN (Prüfziffer stimmt) und BIC
IBAN = "DE89370400440532013000"
BIC = "COBADEFFXXX"

#: Lese-Rechte, die SessionRole von sich aus gewährt – für Rollen ohne sie ausdrücklich abschalten
_DEFAULT_VIEWS = ("view_dashboard", "view_meetings", "view_papers", "view_applications", "view_protocols")


def konto(tenant: SessionTenant, name: str, *perms: str) -> SessionUser:
    """Konto mit genau diesen Rechten (auch die sonst voreingestellten Lese-Rechte nur, wenn genannt)."""
    flags = {f"can_{perm}": False for perm in _DEFAULT_VIEWS}
    flags.update({f"can_{perm}": True for perm in perms})
    role = SessionRole.objects.create(tenant=tenant, name=f"r-{name}", **flags)
    user = cast(Any, UserFactory)(email=f"{name}@{tenant.slug}.example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    return session_user


def client(session_user: SessionUser) -> Client:
    result = Client()
    result.force_login(session_user.user)
    return result


def meldungen(response: Any) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


def am(tag: date, stunde: int = 18) -> datetime:
    return datetime.combine(tag, time(stunde, 0), tzinfo=BERLIN)


@dataclass
class Kasse:
    tenant: SessionTenant
    rat: SessionOrganization
    kaemmerei: SessionUser
    pruefer: SessionUser
    base: str
    tag: date

    def sitzung(self, tag: date | None = None, **extra: Any) -> SessionMeeting:
        return SessionMeeting.objects.create(
            tenant=self.tenant, name="Ratssitzung", organization=self.rat, start=am(tag or self.tag), **extra
        )

    def anwesend(self, sitzung: SessionMeeting, name: str, status: str = "present") -> SessionAttendance:
        person = SessionPerson.objects.create(tenant=self.tenant, given_name="P", family_name=name)
        cast(Any, person).set_bank_iban_encrypted(IBAN)
        person.save()
        return SessionAttendance.objects.create(meeting=sitzung, person=person, status=status)

    @property
    def zeitraum(self) -> dict[str, str]:
        return {"from": (self.tag - timedelta(days=3)).isoformat(), "to": (self.tag + timedelta(days=3)).isoformat()}


@pytest.fixture
def kasse() -> Kasse:
    tenant = SessionTenant.objects.create(name="Stadt Kasse", slug="kasse")
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", allowance_amount=Decimal("35.00"))
    return Kasse(
        tenant=tenant,
        rat=rat,
        kaemmerei=konto(tenant, "kaemmerei", "view_dashboard", "manage_allowances"),
        pruefer=konto(tenant, "pruefer", "view_dashboard", "manage_allowances"),
        base=f"/session/{tenant.slug}/allowances",
        tag=timezone.localdate() - timedelta(days=10),
    )


# =============================================================================
# Sitzungsgeld: Stornierung und Abgleich mit Anwesenheit/Absage
# =============================================================================


def test_genehmigung_storniert_position_nach_korrigierter_anwesenheit(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    client(kasse.kaemmerei).post(f"{kasse.base}/generate/", kasse.zeitraum)
    position = SessionAllowance.objects.get(attendance=anwesenheit)
    assert position.status == "pending"

    anwesenheit.status = "absent"
    anwesenheit.save()
    antwort = client(kasse.pruefer).post(f"{kasse.base}/approve/", kasse.zeitraum)

    position.refresh_from_db()
    assert position.status == "cancelled"
    assert position.notes == allowance_service.AUTO_CANCEL_NOTE
    assert position.approved_by is None
    assert any("storniert" in text for text in meldungen(antwort))
    # Die Stornierung steht im Protokoll
    assert SessionAuditLog.objects.filter(
        tenant=kasse.tenant, action="allowance_cancelled", object_id=position.pk
    ).exists()


def test_sepa_export_ueberweist_nichts_fuer_abgesagte_sitzung(kasse: Kasse) -> None:
    sitzung = kasse.sitzung()
    kasse.anwesend(sitzung, "Amsel")
    kasse.tenant.settings = {"allowances": {"debtor_name": "Stadt", "debtor_iban": IBAN}}
    kasse.tenant.save()
    client(kasse.kaemmerei).post(f"{kasse.base}/generate/", kasse.zeitraum)
    client(kasse.pruefer).post(f"{kasse.base}/approve/", kasse.zeitraum)
    assert SessionAllowance.objects.get().status == "approved"

    # Absage nach der Genehmigung: Die Position darf nicht mehr überwiesen werden
    sitzung.cancelled = True
    sitzung.save()
    antwort = client(kasse.pruefer).post(f"{kasse.base}/export/sepa/", kasse.zeitraum)

    assert antwort.status_code == 302
    position = SessionAllowance.objects.get()
    assert position.status == "cancelled" and position.export_reference == ""


def test_abrechnungslauf_storniert_und_nimmt_wieder_auf(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    kaemmerei = client(kasse.kaemmerei)
    kaemmerei.post(f"{kasse.base}/generate/", kasse.zeitraum)

    anwesenheit.status = "excused"
    anwesenheit.save()
    kaemmerei.post(f"{kasse.base}/generate/", kasse.zeitraum)
    position = SessionAllowance.objects.get(attendance=anwesenheit)
    assert position.status == "cancelled"

    # Korrektur zurück: Die Position lebt als „ausstehend“ wieder auf und muss erneut genehmigt werden
    anwesenheit.status = "joined_late"
    anwesenheit.save()
    stats = allowance_service.generate_allowances(kasse.tenant, kasse.tag, kasse.tag, created_by=kasse.pruefer)
    position.refresh_from_db()
    assert stats["reactivated"] == 1 and stats["created"] == 0
    assert position.status == "pending" and position.notes == ""
    assert position.created_by_id == kasse.pruefer.pk
    assert SessionAllowance.objects.count() == 1


def test_wiederaufnahme_uebernimmt_die_waehrung_des_gremiums(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    allowance_service.generate_allowances(kasse.tenant, kasse.tag, kasse.tag, created_by=kasse.kaemmerei)
    anwesenheit.status = "absent"
    anwesenheit.save()
    allowance_service.generate_allowances(kasse.tenant, kasse.tag, kasse.tag, created_by=kasse.kaemmerei)

    kasse.rat.allowance_currency = "CHF"
    kasse.rat.save()
    anwesenheit.status = "present"
    anwesenheit.save()
    stats = allowance_service.generate_allowances(kasse.tenant, kasse.tag, kasse.tag, created_by=kasse.kaemmerei)

    position = SessionAllowance.objects.get(attendance=anwesenheit)
    assert stats["reactivated"] == 1
    assert position.status == "pending" and position.currency == "CHF"


def test_csv_finanzverfahren_ohne_positionen_ohne_grundlage(kasse: Kasse) -> None:
    sitzung = kasse.sitzung()
    korrigiert = kasse.anwesend(sitzung, "Amsel")
    kasse.anwesend(sitzung, "Buchfink")
    kaemmerei = client(kasse.kaemmerei)
    kaemmerei.post(f"{kasse.base}/generate/", kasse.zeitraum)
    client(kasse.pruefer).post(f"{kasse.base}/approve/", kasse.zeitraum)
    assert set(SessionAllowance.objects.values_list("status", flat=True)) == {"approved"}

    # Anwesenheit nach der Genehmigung korrigiert: Die Datei fürs Finanzverfahren enthält die Position nicht mehr
    korrigiert.status = "absent"
    korrigiert.save()
    for abfrage in (kasse.zeitraum, {**kasse.zeitraum, "status": "approved"}):
        datei = kaemmerei.get(f"{kasse.base}/export.csv", abfrage).content.decode("utf-8-sig")
        assert "Amsel" not in datei and "Buchfink" in datei

    # Der Abruf der Datei storniert nichts; das bleibt Lauf, Genehmigung und SEPA-Export vorbehalten
    assert SessionAllowance.objects.get(attendance=korrigiert).status == "approved"


def test_von_hand_stornieren_und_es_bleibt_dabei(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    kaemmerei = client(kasse.kaemmerei)
    kaemmerei.post(f"{kasse.base}/generate/", kasse.zeitraum)
    position = SessionAllowance.objects.get(attendance=anwesenheit)

    seite = kaemmerei.get(f"{kasse.base}/", kasse.zeitraum).content.decode()
    assert f"{kasse.base}/{position.pk}/cancel/" in seite

    antwort = kaemmerei.post(f"{kasse.base}/{position.pk}/cancel/", kasse.zeitraum)
    assert antwort.status_code == 302 and kasse.zeitraum["from"] in antwort["Location"]
    position.refresh_from_db()
    assert position.status == "cancelled" and position.notes == allowance_service.MANUAL_CANCEL_NOTE

    # Der nächste Lauf legt sie nicht neu an und nimmt sie nicht wieder auf
    kaemmerei.post(f"{kasse.base}/generate/", kasse.zeitraum)
    position.refresh_from_db()
    assert position.status == "cancelled" and SessionAllowance.objects.count() == 1
    # Stornierte Positionen zählen nicht zur Summe und stehen nicht in der Datei fürs Finanzverfahren
    uebersicht = kaemmerei.get(f"{kasse.base}/", kasse.zeitraum)
    assert uebersicht.context["total_amount"] == Decimal("0.00")
    assert "Amsel" not in kaemmerei.get(f"{kasse.base}/export.csv", kasse.zeitraum).content.decode()


def test_ausgezahlte_und_fremde_positionen_lassen_sich_nicht_stornieren(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    position = SessionAllowance.objects.create(
        attendance=anwesenheit, amount=Decimal("35.00"), status="paid", export_reference="SG-2026-0001"
    )
    antwort = client(kasse.kaemmerei).post(f"{kasse.base}/{position.pk}/cancel/")
    position.refresh_from_db()
    assert position.status == "paid"
    assert any("lassen sich stornieren" in text for text in meldungen(antwort))

    fremd = SessionTenant.objects.create(name="Fremd", slug="fremd")
    fremd_konto = konto(fremd, "fremd", "manage_allowances")
    assert client(fremd_konto).post(f"/session/fremd/allowances/{position.pk}/cancel/").status_code == 404


# =============================================================================
# Sitzungsgeld: Eingaben prüfen
# =============================================================================


@pytest.mark.parametrize(
    "pfad",
    [
        "/?organization=abc",
        "/export.csv?organization=abc",
        "/year/?year=0",
        "/year/?year=-1",
        "/year/?year=1",
        "/year/?year=10000",
        "/year/?year=99999999999",
        "/year/?year=abc",
    ],
)
def test_ungueltige_filter_ohne_serverfehler(kasse: Kasse, pfad: str) -> None:
    antwort = client(kasse.kaemmerei).get(f"{kasse.base}{pfad}")
    assert antwort.status_code == 200


def test_jahresuebersicht_csv_mit_ungueltigem_jahr_400(kasse: Kasse) -> None:
    assert client(kasse.kaemmerei).get(f"{kasse.base}/year/?year=0&format=csv").status_code == 400


@pytest.mark.parametrize("aktion", ["rates/save/", "generate/", "approve/", "export/sepa/"])
def test_ungueltiges_gremium_bei_aktionen(kasse: Kasse, aktion: str) -> None:
    kasse.anwesend(kasse.sitzung(), "Amsel")
    daten = {**kasse.zeitraum, "organization": "abc", "amount": "5"}
    antwort = client(kasse.kaemmerei).post(f"{kasse.base}/{aktion}", daten)
    assert antwort.status_code == 302
    assert any("Unbekanntes Gremium" in text for text in meldungen(antwort))
    assert not SessionAllowance.objects.exists() and not SessionAllowanceRate.objects.exists()


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("30,50", Decimal("30.50")), ("1.234,56", Decimal("1234.56")), ("0", Decimal("0.00")), ("17.5", Decimal("17.50"))],
)
def test_entschaedigungssatz_gueltig(kasse: Kasse, eingabe: str, erwartet: Decimal) -> None:
    client(kasse.kaemmerei).post(
        f"{kasse.base}/rates/save/", {"organization": str(kasse.rat.pk), "role": "member", "amount": eingabe}
    )
    assert SessionAllowanceRate.objects.get().amount == erwartet


@pytest.mark.parametrize(
    "eingabe", ["NaN", "Infinity", "-Infinity", "sNaN", "1e10", "123456789", "12,345", "0,001", "-5", "", "abc"]
)
def test_entschaedigungssatz_ungueltig(kasse: Kasse, eingabe: str) -> None:
    antwort = client(kasse.kaemmerei).post(
        f"{kasse.base}/rates/save/", {"organization": str(kasse.rat.pk), "role": "member", "amount": eingabe}
    )
    assert antwort.status_code == 302
    assert not SessionAllowanceRate.objects.exists()
    assert any("Ungültiger Betrag" in text for text in meldungen(antwort))
    # Die Übersicht bleibt erreichbar
    assert client(kasse.kaemmerei).get(f"{kasse.base}/").status_code == 200


def test_auftraggeberkonto_prueft_iban_und_bic(kasse: Kasse) -> None:
    kaemmerei = client(kasse.kaemmerei)
    for iban, bic in (("xx<>&", ""), ("DE89370400440532013001", ""), (IBAN, "?"), (IBAN, "COBADE")):
        antwort = kaemmerei.post(
            f"{kasse.base}/debtor/save/", {"debtor_name": "Stadt", "debtor_iban": iban, "debtor_bic": bic}
        )
        assert any("ungültig" in text for text in meldungen(antwort)), (iban, bic)
        kasse.tenant.refresh_from_db()
        assert "allowances" not in (kasse.tenant.settings or {})

    kaemmerei.post(
        f"{kasse.base}/debtor/save/",
        {"debtor_name": "Stadt", "debtor_iban": "de89 3704 0044 0532 0130 00", "debtor_bic": "cobadeffxxx"},
    )
    kasse.tenant.refresh_from_db()
    assert kasse.tenant.settings["allowances"] == {"debtor_name": "Stadt", "debtor_iban": IBAN, "debtor_bic": BIC}


def test_sepa_export_lehnt_gespeicherte_ungueltige_iban_ab(kasse: Kasse) -> None:
    kasse.anwesend(kasse.sitzung(), "Amsel")
    kasse.tenant.settings = {"allowances": {"debtor_name": "Stadt", "debtor_iban": "XX<>&"}}
    kasse.tenant.save()
    client(kasse.kaemmerei).post(f"{kasse.base}/generate/", kasse.zeitraum)
    client(kasse.pruefer).post(f"{kasse.base}/approve/", kasse.zeitraum)

    antwort = client(kasse.pruefer).post(f"{kasse.base}/export/sepa/", kasse.zeitraum)

    assert antwort.status_code == 302
    assert SessionAllowance.objects.get().status == "approved"
    assert any("ungültig" in text for text in meldungen(antwort))


def test_jahresuebersicht_verlinkt_personen_nur_mit_sitzungsrecht(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("35.00"))
    pfad = f"{kasse.base}/year/?year={kasse.tag.year}"
    personenlink = f"/session/{kasse.tenant.slug}/persons/{anwesenheit.person_id}/"

    ohne = client(kasse.kaemmerei).get(pfad).content.decode()
    assert "Amsel" in ohne and personenlink not in ohne

    mit = konto(kasse.tenant, "mit", "view_dashboard", "view_meetings", "manage_allowances")
    assert personenlink in client(mit).get(pfad).content.decode()


# =============================================================================
# Monatspauschalen
# =============================================================================


@pytest.fixture
def pauschalen(kasse: Kasse) -> tuple[Kasse, SessionMonthlyRate, SessionPerson]:
    rate = SessionMonthlyRate.objects.create(tenant=kasse.tenant, name="Fraktionsvorsitz", amount=Decimal("250.00"))
    person = SessionPerson.objects.create(tenant=kasse.tenant, given_name="P", family_name="Birke")
    return kasse, rate, person


def test_katalog_aendern_und_deaktivieren(pauschalen: tuple[Kasse, SessionMonthlyRate, SessionPerson]) -> None:
    kasse, rate, _person = pauschalen
    kaemmerei = client(kasse.kaemmerei)
    seite = kaemmerei.get(f"{kasse.base}/monthly/").content.decode()
    assert f'name="rate_id" value="{rate.pk}"' in seite and 'name="is_active"' in seite

    # Jährliche Anpassung: derselbe Eintrag, neuer Betrag, aktiv
    kaemmerei.post(
        f"{kasse.base}/monthly/rate/",
        {"rate_id": str(rate.pk), "name": "Fraktionsvorsitz", "amount": "260,00", "is_active": "1"},
    )
    rate.refresh_from_db()
    assert (rate.amount, rate.is_active) == (Decimal("260.00"), True)
    assert SessionMonthlyRate.objects.count() == 1

    # Abgewähltes Ankreuzfeld deaktiviert
    kaemmerei.post(
        f"{kasse.base}/monthly/rate/", {"rate_id": str(rate.pk), "name": "Fraktionsvorsitz", "amount": "260,00"}
    )
    rate.refresh_from_db()
    assert rate.is_active is False


def test_keine_zweite_aktive_pauschale_gleichen_namens(
    pauschalen: tuple[Kasse, SessionMonthlyRate, SessionPerson],
) -> None:
    kasse, _rate, _person = pauschalen
    antwort = client(kasse.kaemmerei).post(f"{kasse.base}/monthly/rate/", {"name": "fraktionsvorsitz", "amount": "260"})
    assert SessionMonthlyRate.objects.count() == 1
    assert any("gibt es bereits" in text for text in meldungen(antwort))


def test_geloescht_vermerkt_nur_was_geloescht_wurde(
    pauschalen: tuple[Kasse, SessionMonthlyRate, SessionPerson],
) -> None:
    kasse, rate, person = pauschalen
    SessionMonthlyAllowance.objects.create(
        tenant=kasse.tenant, person=person, rate=rate, period=date(2026, 1, 1), amount=rate.amount, status="paid"
    )
    vorher = SessionAuditLog.objects.filter(tenant=kasse.tenant, action="delete").count()

    antwort = client(kasse.kaemmerei).post(f"{kasse.base}/monthly/rate/delete/", {"rate_id": str(rate.pk)})

    assert SessionMonthlyRate.objects.filter(pk=rate.pk).exists()
    assert any("deaktivieren" in text for text in meldungen(antwort))
    assert SessionAuditLog.objects.filter(tenant=kasse.tenant, action="delete").count() == vorher


@pytest.mark.parametrize(
    "daten",
    [
        {"year": "2025", "month": "13"},
        {"year": "2025", "month": "0"},
        {"year": "99999999999", "month": "1"},
        {"year": "2025", "month": "99999999999999999999"},
        {"year": "abc", "month": "1"},
    ],
)
@pytest.mark.parametrize("aktion", ["generate/", "approve/", "export/csv/", "export/sepa/"])
def test_ungueltiger_monat_fuehrt_nichts_aus(
    pauschalen: tuple[Kasse, SessionMonthlyRate, SessionPerson], aktion: str, daten: dict[str, str]
) -> None:
    kasse, rate, person = pauschalen
    from apps.session.models import SessionPersonMonthlyRate

    SessionPersonMonthlyRate.objects.create(person=person, rate=rate)
    antwort = client(kasse.kaemmerei).post(f"{kasse.base}/monthly/{aktion}", daten)

    assert antwort.status_code == 302
    assert not SessionMonthlyAllowance.objects.exists()
    assert any("Ungültiger Abrechnungsmonat" in text for text in meldungen(antwort))


@pytest.mark.parametrize(
    "query", ["?year=99999999999&month=1", "?year=2025&month=13", "?year=2025&month=99999999999999999999"]
)
def test_monatsseite_ungueltiger_monat_ohne_serverfehler(kasse: Kasse, query: str) -> None:
    antwort = client(kasse.kaemmerei).get(f"{kasse.base}/monthly/{query}")
    assert antwort.status_code == 200
    assert any("Ungültiger Abrechnungsmonat" in text for text in meldungen(antwort))


def test_export_hinweis_behaelt_den_monat(kasse: Kasse) -> None:
    kaemmerei = client(kasse.kaemmerei)
    for aktion in ("export/csv/", "export/sepa/"):
        antwort = kaemmerei.post(f"{kasse.base}/monthly/{aktion}", {"year": "2025", "month": "3"})
        assert antwort.status_code == 302
        assert antwort["Location"].endswith("/allowances/monthly/?year=2025&month=3"), aktion


def test_monatsposten_stornieren(pauschalen: tuple[Kasse, SessionMonthlyRate, SessionPerson]) -> None:
    kasse, rate, person = pauschalen
    from apps.session.models import SessionPersonMonthlyRate

    SessionPersonMonthlyRate.objects.create(person=person, rate=rate)
    kaemmerei = client(kasse.kaemmerei)
    monat = {"year": "2026", "month": "2"}
    kaemmerei.post(f"{kasse.base}/monthly/generate/", monat)
    posten = SessionMonthlyAllowance.objects.get()
    assert "monthly/cancel/" in kaemmerei.get(f"{kasse.base}/monthly/", monat).content.decode()

    antwort = kaemmerei.post(f"{kasse.base}/monthly/cancel/", {"allowance_id": str(posten.pk), **monat})
    assert antwort["Location"].endswith("?year=2026&month=2")
    posten.refresh_from_db()
    assert posten.status == "cancelled"
    # Der Monatslauf legt ihn nicht neu an
    kaemmerei.post(f"{kasse.base}/monthly/generate/", monat)
    assert SessionMonthlyAllowance.objects.get().status == "cancelled"

    # Ausgezahlte Posten bleiben unverändert
    SessionMonthlyAllowance.objects.filter(pk=posten.pk).update(status="paid")
    kaemmerei.post(f"{kasse.base}/monthly/cancel/", {"allowance_id": str(posten.pk), **monat})
    assert SessionMonthlyAllowance.objects.get().status == "paid"


# =============================================================================
# Berichte
# =============================================================================


def test_durchlaufzeit_bis_zur_freigabe_im_jahr_der_freigabe(kasse: Kasse) -> None:
    vorlage = SessionPaper.objects.create(tenant=kasse.tenant, name="Haushalt", status="approved")
    SessionPaper.objects.filter(pk=vorlage.pk).update(
        created_at=datetime(2025, 1, 10, 9, 0, tzinfo=BERLIN), approved_at=datetime(2025, 1, 15, 9, 0, tzinfo=BERLIN)
    )
    vorlage.refresh_from_db()
    vorlage.status = "completed"
    vorlage.save()  # spätere Änderung setzt updated_at auf heute

    assert report_service.paper_throughput(kasse.tenant, 2025) == {"count": 1, "median_days": 5, "max_days": 5}
    assert report_service.paper_throughput(kasse.tenant, timezone.localdate().year)["count"] == 0


def test_bericht_csv_sitzungsgeld_mit_dezimalkomma(kasse: Kasse) -> None:
    anwesenheit = kasse.anwesend(kasse.sitzung(), "Amsel")
    SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("35.00"), status="paid")
    bericht = konto(kasse.tenant, "bericht", "view_dashboard", "view_meetings", "manage_allowances")

    antwort = client(bericht).get(
        f"/session/{kasse.tenant.slug}/reports/export.csv?type=allowances&year={kasse.tag.year}"
    )

    zeilen = list(csv.reader(io.StringIO(antwort.content.decode("utf-8-sig")), delimiter=";"))
    assert zeilen[1] == ["P Amsel", "1", "0,00", "35,00", "35,00"]
    assert zeilen[2] == ["Gesamt", "1", "0,00", "35,00", "35,00"]


# =============================================================================
# Wahlperiode, Archiv und Suche
# =============================================================================


@pytest.fixture
def periode(kasse: Kasse) -> SessionLegislativeTerm:
    return SessionLegislativeTerm.objects.create(tenant=kasse.tenant, name="22. WP", start_date=date(2024, 6, 9))


def test_serienplanung_setzt_die_wahlperiode(kasse: Kasse, periode: SessionLegislativeTerm) -> None:
    planung = konto(kasse.tenant, "planung", "view_dashboard", "view_meetings", "edit_meetings")
    antwort = client(planung).post(
        f"/session/{kasse.tenant.slug}/meetings/plan/",
        {
            "organization": str(kasse.rat.pk),
            "rhythm": "weekly",
            "weekday": "2",
            "time": "18:00",
            "date_from": "2026-11-01",
            "date_to": "2026-11-30",
            "action": "create",
        },
    )
    assert antwort.status_code == 302
    sitzungen = SessionMeeting.objects.filter(tenant=kasse.tenant)
    assert sitzungen.count() == 4
    assert set(sitzungen.values_list("legislative_term", flat=True)) == {periode.pk}


def test_archiv_und_sitzungsliste_zaehlen_sitzungen_ohne_zuordnung_nach_datum(
    kasse: Kasse, periode: SessionLegislativeTerm
) -> None:
    alt = SessionLegislativeTerm.objects.create(
        tenant=kasse.tenant, name="21. WP", start_date=date(2019, 6, 1), end_date=date(2024, 6, 8)
    )
    sitzung = kasse.sitzung(date(2025, 3, 4))
    altsitzung = kasse.sitzung(date(2020, 3, 4))
    SessionMeeting.objects.filter(pk__in=[sitzung.pk, altsitzung.pk]).update(legislative_term=None)
    leser = client(konto(kasse.tenant, "leser", "view_dashboard", "view_meetings", "view_papers"))

    archiv = leser.get(f"/session/{kasse.tenant.slug}/archive/")
    zahlen = {row["term"].pk: row["meeting_count"] for row in archiv.context["rows"]}
    assert zahlen == {periode.pk: 1, alt.pk: 1}
    liste = leser.get(f"/session/{kasse.tenant.slug}/meetings/?term={periode.pk}")
    assert [m.pk for m in liste.context["meetings"]] == [sitzung.pk]


def test_archiv_ohne_vorlagenrecht_ohne_vorlagenkachel(kasse: Kasse, periode: SessionLegislativeTerm) -> None:
    SessionPaper.objects.create(tenant=kasse.tenant, name="Vorlage", date=date(2025, 1, 1), is_public=True)
    ohne = client(konto(kasse.tenant, "ohne", "view_dashboard", "view_meetings"))
    seite = ohne.get(f"/session/{kasse.tenant.slug}/archive/").content.decode()
    assert f"/papers/?term={periode.pk}" not in seite and "Vorlagen</div>" not in seite

    mit = client(konto(kasse.tenant, "mit", "view_dashboard", "view_meetings", "view_papers"))
    assert f"/papers/?term={periode.pk}" in mit.get(f"/session/{kasse.tenant.slug}/archive/").content.decode()


def test_suche_beachtet_wahlperiode_und_gremium(kasse: Kasse, periode: SessionLegislativeTerm) -> None:
    SessionPaper.objects.create(tenant=kasse.tenant, name="FJ-Altvorlage", date=date(2019, 5, 1), is_public=True)
    SessionPaper.objects.create(tenant=kasse.tenant, name="FJ-Neuvorlage", date=date(2025, 5, 1), is_public=True)
    ausschuss = SessionOrganization.objects.create(tenant=kasse.tenant, name="Bauausschuss")
    SessionApplication.objects.create(
        tenant=kasse.tenant,
        title="FJ-Antrag Rat",
        submitter_name="Fraktion",
        submitter_email="f@example.org",
        target_organization=kasse.rat,
    )
    leser = client(konto(kasse.tenant, "leser", "view_dashboard", "view_papers", "view_applications"))
    suche = f"/session/{kasse.tenant.slug}/search/"

    vorlagen = leser.get(suche, {"q": "FJ-", "term": str(periode.pk), "kind": "papers"}).context["results"]["papers"]
    assert [p.name for p in vorlagen] == ["FJ-Neuvorlage"]
    antraege = leser.get(suche, {"q": "FJ-Antrag", "organization": str(ausschuss.pk)}).context["results"]
    assert antraege["applications"] == []
    antraege = leser.get(suche, {"q": "FJ-Antrag", "organization": str(kasse.rat.pk)}).context["results"]
    assert [a.title for a in antraege["applications"]] == ["FJ-Antrag Rat"]


# =============================================================================
# Dashboard, Leitstelle, API-Übersicht
# =============================================================================


def test_dashboard_rechnet_heute_in_der_zeitzone_der_kommune(kasse: Kasse) -> None:
    sitzung = kasse.sitzung(date(2026, 10, 1), is_public=True)
    leser = client(konto(kasse.tenant, "leser", "view_dashboard", "view_meetings"))
    # 02.10.2026, 00:30 in Berlin = 01.10.2026, 22:30 UTC: Die Sitzung vom 01.10. ist vorbei
    with mock.patch("django.utils.timezone.now", return_value=datetime(2026, 10, 1, 22, 30, tzinfo=UTC)):
        antwort = leser.get(f"/session/{kasse.tenant.slug}/")
    assert sitzung not in list(antwort.context["upcoming_meetings"])


def test_leitstelle_zeigt_verstrichene_pruefungsfrist_als_ueberfaellig(kasse: Kasse) -> None:
    gruppe = SessionTenantGroup.objects.create(name="Bezirke", slug="bezirke")
    SessionTenantGroupTenant.objects.create(group=gruppe, tenant=kasse.tenant)
    user = cast(Any, UserFactory)(email="leitstelle@example.org")
    membership = SessionTenantGroupMembership.objects.create(group=gruppe, user=user)
    SessionPaper.objects.create(
        tenant=kasse.tenant,
        name="Feuerwehrbedarfsplan",
        status="review",
        is_public=True,
        deadline=timezone.localdate() - timedelta(days=30),
    )
    overview = leitstelle_service.build_overview(membership, user)
    assert [(row.title, row.overdue) for row in overview.review_papers] == [("Feuerwehrbedarfsplan", True)]


@pytest.mark.parametrize("pfad", ["/session/{slug}/api/", "/api/v1/session/{slug}/"])
def test_api_uebersicht_nennt_oparl_erst_nach_freischaltung(kasse: Kasse, pfad: str) -> None:
    from apps.session.services import oparl_access

    mitglied = client(konto(kasse.tenant, "mitglied", "view_dashboard", "view_meetings"))
    assert mitglied.get(pfad.format(slug=kasse.tenant.slug)).json()["oparl"] is None

    oparl_access.release(kasse.tenant)
    link = mitglied.get(pfad.format(slug=kasse.tenant.slug)).json()["oparl"]
    assert link.endswith(f"/session/{kasse.tenant.slug}/api/oparl/")
    assert mitglied.get(link).status_code == 200
