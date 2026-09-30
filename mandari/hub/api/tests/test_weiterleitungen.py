# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adressen der offenen Schnittstelle: Bestehende bleiben gültig, abweichende Schreibweisen leiten weiter
(Issue #561).

Die Adresse eines Objekts ist seine Kennung. Sie darf sich durch einen Umbau nie ändern; wer die
Schreibweise der jeweils anderen Ausgabe verwendet (mit bzw. ohne Schrägstrich am Ende), wird dauerhaft
weitergeleitet.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client, RequestFactory, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from apps.session.api import oparl as session_ausgabe
from apps.session.models import SessionTenant
from hub.api import aggregator, redirects

# Jede Anfrage prüft die Datenbankverbindung (Middleware), auch wenn der Endpunkt nichts liest
pytestmark = pytest.mark.django_db

KENNUNG = uuid.UUID("5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c")

#: Name der Route -> (Parameter, Adresse, Endpunkt). Diese Adressen sind veröffentlicht.
AGGREGATOR: dict[str, tuple[dict[str, Any], str, Any]] = {
    "oparl_api:root": ({}, "/oparl/", aggregator.root_view),
    "oparl_api:root_v1": ({}, "/oparl/v1/", aggregator.root_view),
    "oparl_api:system": ({}, "/oparl/v1/system", aggregator.system_view),
    "oparl_api:bodies": ({}, "/oparl/v1/bodies", aggregator.bodies_view),
    "oparl_api:body": ({"pk": KENNUNG}, f"/oparl/v1/body/{KENNUNG}", aggregator.object_view),
    "oparl_api:body_list": (
        {"pk": KENNUNG, "segment": "meetings"},
        f"/oparl/v1/body/{KENNUNG}/meetings",
        aggregator.body_sub_list,
    ),
    "oparl_api:object": ({"kind": "paper", "pk": KENNUNG}, f"/oparl/v1/paper/{KENNUNG}", aggregator.object_view),
}
SESSION: dict[str, tuple[dict[str, Any], str, Any]] = {
    "session:oparl_system": ({}, "/session/musterstadt/api/oparl/", session_ausgabe.system_view),
    "session:oparl_bodies": ({}, "/session/musterstadt/api/oparl/bodies/", session_ausgabe.bodies_view),
    "session:oparl_body": ({}, "/session/musterstadt/api/oparl/body/", session_ausgabe.body_view),
    "session:oparl_list": (
        {"segment": "meetings"},
        "/session/musterstadt/api/oparl/meetings/",
        session_ausgabe.list_view,
    ),
    "session:oparl_object": (
        {"kind": "paper", "pk": KENNUNG},
        f"/session/musterstadt/api/oparl/paper/{KENNUNG}/",
        session_ausgabe.object_view,
    ),
    "session:oparl_file_download": (
        {"pk": KENNUNG},
        f"/session/musterstadt/api/oparl/file/{KENNUNG}/download/",
        session_ausgabe.file_download_view,
    ),
}


@pytest.fixture(autouse=True)
def _ohne_ratenbegrenzung() -> Any:
    cache.clear()
    with override_settings(OPARL_API_RATE_LIMIT=0):
        yield
    cache.clear()


# =============================================================================
# Bestehende Adressen bleiben gültig
# =============================================================================


@pytest.mark.parametrize("name", sorted(AGGREGATOR))
def test_adressen_des_aggregators_sind_unveraendert(name: str) -> None:
    parameter, adresse, endpunkt = AGGREGATOR[name]

    assert reverse(name, kwargs=parameter) == adresse
    assert resolve(adresse).func is endpunkt


@pytest.mark.parametrize("name", sorted(SESSION))
def test_adressen_der_session_schnittstelle_sind_unveraendert(name: str) -> None:
    parameter, adresse, endpunkt = SESSION[name]

    assert reverse(name, kwargs={"tenant_slug": "musterstadt", **parameter}) == adresse
    assert resolve(adresse).func is endpunkt


def test_objekttyp_in_anderer_schreibweise_ergibt_denselben_endpunkt() -> None:
    """Groß geschriebene Typen wurden schon immer angenommen; die Kennung in der Antwort ist die gültige."""
    assert resolve(f"/oparl/v1/PAPER/{KENNUNG}").func is aggregator.object_view
    assert resolve(f"/session/musterstadt/api/oparl/PAPER/{KENNUNG}/").func is session_ausgabe.object_view


# =============================================================================
# Abweichende Schreibweise: Weiterleitung statt Fehlerseite
# =============================================================================

OHNE_SCHRAEGSTRICH = [adresse for _, adresse, _ in AGGREGATOR.values() if not adresse.endswith("/")]


@pytest.mark.parametrize("adresse", OHNE_SCHRAEGSTRICH)
def test_aggregator_leitet_adresse_mit_schraegstrich_dauerhaft_weiter(adresse: str) -> None:
    antwort = Client().get(f"{adresse}/")

    assert antwort.status_code == 301
    # Pfad ohne Host: Der Abnehmer bleibt auf dem Host, über den er die Schnittstelle erreicht
    assert antwort["Location"] == adresse
    assert resolve(f"{adresse}/").func is redirects.without_trailing_slash


def test_jede_gueltige_adresse_hat_ihre_weiterleitung() -> None:
    """Neue Routen des Aggregators bekommen die Weiterleitung von selbst (``hub.api.urls.ROUTES``)."""
    from hub.api import urls

    routen = {str(muster.pattern) for muster in urls.urlpatterns}
    for route, _, _, _ in urls.ROUTES:
        assert route in routen and f"{route}/" in routen


def test_weiterleitung_behaelt_die_parameter_der_anfrage() -> None:
    antwort = Client().get(
        f"/oparl/v1/body/{KENNUNG}/meetings/", {"modified_since": "2026-09-01T10:00:00+02:00", "page": "2"}
    )

    assert antwort.status_code == 301
    assert antwort["Location"] == (
        f"/oparl/v1/body/{KENNUNG}/meetings?modified_since=2026-09-01T10%3A00%3A00%2B02%3A00&page=2"
    )


def test_weiterleitung_fuehrt_zur_antwort() -> None:
    antwort = Client().get("/oparl/v1/system/", follow=True)

    assert antwort.redirect_chain == [("/oparl/v1/system", 301)]
    assert antwort.status_code == 200
    assert antwort.json()["type"] == "https://schema.oparl.org/1.1/System"


def test_weiterleitung_ist_rein_lesend() -> None:
    assert Client().post("/oparl/v1/system/").status_code == 405
    assert Client().options("/oparl/v1/system/").status_code == 204
    assert Client().head("/oparl/v1/system/").status_code == 301


@pytest.mark.parametrize(
    "adresse",
    [
        "/oparl/v1/quatsch",
        "/oparl/v1/quatsch/",
        "/oparl/v1/system//",
        f"/oparl/v1/body/{KENNUNG}/meetings/mehr/",
        "/oparl/v1/paper/keine-kennung/",
    ],
)
def test_unbekannte_adresse_bleibt_nicht_gefunden(adresse: str) -> None:
    """Keine Weiterleitung auf Adressen, die es nicht gibt – auch nicht über ``APPEND_SLASH``."""
    assert Client().get(adresse).status_code == 404


def test_weiterleitung_fuehrt_nie_auf_einen_fremden_host() -> None:
    """Ein Pfad mit doppeltem Schrägstrich am Anfang wäre für Browser ein anderer Host."""
    anfrage = RequestFactory().get("/oparl/v1/system/")
    anfrage.path = "//fremd.example/oparl/v1/system/"

    antwort = redirects.without_trailing_slash(anfrage)

    assert antwort.status_code == 301
    assert antwort["Location"] == "/%2Ffremd.example/oparl/v1/system"


def test_session_schnittstelle_leitet_adresse_ohne_schraegstrich_weiter() -> None:
    SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now())

    for adresse in (
        "/session/musterstadt/api/oparl",
        "/session/musterstadt/api/oparl/body",
        "/session/musterstadt/api/oparl/meetings",
    ):
        antwort = Client().get(adresse, {"page": 1})
        assert antwort.status_code == 301, adresse
        assert antwort["Location"] == f"{adresse}/?page=1"
        assert Client().get(adresse, follow=True).status_code == 200
