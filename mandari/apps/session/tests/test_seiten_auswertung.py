# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rundgang durch die Auswertungsseiten des Session-RIS: kein Serverfehler, mit keiner Rolle und keiner Eingabe.

Abgeleitet aus dem Fehlerjagd-Durchlauf: Alle URL-Muster des Bereichs Sitzungsgeld, Monatspauschalen,
Berichte, Leitstelle, Dashboard, Suche, Archiv und API-Übersicht werden je Rolle aufgerufen – GET normal, als
HTMX-Anfrage und mit kaputten Parametern (Text statt Kennung, Monat 13, Jahr 0 und 99999999999, NaN), POST leer
und mit kaputten Formularwerten. Jede Antwort muss unter 500 bleiben; eine Ausnahme bricht den Test ab.

Neue URL-Muster des Bereichs werden automatisch mitgeprüft (Auswahl über den Namen). Braucht ein neues Muster
eine unbekannte Kennung, schlägt der Test fehl, bis die Testdaten sie liefern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from django.test import Client
from django.urls import URLPattern, reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session import urls as session_urls
from apps.session.models import (
    SessionAllowance,
    SessionAllowanceRate,
    SessionApplication,
    SessionAttendance,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionPersonMonthlyRate,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

#: URL-Namen des Bereichs (Namensraum ``session``)
BEREICH = re.compile(
    r"^(dashboard|dashboard_explicit|search|reports|reports_export_csv|archive|allowances?(_.+)?|monthly_.+"
    r"|leitstelle(_.+)?|api_root)$"
)

UNBEKANNT = "00000000-0000-4000-8000-000000000000"

#: Kaputte Query-Parameter (alle Namen, die die Views des Bereichs lesen)
GET_VARIANTEN: list[dict[str, str]] = [
    {
        "organization": "abc",
        "status": "zzz",
        "from": "2026-13-45",
        "to": "x",
        "year": "0",
        "month": "13",
        "term": "abc",
        "kind": "x",
        "q": "a",
        "type": "allowances",
        "format": "csv",
    },
    {
        "organization": UNBEKANNT,
        "year": "99999999999",
        "month": "99999999999999999999",
        "from": "9999-12-31",
        "to": "0001-01-01",
        "q": "NaN",
        "type": "x",
        "term": UNBEKANNT,
    },
    {"year": "-1", "month": "-1", "from": "2020-01-01", "to": "2030-12-31", "q": "Infinity", "kind": "papers"},
]

#: Kaputte Formularwerte (alle Namen, die die POST-Views des Bereichs lesen)
POST_VARIANTEN: list[dict[str, str]] = [
    {},
    {
        "organization": "abc",
        "role": "zzz",
        "amount": "NaN",
        "from": "x",
        "to": "y",
        "year": "99999999999",
        "month": "13",
        "rate_id": "abc",
        "allowance_id": "abc",
        "assignment_id": "abc",
        "person": "abc",
        "rate": "abc",
        "name": "x",
        "debtor_iban": "<>",
        "debtor_bic": "?",
        "start_date": "x",
        "end_date": "y",
        "is_active": "2",
    },
    {
        "organization": UNBEKANNT,
        "amount": "1e10",
        "from": "2020-01-01",
        "to": "2030-12-31",
        "year": "2025",
        "month": "99999999999999999999",
        "rate_id": UNBEKANNT,
        "allowance_id": UNBEKANNT,
        "assignment_id": UNBEKANNT,
        "name": "Neu",
        "debtor_iban": "DE89370400440532013000",
        "debtor_bic": "COBADEFFXXX",
    },
]


@dataclass
class Welt:
    a: SessionTenant
    gruppe: SessionTenantGroup
    kennungen: dict[str, str]
    konten: dict[str, User | None] = field(default_factory=dict)


def _nutzer(email: str) -> User:
    return cast(User, cast(Any, UserFactory)(email=email))


def _mitglied(user: User, tenant: SessionTenant, role: SessionRole) -> None:
    SessionUser.objects.create(user=user, tenant=tenant).roles.add(role)


def _mandant(slug: str) -> tuple[SessionTenant, dict[str, SessionRole]]:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)
    return tenant, SessionRole.create_default_roles(tenant)


def _bestand(tenant: SessionTenant) -> dict[str, str]:
    """Daten, die jede Seite des Bereichs füllen: Sitzungsgeld, Pauschalen, Vorlagen, Wahlperiode."""
    berlin = ZoneInfo("Europe/Berlin")
    heute = timezone.localdate()
    tenant.settings = {"allowances": {"debtor_name": tenant.name, "debtor_iban": "DE89370400440532013000"}}
    tenant.save()
    SessionLegislativeTerm.objects.create(tenant=tenant, name="22. WP", start_date=date(2024, 6, 9))
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", allowance_amount=Decimal("35.00"))
    SessionAllowanceRate.objects.create(organization=rat, role="chair", amount=Decimal("50.00"))
    person = SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name="Amsel")
    cast(Any, person).set_bank_iban_encrypted("DE02120300000000202051")
    person.save()
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=datetime.combine(heute, time(18), tzinfo=berlin)
    )
    abgesagt = SessionMeeting.objects.create(
        tenant=tenant,
        name="Abgesagt",
        organization=rat,
        cancelled=True,
        start=datetime.combine(heute - timedelta(days=1), time(18), tzinfo=berlin),
    )
    position = SessionAllowance.objects.create(
        attendance=SessionAttendance.objects.create(meeting=sitzung, person=person, status="present"),
        amount=Decimal("35.00"),
    )
    # Grundlage entfallen: Position einer abgesagten Sitzung
    SessionAllowance.objects.create(
        attendance=SessionAttendance.objects.create(meeting=abgesagt, person=person, status="present"),
        amount=Decimal("35.00"),
    )
    pauschale = SessionMonthlyRate.objects.create(tenant=tenant, name="Teilpauschale", amount=Decimal("300.00"))
    zuordnung = SessionPersonMonthlyRate.objects.create(person=person, rate=pauschale)
    posten = SessionMonthlyAllowance.objects.create(
        tenant=tenant, person=person, rate=pauschale, period=heute.replace(day=1), amount=pauschale.amount
    )
    SessionPaper.objects.create(
        tenant=tenant,
        name="Haushaltssatzung",
        status="review",
        is_public=True,
        date=heute,
        deadline=heute - timedelta(days=3),
        main_organization=rat,
    )
    SessionApplication.objects.create(
        tenant=tenant,
        title="Antrag Spielplatz",
        submitter_name="Fraktion",
        submitter_email="f@example.org",
        target_organization=rat,
    )
    return {
        "allowance_id": str(position.pk),
        "rate_id": str(SessionAllowanceRate.objects.get(organization=rat).pk),
        "person_id": str(person.pk),
        "monthly_rate_id": str(pauschale.pk),
        "assignment_id": str(zuordnung.pk),
        "monthly_allowance_id": str(posten.pk),
        "organization_id": str(rat.pk),
    }


@pytest.fixture
def welt() -> Welt:
    a, rollen_a = _mandant("auswertung-a")
    b, rollen_b = _mandant("auswertung-b")
    gruppe = SessionTenantGroup.objects.create(name="Bezirke", slug="auswertung-gruppe")
    for tenant in (a, b):
        SessionTenantGroupTenant.objects.create(group=gruppe, tenant=tenant)
    kennungen = _bestand(a)
    _bestand(b)

    konten: dict[str, User | None] = {}
    for name, schluessel in (
        ("verwaltung", "admin"),
        ("sachbearbeitung", "clerk"),
        ("protokoll", "recorder"),
        ("lesezugriff", "viewer"),
        ("datenschutz", "privacy"),
    ):
        konten[name] = konto = _nutzer(f"{name}@auswertung.example.org")
        _mitglied(konto, a, rollen_a[schluessel])
    # Nur Sitzungsgeld, ohne die voreingestellten Lese-Rechte
    kaemmerei = SessionRole.objects.create(
        tenant=a,
        name="Kämmerei",
        can_view_dashboard=True,
        can_manage_allowances=True,
        can_view_meetings=False,
        can_view_papers=False,
        can_view_applications=False,
        can_view_protocols=False,
    )
    konten["kaemmerei"] = konto = _nutzer("kaemmerei@auswertung.example.org")
    _mitglied(konto, a, kaemmerei)
    # Leitstelle: Administrator in A und B, Mitglied der Gruppe
    konten["leitstelle"] = leitstelle = _nutzer("leitstelle@auswertung.example.org")
    _mitglied(leitstelle, a, rollen_a["admin"])
    _mitglied(leitstelle, b, rollen_b["admin"])
    SessionTenantGroupMembership.objects.create(group=gruppe, user=leitstelle)
    konten["ohne_mandant"] = _nutzer("ohne@auswertung.example.org")
    konten["anonym"] = None
    return Welt(a=a, gruppe=gruppe, kennungen=kennungen, konten=konten)


def _bereichsmuster() -> list[URLPattern]:
    return [p for p in session_urls.urlpatterns if isinstance(p, URLPattern) and p.name and BEREICH.match(p.name)]


def _kwargs(welt: Welt, muster: URLPattern) -> dict[str, str]:
    werte = {"tenant_slug": welt.a.slug, "group_slug": welt.gruppe.slug, **welt.kennungen}
    namen = muster.pattern.regex.groupindex
    fehlend = [name for name in namen if name not in werte]
    assert not fehlend, f"Neues URL-Muster {muster.name}: Testdaten für {fehlend} ergänzen"
    return {name: werte[name] for name in namen}


def _ziele(welt: Welt) -> list[tuple[str, bool, bool]]:
    """(URL, GET erlaubt, POST erlaubt) je Muster des Bereichs, dazu die Übersicht der Session-API v1."""
    ziele = []
    for muster in _bereichsmuster():
        view = cast(Any, muster.callback).view_class
        methoden = set(view.http_method_names)
        url = reverse(f"session:{muster.name}", kwargs=_kwargs(welt, muster))
        ziele.append((url, "get" in methoden and hasattr(view, "get"), "post" in methoden and hasattr(view, "post")))
    ziele.append((reverse("session_api_v1:tenant_root", kwargs={"tenant_slug": welt.a.slug}), True, False))
    return ziele


def test_bereich_vollstaendig_erfasst(welt: Welt) -> None:
    """Die Auswahl greift: alle bekannten Seiten des Bereichs sind dabei."""
    namen = {muster.name for muster in _bereichsmuster()}
    erwartet = {
        "dashboard",
        "search",
        "reports",
        "reports_export_csv",
        "archive",
        "allowances",
        "allowance_year",
        "allowance_cancel",
        "allowances_monthly",
        "monthly_cancel",
        "monthly_rate_save",
        "leitstelle",
        "leitstelle_search",
        "api_root",
    }
    assert erwartet <= namen, erwartet - namen
    assert all(url for url, _get, _post in _ziele(welt))


@pytest.mark.parametrize(
    "rolle",
    [
        "verwaltung",
        "kaemmerei",
        "leitstelle",
        "sachbearbeitung",
        "protokoll",
        "lesezugriff",
        "datenschutz",
        "ohne_mandant",
        "anonym",
    ],
)
def test_keine_serverfehler(welt: Welt, rolle: str) -> None:
    client = Client()  # raise_request_exception: eine Ausnahme in der View lässt den Test scheitern
    user = welt.konten[rolle]
    if user is not None:
        client.force_login(user)
    # Kaputte Formularwerte nur für Rollen, die die Seiten erreichen – die übrigen enden ohnehin vorher
    mit_varianten = rolle in ("verwaltung", "kaemmerei", "leitstelle")
    fehler: list[str] = []
    for url, get, post in _ziele(welt):
        if get:
            for variante in [{}, *(GET_VARIANTEN if mit_varianten else [])]:
                for headers in ({}, {"HX-Request": "true"}):
                    antwort = client.get(url, variante, headers=headers)
                    if antwort.status_code >= 500:
                        fehler.append(f"GET {url} {variante} {headers} -> {antwort.status_code}")
        if post:
            for variante in POST_VARIANTEN if mit_varianten else [{}]:
                antwort = client.post(url, variante)
                if antwort.status_code >= 500:
                    fehler.append(f"POST {url} {variante} -> {antwort.status_code}")
    assert not fehler, "\n".join(fehler)
