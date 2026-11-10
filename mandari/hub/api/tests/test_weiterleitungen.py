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
    # Änderungsfeed (Issue #562)
    "oparl_api:body_changes": ({"pk": KENNUNG}, f"/oparl/v1/body/{KENNUNG}/changes", aggregator.body_changes),
    # Snapshot (Issue #563)
    "oparl_api:body_snapshot": ({"pk": KENNUNG}, f"/oparl/v1/body/{KENNUNG}/snapshot", aggregator.body_snapshot),
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
    "session:oparl_changes": ({}, "/session/musterstadt/api/oparl/body/changes/", session_ausgabe.changes_view),
    "session:oparl_snapshot": ({}, "/session/musterstadt/api/oparl/body/snapshot/", session_ausgabe.snapshot_view),
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


# =============================================================================
# Kurzadresse einer Kommune: Name statt Kennung (Issue #1309)
# =============================================================================


@pytest.fixture
def muenster() -> Any:
    from insight_core.models import OParlBody, OParlSource

    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.example/oparl/body/muenster", source=quelle, name="Stadt Münster", slug="muenster"
    )


@pytest.mark.parametrize(
    ("kurz", "gueltig"),
    [
        ("", ""),
        ("/", ""),
        ("/papers", "/papers"),
        ("/meetings/", "/meetings"),
        ("/kommparl/changes", "/kommparl/changes"),
    ],
)
def test_kurzadresse_fuehrt_vorlaeufig_auf_die_kennung(muenster: Any, kurz: str, gueltig: str) -> None:
    antwort = Client().get(f"/oparl/v1/body/muenster{kurz}")

    # Vorläufig: Der Name einer Kommune kann sich ändern, ihre Kennung nicht
    assert antwort.status_code == 307
    assert antwort["Location"] == f"/oparl/v1/body/{muenster.pk}{gueltig}"
    assert antwort["Access-Control-Allow-Origin"] == "*"


def test_kurzadresse_behaelt_die_parameter_und_liefert_dieselben_daten(muenster: Any) -> None:
    antwort = Client().get("/oparl/v1/body/muenster/papers?modified_since=2026-11-01T00:00:00Z&limit=5")
    assert antwort["Location"] == f"/oparl/v1/body/{muenster.pk}/papers?modified_since=2026-11-01T00:00:00Z&limit=5"

    gefolgt = Client().get("/oparl/v1/body/muenster", follow=True)
    assert gefolgt.status_code == 200
    # Die Kennung in der Antwort bleibt die Adresse mit der UUID
    assert gefolgt.json()["id"].endswith(f"/v1/body/{muenster.pk}")


def test_kurzadresse_unbekannter_name_ist_404_als_json(muenster: Any) -> None:
    antwort = Client().get("/oparl/v1/body/gibt-es-nicht/papers")

    assert antwort.status_code == 404
    assert antwort["Content-Type"].startswith("application/json")
    assert "bodyIndex" in antwort.json()["error"]


def test_kennung_wird_nie_als_name_gelesen() -> None:
    assert resolve(f"/oparl/v1/body/{KENNUNG}").func is aggregator.object_view
    assert resolve(f"/oparl/v1/body/{KENNUNG}/papers").func is aggregator.body_sub_list
    assert resolve(f"/oparl/v1/body/{KENNUNG}/").func is redirects.without_trailing_slash
    assert resolve("/oparl/v1/body/muenster").func is redirects.body_by_slug
    assert resolve("/oparl/v1/body/muenster/papers").func is redirects.body_by_slug
