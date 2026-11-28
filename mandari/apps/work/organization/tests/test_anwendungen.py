# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen › Mitglieder und Rechte › Anwendungen (Issue #1591): eigene Anwendungen der Organisation.

- Nur bei eingeschaltetem Anbieter (sonst 404 und kein Eintrag in der Navigation), nur mit „Organisation bearbeiten“.
- Anlegen mit den Scopes für Organisationen (Work, Ratsdaten, Einreichung), nie Session oder Nichtöffentliches; das
  Client-Secret steht nur in dieser Antwort. Die Anwendung trägt die Organisation als Bereich.
- Beide Erscheinungsbilder zeigen die Seite; die Profilseite „Sicherheit“ listet verbundene Anwendungen mit Widerruf.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.accounts.oidc import catalog, registration
from apps.accounts.oidc.models import OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.work.organization import anwendungen, einstellungen

pytestmark = pytest.mark.django_db


@pytest.fixture
def anbieter() -> Iterator[None]:
    with h.eingeschaltet():
        yield


@pytest.fixture
def bearbeitung(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "organization.view", "organization.edit"], email="b@example.org")


def _url(org: Any) -> str:
    return reverse("work:organization_applications", kwargs={"org_slug": org.slug})


def test_ohne_anbieter_nicht_da(org: Any, bearbeitung: Any, client_for: Any) -> None:
    assert client_for(bearbeitung.user).get(_url(org)).status_code == 404
    navi = einstellungen.navigation(org, bearbeitung, "mitglieder")
    assert "Anwendungen" not in [punkt["label"] for punkt in navi["unterpunkte"]]


def test_nur_mit_recht(anbieter: None, org: Any, make_member: Any, client_for: Any) -> None:
    mitglied = make_member(org, ["dashboard.view", "organization.view"], email="m@example.org")
    assert client_for(mitglied.user).get(_url(org)).status_code == 403


@pytest.mark.parametrize("neu", [True, False])
def test_anlegen(anbieter: None, org: Any, bearbeitung: Any, client_for: Any, neu: bool) -> None:
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    client = client_for(bearbeitung.user)
    seite = client.get(_url(org))
    assert seite.status_code == 200
    html = seite.content.decode()
    assert catalog.WORK_LESEN in html and catalog.SESSION_LESEN not in html
    assert catalog.RATSDATEN_NICHTOEFFENTLICH not in html
    antwort = client.post(
        _url(org),
        {
            "aktion": "anlegen",
            "name": "Fraktions-App",
            "art": "oeffentlich",
            "redirect_uris": h.REDIRECT_URI,
            "scopes": [catalog.OPENID, catalog.WORK_LESEN],
        },
    )
    assert antwort.status_code == 200
    app = OAuthApplication.objects.get(name="Fraktions-App")
    assert app.owner == anwendungen.owner(org.pk)
    assert app.client_type == OAuthApplication.CLIENT_PUBLIC
    assert app.client_id in antwort.content.decode()


def test_navigation_mit_anbieter(anbieter: None, org: Any, bearbeitung: Any) -> None:
    navi = einstellungen.navigation(org, bearbeitung, "anwendungen")
    assert "Anwendungen" in [punkt["label"] for punkt in navi["unterpunkte"]]


def test_profilseite_widerruft(anbieter: None, org: Any, bearbeitung: Any, client_for: Any) -> None:
    app = registration.register(
        None,
        anwendungen.owner(org.pk),
        user=bearbeitung.user,
        name="Fraktions-App",
        confidential=True,
        redirect_uris=[h.REDIRECT_URI],
        scopes=[catalog.OPENID, catalog.WORK_LESEN],
    ).application
    h.access_token(app, bearbeitung.user, scope=catalog.WORK_LESEN)
    sicherheit = reverse("work:security", kwargs={"org_slug": org.slug})
    for neu in (True, False):
        org.work_new_design = neu
        org.save(update_fields=["work_new_design"])
        html = client_for(bearbeitung.user).get(sicherheit).content.decode()
        assert 'data-testid="verbundene-liste"' in html and "Fraktions-App" in html, neu
        assert "Registriert von Organisation Fraktion Test" in html
    with override_settings(OIDC_PROVIDER_ENABLED=False):
        assert "verbundene-liste" not in client_for(bearbeitung.user).get(sicherheit).content.decode()
