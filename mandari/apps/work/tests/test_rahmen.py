# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild von Work je Organisation (Issue #852).

Geprüft: Der Schalter steht für bestehende und neue Organisationen auf „aus“, dann bleibt der bisherige Rahmen
unverändert; eingeschaltet trägt jede Seite den neuen Rahmen (sieben Bereiche, Brotkrumen, Suche, Leiste unten,
Raum-Dialog). Rechte wie bisher: Gast sieht nur Freigegebenes, Mitglied ohne ``organization.view`` keine
Einstellungen. Alle bisherigen Adressen bleiben gültig und landen im passenden Bereich bzw. Reiter.
"""

from __future__ import annotations

import importlib
import re
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.urls import reverse

from apps.accounts.models import User
from apps.common.tests import factories
from apps.tenants.models import Membership, Organization
from apps.work import rahmen

NEU = 'data-rahmen="neu"'
ALT = 'class="work-sidebar"'


def _url(name: str, org: Organization, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def _org(**kwargs: Any) -> Organization:
    return cast(Organization, cast(Any, factories.OrganizationFactory)(**kwargs))


def _mitglied(org: Organization, email: str, is_guest: bool = False, admin: bool = False) -> Membership:
    """Mitgliedschaft (mit Administratorrolle, wenn ``admin``) für ein Konto mit dieser Adresse.

    Ein vorhandenes Konto wird direkt aus der Datenbank genommen: Die Fabrik setzte sonst das Passwort neu, und eine
    schon angemeldete Sitzung wäre ungültig.
    """
    user = User.objects.filter(email=email).first() or cast(Any, factories.UserFactory)(email=email)
    rollen = [cast(Any, factories.RoleFactory)(organization=org, is_admin=True)] if admin else []
    return cast(
        Membership, cast(Any, factories.MembershipFactory)(user=user, organization=org, is_guest=is_guest, roles=rollen)
    )


def _html(response: Any) -> str:
    assert response.status_code == 200, response.status_code
    return str(response.content.decode())


def _aktiver_eintrag(html: str) -> list[str]:
    """Beschriftungen aller Einträge mit aria-current (Leiste, Unterpunkte, Reiter, Leiste unten, Brotkrumen)."""
    return re.findall(r'aria-current="(?:page|true)"[^>]*>\s*(?:<i[^>]*></i>\s*)?(?:<span[^>]*>)?\s*([^<]+?)\s*<', html)


@pytest.fixture
def neu(org: Organization) -> Organization:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def admin(org: Organization, make_member: Any) -> Any:
    return make_member(org, [], email="vorsitz@example.org", is_admin=True)


# ---- Schalter ------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_schalter_steht_standardmaessig_aus() -> None:
    org = _org(name="Neue Fraktion")
    org.refresh_from_db()
    assert org.work_new_design is False
    assert rahmen.neues_design(org) is False
    assert rahmen.neues_design(None) is False


@pytest.mark.django_db
def test_ohne_schalter_bleibt_der_bisherige_rahmen(org: Organization, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("dashboard", org)))
    assert ALT in html
    assert NEU not in html


@pytest.mark.django_db
def test_mit_schalter_traegt_die_seite_den_neuen_rahmen(neu: Organization, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("dashboard", neu)))
    assert NEU in html
    assert ALT not in html
    # sieben Bereiche in der Seitenleiste (Fraktionssitzungen eigener Eintrag, Entscheidung vom 06.10.2026)
    for label in ("Start", "Sitzungen", "Fraktionssitzungen", "Dokumente", "Aufgaben", "Team", "Recherche"):
        assert f">{label}</span>" in html
    assert 'aria-label="Brotkrumen"' in html
    # Suche in der Kopfzeile führt in die Recherche, Strg+K als Tastenkürzel angekündigt
    assert f'action="{_url("ris_search", neu)}"' in html
    assert 'aria-keyshortcuts="Control+K"' in html
    # Leiste unten am Handy mit „Mehr“ und Raum-Dialog
    assert 'aria-label="Hauptbereiche"' in html
    assert 'id="work-mehr"' in html and 'role="dialog"' in html
    assert 'id="raum-dialog"' in html
    # Seiten-Blöcke kommen an
    assert "<title>" in html and "| Fraktion Test</title>" in html


@pytest.mark.django_db
def test_schalter_wirkt_nur_fuer_die_eigene_organisation(neu: Organization, client_for: Any) -> None:
    andere = _org(name="Andere Fraktion")
    person = _mitglied(neu, "beide@example.org", admin=True).user
    _mitglied(andere, "beide@example.org", admin=True)
    client = client_for(person)
    assert NEU in _html(client.get(_url("dashboard", neu)))
    html_andere = _html(client.get(_url("dashboard", andere)))
    assert NEU not in html_andere and ALT in html_andere


# ---- Rechte wie bisher ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_einstellungen_nur_mit_recht(neu: Organization, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(neu, ["dashboard.view"], email="mitglied@example.org")
    html = _html(client_for(mitglied.user).get(_url("dashboard", neu)))
    assert f'href="{_url("organization", neu)}"' not in html
    assert f'href="{_url("support", neu)}"' in html

    leitung = make_member(neu, ["dashboard.view", "organization.view"], email="leitung@example.org")
    html = _html(client_for(leitung.user).get(_url("dashboard", neu)))
    assert f'href="{_url("organization", neu)}"' in html


@pytest.mark.django_db
def test_gast_sieht_nur_freigaben(neu: Organization, client_for: Any) -> None:
    gast = _mitglied(neu, "gast@example.org", is_guest=True)
    client = client_for(gast.user)
    # Gäste landen weiter auf ihrer Übersicht
    antwort = client.get(_url("dashboard", neu))
    assert antwort.status_code == 302 and antwort["Location"] == _url("guest_documents", neu)
    html = _html(client.get(_url("guest_documents", neu)))
    assert NEU in html
    assert ">Freigegebene Dokumente</span>" in html
    for verboten in ("ris_search", "ris_overview", "tasks", "team", "meetings", "organization"):
        assert f'href="{_url(verboten, neu)}"' not in html, verboten
    assert 'id="kopf-suche"' not in html


# ---- Bisherige Adressen und Bereiche -----------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "bereich", "reiter"),
    [
        ("meetings", "Sitzungen", "Für mich"),
        ("ris_meetings", "Sitzungen", "Alle Gremien"),
        ("ris_overview", "Recherche", "Übersicht"),
        ("ris_search", "Recherche", "Suche"),
        ("ris_papers", "Recherche", "Vorgänge"),
        ("ris_decisions", "Recherche", "Beschlüsse"),
        ("ris_organizations", "Recherche", "Gremien"),
        ("ris_persons", "Recherche", "Personen"),
        ("ris_files", "Recherche", "Dokumente"),
        ("ris_map", "Recherche", "Karte"),
    ],
)
def test_bisherige_adressen_bleiben_und_zeigen_bereich_und_reiter(
    neu: Organization, admin: Any, client_for: Any, name: str, bereich: str, reiter: str
) -> None:
    html = _html(client_for(admin.user).get(_url(name, neu)))
    aktiv = _aktiver_eintrag(html)
    assert bereich in aktiv, aktiv
    assert reiter in aktiv, aktiv


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "bereich"),
    [
        ("dashboard", "Start"),
        ("faction", "Fraktionssitzungen"),
        ("documents", "Dokumente"),
        ("tasks", "Aufgaben"),
        ("team", "Team"),
        ("support", "Hilfe und Support"),
        ("organization", "Einstellungen"),
    ],
)
def test_bereiche_der_seitenleiste(neu: Organization, admin: Any, client_for: Any, name: str, bereich: str) -> None:
    html = _html(client_for(admin.user).get(_url(name, neu)))
    assert bereich in _aktiver_eintrag(html)


@pytest.mark.django_db
def test_brotkrumen_raum_bereich_reiter(neu: Organization, admin: Any, client_for: Any) -> None:
    html = _html(client_for(admin.user).get(_url("ris_papers", neu)))
    krumen = html.split('aria-label="Brotkrumen"', 1)[1].split("</nav>", 1)[0]
    assert f'href="{_url("dashboard", neu)}"' in krumen and "Fraktion Test" in krumen
    assert f'href="{_url("ris_overview", neu)}"' in krumen
    assert re.search(r'aria-current="page"[^>]*>Vorgänge<', krumen)


@pytest.mark.django_db
def test_raum_dialog_listet_alle_organisationen(neu: Organization, admin: Any, client_for: Any) -> None:
    zweite = _org(name="Kreistagsfraktion Muster")
    _mitglied(zweite, admin.user.email, is_guest=True)
    html = _html(client_for(admin.user).get(_url("dashboard", neu)))
    dialog = html.split('id="raum-dialog"', 1)[1]
    assert f'href="{_url("dashboard", zweite)}"' in dialog
    assert "Kreistagsfraktion Muster" in dialog and "Gastzugang" in dialog
    assert "Geöffnet" in dialog


# ---- Hilfsfunktionen -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url_name", "erwartet"),
    [
        ("dashboard", ("start", None, None)),
        ("meeting_prepare", ("sitzungen", "fuer_mich", None)),
        ("faction_detail", ("fraktionssitzungen", None, None)),
        ("ris_meeting_detail", ("sitzungen", "alle_gremien", None)),
        ("meetings_calendar", ("sitzungen", None, "Kalender")),
        ("document_editor", ("dokumente", None, None)),
        ("ris_paper_detail", ("recherche", "vorgaenge", None)),
        ("members", ("einstellungen", None, None)),
        ("security", ("person", None, None)),
        ("unbekannt", (None, None, None)),
    ],
)
def test_einordnen(url_name: str, erwartet: tuple[str | None, str | None, str | None]) -> None:
    assert rahmen.einordnen(url_name) == erwartet


@pytest.mark.parametrize(
    ("name", "kuerzel"),
    [("Musterfraktion (Demo)", "MD"), ("Fraktion", "FR"), ("Ökologische Liste", "ÖL"), ("", "?")],
)
def test_raum_kuerzel(name: str, kuerzel: str) -> None:
    assert rahmen.raum_kuerzel(name) == kuerzel


def test_raum_farbe_nur_gueltige_werte() -> None:
    class Org:
        effective_primary_color = "#12ab9F"

    assert rahmen.raum_farbe(Org()) == "#12ab9F"

    class Boese:
        effective_primary_color = "red; } body { display:none"

    assert rahmen.raum_farbe(Boese()) == rahmen.STANDARD_FARBE


# ---- Datenmigration: nur die Demo wird eingeschaltet ---------------------------------------------------------


@pytest.mark.django_db
def test_migration_schaltet_nur_die_demo_ein() -> None:
    demo = _org(name="Musterfraktion (Demo)", slug="musterfraktion-demo")
    andere = _org(name="Echte Fraktion")
    vorher = {o.pk: (o.name, o.slug, o.primary_color, o.settings) for o in Organization.objects.all()}

    migration = importlib.import_module("apps.tenants.migrations.0026_work_neues_erscheinungsbild")
    migration.demo_einschalten(django_apps, None)
    migration.demo_einschalten(django_apps, None)  # wiederholbar

    demo.refresh_from_db()
    andere.refresh_from_db()
    assert demo.work_new_design is True
    assert andere.work_new_design is False
    # Bestand sonst unverändert
    assert {o.pk: (o.name, o.slug, o.primary_color, o.settings) for o in Organization.objects.all()} == vorher
