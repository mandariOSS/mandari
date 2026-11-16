# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Organisationen mit Laufzeit (Issue #1406).

Die Laufzeit beginnt mit der ersten angenommenen Einladung. Danach sperrt der Zeitplan ``demo_ablauf`` die
Organisation (nur ``is_active``, alle Daten bleiben). Konten, die nur noch über die abgelaufene Demo Zugang hätten,
können sich nicht mehr anmelden und sehen „Demozeit abgelaufen“; Work leitet angemeldete Personen auf den Hinweis.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests import factories
from apps.common.tests.factories import DEFAULT_PASSWORD
from apps.tenants import demo
from apps.tenants.models import Membership, Organization, UserInvitation
from apps.work.organization import services

pytestmark = pytest.mark.django_db

#: Fabriken sind untypisiert (factory_boy)
fabrik: Any = factories

TAG = timedelta(days=1)


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def demo_org(db: Any) -> Organization:
    org: Organization = fabrik.OrganizationFactory(name="Fraktion Demo", slug="fraktion-demo")
    org.demo_laufzeit_tage = 14
    org.save(update_fields=["demo_laufzeit_tage"])
    return org


@pytest.fixture
def mitglied(demo_org: Organization, make_member: Any) -> Membership:
    membership: Membership = make_member(demo_org, ["dashboard.view"], email="demo@example.org")
    return membership


def abgelaufen(org: Organization, *, vor: timedelta = TAG) -> Organization:
    org.demo_endet_am = timezone.now() - vor
    org.save(update_fields=["demo_endet_am"])
    return org


def ziel(antwort: Any) -> str:
    """Ziel einer Umleitung."""
    return str(antwort["Location"])


def anmelden(email: str) -> Any:
    return Client().post(reverse("accounts:login"), {"email": email, "password": DEFAULT_PASSWORD})


# ---------------------------------------------------------------------------
# Laufzeit beginnt mit der ersten angenommenen Einladung
# ---------------------------------------------------------------------------


def test_erste_angenommene_einladung_startet_die_laufzeit(demo_org: Organization) -> None:
    admin = fabrik.UserFactory(email="admin@example.org", is_superuser=True)
    einladung = UserInvitation.create_for_organization(organization=demo_org, email="neu@example.org", invited_by=admin)
    person = fabrik.UserFactory(email="neu@example.org")
    vorher = timezone.now()

    services.accept_invitation(einladung, person)

    demo_org.refresh_from_db()
    assert demo_org.demo_endet_am is not None
    assert vorher + 14 * TAG <= demo_org.demo_endet_am <= timezone.now() + 14 * TAG

    # Eine zweite Annahme verschiebt das Ende nicht
    ende = demo_org.demo_endet_am
    zweite = UserInvitation.create_for_organization(organization=demo_org, email="zwei@example.org", invited_by=admin)
    services.accept_invitation(zweite, fabrik.UserFactory(email="zwei@example.org"))
    demo_org.refresh_from_db()
    assert demo_org.demo_endet_am == ende


def test_annahme_als_schon_bestehendes_mitglied_startet_die_laufzeit(
    demo_org: Organization, mitglied: Membership
) -> None:
    """Etwa ein Gast, der danach seine offene Mitglieds-Einladung annimmt (Hinweis aus der Prüfung von #1413)."""
    admin = fabrik.UserFactory(email="admin@example.org", is_superuser=True)
    einladung = UserInvitation.create_for_organization(
        organization=demo_org, email=mitglied.user.email, invited_by=admin
    )

    services.accept_invitation(einladung, mitglied.user)

    demo_org.refresh_from_db()
    assert demo_org.demo_endet_am is not None


def test_ohne_laufzeit_keine_demo(org: Organization) -> None:
    assert demo.laufzeit_starten(org) is False
    org.refresh_from_db()
    assert org.demo_endet_am is None


def test_bereitgestellte_organisation_ist_nie_demo(demo_org: Organization) -> None:
    demo_org.provisioning_ref = "BS-1"
    demo_org.save(update_fields=["provisioning_ref"])
    assert demo.laufzeit_starten(demo_org) is False
    abgelaufen(demo_org)
    assert demo.ist_abgelaufen(demo_org) is False
    assert demo.abgelaufene_sperren() == []


# ---------------------------------------------------------------------------
# Zeitplan sperrt, Daten bleiben
# ---------------------------------------------------------------------------


def test_zeitplan_sperrt_nur_abgelaufene_demos(demo_org: Organization, mitglied: Membership, org: Organization) -> None:
    laufend: Organization = fabrik.OrganizationFactory(name="Laufend", slug="laufend")
    laufend.demo_laufzeit_tage = 14
    laufend.demo_endet_am = timezone.now() + TAG
    laufend.save()
    abgelaufen(demo_org)

    ausgabe = StringIO()
    call_command("demo_ablauf", "--dry-run", stdout=ausgabe)
    assert "würde sperren: fraktion-demo" in ausgabe.getvalue()
    demo_org.refresh_from_db()
    assert demo_org.is_active is True

    call_command("demo_ablauf", "--trotz-zeitplan", stdout=StringIO())

    demo_org.refresh_from_db()
    laufend.refresh_from_db()
    org.refresh_from_db()
    assert demo_org.is_active is False
    assert laufend.is_active is True and org.is_active is True
    # Organisation und Mitgliedschaft bestehen weiter
    mitglied.refresh_from_db()
    assert mitglied.is_active is True and Organization.objects.filter(pk=demo_org.pk).exists()


# ---------------------------------------------------------------------------
# Anmeldung und Hinweis
# ---------------------------------------------------------------------------


def test_anmeldung_gesperrt_mit_hinweis(demo_org: Organization, mitglied: Membership) -> None:
    abgelaufen(demo_org)
    demo.abgelaufene_sperren()

    antwort = anmelden("demo@example.org")

    assert antwort.status_code == 200
    inhalt = antwort.content.decode()
    assert "Die Demozeit für „Fraktion Demo“ ist am" in inhalt
    assert "Eure Daten bleiben erhalten." in inhalt
    assert "_auth_user_id" not in antwort.wsgi_request.session


def test_falsches_passwort_verraet_keine_demo(demo_org: Organization, mitglied: Membership) -> None:
    abgelaufen(demo_org)
    antwort = Client().post(reverse("accounts:login"), {"email": "demo@example.org", "password": "falsch"})
    assert "Demozeit" not in antwort.content.decode()


@pytest.mark.parametrize("zugang", ["andere_organisation", "staff", "offene_einladung", "weiterer_zugang"])
def test_anmeldung_bleibt_mit_weiterem_zugang(
    zugang: str, demo_org: Organization, mitglied: Membership, org: Organization, make_member: Any, monkeypatch: Any
) -> None:
    abgelaufen(demo_org)
    person = mitglied.user
    if zugang == "andere_organisation":
        rolle = fabrik.RoleFactory(organization=org, permissions=["dashboard.view"])
        fabrik.MembershipFactory(user=person, organization=org, roles=[rolle])
    elif zugang == "staff":
        person.is_staff = True
        person.save(update_fields=["is_staff"])
    elif zugang == "offene_einladung":
        einladend = fabrik.UserFactory(email="einladend@example.org")
        UserInvitation.create_for_organization(organization=org, email=person.email, invited_by=einladend)
    else:
        monkeypatch.setattr(demo, "_weitere_zugaenge", [lambda user: user.pk == person.pk])

    assert demo.abgelaufene_demo_fuer(person) is None


def test_session_meldet_ihren_zugang_an() -> None:
    from apps.session.apps import _hat_session_zugang

    assert _hat_session_zugang in demo._weitere_zugaenge


def test_angemeldete_person_landet_auf_dem_hinweis(demo_org: Organization, mitglied: Membership) -> None:
    client = Client()
    client.force_login(mitglied.user)
    dashboard = reverse("work:dashboard", kwargs={"org_slug": demo_org.slug})

    # Ende erreicht, der Zeitplan hat noch nicht gesperrt
    abgelaufen(demo_org)
    assert ziel(client.get(dashboard)) == reverse("accounts:demo_abgelaufen")
    # Nach der Sperre ebenso, auch über /work/
    demo.abgelaufene_sperren()
    assert ziel(client.get(dashboard)) == reverse("accounts:demo_abgelaufen")
    assert ziel(client.get(reverse("work:root"))) == reverse("accounts:demo_abgelaufen")

    hinweis = client.get(reverse("accounts:demo_abgelaufen"))

    assert hinweis.status_code == 200
    assert "Die Demozeit für „Fraktion Demo“ ist am" in hinweis.content.decode()
    # Die Seite meldet ab
    assert "_auth_user_id" not in client.session


def test_mit_weiterem_zugang_404_statt_hinweis(demo_org: Organization, mitglied: Membership, org: Organization) -> None:
    rolle = fabrik.RoleFactory(organization=org, permissions=["dashboard.view"])
    fabrik.MembershipFactory(user=mitglied.user, organization=org, roles=[rolle])
    abgelaufen(demo_org)
    demo.abgelaufene_sperren()
    client = Client()
    client.force_login(mitglied.user)

    assert client.get(reverse("work:dashboard", kwargs={"org_slug": demo_org.slug})).status_code == 404
    # /work/ führt zur anderen Organisation, der Hinweis zurück zu Work
    assert ziel(client.get(reverse("work:root"))) == reverse("work:dashboard", kwargs={"org_slug": org.slug})
    assert ziel(client.get(reverse("accounts:demo_abgelaufen"))) == reverse("work:root")


def test_hinweis_ohne_anmeldung() -> None:
    antwort = Client().get(reverse("accounts:demo_abgelaufen"))
    assert antwort.status_code == 200
    assert demo.HINWEIS in antwort.content.decode()
