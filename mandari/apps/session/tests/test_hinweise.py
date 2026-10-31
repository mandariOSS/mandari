# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweise im Sitzungsdienst (Issue #745), jeweils im bisherigen und im neuen Rahmen.

- Hinweise für den Betrieb (Befehle ``audit_chain_backfill``, ``send_session_reminders``, ``session_privacy_purge``)
  sehen nur Administratoren, in der Demo niemand. Die Funktionen daneben bleiben.
- Der Stand der Kette bleibt für Kontrollrollen sichtbar, nur ohne Befehl; in der Demo entfällt er.
- Meldungen nach einer Aktion behalten ihren Inhalt und nennen den Befehl nur der Administration.
- Fristen bleiben auf dem Start, im bisherigen Rahmen in einer Karte statt in zwei Warnkästen.
- Dauerhinweise des bisherigen Rahmens behalten ihren Text, verlieren aber den Kasten.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common import audit_chain
from apps.common.models import AuditChainHead
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.permissions import SessionPermissionChecker

pytestmark = pytest.mark.django_db

RAHMEN = pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
KONTROLLE = ("manage_settings", "view_dashboard", "view_audit_log", "export_audit_log")
BEFEHLE = ("audit_chain_backfill", "send_session_reminders", "session_privacy_purge")


def _mandant(neu: bool, slug: str = "hinweisstadt") -> SessionTenant:
    return SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug, session_new_design=neu)


def _nutzer(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionUser:
    flags = {f"can_{recht}": True for recht in rechte}
    rolle = SessionRole.objects.create(tenant=tenant, name=f"rolle-{name}", is_admin=admin, **flags)
    konto = cast(User, UserFactory(email=f"{name}@example.org"))  # type: ignore[no-untyped-call]
    nutzer = SessionUser.objects.create(user=konto, tenant=tenant)
    nutzer.roles.add(rolle)
    return nutzer


def _client(nutzer: SessionUser) -> Client:
    client = Client()
    client.force_login(nutzer.user)
    return client


def _adresse(tenant: SessionTenant, name: str, **kwargs: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, **kwargs})


def _seite(client: Client, tenant: SessionTenant, name: str, **kwargs: Any) -> str:
    antwort = client.get(_adresse(tenant, name, **kwargs), follow=True)
    assert antwort.status_code == 200, name
    return antwort.content.decode()


def _altbestand(tenant: SessionTenant) -> None:
    """Die Kette des Mandanten ist noch nicht scharf: Stand wie vor ``audit_chain_backfill``."""
    AuditChainHead.objects.update_or_create(
        scope=audit_chain.SESSION.scope_key(tenant.pk), defaults={"initialized": False}
    )


def _meldungen(antwort: Any) -> str:
    return " ".join(str(meldung) for meldung in get_messages(antwort.wsgi_request))


def test_nur_administratoren_ausserhalb_der_demo_sehen_betriebshinweise() -> None:
    tenant = _mandant(False)
    admin = _nutzer(tenant, "admin", admin=True)
    einstellungen = _nutzer(tenant, "einstellungen", *KONTROLLE)
    assert SessionPermissionChecker(admin).sieht_betriebshinweise is True
    assert SessionPermissionChecker(einstellungen).sieht_betriebshinweise is False
    assert SessionPermissionChecker(None).sieht_betriebshinweise is False
    with override_settings(DEMO_INSTANCE=True):
        assert SessionPermissionChecker(admin).sieht_betriebshinweise is False


@RAHMEN
def test_administration_sieht_die_betriebshinweise(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "admin", *KONTROLLE, admin=True))
    _altbestand(tenant)
    assert "send_session_reminders" in _seite(client, tenant, "settings")
    assert "session_privacy_purge" in _seite(client, tenant, "privacy_settings")
    audit = _seite(client, tenant, "audit_log")
    assert "Altbestand noch nicht verkettet" in audit and "audit_chain_backfill" in audit


@RAHMEN
def test_andere_rollen_sehen_keine_befehle_aber_alle_funktionen(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "revision", *KONTROLLE))
    _altbestand(tenant)
    einstellungen = _seite(client, tenant, "settings")
    datenschutz = _seite(client, tenant, "privacy_settings")
    audit = _seite(client, tenant, "audit_log")
    for befehl in BEFEHLE:
        assert befehl not in einstellungen + datenschutz + audit, befehl
    # Der Stand bleibt für Kontrollrollen sichtbar, mit dem Weg, den sie gehen können
    assert "Altbestand noch nicht verkettet" in audit
    assert "dministration kann die Verkettung veranlassen" in audit
    # Keine Funktion fällt weg: Erinnerungen, Löschlauf, Kettenprüfung und Export bleiben
    assert 'data-testid="karte-erinnerungen"' in einstellungen
    assert _adresse(tenant, "settings_reminders") in einstellungen
    assert _adresse(tenant, "privacy_purge") in datenschutz and "Löschlauf ausführen" in datenschutz
    assert _adresse(tenant, "audit_log_verify") in audit and _adresse(tenant, "audit_log_export") in audit


@RAHMEN
@override_settings(DEMO_INSTANCE=True)
def test_demo_blendet_betriebshinweise_auch_fuer_administratoren_aus(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "admin", *KONTROLLE, admin=True))
    _altbestand(tenant)
    einstellungen = _seite(client, tenant, "settings")
    datenschutz = _seite(client, tenant, "privacy_settings")
    audit = _seite(client, tenant, "audit_log")
    for befehl in BEFEHLE:
        assert befehl not in einstellungen + datenschutz + audit, befehl
    assert "Altbestand noch nicht verkettet" not in audit
    # Funktionen und der Hinweis der Demo selbst (Issue #99) bleiben
    assert _adresse(tenant, "settings_reminders") in einstellungen
    assert _adresse(tenant, "privacy_purge") in datenschutz
    assert _adresse(tenant, "audit_log_verify") in audit
    for seite in (einstellungen, datenschutz, audit):
        assert 'id="demo-hinweis"' in seite
        # Nicht mehr unter der Seitenleiste des bisherigen Rahmens
        assert "lg:right-3" in seite


def test_ohne_betriebshinweis_entfernt_nur_den_befehl() -> None:
    for vorher, nachher in (
        (
            "Altbestand: 3 Einträge sind noch nicht verkettet – Befehl audit_chain_backfill ausführen.",
            "Altbestand: 3 Einträge sind noch nicht verkettet.",
        ),
        (
            "Die Kette ist noch nicht scharf geschaltet – Befehl audit_chain_backfill ausführen.",
            "Die Kette ist noch nicht scharf geschaltet.",
        ),
        (
            "Die Kette ist noch nicht angelegt; 3 Einträge sind unverkettet (audit_chain_backfill).",
            "Die Kette ist noch nicht angelegt; 3 Einträge sind unverkettet.",
        ),
        (
            "Audit-Log (Altbestand noch nicht verkettet: audit_chain_backfill ausführen)",
            "Audit-Log (Altbestand noch nicht verkettet)",
        ),
        ("Audit-Log (Frist deaktiviert)", "Audit-Log (Frist deaktiviert)"),
    ):
        assert audit_chain.ohne_betriebshinweis(vorher) == nachher


@pytest.mark.parametrize(("admin", "mit_befehl"), [(True, True), (False, False)])
def test_kettenpruefung_nennt_den_befehl_nur_der_administration(admin: bool, mit_befehl: bool) -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "kontrolle", *KONTROLLE, admin=admin))
    _altbestand(tenant)
    text = _meldungen(client.post(_adresse(tenant, "audit_log_verify")))
    assert "noch nicht" in text, text
    assert ("audit_chain_backfill" in text) is mit_befehl, text


@pytest.mark.parametrize(("admin", "mit_befehl"), [(True, True), (False, False)])
def test_grosse_protokolle_nennen_den_befehl_nur_der_administration(
    admin: bool, mit_befehl: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "kontrolle", *KONTROLLE, admin=admin))
    monkeypatch.setattr("apps.session.views.audit.UI_VERIFY_MAX_ENTRIES", -1)
    pruefung = _meldungen(client.post(_adresse(tenant, "audit_log_verify")))
    assert "zu groß für eine Prüfung" in pruefung and "Der Betrieb prüft es" in pruefung
    assert ("verify_audit_chain" in pruefung) is mit_befehl
    with override_settings(AUDIT_EXPORT_MAX_ROWS=-1):
        export = _meldungen(client.post(_adresse(tenant, "audit_log_export"), {"format": "csv"}))
    assert "kürzeren Zeitraum" in export and "der Betrieb kann größere Exporte" in export
    assert ("export_audit_log" in export) is mit_befehl


@pytest.mark.parametrize(("admin", "mit_befehl"), [(True, True), (False, False)])
def test_loeschlauf_nennt_den_befehl_nur_der_administration(admin: bool, mit_befehl: bool) -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "datenschutz", *KONTROLLE, admin=admin))
    _altbestand(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(settings={"privacy": {"audit_years": 1}})
    text = _meldungen(client.post(_adresse(tenant, "privacy_purge")))
    assert "Altbestand noch nicht verkettet" in text, text
    assert ("audit_chain_backfill" in text) is mit_befehl, text


def _fristen(tenant: SessionTenant) -> None:
    """Eine offene und eine verstrichene Ladungsfrist, dazu ein überfälliger Beschluss."""
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    jetzt = timezone.now()
    for name, tage in (("Sitzung spaeter", 60), ("Sitzung bald", 1)):
        SessionMeeting.objects.create(
            tenant=tenant,
            name=name,
            organization=gremium,
            start=jetzt + dt.timedelta(days=tage),
            meeting_state="scheduled",
            is_public=True,
        )
    SessionAgendaItem.objects.create(
        meeting=SessionMeeting.objects.get(tenant=tenant, name="Sitzung spaeter"),
        number="1",
        name="Spielplatz",
        vote_result="approved",
        implementation_deadline=timezone.localdate() - dt.timedelta(days=5),
    )


def test_fristen_bleiben_im_bisherigen_rahmen_in_einer_karte() -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "dienst", "view_dashboard", "view_meetings"))
    _fristen(tenant)
    start = _seite(client, tenant, "dashboard")
    assert start.count('data-testid="start-fristen"') == 1
    for text in (
        "Ladung muss bis",
        "Ladungsfrist verstrichen",
        "Überfällige Beschlüsse (1)",
        "Zur Beschlusskontrolle",
        "TOP 1: Spielplatz",
        "verstrichen",
    ):
        assert text in start, text
    assert f"{_adresse(tenant, 'resolutions')}?overdue=1" in start
    # Keine farbigen Warnkästen mehr; verstrichene Fristen bleiben rot
    assert "bg-amber-50" not in start and "bg-red-50" not in start
    assert "text-red-700" in start


def test_fristen_bleiben_im_neuen_rahmen() -> None:
    tenant = _mandant(True)
    client = _client(_nutzer(tenant, "dienst", "view_dashboard", "view_meetings"))
    _fristen(tenant)
    start = _seite(client, tenant, "dashboard")
    assert 'data-testid="start-fristen"' in start
    for text in ("Ladung bis", "Ladungsfrist am", "Umsetzungsfrist am", "1 überfällig"):
        assert text in start, text


def test_dauerhinweise_des_bisherigen_rahmens_behalten_den_text_ohne_kasten() -> None:
    tenant = _mandant(False)
    nutzer = _nutzer(
        tenant, "dienst", "view_applications", "manage_settings", "manage_allowances", "view_papers", "edit_papers"
    )
    client = _client(nutzer)
    vorlage = SessionPaper.objects.create(tenant=tenant, name="Vorlage", reference="V/1", created_by=nutzer)

    antraege = _seite(client, tenant, "applications")
    assert "erscheinen hier automatisch zur Bearbeitung" in antraege and "bg-blue-50" not in antraege

    formate = _seite(client, tenant, "settings_meeting_formats")
    assert "Keine Rechtsberatung." in formate and 'data-testid="keine-rechtsberatung"' in formate

    pauschalen = _seite(client, tenant, "allowances_monthly")
    assert "bitte jeweils zum Jahreswechsel im Katalog anpassen" in pauschalen
    assert "EntschVO NRW (Stand 2025)" in pauschalen and "<details" in pauschalen and "bg-blue-50" not in pauschalen

    fassungen = _seite(client, tenant, "paper_versions", paper_id=vorlage.pk)
    assert "Ins Bürgerportal gelangt nie eine ältere Fassung" in fassungen
    assert 'role="status"' not in fassungen.split('data-testid="fassungen-liste"')[1].split("<form")[0]
