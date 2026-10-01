# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Restposten der systematischen Fehlersuche im Session-RIS (Issue #708).

- Formulare: Datums- und Zeitfelder behalten beim Bearbeiten und nach einem Eingabefehler ihren Wert
  (Personen, Gremien, Sitzungen)
- Umlaufbeschluss: Stimmberechtigt sind nur laufende Besetzungen aktiver Personen (gemeinsame Regel)
- Kalendertag in Ortszeit statt UTC: Besetzungen, Eingangsnummer, Berichtsjahre, Löschfristen, Stornomeldung
- Personen-IBAN und -BIC: Prüfung wie beim Auftraggeberkonto, verständliche Meldung
- Dashboard für Kontrollrollen (Revision, Datenschutz): Protokollstand und Wegweiser statt leerer Seite
- Nullbyte im Suchbegriff: Der Fehler tritt nur auf PostgreSQL auf (Zeichenketten mit Nullbyte lehnt die
  Datenbank ab, HTTP 500); auf SQLite prüfen die Tests, dass das Nullbyte vor der Abfrage entfernt wird
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.messages import get_messages
from django.http import QueryDict
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.params import text_param, without_nul
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAllowance,
    SessionApplication,
    SessionAttendance,
    SessionAuditLog,
    SessionCircularResolution,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
)
from apps.session.services import allowance_service, leitstelle_service, privacy_service, report_service, voting_service

pytestmark = pytest.mark.django_db

BERLIN = ZoneInfo("Europe/Berlin")
FLAGS = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]
#: 31.12.2025, 23:30 UTC ist in Deutschland schon der 01.01.2026
SILVESTER_UTC = datetime(2025, 12, 31, 23, 30, tzinfo=UTC)


def _nutzer(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionUser:
    """Konto mit genau diesen Rechten (auch die sonst voreingestellten Lese-Rechte nur, wenn genannt)."""
    flags = {feld: feld[4:] in rechte for feld in FLAGS}
    rolle = SessionRole.objects.create(tenant=tenant, name=f"Rolle {name}", is_admin=admin, **flags)
    user = cast(User, cast(Any, UserFactory)(email=f"{name}@{tenant.slug}.example.org"))
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(rolle)
    return session_user


def _client(session_user: SessionUser) -> Client:
    client = Client()
    client.force_login(session_user.user)
    return client


def _meldungen(response: Any) -> list[str]:
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def admin(tenant: SessionTenant) -> Client:
    return _client(_nutzer(tenant, "verwaltung", admin=True))


@pytest.fixture
def gremium(tenant: SessionTenant) -> SessionOrganization:
    return SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")


def _url(tenant: SessionTenant, pfad: str) -> str:
    return f"/session/{tenant.slug}{pfad}"


def _person(tenant: SessionTenant, nachname: str, **extra: Any) -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name=nachname, **extra)


# =============================================================================
# 1. Datums- und Zeitfelder behalten ihren Wert
# =============================================================================


def _personendaten(**aenderungen: str) -> dict[str, str]:
    daten = {
        "given_name": "Anna",
        "family_name": "Amberg",
        "delivery_channel": "email",
        "is_active": "on",
        "start_date": "2024-07-01",
        "end_date": "",
    }
    daten.update(aenderungen)
    return daten


def test_personenformular_zeigt_gespeicherte_mandatsdaten(tenant: SessionTenant, admin: Client) -> None:
    person = _person(tenant, "Amberg", start_date=date(2024, 7, 1), end_date=date(2029, 6, 30))
    seite = admin.get(_url(tenant, f"/persons/{person.pk}/edit/")).content.decode()
    assert 'name="start_date" id="id_start_date" value="2024-07-01"' in seite
    assert 'name="end_date" id="id_end_date" value="2029-06-30"' in seite


def test_personenformular_behaelt_mandatsdaten_nach_einem_fehler(tenant: SessionTenant, admin: Client) -> None:
    person = _person(tenant, "Amberg", start_date=date(2024, 7, 1))
    antwort = admin.post(_url(tenant, f"/persons/{person.pk}/edit/"), _personendaten(end_date="2020-01-01"))
    assert antwort.status_code == 200
    assert "end_date" in antwort.context["form"].errors
    seite = antwort.content.decode()
    # Vorher blieben beide Felder leer: Das Datumsfilter liefert für die Eingabe (Text) nichts
    assert 'name="start_date" id="id_start_date" value="2024-07-01"' in seite
    assert 'name="end_date" id="id_end_date" value="2020-01-01"' in seite


def _gremiendaten(**aenderungen: str) -> dict[str, str]:
    daten = {
        "name": "Hauptausschuss",
        "organization_type": "committee",
        "invitation_period_days": "7",
        "allowance_amount": "35.00",
        "default_meeting_start_time": "18:30",
        "start_date": "2024-07-01",
        "end_date": "2029-06-30",
        "is_active": "on",
    }
    daten.update(aenderungen)
    return daten


def test_gremienformular_zeigt_gespeicherte_daten_und_uhrzeit(tenant: SessionTenant, admin: Client) -> None:
    gremium = SessionOrganization.objects.create(
        tenant=tenant, name="Hauptausschuss", start_date=date(2024, 7, 1), default_meeting_start_time=time(18, 30)
    )
    seite = admin.get(_url(tenant, f"/organizations/{gremium.pk}/edit/")).content.decode()
    assert 'name="start_date" id="id_start_date" value="2024-07-01"' in seite
    assert 'value="18:30"' in seite


def test_gremienformular_behaelt_daten_und_uhrzeit_nach_einem_fehler(
    tenant: SessionTenant, admin: Client, gremium: SessionOrganization
) -> None:
    antwort = admin.post(_url(tenant, f"/organizations/{gremium.pk}/edit/"), _gremiendaten(allowance_amount="-5"))
    assert antwort.status_code == 200
    assert "allowance_amount" in antwort.context["form"].errors
    seite = antwort.content.decode()
    assert 'name="start_date" id="id_start_date" value="2024-07-01"' in seite
    assert 'name="end_date" id="id_end_date" value="2029-06-30"' in seite
    assert 'id="id_default_meeting_start_time"\n                               value="18:30"' in seite.replace(
        "\r\n", "\n"
    )


def test_sitzungsformular_behaelt_beginn_und_ende_nach_einem_fehler(
    tenant: SessionTenant, admin: Client, gremium: SessionOrganization
) -> None:
    beginn = datetime(2026, 11, 5, 18, 0, tzinfo=BERLIN)
    sitzung = SessionMeeting.objects.create(tenant=tenant, name="12. Sitzung", organization=gremium, start=beginn)
    daten = {
        "name": sitzung.name,
        "organization": str(gremium.pk),
        "start": "2026-11-05T18:00",
        "end": "2026-11-05T17:00",
        "is_public": "on",
        "format": "presence",
        "meeting_state": sitzung.meeting_state,
    }
    antwort = admin.post(_url(tenant, f"/meetings/{sitzung.pk}/edit/"), daten)
    assert antwort.status_code == 200
    assert "Das Ende muss nach dem Beginn" in antwort.content.decode()
    seite = antwort.content.decode().replace("\r\n", "\n")
    assert 'name="start" id="id_start"\n                           value="2026-11-05T18:00"' in seite
    assert 'name="end" id="id_end"\n                           value="2026-11-05T17:00"' in seite


def test_sitzungsformular_zeigt_beginn_in_ortszeit(
    tenant: SessionTenant, admin: Client, gremium: SessionOrganization
) -> None:
    beginn = datetime(2026, 11, 5, 18, 0, tzinfo=BERLIN)
    sitzung = SessionMeeting.objects.create(tenant=tenant, name="12. Sitzung", organization=gremium, start=beginn)
    seite = admin.get(_url(tenant, f"/meetings/{sitzung.pk}/edit/")).content.decode().replace("\r\n", "\n")
    assert 'name="start" id="id_start"\n                           value="2026-11-05T18:00"' in seite


# =============================================================================
# 2. Umlaufbeschluss: nur aktive Personen sind stimmberechtigt
# =============================================================================


def test_umlaufbeschluss_zaehlt_deaktivierte_personen_nicht(
    tenant: SessionTenant, gremium: SessionOrganization
) -> None:
    aktiv = _person(tenant, "Aktiv")
    deaktiviert = _person(tenant, "Deaktiviert", is_active=False)
    for person in (aktiv, deaktiviert):
        SessionOrganizationMembership.objects.create(
            organization=gremium, person=person, has_voting_rights=True, start_date=date(2024, 7, 1)
        )
    umlauf = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=gremium,
        title="Zuschuss Sportverein",
        resolution_text="Der Zuschuss wird gewährt.",
        deadline=timezone.localdate() + timedelta(days=7),
    )
    assert [m.person for m in voting_service.voting_members(umlauf)] == [aktiv]
    auszaehlung = voting_service.circular_tally(umlauf)
    assert auszaehlung["outstanding"] == [aktiv]


# =============================================================================
# 3. Kalendertag in Ortszeit
# =============================================================================


def test_besetzung_ohne_datum_beginnt_am_ortstag(
    tenant: SessionTenant, admin: Client, gremium: SessionOrganization
) -> None:
    person = _person(tenant, "Neujahr")
    with mock.patch("django.utils.timezone.now", return_value=SILVESTER_UTC):
        admin.post(_url(tenant, f"/organizations/{gremium.pk}/memberships/add/"), {"person": str(person.pk)})
    besetzung = SessionOrganizationMembership.objects.get(organization=gremium, person=person)
    assert besetzung.start_date == date(2026, 1, 1)


def test_eingangsnummer_eines_antrags_nach_ortsjahr(tenant: SessionTenant) -> None:
    with mock.patch("django.utils.timezone.now", return_value=SILVESTER_UTC):
        antrag = SessionApplication.objects.create(
            tenant=tenant, title="Antrag zu Neujahr", submitter_name="Fraktion", submitter_email="f@example.org"
        )
    assert antrag.reference.startswith("A/2026/")


def test_berichtsjahre_nach_ortszeit(tenant: SessionTenant, gremium: SessionOrganization) -> None:
    # 31.12.2023, 23:30 UTC ist in Deutschland schon der 01.01.2024
    SessionMeeting.objects.create(
        tenant=tenant, name="Neujahrssitzung", organization=gremium, start=datetime(2023, 12, 31, 23, 30, tzinfo=UTC)
    )
    jahre = report_service.available_years(tenant)
    assert 2024 in jahre
    assert 2023 not in jahre


def test_loeschfrist_rechnet_mit_dem_ortstag(tenant: SessionTenant) -> None:
    tenant.settings = {"privacy": {"persons_years": 10}}
    tenant.save()
    _person(tenant, "Mandat", is_active=False, end_date=date(2016, 10, 2), email="alt@example.org")
    # 01.10.2026, 22:30 UTC ist in Deutschland schon der 02.10.2026: Die Frist ist abgelaufen
    stats = privacy_service.run_privacy_purge(tenant, now=datetime(2026, 10, 1, 22, 30, tzinfo=UTC), dry_run=True)
    assert stats["persons_anonymized"] == 1


def test_stornomeldung_nennt_den_sitzungstag_in_ortszeit(tenant: SessionTenant, gremium: SessionOrganization) -> None:
    kaemmerei = _nutzer(tenant, "kaemmerei", "view_dashboard", "manage_allowances")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Nachtsitzung", organization=gremium, start=datetime(2025, 12, 31, 23, 30, tzinfo=UTC)
    )
    anwesenheit = SessionAttendance.objects.create(meeting=sitzung, person=_person(tenant, "Eule"), status="present")
    position = SessionAllowance.objects.create(attendance=anwesenheit, amount=Decimal("35.00"))
    antwort = _client(kaemmerei).post(_url(tenant, f"/allowances/{position.pk}/cancel/"))
    assert any("(01.01.2026) storniert" in text for text in _meldungen(antwort))


# =============================================================================
# 4. Personen-IBAN und -BIC
# =============================================================================


@pytest.mark.parametrize(
    ("iban", "hinweis"),
    [("DE00 1234", "falsches Format"), ("DE88 3704 0044 0532 0130 00", "Prüfziffer stimmt nicht")],
)
def test_personen_iban_meldet_format_oder_pruefziffer(
    tenant: SessionTenant, admin: Client, iban: str, hinweis: str
) -> None:
    person = _person(tenant, "Amberg")
    antwort = admin.post(_url(tenant, f"/persons/{person.pk}/edit/"), _personendaten(bank_iban=iban))
    assert antwort.status_code == 200
    fehler = antwort.context["form"].errors["bank_iban"][0]
    assert fehler.startswith("Die IBAN ist ungültig")
    assert hinweis in fehler
    assert hinweis in antwort.content.decode()


def test_personen_bic_wird_geprueft_und_angezeigt(tenant: SessionTenant, admin: Client) -> None:
    person = _person(tenant, "Amberg")
    antwort = admin.post(
        _url(tenant, f"/persons/{person.pk}/edit/"), _personendaten(bank_iban="DE89370400440532013000", bank_bic="?")
    )
    assert antwort.status_code == 200
    assert "bank_bic" in antwort.context["form"].errors
    assert "Die BIC ist ungültig" in antwort.content.decode()
    person.refresh_from_db()
    assert cast(Any, person).get_bank_bic_decrypted() == ""


def test_personen_bankdaten_werden_einheitlich_gespeichert(tenant: SessionTenant, admin: Client) -> None:
    person = _person(tenant, "Amberg")
    antwort = admin.post(
        _url(tenant, f"/persons/{person.pk}/edit/"),
        _personendaten(bank_iban="de89 3704 0044 0532 0130 00", bank_bic="coba de ff xxx"),
    )
    assert antwort.status_code == 302
    person.refresh_from_db()
    assert cast(Any, person).get_bank_iban_decrypted() == "DE89370400440532013000"
    assert cast(Any, person).get_bank_bic_decrypted() == "COBADEFFXXX"


def test_iban_pruefung_ist_dieselbe_wie_beim_auftraggeberkonto() -> None:
    assert allowance_service.iban_problem("DE89 3704 0044 0532 0130 00") == ""
    assert allowance_service.valid_iban("DE89370400440532013000")
    assert not allowance_service.valid_iban("DE88370400440532013000")
    assert "Format" in allowance_service.iban_problem("xx<>&")


# =============================================================================
# 5. Dashboard für Kontrollrollen
# =============================================================================


def _kontrollrolle(tenant: SessionTenant, schluessel: str = "revision") -> SessionUser:
    """Konto mit der Standardrolle „Revision“ bzw. „Datenschutz“ (nur Protokoll, keine Fachrechte)."""
    rolle = SessionRole.objects.create(tenant=tenant, is_system_role=True, **SessionRole.CONTROL_ROLES[schluessel])
    user = cast(User, cast(Any, UserFactory)(email=f"{schluessel}@{tenant.slug}.example.org"))
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(rolle)
    return session_user


@pytest.mark.parametrize("schluessel", ["revision", "privacy"])
def test_dashboard_der_kontrollrolle_zeigt_protokollstand_und_wege(tenant: SessionTenant, schluessel: str) -> None:
    pruefer = _kontrollrolle(tenant, schluessel)
    SessionPaper.objects.create(tenant=tenant, name="Vorlage", is_public=True)  # erzeugt einen Protokolleintrag
    assert SessionAuditLog.objects.filter(tenant=tenant).exists()
    antwort = _client(pruefer).get(_url(tenant, "/"))
    assert antwort.status_code == 200
    seite = antwort.content.decode()
    assert 'data-testid="dashboard-protokollkontrolle"' in seite
    assert antwort.context["audit_summary"]["recent"] >= 1
    assert "Letzte Kettenprüfung" in seite
    assert "Export und Kettenprüfung" in seite
    assert f'href="{_url(tenant, "/audit/")}"' in seite
    assert "Funktionstrennung" in seite
    # Keine Fachinhalte, keine neuen Rechte: Sitzungen und Vorlagen bleiben gesperrt
    assert not antwort.context["can_view_meetings"]
    assert _client(pruefer).get(_url(tenant, "/meetings/")).status_code == 403


def test_kontrollkarte_im_audit_log_ohne_fremden_kopfinhalt(tenant: SessionTenant) -> None:
    """Der Kopf der Karte „Export und Manipulationsschutz“ (Sprungziel vom Dashboard) zeigt nur Titel und Text."""
    seite = _client(_kontrollrolle(tenant)).get(_url(tenant, "/audit/")).content.decode()
    kopf = seite.split('id="kontrolle"', 1)[1].split("</header>", 1)[0]
    assert "Export und Manipulationsschutz" in kopf
    # Vorher landete die Aktionsliste des Filters (Kontextvariable „actions“) im Kopf-Slot der Karte
    assert "Erstellt" not in kopf
    assert "Erstellt" in seite  # weiterhin im Aktionsfilter


def test_dashboard_ohne_exportrecht_zeigt_keine_kettenpruefung(tenant: SessionTenant) -> None:
    leser = _nutzer(tenant, "protokollleser", "view_dashboard", "view_audit_log")
    seite = _client(leser).get(_url(tenant, "/")).content.decode()
    assert 'data-testid="dashboard-protokollkontrolle"' in seite
    assert "Export und Kettenprüfung" not in seite
    assert "Letzte Kettenprüfung" not in seite


def test_dashboard_weist_rollen_ohne_fachkacheln_den_weg(tenant: SessionTenant) -> None:
    kaemmerei = _nutzer(tenant, "kaemmerei", "view_dashboard", "manage_allowances")
    antwort = _client(kaemmerei).get(_url(tenant, "/"))
    seite = antwort.content.decode()
    assert 'data-testid="dashboard-bereiche"' in seite
    assert [bereich["label"] for bereich in antwort.context["role_areas"]] == ["Sitzungsgelder"]
    assert 'data-testid="dashboard-protokollkontrolle"' not in seite
    # Nur Bereiche, die die Rolle öffnen darf
    assert _client(kaemmerei).get(antwort.context["role_areas"][0]["url"]).status_code == 200
    assert _client(kaemmerei).get(_url(tenant, "/audit/")).status_code == 403


def test_dashboard_ohne_jeden_bereich_nennt_den_grund(tenant: SessionTenant) -> None:
    nur_dashboard = _nutzer(tenant, "nur-dashboard", "view_dashboard")
    seite = _client(nur_dashboard).get(_url(tenant, "/")).content.decode()
    assert "Noch keine Bereiche" in seite


def test_dashboard_mit_fachrechten_bleibt_wie_bisher(tenant: SessionTenant, admin: Client) -> None:
    antwort = admin.get(_url(tenant, "/"))
    assert "without_content_tiles" not in antwort.context
    assert 'data-testid="dashboard-bereiche"' not in antwort.content.decode()


# =============================================================================
# 6. Nullbyte im Suchbegriff (Serverfehler nur auf PostgreSQL)
# =============================================================================


def test_parameterhelfer_entfernen_nullbytes() -> None:
    assert text_param(" Haus\x00halt ") == "Haushalt"
    assert text_param(None) == ""
    assert text_param("abcdef", max_length=3) == "abc"
    sauber = QueryDict("q=Haushalt&kind=papers")
    assert without_nul(sauber) is sauber
    bereinigt = without_nul(QueryDict("q=Haus%00halt&kind=pa%00pers&kind=files&x%00=1"))
    assert bereinigt["q"] == "Haushalt"
    assert bereinigt.getlist("kind") == ["papers", "files"]
    assert "x" in bereinigt
    assert not bereinigt._mutable


def test_suche_entfernt_nullbyte_vor_der_abfrage(tenant: SessionTenant) -> None:
    SessionPaper.objects.create(tenant=tenant, name="Haushaltssatzung 2027", is_public=True)
    leser = _nutzer(tenant, "leser", "view_dashboard", "view_papers")
    antwort = _client(leser).get(_url(tenant, "/search/"), {"q": "Haus\x00halt"})
    assert antwort.status_code == 200
    assert antwort.context["query"] == "Haushalt"
    assert [p.name for p in antwort.context["results"]["papers"]] == ["Haushaltssatzung 2027"]


def test_listenfilter_entfernen_nullbyte(tenant: SessionTenant) -> None:
    SessionPaper.objects.create(tenant=tenant, name="Haushaltssatzung 2027", is_public=True)
    leser = _nutzer(tenant, "leser", "view_dashboard", "view_papers")
    antwort = _client(leser).get(_url(tenant, "/papers/"), {"q": "Haus\x00halt"})
    assert antwort.status_code == 200
    assert antwort.wsgi_request.GET["q"] == "Haushalt"
    assert [p.name for p in antwort.context["papers"]] == ["Haushaltssatzung 2027"]


def test_leitstellen_suche_entfernt_nullbyte(tenant: SessionTenant) -> None:
    gruppe = SessionTenantGroup.objects.create(name="Bezirke", slug="bezirke")
    SessionTenantGroupTenant.objects.create(group=gruppe, tenant=tenant)
    user = cast(User, cast(Any, UserFactory)(email="leitstelle@example.org"))
    SessionTenantGroupMembership.objects.create(group=gruppe, user=user)
    SessionPaper.objects.create(tenant=tenant, name="Feuerwehrbedarfsplan", is_public=True, status="review")
    assert leitstelle_service.normalize_query("Feuer\x00wehr") == "Feuerwehr"
    client = Client()
    client.force_login(user)
    antwort = client.get("/session/leitstelle/bezirke/suche/", {"q": "Feuer\x00wehr"})
    assert antwort.status_code == 200
    assert antwort.context["query"] == "Feuerwehr"
    assert "\x00" not in antwort.context["raw_query"]
    assert [row.title for row in antwort.context["result"].papers] == ["Feuerwehrbedarfsplan"]
