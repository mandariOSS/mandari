# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auszahlungen, Vertretung, Mitzeichnung und Sicht auf Nichtöffentliches im Sitzungsdienst.

- Die Genehmigung einer Auszahlung (Sitzungsgeld, Monatspauschale, Endgeräte-Zuschuss) gilt auch dem Empfängerkonto:
  Ändert es sich danach, steht die Auszahlung wieder auf „Ausstehend“, und wer das Konto geändert hat, genehmigt sie
  nicht selbst.
- Genehmigen, Stornieren und Auszahlen prüfen den gespeicherten Stand, nicht den vom Beginn der Anfrage.
- Eine Vertretung wirkt nur, solange das Konto der vertretenen Person besteht, und nie über deren Sicht hinaus;
  die eigene Amts-Zuordnung nimmt nur ein Administrator vor.
- Vorlegen schreibt Status und Mitzeichnungskette zusammen fest.
- Die Beratungsfolge nennt von einer nichtöffentlichen Beratung ohne NÖ-Sichtrecht weder Gremium noch Ergebnis,
  die Suche findet Protokolltexte nur mit dem Recht für Niederschriften.
- Mit der Deaktivierung eines Zugangs verfallen die offenen Einladungen an die Person und von ihr; die Meldung
  nennt sie. Wer eine Einladung erneut sendet, gilt danach als einladende Person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAttendance,
    SessionAuditLog,
    SessionConsultation,
    SessionCosignature,
    SessionDelegation,
    SessionDeviceGrant,
    SessionInvitation,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.permissions import SessionPermissionChecker
from apps.session.services import allowance_service, cosign_service, delegation_service
from apps.session.tests._niederschrift import client, nutzer, welt

pytestmark = pytest.mark.django_db

HEUTE = timezone.localdate()
IBAN_ALT = "DE02120300000000202051"
IBAN_NEU = "DE89370400440532013000"


def _tenant(slug: str = "kasse", **felder: Any) -> SessionTenant:
    return SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug, **felder)


def _meldungen(antwort: Any) -> str:
    return " ".join(str(m) for m in get_messages(antwort.wsgi_request))


def _frisch(session_user: SessionUser) -> SessionUser:
    """Neu geladen, ohne zwischengespeicherte Vertretungen."""
    return SessionUser.objects.select_related("tenant", "user").get(pk=session_user.pk)


def _admin(tenant: SessionTenant, name: str) -> SessionUser:
    rolle = SessionRole.objects.create(tenant=tenant, name=f"admin-{name}", is_admin=True)
    user = cast(Any, UserFactory)(email=f"{name}@{tenant.slug}.example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(rolle)
    return session_user


def _vertretung(principal: SessionUser, deputy: SessionUser, **felder: Any) -> SessionDelegation:
    return SessionDelegation.objects.create(
        tenant=principal.tenant,
        principal=principal,
        deputy=deputy,
        start_date=HEUTE,
        end_date=HEUTE + timedelta(days=7),
        **felder,
    )


# =============================================================================
# Empfängerkonto und Genehmigung
# =============================================================================


@dataclass
class Kasse:
    tenant: SessionTenant
    gremium: SessionOrganization
    empfaenger: SessionPerson
    lauf: SessionUser
    pruefung: SessionUser
    stamm: SessionUser

    def sitzungsgeld(self, tag: int = 1, **felder: Any) -> SessionAllowance:
        sitzung = SessionMeeting.objects.create(
            tenant=self.tenant,
            name=f"Rat {tag}",
            organization=self.gremium,
            start=timezone.now() - timedelta(days=tag),
        )
        anwesenheit = SessionAttendance.objects.create(meeting=sitzung, person=self.empfaenger, status="present")
        felder.setdefault("status", "approved")
        felder.setdefault("created_by", self.lauf)
        if felder["status"] != "pending":
            felder.setdefault("approved_by", self.pruefung)
            felder.setdefault("approved_at", timezone.now())
        return SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("25.00"), **felder)

    def pauschale(self, monat: int = 1, **felder: Any) -> SessionMonthlyAllowance:
        rate, _ = SessionMonthlyRate.objects.get_or_create(
            tenant=self.tenant, name="Teilpauschale", defaults={"amount": Decimal("322.50")}
        )
        felder.setdefault("status", "approved")
        felder.setdefault("created_by", self.lauf)
        if felder["status"] != "pending":
            felder.setdefault("approved_by", self.pruefung)
            felder.setdefault("approved_at", timezone.now())
        return SessionMonthlyAllowance.objects.create(
            tenant=self.tenant,
            person=self.empfaenger,
            rate=rate,
            period=date(HEUTE.year, monat, 1),
            amount=Decimal("322.50"),
            **felder,
        )

    def zuschuss(self, **felder: Any) -> SessionDeviceGrant:
        felder.setdefault("status", "approved")
        felder.setdefault("created_by", self.lauf)
        if felder["status"] != "pending":
            felder.setdefault("approved_by", self.pruefung)
            felder.setdefault("approved_at", timezone.now())
        return SessionDeviceGrant.objects.create(
            tenant=self.tenant, person=self.empfaenger, amount=Decimal("400.00"), **felder
        )

    def konto_aendern(self, wer: SessionUser, iban: str = IBAN_NEU) -> Any:
        url = reverse("session:person_edit", kwargs={"tenant_slug": self.tenant.slug, "person_id": self.empfaenger.pk})
        daten = {
            "given_name": self.empfaenger.given_name,
            "family_name": self.empfaenger.family_name,
            "delivery_channel": "email",
            "is_active": "on",
            "start_date": "",
            "end_date": "",
            "bank_iban": iban,
        }
        return client(wer).post(url, daten)


@pytest.fixture
def kasse() -> Kasse:
    tenant = _tenant()
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    empfaenger = SessionPerson(tenant=tenant, given_name="Anna", family_name="Amberg")
    cast(Any, empfaenger).set_bank_iban_encrypted(IBAN_ALT)
    empfaenger.save()
    return Kasse(
        tenant=tenant,
        gremium=gremium,
        empfaenger=empfaenger,
        lauf=nutzer(tenant, "lauf", "manage_allowances"),
        pruefung=nutzer(tenant, "pruefung", "manage_allowances", "manage_devices"),
        stamm=nutzer(tenant, "stamm", "manage_allowances", "manage_organizations", "manage_devices"),
    )


def test_kontoaenderung_laesst_genehmigungen_offener_auszahlungen_verfallen(kasse: Kasse) -> None:
    position = kasse.sitzungsgeld()
    pauschale = kasse.pauschale()
    zuschuss = kasse.zuschuss()
    ausgezahlt = kasse.sitzungsgeld(tag=2, status="paid", export_reference="SG-2026-0001")
    ausstehend = kasse.pauschale(monat=2, status="pending")

    antwort = kasse.konto_aendern(kasse.stamm)

    assert antwort.status_code == 302
    assert allowance_service.bank_account(SessionPerson.objects.get(pk=kasse.empfaenger.pk))[1] == IBAN_NEU
    for auszahlung in (position, pauschale, zuschuss):
        auszahlung.refresh_from_db()
        assert auszahlung.status == "pending"
        assert auszahlung.approved_by_id is None and auszahlung.approved_at is None
        # Wer das Konto geändert hat, hat die Auszahlung neu festgesetzt und genehmigt sie nicht selbst
        assert auszahlung.created_by_id == kasse.stamm.pk
    ausgezahlt.refresh_from_db()
    ausstehend.refresh_from_db()
    assert ausgezahlt.status == "paid" and ausgezahlt.created_by_id == kasse.lauf.pk
    assert ausstehend.status == "pending" and ausstehend.created_by_id == kasse.lauf.pk
    assert "3 bereits genehmigte Auszahlung(en)" in _meldungen(antwort)
    # Der Verfall der Genehmigung eines Zuschusses steht im Protokoll (Zuschüsse laufen nicht über das Signal),
    # mit der Person, die ihn bisher erfasst hatte
    eintrag = SessionAuditLog.objects.get(object_id=zuschuss.pk, changes__grund="Empfängerkonto geändert")
    assert eintrag.changes["erfasst_von_bisher"] == kasse.lauf.user.email


def test_nach_kontoaenderung_kein_export_ohne_neue_genehmigung_durch_eine_andere_person(kasse: Kasse) -> None:
    position = kasse.sitzungsgeld()
    pauschale = kasse.pauschale()
    kasse.konto_aendern(kasse.stamm)

    # Der Export findet nichts Genehmigtes mehr: Das neue Konto steht in keiner Überweisungsdatei
    export = allowance_service.export_sepa(
        kasse.tenant,
        SessionAllowance.objects.filter(pk=position.pk),
        kind=allowance_service.KIND_SESSION,
        debtor_name="Stadt Kasse",
        debtor_iban="DE02100100100006820101",
    )
    assert export.exported == 0 and export.reference == "" and export.xml == b""

    # Die ändernde Person genehmigt nicht selbst, eine andere schon – für Sitzungsgeld wie Pauschale
    selbst = allowance_service.approve_allowances([SessionAllowance.objects.get(pk=position.pk)], kasse.stamm)
    assert selbst == {"approved": 0, "blocked_four_eyes": 1}
    selbst = allowance_service.approve_monthly_allowances(
        [SessionMonthlyAllowance.objects.get(pk=pauschale.pk)], kasse.stamm
    )
    assert selbst == {"approved": 0, "blocked_four_eyes": 1}
    andere = allowance_service.approve_allowances([SessionAllowance.objects.get(pk=position.pk)], kasse.pruefung)
    assert andere == {"approved": 1, "blocked_four_eyes": 0}
    position.refresh_from_db()
    assert position.status == "approved" and position.approved_by_id == kasse.pruefung.pk


def test_zuschuss_nach_kontoaenderung_nicht_von_der_aendernden_person_genehmigt(kasse: Kasse) -> None:
    zuschuss = kasse.zuschuss()
    kasse.konto_aendern(kasse.stamm)
    url = reverse(
        "session:device_grant_action",
        kwargs={"tenant_slug": kasse.tenant.slug, "grant_id": zuschuss.pk, "action": "approve"},
    )

    selbst = client(kasse.stamm).post(url)
    zuschuss.refresh_from_db()
    assert zuschuss.status == "pending" and "andere Person" in _meldungen(selbst)
    # Die Meldung nennt auch den Grund, der hier zutrifft
    assert "das Empfängerkonto geändert hat" in _meldungen(selbst)

    client(kasse.pruefung).post(url)
    zuschuss.refresh_from_db()
    assert zuschuss.status == "approved" and zuschuss.approved_by_id == kasse.pruefung.pk


def test_meldungen_nennen_die_kontoaenderung_als_grund_der_sperre(kasse: Kasse) -> None:
    kasse.sitzungsgeld()
    kasse.pauschale()
    kasse.konto_aendern(kasse.stamm)
    slug = {"tenant_slug": kasse.tenant.slug}

    sitzungsgeld = client(kasse.stamm).post(
        reverse("session:allowance_approve", kwargs=slug),
        {"from": (HEUTE - timedelta(days=7)).isoformat(), "to": HEUTE.isoformat()},
    )
    pauschalen = client(kasse.stamm).post(
        reverse("session:monthly_approve", kwargs=slug), {"year": str(HEUTE.year), "month": "1"}
    )

    # Den Abrechnungs- oder Monatslauf hat hier jemand anderes erzeugt: Gesperrt ist, wer das Konto geändert hat
    assert "1 Position(en) nicht genehmigt" in _meldungen(sitzungsgeld)
    assert "das Empfängerkonto geändert" in _meldungen(sitzungsgeld)
    assert "1 Posten nicht genehmigt" in _meldungen(pauschalen)
    assert "das Empfängerkonto geändert" in _meldungen(pauschalen)


def test_speichern_ohne_kontoaenderung_laesst_genehmigungen_bestehen(kasse: Kasse) -> None:
    position = kasse.sitzungsgeld()
    zuschuss = kasse.zuschuss()

    antwort = kasse.konto_aendern(kasse.stamm, iban="DE02 1203 0000 0000 2020 51")

    position.refresh_from_db()
    zuschuss.refresh_from_db()
    assert position.status == "approved" and position.created_by_id == kasse.lauf.pk
    assert zuschuss.status == "approved"
    assert "genehmigte Auszahlung" not in _meldungen(antwort)


def test_kontoaenderung_ohne_handelnde_person_setzt_nur_die_genehmigung_zurueck(kasse: Kasse) -> None:
    """Import ohne angemeldete Person: Die Genehmigung verfällt, festgesetzt hat weiter der Abrechnungslauf."""
    position = kasse.sitzungsgeld()

    assert allowance_service.reset_approvals_after_account_change(kasse.empfaenger) == 1

    position.refresh_from_db()
    assert position.status == "pending" and position.created_by_id == kasse.lauf.pk


# =============================================================================
# Statuswechsel am gespeicherten Stand
# =============================================================================


@pytest.mark.parametrize("art", ["sitzungsgeld", "pauschale"])
def test_genehmigung_aus_ueberholtem_stand_hebt_keine_stornierung_auf(kasse: Kasse, art: str) -> None:
    position: Any = kasse.sitzungsgeld(status="pending") if art == "sitzungsgeld" else kasse.pauschale(status="pending")
    modell = type(position)
    gelesen = modell.objects.get(pk=position.pk)  # Stand vom Beginn der Genehmigung
    assert allowance_service.cancel_allowances([position], note=allowance_service.MANUAL_CANCEL_NOTE) == 1

    genehmigen = (
        allowance_service.approve_allowances if art == "sitzungsgeld" else allowance_service.approve_monthly_allowances
    )
    ergebnis = genehmigen([gelesen], kasse.pruefung)

    assert ergebnis == {"approved": 0, "blocked_four_eyes": 0}
    assert modell.objects.get(pk=position.pk).status == "cancelled"


@pytest.mark.parametrize("art", ["sitzungsgeld", "pauschale"])
def test_stornierung_und_genehmigung_aus_ueberholtem_stand_lassen_eine_auszahlung_stehen(
    kasse: Kasse, art: str
) -> None:
    position: Any = kasse.sitzungsgeld() if art == "sitzungsgeld" else kasse.pauschale()
    modell = type(position)
    gelesen = modell.objects.get(pk=position.pk)  # genehmigt, noch nicht exportiert
    gelesen_ausstehend = modell.objects.get(pk=position.pk)
    gelesen_ausstehend.status = "pending"  # Stand einer Genehmigung, die noch „ausstehend“ gelesen hat
    markieren = allowance_service.mark_exported if art == "sitzungsgeld" else allowance_service.mark_monthly_exported
    markieren([modell.objects.get(pk=position.pk)], "SG-2026-0007")

    assert allowance_service.cancel_allowances([gelesen], note=allowance_service.MANUAL_CANCEL_NOTE) == 0
    genehmigen = (
        allowance_service.approve_allowances if art == "sitzungsgeld" else allowance_service.approve_monthly_allowances
    )
    assert genehmigen([gelesen_ausstehend], kasse.pruefung)["approved"] == 0

    aktuell = modell.objects.get(pk=position.pk)
    assert aktuell.status == "paid" and aktuell.export_reference == "SG-2026-0007"


def _zweite_anfrage(monkeypatch: pytest.MonkeyPatch, **felder: Any) -> None:
    """Zwischen Lesen und Schreiben der Ansicht entscheidet eine zweite Anfrage über denselben Zuschuss."""
    from django.shortcuts import get_object_or_404

    from apps.session.views import devices

    def lesen(*args: Any, **kwargs: Any) -> Any:
        zuschuss = get_object_or_404(*args, **kwargs)
        SessionDeviceGrant.objects.filter(pk=zuschuss.pk).update(**felder)
        return zuschuss

    monkeypatch.setattr(devices, "get_object_or_404", lesen)


@pytest.mark.parametrize(
    ("ausgang", "aktion", "inzwischen"),
    [
        # Stornierung trifft auf einen gerade als ausgezahlt markierten Zuschuss
        ("approved", "cancel", "paid"),
        # Genehmigung trifft auf einen gerade stornierten Zuschuss
        ("pending", "approve", "cancelled"),
        # Auszahlung trifft auf einen gerade stornierten Zuschuss
        ("approved", "pay", "cancelled"),
    ],
)
def test_zuschuss_statuswechsel_nur_aus_dem_gespeicherten_stand(
    kasse: Kasse, monkeypatch: pytest.MonkeyPatch, ausgang: str, aktion: str, inzwischen: str
) -> None:
    zuschuss = kasse.zuschuss(status=ausgang)
    _zweite_anfrage(monkeypatch, status=inzwischen)
    url = reverse(
        "session:device_grant_action",
        kwargs={"tenant_slug": kasse.tenant.slug, "grant_id": zuschuss.pk, "action": aktion},
    )

    antwort = client(kasse.pruefung).post(url)

    zuschuss.refresh_from_db()
    assert zuschuss.status == inzwischen
    assert "inzwischen geändert" in _meldungen(antwort)


def test_zuschuss_statuswechsel_im_regelfall(kasse: Kasse) -> None:
    zuschuss = kasse.zuschuss(status="pending")
    for aktion, status in (("approve", "approved"), ("pay", "paid")):
        url = reverse(
            "session:device_grant_action",
            kwargs={"tenant_slug": kasse.tenant.slug, "grant_id": zuschuss.pk, "action": aktion},
        )
        client(kasse.pruefung).post(url)
        zuschuss.refresh_from_db()
        assert zuschuss.status == status
    assert zuschuss.approved_by_id == kasse.pruefung.pk and zuschuss.paid_at is not None


# =============================================================================
# Vertretung, Mitzeichnung, Amts-Zuordnung
# =============================================================================


def test_vertretung_endet_mit_gesperrtem_konto_der_vertretenen_person() -> None:
    tenant = _tenant("nord")
    leitung = nutzer(tenant, "leitung", "view_papers", "approve_papers")
    vertretung = nutzer(tenant, "vertretung", "view_papers")
    _vertretung(leitung, vertretung, scope_approvals=True, scope_worklist=True)
    assert "approve_papers" in SessionPermissionChecker(_frisch(vertretung)).permissions

    User.objects.filter(pk=leitung.user_id).update(is_active=False)

    deputy = _frisch(vertretung)
    assert delegation_service.incoming(deputy) == []
    assert "approve_papers" not in SessionPermissionChecker(deputy).permissions


@pytest.mark.parametrize("aktion", ["sign", "reject"])
@pytest.mark.parametrize("fall", ["ohne_noe_recht", "ohne_vorlagenrecht"])
def test_mitzeichnung_in_vertretung_nur_mit_sicht_der_vertretenen_person(aktion: str, fall: str) -> None:
    tenant = _tenant("nord")
    amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    amtsperson = nutzer(tenant, "amtsperson", "view_meetings")
    # „Vorlagen sehen“ ist die Vorgabe neuer Rollen und wird für den zweiten Fall eigens entzogen
    amtsperson.roles.all().update(can_view_papers=fall == "ohne_noe_recht")
    amtsperson.departments.add(amt)
    vertretung = nutzer(tenant, "vertretung", "view_papers", "view_non_public_papers")
    _vertretung(amtsperson, vertretung, scope_worklist=True)
    # Was die vertretene Person nicht sehen darf: eine nichtöffentliche Vorlage bzw. Vorlagen überhaupt
    vorlage = SessionPaper.objects.create(
        tenant=tenant, name="Grundstück", status="review", is_public=fall != "ohne_noe_recht"
    )
    station = SessionCosignature.objects.create(paper=vorlage, department=amt, order=1)

    deputy = _frisch(vertretung)
    assert cosign_service.acting_for(deputy, station) is None
    assert not cosign_service.can_decide(deputy, station)
    assert list(cosign_service.my_pending_cosignatures(deputy)) == []

    url = reverse(
        "session:cosignature_action",
        kwargs={"tenant_slug": tenant.slug, "cosign_id": station.pk, "action": aktion},
    )
    client(vertretung).post(url, {"comment": "in Vertretung"})

    station.refresh_from_db()
    vorlage.refresh_from_db()
    assert station.status == "pending" and vorlage.status == "review"


def test_mitzeichnung_in_vertretung_bleibt_mit_sicht_der_vertretenen_person_moeglich() -> None:
    tenant = _tenant("nord")
    amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    amtsperson = nutzer(tenant, "amtsperson", "view_papers")
    amtsperson.departments.add(amt)
    vertretung = nutzer(tenant, "vertretung", "view_papers", "view_non_public_papers")
    _vertretung(amtsperson, vertretung, scope_worklist=True)
    offen = SessionPaper.objects.create(tenant=tenant, name="Spielplatz", status="review", is_public=True)
    geheim = SessionPaper.objects.create(tenant=tenant, name="Grundstück", status="review", is_public=False)
    station = SessionCosignature.objects.create(paper=offen, department=amt, order=1)
    SessionCosignature.objects.create(paper=geheim, department=amt, order=1)

    deputy = _frisch(vertretung)
    assert cosign_service.acting_for(deputy, station) == amtsperson
    # Im Arbeitsvorrat steht nur die Station, die sich in Vertretung auch entscheiden lässt
    assert list(cosign_service.my_pending_cosignatures(deputy)) == [station]


def test_eigenes_konto_ordnet_nur_ein_administrator_einem_amt_zu() -> None:
    tenant = _tenant("nord")
    amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    verwaltung = nutzer(tenant, "verwaltung", "manage_users", "view_papers")
    kollegin = nutzer(tenant, "kollegin", "view_papers")
    vertretene = nutzer(tenant, "vertretene", "view_papers")
    _vertretung(vertretene, verwaltung, scope_worklist=True)
    url = reverse("session:department_assignment", kwargs={"tenant_slug": tenant.slug})

    def zuordnen(wer: SessionUser, ziel: SessionUser) -> str:
        return _meldungen(client(wer).post(url, {"session_user": str(ziel.pk), "department": str(amt.pk)}))

    # Sich selbst und eine gerade im Arbeitsvorrat vertretene Person: keine selbst verschaffte Zuständigkeit
    assert "nur ein Administrator" in zuordnen(verwaltung, verwaltung)
    assert "nur ein Administrator" in zuordnen(verwaltung, vertretene)
    assert not verwaltung.departments.exists() and not vertretene.departments.exists()
    station = SessionCosignature.objects.create(
        paper=SessionPaper.objects.create(tenant=tenant, name="Spielplatz", status="review"), department=amt, order=1
    )
    assert not cosign_service.can_decide(_frisch(verwaltung), station)

    # Andere Personen ordnet die Benutzerverwaltung weiter zu, Administratoren auch sich selbst
    zuordnen(verwaltung, kollegin)
    assert kollegin.departments.filter(pk=amt.pk).exists()
    admin = _admin(tenant, "admin")
    zuordnen(admin, admin)
    assert admin.departments.filter(pk=amt.pk).exists()

    # Entfernen bleibt für das eigene Konto möglich: Es verschafft keine Zuständigkeit
    verwaltung.departments.add(amt)
    client(verwaltung).post(url, {"session_user": str(verwaltung.pk), "department": str(amt.pk), "action": "remove"})
    assert not verwaltung.departments.exists()


# =============================================================================
# Vorlegen: Status und Mitzeichnungskette zusammen
# =============================================================================


def test_vorlegen_schreibt_status_und_mitzeichnungskette_zusammen_fest(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = _tenant("nord")
    autorin = nutzer(tenant, "autorin", "view_papers", "edit_papers")
    vorlage = SessionPaper.objects.create(
        tenant=tenant, name="Spielplatz", status="draft", has_financial_impact=False, created_by=autorin
    )

    def scheitert(_paper: Any) -> int:
        raise RuntimeError("Kette nicht aufgebaut")

    monkeypatch.setattr(cosign_service, "build_chain", scheitert)
    with pytest.raises(RuntimeError):
        client(autorin).post(f"/session/{tenant.slug}/papers/{vorlage.pk}/workflow/submit/")

    # Ohne Kette kein „In Prüfung“: Eine Freigabe fände sonst keine offene Mitzeichnung
    vorlage.refresh_from_db()
    assert vorlage.status == "draft"


# =============================================================================
# Beratungsfolge und Suche
# =============================================================================


@pytest.mark.parametrize("neues_design", [False, True])
def test_beratungsfolge_nennt_nichtoeffentliche_beratung_nur_mit_sichtrecht(neues_design: bool) -> None:
    tenant = _tenant("nord", session_new_design=neues_design)
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    ausschuss = SessionOrganization.objects.create(tenant=tenant, name="MARKERAUSSCHUSS", short_name="MARKERKURZ")
    vorlage = SessionPaper.objects.create(tenant=tenant, name="Spielplatz", status="approved", is_public=True)
    start = timezone.now() + timedelta(days=7)
    klausur = SessionMeeting.objects.create(
        tenant=tenant, name="Klausur", organization=ausschuss, start=start, is_public=False
    )
    ratssitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=start + timedelta(days=7), is_public=True
    )
    top_klausur = SessionAgendaItem.objects.create(
        meeting=klausur, number="1", order=1, name="Vorberatung", paper=vorlage, is_public=False
    )
    top_rat = SessionAgendaItem.objects.create(
        meeting=ratssitzung, number="1", order=1, name="Spielplatz", paper=vorlage
    )
    vorberatung = SessionConsultation.objects.create(
        paper=vorlage, organization=ausschuss, meeting=klausur, agenda_item=top_klausur, role="hearing", order=1
    )
    SessionConsultation.objects.create(
        paper=vorlage, organization=rat, meeting=ratssitzung, agenda_item=top_rat, role="decision", order=2
    )
    # Ergebnis der nichtöffentlichen Vorberatung (ohne den Weg über die Abstimmung)
    SessionConsultation.objects.filter(pk=vorberatung.pk).update(result="rejected")
    leser = nutzer(tenant, "leser", "view_meetings", "view_papers")
    leser_noe = nutzer(tenant, "leser-noe", "view_meetings", "view_papers", "view_non_public_meetings")
    seiten = [
        reverse("session:paper_detail", kwargs={"tenant_slug": tenant.slug, "paper_id": vorlage.pk}),
        reverse("session:meeting_detail", kwargs={"tenant_slug": tenant.slug, "meeting_id": ratssitzung.pk}),
    ]

    farben = []
    for seite in seiten:
        ohne = client(leser).get(seite)
        mit = client(leser_noe).get(seite)
        assert ohne.status_code == 200 and mit.status_code == 200
        text_ohne, text_mit = ohne.content.decode(), mit.content.decode()
        # Gremium, Rolle und Ergebnis der nichtöffentlichen Station bleiben ohne das Sichtrecht verborgen
        assert "MARKER" not in text_ohne
        assert "Anhörung" not in text_ohne and "Abgelehnt" not in text_ohne
        assert "ichtöffentliche Beratung" in text_ohne
        assert "MARKER" in text_mit and "Anhörung" in text_mit and "Abgelehnt" in text_mit
        # Bisheriger Rahmen: Die Stationsnummer trägt eine Farbe je Stand (grün, blau, grau)
        farben += re.findall(r'font-bold\s+bg-(\w+)-100[^"]*">\s*1\s*</span>', text_ohne)
    # Auch die Farbe verrät von der verborgenen Station nichts: weder das Ergebnis noch den angelegten TOP
    assert farben == ([] if neues_design else ["gray"])


def test_suche_findet_protokolltext_nur_mit_recht_fuer_niederschriften() -> None:
    w = welt(status="draft")
    kwargs = {"can_view_dashboard": True, "can_view_meetings": True}
    ohne_rolle = SessionRole.objects.create(tenant=w.tenant, name="ohne", can_view_protocols=False, **kwargs)
    mit_rolle = SessionRole.objects.create(tenant=w.tenant, name="mit", can_view_protocols=True, **kwargs)
    konten = []
    for name, rolle in (("ohne", ohne_rolle), ("mit", mit_rolle)):
        konto = SessionUser.objects.create(
            user=cast(Any, UserFactory)(email=f"{name}@suche.example.org"), tenant=w.tenant
        )
        konto.roles.add(rolle)
        konten.append(konto)
    url = reverse("session:search", kwargs={"tenant_slug": w.tenant.slug})

    # „Aussprache“ steht nur im Protokolltext des TOP, „Radweg“ auch im Betreff
    ohne = client(konten[0]).get(url, {"q": "Aussprache", "kind": "resolutions"})
    mit = client(konten[1]).get(url, {"q": "Aussprache", "kind": "resolutions"})
    betreff = client(konten[0]).get(url, {"q": "Radweg", "kind": "resolutions"})

    assert ohne.status_code == 200 and mit.status_code == 200
    assert "Radweg Hauptstraße" not in ohne.content.decode()
    assert "Radweg Hauptstraße" in mit.content.decode()
    assert "Radweg Hauptstraße" in betreff.content.decode()


# =============================================================================
# Deaktivierung und offene Einladungen
# =============================================================================


def test_deaktivierung_zieht_offene_einladungen_an_und_von_der_person_zurueck() -> None:
    tenant = _tenant("nord")
    verwaltung = nutzer(tenant, "verwaltung", "manage_users")
    ziel = nutzer(tenant, "ziel", "manage_users")
    von_ziel = SessionInvitation.create_for_tenant(tenant, "zweitadresse@example.org", invited_by=ziel)
    an_ziel = SessionInvitation.create_for_tenant(tenant, ziel.user.email.upper())
    fremd = SessionInvitation.create_for_tenant(tenant, "kollegin@example.org", invited_by=verwaltung)
    url = reverse("session:user_deactivate", kwargs={"tenant_slug": tenant.slug, "session_user_id": ziel.pk})

    antwort = client(verwaltung).post(url)

    ziel.refresh_from_db()
    assert ziel.is_active is False
    offen = set(SessionInvitation.objects.filter(tenant=tenant).values_list("pk", flat=True))
    assert offen == {fremd.pk}
    protokolliert = SessionAuditLog.objects.filter(
        tenant=tenant, action="delete", changes__grund="Zugang deaktiviert"
    ).values_list("object_id", flat=True)
    assert set(protokolliert) == {von_ziel.pk, an_ziel.pk}
    # Die Verwaltung liest, welche Einladungen mit der Deaktivierung verfallen sind
    meldung = _meldungen(antwort)
    assert f"2 offene Einladungen wurden zurückgezogen: {ziel.user.email}, zweitadresse@example.org." in meldung
    assert "kollegin@example.org" not in meldung

    # Reaktivieren und eine danach neu ausgestellte Einladung bleiben der Weg zurück
    antwort = client(verwaltung).post(url)
    ziel.refresh_from_db()
    assert ziel.is_active is True
    assert "zurückgezogen" not in _meldungen(antwort)


def test_deaktivierung_ohne_offene_einladungen_meldet_keinen_rueckzug() -> None:
    tenant = _tenant("nord")
    verwaltung = nutzer(tenant, "verwaltung", "manage_users")
    ziel = nutzer(tenant, "ziel", "manage_users")
    SessionInvitation.create_for_tenant(tenant, "kollegin@example.org", invited_by=verwaltung)
    url = reverse("session:user_deactivate", kwargs={"tenant_slug": tenant.slug, "session_user_id": ziel.pk})

    meldung = _meldungen(client(verwaltung).post(url))

    assert "wurde deaktiviert" in meldung and "zurückgezogen" not in meldung


def test_erneut_senden_bindet_die_einladung_an_die_sendende_person() -> None:
    tenant = _tenant("nord")
    verwaltung = nutzer(tenant, "verwaltung", "manage_users")
    einladende = nutzer(tenant, "einladende", "manage_users")
    einladung = SessionInvitation.create_for_tenant(tenant, "neu@example.org", invited_by=einladende)
    # Der Zugang der einladenden Person endet an der Benutzerverwaltung vorbei (etwa über den Django-Admin):
    # Die Einladung steht weiter unter „Offene Einladungen“, ihr Link gilt nicht mehr
    SessionUser.objects.filter(pk=einladende.pk).update(is_active=False)
    alter_link = reverse("session:invitation_accept", kwargs={"token": cast(Any, einladung).plain_token})
    assert Client().get(alter_link).status_code == 404
    mail.outbox = []

    antwort = client(verwaltung).post(
        reverse("session:invitation_resend", kwargs={"tenant_slug": tenant.slug, "invitation_id": einladung.pk})
    )

    assert "erneut versendet" in _meldungen(antwort)
    einladung.refresh_from_db()
    assert einladung.invited_by_id == verwaltung.pk
    # Der neue Link aus der Mail gilt: Die Einladung hängt jetzt am Zugang der sendenden Person
    treffer = re.search(r"/session/invite/([^/\s\"]+)/", str(mail.outbox[-1].body))
    assert treffer is not None
    assert Client().get(reverse("session:invitation_accept", kwargs={"token": treffer.group(1)})).status_code == 200
    eintrag = SessionAuditLog.objects.get(tenant=tenant, object_id=einladung.pk, changes__erneut_gesendet=True)
    assert eintrag.changes["eingeladen_von"] == verwaltung.user.email
    assert eintrag.changes["eingeladen_von_bisher"] == einladende.user.email


def test_einladung_gilt_nur_solange_der_zugang_der_einladenden_person_besteht() -> None:
    tenant = _tenant("nord")
    einladende = nutzer(tenant, "einladende", "manage_users")
    einladung = SessionInvitation.create_for_tenant(tenant, "neu@example.org", invited_by=einladende)
    einrichtung = SessionInvitation.create_for_tenant(tenant, "erste@example.org")

    def annahme(invitation: SessionInvitation) -> int:
        url = reverse("session:invitation_accept", kwargs={"token": cast(Any, invitation).plain_token})
        return int(Client().get(url).status_code)

    assert annahme(einladung) == 200
    SessionUser.objects.filter(pk=einladende.pk).update(is_active=False)

    assert annahme(einladung) == 404
    antwort = Client().post(
        reverse("session:invitation_accept", kwargs={"token": cast(Any, einladung).plain_token}),
        {"password": "Sehr-Langes-Passwort-2026!", "password_confirm": "Sehr-Langes-Passwort-2026!"},
    )
    assert antwort.status_code == 404
    assert not SessionUser.objects.filter(tenant=tenant, user__email="neu@example.org").exists()
    # Einladungen der Mandanteneinrichtung haben keine einladende Person und bleiben gültig
    assert annahme(einrichtung) == 200
