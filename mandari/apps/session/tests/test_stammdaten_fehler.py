# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stammdaten des Session RIS: Gremien, Personen, Besetzungen, Wahlperioden, Rollen, Einstellungen,
Endgeräte und Datenschutz (systematische Fehlersuche, Issue #708).

Geprüft wird je Befund das richtige Verhalten: Plausibilitätsregeln statt Serverfehler, eine gemeinsame
Regel für laufende Besetzungen, vollständige Transaktionen, richtige Zählungen und Fristen.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.common.formatting import format_money, parse_money
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionCircularResolution,
    SessionCircularVote,
    SessionDevice,
    SessionDeviceGrant,
    SessionDeviceLog,
    SessionInvitation,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionPersonMonthlyRate,
    SessionRole,
    SessionTenant,
    SessionUser,
    SessionVote,
)
from apps.session.services import membership_service, privacy_service

pytestmark = pytest.mark.django_db

HEUTE = timezone.localdate()
FLAGS = [field.name for field in SessionRole._meta.concrete_fields if field.name.startswith("can_")]


def _rolle(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionRole:
    flags = {feld: feld[4:] in rechte for feld in FLAGS}
    return SessionRole.objects.create(tenant=tenant, name=name, is_admin=admin, **flags)


def _nutzer(tenant: SessionTenant, name: str, *rechte: str, admin: bool = False) -> SessionUser:
    user = cast(User, cast(Any, UserFactory)(email=f"{name}@example.org"))
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(_rolle(tenant, f"Rolle {name}", *rechte, admin=admin))
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
    return SessionOrganization.objects.create(tenant=tenant, name="Ausschuss für Bauen und Verkehr")


def _person(tenant: SessionTenant, vorname: str, nachname: str, **extra: Any) -> SessionPerson:
    return SessionPerson.objects.create(tenant=tenant, given_name=vorname, family_name=nachname, **extra)


def _besetzung(gremium: SessionOrganization, person: SessionPerson, **extra: Any) -> SessionOrganizationMembership:
    return SessionOrganizationMembership.objects.create(organization=gremium, person=person, **extra)


def _url(tenant: SessionTenant, pfad: str) -> str:
    return f"/session/{tenant.slug}{pfad}"


# ---------------------------------------------------------------------------
# Wahlperioden und Periodenwechsel
# ---------------------------------------------------------------------------


class TestPeriodenwechsel:
    @pytest.fixture
    def periode(self, tenant: SessionTenant) -> SessionLegislativeTerm:
        return SessionLegislativeTerm.objects.create(tenant=tenant, name="22. Wahlperiode", start_date=date(2024, 6, 9))

    def test_stichtag_am_beginn_der_besetzungen_ohne_serverfehler(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization, periode: SessionLegislativeTerm
    ) -> None:
        for i in range(3):
            _besetzung(
                gremium, _person(tenant, "P", f"Person {i}"), start_date=date(2024, 7, 1), legislative_term=periode
            )
        response = admin.post(
            _url(tenant, "/settings/terms/change/"), {"name": "WP Test", "start_date": "2024-07-01", "mode": "carry"}
        )
        assert response.status_code == 302
        neu = SessionLegislativeTerm.objects.get(tenant=tenant, name="WP Test")
        # Die Besetzungen beginnen am Stichtag: Sie gehören zur neuen Periode und bleiben laufend
        laufend = SessionOrganizationMembership.objects.filter(organization=gremium, end_date__isnull=True)
        assert laufend.count() == 3
        assert set(laufend.values_list("legislative_term_id", flat=True)) == {neu.pk}
        periode.refresh_from_db()
        assert periode.end_date == date(2024, 6, 30)

    def test_besetzung_nach_dem_stichtag_endet_nicht_vor_ihrem_beginn(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization, periode: SessionLegislativeTerm
    ) -> None:
        alt = _besetzung(gremium, _person(tenant, "A", "Alt"), start_date=date(2024, 7, 1))
        spaet = _besetzung(gremium, _person(tenant, "S", "Spät"), start_date=date(2026, 10, 1))
        admin.post(
            _url(tenant, "/settings/terms/change/"), {"name": "WP 23", "start_date": "2025-01-01", "mode": "carry"}
        )
        spaet.refresh_from_db()
        assert spaet.end_date is None
        assert spaet.start_date == date(2026, 10, 1)
        alt.refresh_from_db()
        assert alt.end_date == date(2024, 12, 31)
        # Übernommen ab dem Stichtag, keine weitere Kopie der späten Besetzung
        assert SessionOrganizationMembership.objects.filter(organization=gremium, person=alt.person).count() == 2
        assert SessionOrganizationMembership.objects.filter(organization=gremium, person=spaet.person).count() == 1

    def test_fehler_hinterlaesst_keinen_halben_stand(
        self, tenant: SessionTenant, gremium: SessionOrganization, periode: SessionLegislativeTerm
    ) -> None:
        besetzung = _besetzung(gremium, _person(tenant, "A", "Alt"), start_date=date(2024, 7, 1))
        with (
            mock.patch.object(SessionOrganizationMembership.objects, "create", side_effect=IntegrityError),
            pytest.raises(IntegrityError),
        ):
            membership_service.change_term(
                tenant, name="WP 23", number=None, start_date=date(2025, 1, 1), end_date=None, mode="carry"
            )
        periode.refresh_from_db()
        besetzung.refresh_from_db()
        assert periode.end_date is None
        assert besetzung.end_date is None
        assert not SessionLegislativeTerm.objects.filter(name="WP 23").exists()

    def test_stichtag_vor_beginn_der_laufenden_periode_wird_abgelehnt(
        self, tenant: SessionTenant, admin: Client, periode: SessionLegislativeTerm
    ) -> None:
        response = admin.post(
            _url(tenant, "/settings/terms/change/"), {"name": "Zu früh", "start_date": "2024-01-01", "mode": "fresh"}
        )
        assert any("muss nach dem Beginn" in m for m in _meldungen(response))
        periode.refresh_from_db()
        assert periode.end_date is None
        assert not SessionLegislativeTerm.objects.filter(name="Zu früh").exists()

    def test_ueberschneidende_perioden_werden_abgelehnt(
        self, tenant: SessionTenant, admin: Client, periode: SessionLegislativeTerm
    ) -> None:
        response = admin.post(
            _url(tenant, "/settings/terms/save/"),
            {"name": "Überlappend", "start_date": "2025-01-01", "end_date": "2027-12-31"},
        )
        assert any("überschneidet sich" in m for m in _meldungen(response))
        assert not SessionLegislativeTerm.objects.filter(name="Überlappend").exists()
        assert cast(Any, SessionLegislativeTerm).for_date(tenant, date(2026, 10, 1)) == periode

    def test_ungueltige_kennung_beim_speichern(self, tenant: SessionTenant, admin: Client) -> None:
        response = admin.post(_url(tenant, "/settings/terms/save/"), {"name": "X", "term_id": "abc"})
        assert response.status_code == 404
        assert not SessionLegislativeTerm.objects.filter(name="X").exists()


# ---------------------------------------------------------------------------
# Gremien: Liste, Ansicht, laufende Besetzung
# ---------------------------------------------------------------------------


class TestGremien:
    def test_mitgliederzahl_mit_periodenfilter_nicht_vervielfacht(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        periode = SessionLegislativeTerm.objects.create(tenant=tenant, name="WP", start_date=date(2024, 1, 1))
        for i in range(4):
            _besetzung(
                gremium, _person(tenant, "P", f"Person {i}"), start_date=date(2024, 7, 1), legislative_term=periode
            )
        ohne = admin.get(_url(tenant, "/organizations/"))
        mit = admin.get(_url(tenant, f"/organizations/?term={periode.pk}"))
        assert [o.member_count for o in ohne.context["organizations"]] == [4]
        assert [o.member_count for o in mit.context["organizations"]] == [4]

    @pytest.mark.parametrize("wert", ["abc", "-1", "0", "2026-02-30"])
    def test_ansicht_mit_ungueltiger_periode(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization, wert: str
    ) -> None:
        response = admin.get(_url(tenant, f"/organizations/{gremium.pk}/?term={wert}"))
        assert response.status_code == 200
        assert response.context["selected_term"] is None

    def test_laufende_besetzung_wie_in_ladung_und_anwesenheit(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        endet_bald = _besetzung(
            gremium, _person(tenant, "E", "Endet"), start_date=date(2024, 7, 1), end_date=HEUTE + timedelta(days=90)
        )
        inaktiv = _besetzung(gremium, _person(tenant, "I", "Inaktiv", is_active=False), start_date=date(2024, 7, 1))
        kuenftig = _besetzung(gremium, _person(tenant, "K", "Künftig"), start_date=HEUTE + timedelta(days=30))
        response = admin.get(_url(tenant, f"/organizations/{gremium.pk}/"))
        aktive = list(response.context["memberships"])
        assert aktive == [endet_bald]
        assert inaktiv in list(response.context["ended_memberships"])
        assert list(response.context["upcoming_memberships"]) == [kuenftig]
        liste = admin.get(_url(tenant, "/organizations/"))
        assert [o.member_count for o in liste.context["organizations"]] == [1]

    def test_keine_zweite_laufende_besetzung_bei_kuenftigem_ende(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        person = _person(tenant, "E", "Endet")
        _besetzung(gremium, person, start_date=date(2024, 7, 1), end_date=HEUTE + timedelta(days=90))
        response = admin.post(_url(tenant, f"/organizations/{gremium.pk}/memberships/add/"), {"person": str(person.pk)})
        assert response.status_code == 302
        assert SessionOrganizationMembership.objects.filter(organization=gremium, person=person).count() == 1

    def test_vertretung_einer_inaktiven_person_bleibt_beim_speichern(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        vertretene = _person(tenant, "V", "Vertreten", is_active=False)
        besetzung = _besetzung(gremium, _person(tenant, "S", "Stellvertretung"), start_date=date(2024, 7, 1))
        besetzung.substitute_for = vertretene
        besetzung.save()
        seite = admin.get(_url(tenant, f"/organizations/{gremium.pk}/")).content.decode()
        assert f'<option value="{vertretene.pk}" selected>' in seite
        admin.post(
            _url(tenant, f"/memberships/{besetzung.pk}/update/"),
            {"role": "chair", "substitute_for": str(vertretene.pk), "has_voting_rights": "on"},
        )
        besetzung.refresh_from_db()
        assert besetzung.role == "chair"
        assert besetzung.substitute_for == vertretene

    def test_formular_prueft_sitzungsgeld_zeitraum_und_hierarchie(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        kind = SessionOrganization.objects.create(tenant=tenant, name="Unterausschuss", parent=gremium)
        response = admin.post(
            _url(tenant, f"/organizations/{gremium.pk}/edit/"),
            {
                "name": gremium.name,
                "organization_type": "committee",
                "parent": str(kind.pk),
                "invitation_period_days": "7",
                "allowance_amount": "-50",
                "start_date": "2026-01-01",
                "end_date": "2020-01-01",
                "is_active": "on",
            },
        )
        assert response.status_code == 200
        fehler = response.context["form"].errors
        assert {"allowance_amount", "end_date", "parent"} <= set(fehler)
        gremium.refresh_from_db()
        assert gremium.allowance_amount == Decimal("0.00")
        assert gremium.parent is None
        # Das eigene Untergremium wird gar nicht erst als übergeordnet angeboten
        auswahl = admin.get(_url(tenant, f"/organizations/{gremium.pk}/edit/")).context["form"].fields["parent"]
        assert kind not in list(auswahl.queryset)


# ---------------------------------------------------------------------------
# Besetzungen: Aufnahme, Ende, Nachrücken
# ---------------------------------------------------------------------------


class TestBesetzungen:
    def test_erneute_aufnahme_am_selben_tag_ohne_serverfehler(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        person = _person(tenant, "A", "Amberg")
        add = _url(tenant, f"/organizations/{gremium.pk}/memberships/add/")
        admin.post(add, {"person": str(person.pk)})
        besetzung = SessionOrganizationMembership.objects.get(organization=gremium, person=person)
        admin.post(_url(tenant, f"/memberships/{besetzung.pk}/end/"))
        response = admin.post(add, {"person": str(person.pk)})
        assert response.status_code == 302
        assert any("bis einschließlich" in m for m in _meldungen(response))
        # Versehentliches Ende aufheben: Feld „bis“ leeren
        admin.post(
            _url(tenant, f"/memberships/{besetzung.pk}/update/"),
            {"role": "member", "start_date": HEUTE.isoformat(), "end_date": "", "has_voting_rights": "on"},
        )
        besetzung.refresh_from_db()
        assert besetzung.end_date is None

    def test_ende_vor_beginn_wird_abgelehnt(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        besetzung = _besetzung(gremium, _person(tenant, "A", "Amberg"), start_date=date(2024, 7, 1))
        response = admin.post(_url(tenant, f"/memberships/{besetzung.pk}/end/"), {"end_date": "1990-01-01"})
        assert any("vor dem Beginn" in m for m in _meldungen(response))
        besetzung.refresh_from_db()
        assert besetzung.end_date is None

    def test_wahlperiode_folgt_dem_beginn(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        alt = SessionLegislativeTerm.objects.create(
            tenant=tenant, name="WP 21", start_date=date(2019, 1, 1), end_date=date(2024, 6, 8)
        )
        aktuell = SessionLegislativeTerm.objects.create(tenant=tenant, name="WP 22", start_date=date(2024, 6, 9))
        besetzung = _besetzung(
            gremium, _person(tenant, "A", "Amberg"), start_date=date(2024, 7, 1), legislative_term=aktuell
        )
        admin.post(
            _url(tenant, f"/memberships/{besetzung.pk}/update/"),
            {"role": "member", "start_date": "2020-01-01", "has_voting_rights": "on"},
        )
        besetzung.refresh_from_db()
        assert besetzung.start_date == date(2020, 1, 1)
        assert besetzung.legislative_term == alt
        # Beginn außerhalb jeder Periode: keine Periode statt der aktuellen
        person = _person(tenant, "B", "Berg")
        admin.post(
            _url(tenant, f"/organizations/{gremium.pk}/memberships/add/"),
            {"person": str(person.pk), "start_date": "2010-01-01"},
        )
        assert SessionOrganizationMembership.objects.get(person=person).legislative_term is None

    @pytest.mark.parametrize("wert", ["abc", ""])
    def test_ungueltige_person_bei_aufnahme(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization, wert: str
    ) -> None:
        response = admin.post(_url(tenant, f"/organizations/{gremium.pk}/memberships/add/"), {"person": wert})
        assert response.status_code == 302
        assert not SessionOrganizationMembership.objects.exists()

    def test_nachruecken_eines_laufenden_mitglieds_wird_abgelehnt(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        geht = _besetzung(gremium, _person(tenant, "G", "Geht"), start_date=date(2024, 7, 1))
        schon_da = _person(tenant, "S", "Schon da")
        _besetzung(gremium, schon_da, start_date=date(2024, 7, 1))
        seite = admin.get(_url(tenant, f"/organizations/{gremium.pk}/")).context
        assert schon_da.pk in seite["current_member_ids"]
        for stichtag in ("", "2024-07-01"):
            response = admin.post(
                _url(tenant, f"/memberships/{geht.pk}/succession/"),
                {"successor": str(schon_da.pk), "change_date": stichtag},
            )
            assert response.status_code == 302
        geht.refresh_from_db()
        assert geht.end_date is None
        assert SessionOrganizationMembership.objects.filter(organization=gremium, person=schon_da).count() == 1

    def test_nachruecken_ohne_doppelten_sitz(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        geht = _besetzung(gremium, _person(tenant, "G", "Geht"), start_date=date(2024, 7, 1))
        kommt = _person(tenant, "K", "Kommt")
        admin.post(
            _url(tenant, f"/memberships/{geht.pk}/succession/"),
            {"successor": str(kommt.pk), "change_date": "2026-01-01"},
        )
        geht.refresh_from_db()
        neu = SessionOrganizationMembership.objects.get(organization=gremium, person=kommt)
        assert geht.end_date == date(2025, 12, 31)
        assert neu.start_date == date(2026, 1, 1)
        laufend_am_wechsel = SessionOrganizationMembership.objects.filter(organization=gremium).filter(
            membership_service.running_q(date(2026, 1, 1))
        )
        assert list(laufend_am_wechsel) == [neu]

    def test_nachruecken_vor_beginn_wird_abgelehnt(
        self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization
    ) -> None:
        geht = _besetzung(gremium, _person(tenant, "G", "Geht"), start_date=date(2024, 7, 1))
        kommt = _person(tenant, "K", "Kommt")
        admin.post(
            _url(tenant, f"/memberships/{geht.pk}/succession/"),
            {"successor": str(kommt.pk), "change_date": "2024-07-01"},
        )
        geht.refresh_from_db()
        assert geht.end_date is None
        assert not SessionOrganizationMembership.objects.filter(person=kommt).exists()


# ---------------------------------------------------------------------------
# Personen
# ---------------------------------------------------------------------------


class TestPersonen:
    @pytest.mark.parametrize("suche", ["Amberg", "Anna Amberg", "Amberg, Anna", "anna  amberg"])
    def test_suche_wortweise(self, tenant: SessionTenant, admin: Client, suche: str) -> None:
        anna = _person(tenant, "Anna", "Amberg")
        _person(tenant, "Bernd", "Amberg")
        response = admin.get(_url(tenant, "/persons/"), {"q": suche})
        treffer = list(response.context["persons"])
        assert anna in treffer
        if " " in suche.strip() or "," in suche:
            assert treffer == [anna]

    def test_blaetterlinks_kodieren_den_suchbegriff(self, tenant: SessionTenant, admin: Client) -> None:
        for i in range(51):
            _person(tenant, "Anna", f"Müller&Co {i:02d}")
        seite = admin.get(_url(tenant, "/persons/"), {"q": "Müller&Co"}).content.decode()
        assert "q=M%C3%BCller%26Co" in seite
        assert "q=Müller&amp;Co&amp;" not in seite

    def test_formular_prueft_mandat_und_iban(self, tenant: SessionTenant, admin: Client) -> None:
        person = _person(tenant, "Anna", "Amberg")
        daten = {
            "given_name": "Anna",
            "family_name": "Amberg",
            "delivery_channel": "email",
            "is_active": "on",
            "start_date": "2024-07-01",
            "end_date": "2020-01-01",
            "bank_iban": "DE00 1234",
        }
        response = admin.post(_url(tenant, f"/persons/{person.pk}/edit/"), daten)
        assert response.status_code == 200
        assert {"end_date", "bank_iban"} <= set(response.context["form"].errors)
        daten.update(end_date="", bank_iban="de02 1203 0000 0000 2020 51")
        response = admin.post(_url(tenant, f"/persons/{person.pk}/edit/"), daten)
        assert response.status_code == 302
        person.refresh_from_db()
        assert cast(Any, person).get_bank_iban_decrypted() == "DE02120300000000202051"


# ---------------------------------------------------------------------------
# Rollen, Einstellungen, Einladungen
# ---------------------------------------------------------------------------


class TestRollenUndEinstellungen:
    def test_umbenennen_auf_vorhandenen_namen(self, tenant: SessionTenant, admin: Client) -> None:
        rolle = _rolle(tenant, "Kämmerei", "manage_allowances")
        _rolle(tenant, "Datenschutz", "view_audit_log")
        response = admin.post(
            _url(tenant, "/settings/roles/save/"),
            {"role_id": str(rolle.pk), "name": "Datenschutz", "can_manage_allowances": "1"},
        )
        assert response.status_code == 302
        assert any("existiert bereits" in m for m in _meldungen(response))
        rolle.refresh_from_db()
        assert rolle.name == "Kämmerei"

    def test_ungueltige_rollenkennungen(self, tenant: SessionTenant, admin: Client) -> None:
        ziel = _nutzer(tenant, "ziel", "view_meetings")
        vorher = set(ziel.roles.all())
        response = admin.post(_url(tenant, f"/settings/users/{ziel.pk}/roles/"), {"roles": "abc"})
        assert response.status_code == 302
        assert set(ziel.roles.all()) == vorher
        response = admin.post(_url(tenant, "/settings/users/invite/"), {"email": "neu@example.org", "roles": "abc"})
        assert response.status_code == 302
        assert not SessionInvitation.objects.exists()

    def test_benutzerverwaltung_ohne_einstellungsrecht_erreicht_die_uebersicht(self, tenant: SessionTenant) -> None:
        client = _client(_nutzer(tenant, "benutzer", "view_dashboard", "view_meetings", "manage_users"))
        uebersicht = client.get(_url(tenant, "/settings/"))
        assert uebersicht.status_code == 200
        inhalt = uebersicht.content.decode()
        assert _url(tenant, "/settings/users/") in inhalt
        # Bereiche für Einstellungen bleiben verborgen
        assert _url(tenant, "/settings/two-factor/") not in inhalt
        dashboard = client.get(_url(tenant, "/dashboard/")).content.decode()
        assert f'href="{_url(tenant, "/settings/")}"' in dashboard

    def test_kontrollrolle_sieht_keine_stammdaten_links(self, tenant: SessionTenant) -> None:
        client = _client(_nutzer(tenant, "datenschutz", "view_dashboard", "view_audit_log", "export_audit_log"))
        seite = client.get(_url(tenant, "/dashboard/")).content.decode()
        assert _url(tenant, "/organizations/") not in seite
        assert _url(tenant, "/persons/") not in seite
        assert ">Sitzungsdienst<" not in seite
        assert _url(tenant, "/audit/") in seite

    def test_einladung_annehmen_ohne_zweites_konto(self, client: Client, tenant: SessionTenant) -> None:
        cast(Any, UserFactory)(email="Max.Muster@example.org")
        einladung = SessionInvitation.create_for_tenant(tenant, "Max.Muster@example.org")
        token = einladung.plain_token
        response = client.post(
            f"/session/invite/{token}/",
            {"password": "Ein-sehr-langes-Passwort-2026!", "password_confirm": "Ein-sehr-langes-Passwort-2026!"},
        )
        assert response.status_code == 302
        assert User.objects.filter(email__iexact="max.muster@example.org").count() == 1


# ---------------------------------------------------------------------------
# Endgeräte
# ---------------------------------------------------------------------------


class TestEndgeraete:
    @pytest.mark.parametrize(
        ("eingabe", "erwartet"),
        [
            ("1.000", Decimal("1000.00")),
            ("12.345", Decimal("12345.00")),
            ("1.000,50", Decimal("1000.50")),
            ("300,00", Decimal("300.00")),
            ("300.5", Decimal("300.50")),
            ("0.001", None),
            ("1e3", None),
            ("-5", None),
            ("1,234", None),
        ],
    )
    def test_betrag_lesen(self, eingabe: str, erwartet: Decimal | None) -> None:
        assert parse_money(eingabe) == erwartet

    def test_zuschuss_ohne_rundung_und_nie_null(self, tenant: SessionTenant, admin: Client) -> None:
        person = _person(tenant, "Anna", "Amberg")
        url = _url(tenant, "/device-grants/add/")
        response = admin.post(url, {"person": str(person.pk), "amount": "1.000"})
        assert SessionDeviceGrant.objects.get().amount == Decimal("1000.00")
        assert any(format_money(Decimal("1000")) in m for m in _meldungen(response))
        for eingabe in ("0.001", "0,00", "1e3"):
            admin.post(url, {"person": str(person.pk), "amount": eingabe})
        assert SessionDeviceGrant.objects.count() == 1

    def test_statuswechsel(self, tenant: SessionTenant, admin: Client) -> None:
        person = _person(tenant, "Anna", "Amberg")
        geraet = SessionDevice.objects.create(tenant=tenant, label="iPad")

        def aktion(name: str, **daten: str) -> str:
            admin.post(_url(tenant, f"/devices/{geraet.pk}/{name}/"), daten)
            geraet.refresh_from_db()
            return geraet.status

        assert aktion("issue", person=str(person.pk)) == "issued"
        assert aktion("defect") == "defect"
        assert aktion("repair") == "in_stock"
        assert aktion("issue", person=str(person.pk)) == "issued"
        assert aktion("retire") == "retired"
        assert aktion("defect") == "retired"
        assert aktion("repair") == "retired"


# ---------------------------------------------------------------------------
# Datenschutz
# ---------------------------------------------------------------------------


class TestDatenschutz:
    def test_auskunft_vollstaendig(self, tenant: SessionTenant, admin: Client, gremium: SessionOrganization) -> None:
        anna = _person(tenant, "Anna", "Amberg")
        pauschale = SessionMonthlyRate.objects.create(tenant=tenant, name="Teilpauschale", amount=Decimal("250.00"))
        SessionPersonMonthlyRate.objects.create(person=anna, rate=pauschale, start_date=date(2026, 1, 1))
        SessionMonthlyAllowance.objects.create(
            tenant=tenant, person=anna, rate=pauschale, period=date(2026, 9, 1), amount=Decimal("250.00")
        )
        SessionDeviceGrant.objects.create(tenant=tenant, person=anna, amount=Decimal("400.00"))
        geraet = SessionDevice.objects.create(tenant=tenant, label="iPad", status="issued", issued_to=anna)
        SessionDeviceLog.objects.create(device=geraet, action="issued", person=anna)
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, organization=gremium, name="Sitzung", start=timezone.now()
        )
        top = SessionAgendaItem.objects.create(meeting=sitzung, number="N1", name="GEHEIMER-TOP", is_public=False)
        SessionVote.objects.create(agenda_item=top, person=anna, vote="yes")
        umlauf = SessionCircularResolution.objects.create(
            tenant=tenant, organization=gremium, title="Umlauf", resolution_text="X", deadline=HEUTE
        )
        SessionCircularVote.objects.create(circular=umlauf, person=anna, vote="no", received_at=HEUTE)

        response = admin.get(_url(tenant, f"/persons/{anna.pk}/auskunft.json"))
        daten = response.json()
        assert [p["betrag"] for p in daten["monatspauschalen"]] == ["250.00"]
        assert daten["monatspauschalen_zuordnungen"][0]["pauschale"] == "Teilpauschale"
        assert [g["betrag"] for g in daten["endgeraete_zuschuesse"]] == ["400.00"]
        assert daten["endgeraete_ausgegeben"][0]["geraet"] == "iPad"
        assert daten["endgeraete_historie"][0]["aktion"] == "Ausgegeben"
        assert daten["stimmabgaben"][0]["stimme"] == "Ja"
        assert daten["stimmabgaben"][0]["betreff"] == "nichtöffentlich"
        assert daten["umlaufbeschluesse"][0]["stimme"] == "Nein"
        assert "GEHEIMER-TOP" not in response.content.decode()

    def test_dateiname_der_auskunft(self, tenant: SessionTenant, admin: Client) -> None:
        person = _person(tenant, "Zoë", "Łukasiewicz")
        kopf = admin.get(_url(tenant, f"/persons/{person.pk}/auskunft.json"))["Content-Disposition"]
        assert "=?utf-8?" not in kopf
        assert kopf.startswith("attachment;")
        assert "filename*=utf-8''auskunft-%C5%82ukasiewicz-zo%C3%AB.json" in kopf

    def test_frist_nach_kalenderjahren(self, tenant: SessionTenant) -> None:
        tenant.settings = {"privacy": {"persons_years": 10}}
        tenant.save()
        _person(tenant, "Alt", "Mandat", is_active=False, end_date=date(2016, 10, 2), email="alt@example.org")
        zeitzone = timezone.get_current_timezone()
        vorher = privacy_service.run_privacy_purge(tenant, now=datetime(2026, 10, 1, 12, tzinfo=zeitzone), dry_run=True)
        assert vorher["persons_anonymized"] == 0
        faellig = privacy_service.run_privacy_purge(
            tenant, now=datetime(2026, 10, 2, 12, tzinfo=zeitzone), dry_run=True
        )
        assert faellig["persons_anonymized"] == 1

    def test_jahre_zurueck_am_schalttag(self) -> None:
        assert privacy_service.years_before(date(2028, 2, 29), 1) == date(2027, 2, 28)
        assert privacy_service.years_before(date(2026, 10, 1), 10) == date(2016, 10, 1)
