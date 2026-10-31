# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweise im Sitzungsdienst (Issue #745), jeweils im bisherigen und im neuen Rahmen.

- Die Audit-Seiten und ihre Meldungen nennen den Befehl für den Betrieb allen, die sie sehen dürfen, außer in der Demo.
- ``session_privacy_purge`` sehen nur Administratoren, in der Demo niemand. Der Satz zu ``send_session_reminders``
  entfällt, weil der Lauf ein Zeitplan im Worker ist. Die Funktionen daneben bleiben.
- Die Meldung nach dem Löschlauf sagt, dass der monatliche Lauf die Verkettung nachholt, und das stimmt auch.
- Ohne Einstellungsrecht nennt das Sitzungsformular die zuständige Stelle für das Landesprofil, ohne Warnkasten.
- Fristen bleiben auf dem Start, im bisherigen Rahmen in einer Karte statt in zwei Warnkästen.
- Dauerhinweise des bisherigen Rahmens behalten ihren Text, verlieren aber den Kasten; die Aufgabe zum Jahreswechsel
  bleibt außerhalb des zugeklappten Bereichs.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common import audit_chain, hilfe
from apps.common.models import AuditChainHead
from apps.common.tests.factories import UserFactory
from apps.events.schedule import autodiscover, registry
from apps.events.verwaltungsbefehle import schedule_name
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
#: Nur das Kontrollrecht, wie die Standardrollen Revision und Datenschutz
KONTROLLRECHT = ("view_audit_log", "export_audit_log")
VERWALTUNG = ("manage_settings", "view_dashboard")
BEFEHL_DATENSCHUTZ = "session_privacy_purge"
BEFEHL_ERINNERUNG = "send_session_reminders"
BEFEHL_KETTE = "audit_chain_backfill"


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


def _zeitplan_registriert(befehl: str) -> bool:
    autodiscover()
    return registry.get(schedule_name(befehl)) is not None


def _meldungen(antwort: Any) -> str:
    return " ".join(str(meldung) for meldung in get_messages(antwort.wsgi_request))


def test_nur_administratoren_ausserhalb_der_demo_sehen_betriebshinweise() -> None:
    tenant = _mandant(False)
    admin = _nutzer(tenant, "admin", admin=True)
    einstellungen = _nutzer(tenant, "einstellungen", *VERWALTUNG)
    assert SessionPermissionChecker(admin).sieht_betriebshinweise is True
    assert SessionPermissionChecker(einstellungen).sieht_betriebshinweise is False
    assert SessionPermissionChecker(None).sieht_betriebshinweise is False
    with override_settings(DEMO_INSTANCE=True):
        assert SessionPermissionChecker(admin).sieht_betriebshinweise is False


@RAHMEN
def test_nur_die_administration_sieht_den_befehl_beim_loeschlauf(neu: bool) -> None:
    tenant = _mandant(neu)
    admin = _client(_nutzer(tenant, "admin", *VERWALTUNG, admin=True))
    einstellungen = _client(_nutzer(tenant, "einstellungen", *VERWALTUNG))
    assert BEFEHL_DATENSCHUTZ in _seite(admin, tenant, "privacy_settings")
    datenschutz = _seite(einstellungen, tenant, "privacy_settings")
    assert BEFEHL_DATENSCHUTZ not in datenschutz
    # Keine Funktion fällt weg
    assert _adresse(tenant, "privacy_purge") in datenschutz and "Löschlauf ausführen" in datenschutz


@RAHMEN
def test_erinnerungslauf_ist_ein_zeitplan_und_kein_hinweis_mehr(neu: bool) -> None:
    tenant = _mandant(neu)
    einstellungen = _seite(_client(_nutzer(tenant, "admin", *VERWALTUNG, admin=True)), tenant, "settings")
    assert BEFEHL_ERINNERUNG not in einstellungen and "ist im Betrieb eingerichtet" not in einstellungen
    # Der Grund für den Wegfall: Der Lauf ist als Zeitplan im Worker registriert, die Einstellungen bleiben
    assert _zeitplan_registriert(BEFEHL_ERINNERUNG)
    assert 'data-testid="karte-erinnerungen"' in einstellungen
    assert _adresse(tenant, "settings_reminders") in einstellungen


@RAHMEN
def test_kontrollrecht_allein_sieht_den_betriebshinweis_auf_der_audit_seite(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "revision", *KONTROLLRECHT))
    _altbestand(tenant)
    audit = _seite(client, tenant, "audit_log")
    assert "Altbestand noch nicht verkettet" in audit and BEFEHL_KETTE in audit
    # Keine Sackgasse: Die Administrator-Rolle kann das nicht, also verweist die Seite auch nicht auf sie
    assert "Administration" not in audit
    assert _adresse(tenant, "audit_log_verify") in audit and _adresse(tenant, "audit_log_export") in audit


@RAHMEN
@override_settings(DEMO_INSTANCE=True)
def test_demo_blendet_betriebshinweise_auch_fuer_administratoren_aus(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "admin", *VERWALTUNG, *KONTROLLRECHT, admin=True))
    _altbestand(tenant)
    einstellungen = _seite(client, tenant, "settings")
    datenschutz = _seite(client, tenant, "privacy_settings")
    audit = _seite(client, tenant, "audit_log")
    for befehl in (BEFEHL_KETTE, BEFEHL_ERINNERUNG, BEFEHL_DATENSCHUTZ):
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


def test_kettenpruefung_nennt_den_befehl_auch_dem_kontrollrecht_allein() -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "revision", *KONTROLLRECHT))
    _altbestand(tenant)
    text = _meldungen(client.post(_adresse(tenant, "audit_log_verify")))
    assert "noch nicht" in text and BEFEHL_KETTE in text, text


@override_settings(DEMO_INSTANCE=True)
def test_kettenpruefung_nennt_in_der_demo_keinen_befehl() -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "revision", *KONTROLLRECHT, admin=True))
    _altbestand(tenant)
    text = _meldungen(client.post(_adresse(tenant, "audit_log_verify")))
    assert "noch nicht" in text and BEFEHL_KETTE not in text, text


@pytest.mark.parametrize(("demo", "mit_befehl"), [(False, True), (True, False)], ids=["betrieb", "demo"])
def test_grosse_protokolle_nennen_den_befehl_ausser_in_der_demo(
    demo: bool, mit_befehl: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "revision", *KONTROLLRECHT))
    monkeypatch.setattr("apps.session.views.audit.UI_VERIFY_MAX_ENTRIES", -1)
    with override_settings(DEMO_INSTANCE=demo, AUDIT_EXPORT_MAX_ROWS=-1):
        pruefung = _meldungen(client.post(_adresse(tenant, "audit_log_verify")))
        export = _meldungen(client.post(_adresse(tenant, "audit_log_export"), {"format": "csv"}))
    assert "zu groß für eine Prüfung" in pruefung and "Der Betrieb prüft es" in pruefung
    assert ("verify_audit_chain" in pruefung) is mit_befehl
    assert "kürzeren Zeitraum" in export and "der Betrieb kann größere Exporte" in export
    assert ("export_audit_log" in export) is mit_befehl


@pytest.mark.parametrize(
    ("admin", "demo", "mit_befehl", "mit_monatslauf"),
    [(True, False, True, True), (False, False, False, True), (True, True, False, False)],
    ids=["administration", "einstellungsrecht", "demo"],
)
def test_meldung_nach_dem_loeschlauf(admin: bool, demo: bool, mit_befehl: bool, mit_monatslauf: bool) -> None:
    tenant = _mandant(False)
    client = _client(_nutzer(tenant, "datenschutz", *VERWALTUNG, admin=admin))
    _altbestand(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(settings={"privacy": {"audit_years": 1}})
    with override_settings(DEMO_INSTANCE=demo):
        text = _meldungen(client.post(_adresse(tenant, "privacy_purge")))
    assert "Altbestand noch nicht verkettet" in text, text
    assert (BEFEHL_KETTE in text) is mit_befehl, text
    assert ("monatliche Lauf am 1." in text) is mit_monatslauf, text


def test_der_monatliche_lauf_holt_die_verkettung_wirklich_nach() -> None:
    assert _zeitplan_registriert(BEFEHL_DATENSCHUTZ)
    tenant = _mandant(False)
    _nutzer(tenant, "datenschutz", *VERWALTUNG)
    _altbestand(tenant)
    SessionTenant.objects.filter(pk=tenant.pk).update(settings={"privacy": {"audit_years": 1}})
    call_command(BEFEHL_DATENSCHUTZ, tenant=tenant.slug, stdout=io.StringIO())
    kopf = AuditChainHead.objects.get(scope=audit_chain.SESSION.scope_key(tenant.pk))
    assert kopf.initialized is True


@RAHMEN
def test_landesprofil_ohne_einstellungsrecht_nennt_die_zustaendige_stelle(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "dienst", "view_meetings", "create_meetings"))
    seite = _seite(client, tenant, "meeting_create")
    assert "Hybride und digitale Sitzungen sind erst möglich" in seite
    assert "Das wählen Personen mit dem Recht „Einstellungen verwalten“." in seite
    assert _adresse(tenant, "settings_meeting_formats") not in seite
    # Kein Kasten mehr: Der Satz steht nicht in einem Hinweis mit Rolle „status“
    absatz = seite.split("Hybride und digitale Sitzungen sind erst möglich")[0].rsplit("<", 1)[-1]
    assert 'role="status"' not in absatz and "bg-blue-50" not in absatz


@RAHMEN
def test_landesprofil_mit_einstellungsrecht_fuehrt_in_die_einstellungen(neu: bool) -> None:
    tenant = _mandant(neu)
    client = _client(_nutzer(tenant, "dienst", "view_meetings", "create_meetings", "manage_settings"))
    seite = _seite(client, tenant, "meeting_create")
    assert "Hybride und digitale Sitzungen sind erst möglich" in seite
    assert _adresse(tenant, "settings_meeting_formats") in seite
    assert "Das wählen Personen" not in seite


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


@RAHMEN
def test_aufgabe_zum_jahreswechsel_steht_ausserhalb_des_zugeklappten_bereichs(neu: bool) -> None:
    tenant = _mandant(neu)
    pauschalen = _seite(_client(_nutzer(tenant, "dienst", "manage_allowances")), tenant, "allowances_monthly")
    assert "EntschVO NRW (Stand 2025)" in pauschalen and "<details" in pauschalen
    ausserhalb = re.sub(r"<details.*?</details>", "", pauschalen, flags=re.DOTALL)
    assert "bitte jeweils zum Jahreswechsel im Katalog anpassen" in ausserhalb
    assert 'data-testid="pauschalen-jahreswechsel"' in ausserhalb
    # Die Erläuterung bleibt vollständig, nur zugeklappt
    assert "Funktionszulagen sind Vielfache der Vollpauschale" in pauschalen
    assert "Funktionszulagen sind Vielfache der Vollpauschale" not in ausserhalb
    assert "bg-blue-50" not in pauschalen


@RAHMEN
def test_muster_dokumente_verlinken_die_dokumentation(neu: bool) -> None:
    tenant = _mandant(neu)
    datenschutz = _seite(_client(_nutzer(tenant, "dienst", *VERWALTUNG)), tenant, "privacy_settings")
    for schluessel in ("datenschutz_avv", "datenschutz_tom", "datenschutz_loeschkonzept"):
        assert hilfe.docs_url(schluessel) in datenschutz, schluessel
    assert "docs/DSGVO_" not in datenschutz and "im Repository" not in datenschutz


def test_dauerhinweise_des_bisherigen_rahmens_behalten_den_text_ohne_kasten() -> None:
    tenant = _mandant(False)
    nutzer = _nutzer(tenant, "dienst", "view_applications", "manage_settings", "view_papers", "edit_papers")
    client = _client(nutzer)
    vorlage = SessionPaper.objects.create(tenant=tenant, name="Vorlage", reference="V/1", created_by=nutzer)

    antraege = _seite(client, tenant, "applications")
    assert "erscheinen hier automatisch zur Bearbeitung" in antraege and "bg-blue-50" not in antraege

    formate = _seite(client, tenant, "settings_meeting_formats")
    assert "Keine Rechtsberatung." in formate and 'data-testid="keine-rechtsberatung"' in formate

    fassungen = _seite(client, tenant, "paper_versions", paper_id=vorlage.pk)
    assert "Ins Bürgerportal gelangt nie eine ältere Fassung" in fassungen
    assert 'role="status"' not in fassungen.split('data-testid="fassungen-liste"')[1].split("<form")[0]
