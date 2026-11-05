# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verweise auf „Verbundene Anwendungen“ (Issue #1215).

Konto- und Profilseiten in Work (Personenmenü, Seite Sicherheit), Session (Personenmenü, Blatt „Mehr“) und Insight
(„Für Sie“, Blatt „Mehr“) verweisen nur bei eingeschaltetem Anbieter auf die Seite, in beiden Erscheinungsbildern.
Die Seite selbst führt über ``next`` zurück, aber nur auf die eigene Herkunft.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests import oidc_hilfen as h
from apps.common.tests.factories import UserFactory
from apps.session.models import SessionRole, SessionTenant, SessionUser
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db


def _ziel() -> str:
    return reverse("accounts:connected_applications")


def _verweise(html: str) -> int:
    """Verweise auf die Seite, jeweils mit Rückweg (``next``)."""
    return html.count(f'href="{_ziel()}?next=')


def _seite(client: Client, url: str) -> str:
    antwort = client.get(url)
    assert antwort.status_code == 200, antwort.status_code
    return str(antwort.content.decode())


@pytest.fixture
def work(org: Organization, make_member: Any, client_for: Any) -> tuple[Client, str]:
    mitglied = make_member(org, ["dashboard.view"], email="mitglied@example.org")
    return client_for(mitglied.user), reverse("work:security", kwargs={"org_slug": org.slug})


class TestWork:
    @pytest.mark.parametrize("neues_design", [False, True])
    def test_eingeschaltet_im_menue_und_auf_der_seite_sicherheit(
        self, org: Organization, work: tuple[Client, str], neues_design: bool
    ) -> None:
        org.work_new_design = neues_design
        org.save(update_fields=["work_new_design"])
        client, url = work
        with h.eingeschaltet():
            html = _seite(client, url)
        assert 'data-testid="sicherheit-anwendungen"' in html
        # Personenmenü (neu) bzw. Benutzermenü im Kopf (bisher) und der Abschnitt auf der Seite
        assert _verweise(html) == 2
        assert f'href="{_ziel()}?next={url}"' in html

    @pytest.mark.parametrize("neues_design", [False, True])
    def test_ausgeschaltet_kein_verweis(self, org: Organization, work: tuple[Client, str], neues_design: bool) -> None:
        org.work_new_design = neues_design
        org.save(update_fields=["work_new_design"])
        client, url = work
        html = _seite(client, url)
        assert _ziel() not in html
        assert 'data-testid="sicherheit-anwendungen"' not in html


def _session(neues_design: bool) -> tuple[Client, str]:
    tenant = SessionTenant.objects.create(name="Stadt Verweis", slug="verweis", session_new_design=neues_design)
    SessionRole.create_default_roles(tenant)
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(SessionRole.objects.get(tenant=tenant, name="Administrator"))
    client = Client()
    client.force_login(user)
    return client, reverse("session:dashboard", kwargs={"tenant_slug": tenant.slug})


class TestSession:
    @pytest.mark.parametrize(("neues_design", "anzahl"), [(False, 1), (True, 2)])
    def test_eingeschaltet_im_personenmenue(self, neues_design: bool, anzahl: int) -> None:
        client, url = _session(neues_design)
        with h.eingeschaltet():
            html = _seite(client, url)
        # Bisher: Benutzermenü im Kopf; neu: Personenmenü der Leiste und Blatt „Mehr“
        assert _verweise(html) == anzahl

    @pytest.mark.parametrize("neues_design", [False, True])
    def test_ausgeschaltet_kein_verweis(self, neues_design: bool) -> None:
        client, url = _session(neues_design)
        assert _ziel() not in _seite(client, url)


class TestInsight:
    URL = "/insight/gespeichert/"

    def test_eingeschaltet_unter_fuer_sie_und_im_blatt(self, client_for: Any) -> None:
        user = cast(User, cast(Any, UserFactory)())
        with h.eingeschaltet():
            html = _seite(client_for(user), self.URL)
        assert _verweise(html) == 2

    def test_ausgeschaltet_kein_verweis(self, client_for: Any) -> None:
        user = cast(User, cast(Any, UserFactory)())
        assert _ziel() not in _seite(client_for(user), self.URL)

    def test_ohne_anmeldung_kein_verweis(self) -> None:
        with h.eingeschaltet():
            assert _ziel() not in _seite(Client(), self.URL)


class TestRueckweg:
    def test_zurueck_zur_herkunft(self, client_for: Any) -> None:
        user = cast(User, cast(Any, UserFactory)())
        client = client_for(user)
        with h.eingeschaltet():
            html = _seite(client, f"{_ziel()}?next=/work/fraktion/profil/sicherheit/")
        assert 'href="/work/fraktion/profil/sicherheit/"' in html and "Zurück" in html

    def test_fremde_adresse_wird_nicht_uebernommen(self, client_for: Any) -> None:
        user = cast(User, cast(Any, UserFactory)())
        with h.eingeschaltet():
            html = _seite(client_for(user), f"{_ziel()}?next=https://fremd.example.org/")
        assert "fremd.example.org" not in html and "Zur Startseite" in html

    def test_widerruf_behaelt_den_rueckweg(self, client_for: Any) -> None:
        user = cast(User, cast(Any, UserFactory)())
        with h.eingeschaltet():
            antwort = client_for(user).post(_ziel(), {"application_id": "1", "next": "/insight/gespeichert/"})
        assert antwort.status_code == 302
        assert antwort["Location"] == f"{_ziel()}?next=%2Finsight%2Fgespeichert%2F"
