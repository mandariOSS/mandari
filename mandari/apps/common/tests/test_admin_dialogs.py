# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Ansichten im Dialog dürfen von der eigenen Herkunft eingebettet werden – und nur sie (Issue #686).

django-unfold zeigt ab 0.107 Bezugsobjekte in einem Dialog mit ``<iframe>``. Die Löschbestätigung
bekäme ohne die Middleware ``X-Frame-Options: DENY`` und bliebe leer.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.admin.options import IS_POPUP_VAR
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.common.admin_dialogs import AdminDialogFrameMiddleware
from apps.common.tests.factories import UserFactory
from apps.tenants.models import PartyGroup

pytestmark = pytest.mark.django_db

#: Anfrageparameter, mit dem der Admin eine Ansicht als Dialog aufruft
DIALOG: dict[str, str] = {IS_POPUP_VAR: "1"}


@pytest.fixture
def betreiber_client() -> Client:
    client = Client()
    client.force_login(cast(Any, UserFactory)(email="betreiber@example.org", is_staff=True, is_superuser=True))
    return client


@pytest.fixture
def gruppe() -> PartyGroup:
    return PartyGroup.objects.create(name="Landesverband", slug="landesverband")


def test_middleware_steht_nach_der_voreinstellung() -> None:
    # Antworten laufen rückwärts durch den Stapel: Nur so ist der Kopf gesetzt, bevor die Voreinstellung greift.
    mw = settings.MIDDLEWARE
    assert mw.index("apps.common.admin_dialogs.AdminDialogFrameMiddleware") > mw.index(
        "django.middleware.clickjacking.XFrameOptionsMiddleware"
    )


def test_loeschbestaetigung_im_dialog_ist_einbettbar(betreiber_client: Client, gruppe: PartyGroup) -> None:
    url = reverse("admin:tenants_partygroup_delete", args=[gruppe.pk])

    response = betreiber_client.get(url, DIALOG)

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_antwort_nach_dem_loeschen_im_dialog_ist_einbettbar(betreiber_client: Client, gruppe: PartyGroup) -> None:
    """Die Bestätigung sendet ``_popup`` als Formularfeld; die Antwort schließt den Dialog per Skript."""
    url = reverse("admin:tenants_partygroup_delete", args=[gruppe.pk])

    response = betreiber_client.post(url, {"post": "yes", **DIALOG})

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert not PartyGroup.objects.filter(pk=gruppe.pk).exists()


def test_admin_ohne_dialog_behaelt_die_voreinstellung(betreiber_client: Client, gruppe: PartyGroup) -> None:
    response = betreiber_client.get(reverse("admin:tenants_partygroup_delete", args=[gruppe.pk]))

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("pfad", ["/accounts/login/", "/"])
def test_seiten_ausserhalb_des_admin_bleiben_gesperrt(client: Client, pfad: str) -> None:
    # Der Parameter allein öffnet nichts: Nur Admin-Pfade sind gemeint.
    response = client.get(pfad, DIALOG)

    assert response.headers["X-Frame-Options"] == "DENY"


def test_vorhandener_kopf_bleibt_stehen() -> None:
    """Eine Ansicht, die das Einbetten selbst regelt, wird nicht überschrieben."""

    def ansicht(request: HttpRequest) -> HttpResponse:
        response = HttpResponse("ok")
        response["X-Frame-Options"] = "DENY"
        return response

    request = RequestFactory().get("/admin/tenants/partygroup/", DIALOG)

    assert AdminDialogFrameMiddleware(ansicht)(request)["X-Frame-Options"] == "DENY"
