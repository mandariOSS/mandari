# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Module der Installation und ihre Schalter (Issue #1594, ``apps.common.module``).

Die Profile selbst (je Modul an oder aus, migriert, Probeabrufe) startet die CI im Job „Installationsprofile“ mit
``manage.py installationsprofil_pruefen``. Hier stehen Katalog, Schalter und die Filter der Register.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.tasks import task
from django.template import Context, Template
from django.test import RequestFactory, override_settings
from django.urls import path

from apps.common import module
from apps.events import registry
from apps.events.schedule import ScheduleRegistry, every

OHNE_WORK = frozenset({module.SESSION, module.PORTAL})
OHNE_PORTAL = frozenset({module.SESSION, module.WORK})


def _ansicht(request: Any) -> HttpResponse:
    return HttpResponse("")


def _als(code_modul: str, funktion: Any) -> Any:
    """Funktion, die so tut, als läge sie in ``code_modul``."""
    funktion.__module__ = code_modul
    return funktion


def _work_lauf() -> None:
    """Auftrag eines Zeitplans, der so tut, als läge er in Work."""


def _plattform_lauf() -> None:
    """Auftrag eines Zeitplans der Plattform."""


# -- Schalter -------------------------------------------------------------------------------


def test_ohne_angabe_sind_alle_module_an() -> None:
    assert module.aus_umgebung({}) == frozenset(module.MODULE)


def test_schalter_an_und_aus() -> None:
    aktiv = module.aus_umgebung({"MODUL_SESSION": "aus", "MODUL_WORK": "false", "MODUL_PORTAL": " AN "})
    assert aktiv == frozenset({module.PORTAL})


def test_unbekannter_wert_bricht_den_start_ab() -> None:
    with pytest.raises(ImproperlyConfigured, match="MODUL_WORK"):
        module.aus_umgebung({"MODUL_WORK": "vielleicht"})


def test_unbekanntes_modul_ist_ein_programmierfehler() -> None:
    with pytest.raises(ImproperlyConfigured):
        module.aktiv("kalender")


def test_startseite_nach_der_anmeldung() -> None:
    assert module.startseite(frozenset(module.MODULE)) == "/work/"
    assert module.startseite({module.SESSION, module.PORTAL}) == "/accounts/login/"
    assert module.startseite({module.PORTAL}) == "/insight/"
    assert module.startseite(set()) == "/accounts/login/"


# -- Katalog ---------------------------------------------------------------------------------


def test_code_gehoert_zu_seinem_modul() -> None:
    assert module.module_fuer("apps.work.faction.views.meetings") == {module.WORK}
    assert module.module_fuer("apps.session") == {module.SESSION}
    # Nur ganze Namensteile: apps.workflow wäre nicht Work
    assert module.module_fuer("apps.workflow.views") == frozenset()
    assert module.module_fuer("apps.events.scheduler") == frozenset()


def test_stelle_eines_moduls_die_ein_anderes_voraussetzt() -> None:
    # Öffentliche Protokolle von Fraktionssitzungen: im Bürgerportal, lesen Work
    assert module.module_fuer("insight_core.views.protocols.PublicProtocolListView") == {module.WORK, module.PORTAL}


def test_gemeinsamer_code_bleibt_ohne_das_modul() -> None:
    assert module.module_fuer("insight_core.views.files.file_proxy", "insight/dokumente/<uuid>/preview/") == set()
    assert module.module_fuer("insight_core.views.files.FileListView") == {module.PORTAL}


def test_adresse_gehoert_ueber_ihren_pfad_zum_modul() -> None:
    # django-ninja: Die Ansicht liegt in ninja, die Adresse unter /api/v1/session/
    assert module.module_fuer("ninja.operation.PathView", "api/v1/session/<tenant>/") == {module.SESSION}


@override_settings(MANDARI_MODULE=OHNE_WORK)
def test_abgeschaltet_fuer() -> None:
    assert module.abgeschaltet_fuer("apps.work.urls") == module.WORK
    assert module.abgeschaltet_fuer("apps.session.urls") is None
    assert not module.aktiv(module.WORK)
    assert [modul.kennung for modul in module.abgeschaltete()] == [module.WORK]


def test_code_von_ansicht_consumer_und_funktion() -> None:
    class Ansicht:
        pass

    def ansicht() -> None:
        pass

    ansicht.view_class = Ansicht  # type: ignore[attr-defined]
    assert module.code_von(ansicht).endswith("test_code_von_ansicht_consumer_und_funktion.<locals>.Ansicht")
    assert module.code_von(_ansicht) == f"{__name__}._ansicht"


# -- Prüfungen -------------------------------------------------------------------------------


@override_settings(MANDARI_MODULE=OHNE_WORK)
def test_adressen_eines_abgeschalteten_moduls_sind_befunde() -> None:
    muster = [
        path("work/start/", _als("apps.work.dashboard.views", lambda request: None)),
        path("api/stats/", _ansicht),
        path("session/x/", _als("apps.session.views", lambda request: None)),
    ]
    befunde = module.adressen_pruefen(muster)
    assert [(befund.modul, befund.art) for befund in befunde] == [(module.WORK, "Adresse")]
    assert "/work/start/" in befunde[0].stelle
    assert "Work" in str(befunde[0])


def test_systempruefung_mit_allen_modulen_ohne_befund() -> None:
    assert module.module_pruefen() == []


@override_settings(MANDARI_MODULE=OHNE_WORK)
def test_geladene_einstiege_abgeschalteter_module() -> None:
    befunde = module.einstiege_geladen(
        ["apps.work.models", "apps.work.faction.views.meetings", "apps.work.urls", "apps.session.urls", "apps.work"]
    )
    assert [befund.stelle for befund in befunde] == ["apps.work.faction.views.meetings", "apps.work.urls"]


@override_settings(MANDARI_MODULE=OHNE_WORK)
def test_registrierte_zeitplaene_und_abonnements_pruefen() -> None:
    befunde = module.registrierte_pruefen("Zeitplan", [("a", "apps.work.schedules.a"), ("b", "apps.events.x")])
    assert [befund.stelle for befund in befunde] == ["a (apps.work.schedules.a)"]


# -- Register: Zeitpläne und Abonnements abgeschalteter Module entstehen nicht ---------------


@override_settings(MANDARI_MODULE=OHNE_WORK)
def test_zeitplan_eines_abgeschalteten_moduls_registriert_sich_nicht() -> None:
    ziel = ScheduleRegistry()
    every(minutes=5, name="work.probe", registry=ziel)(task(_als("apps.work.schedules", _work_lauf)))
    every(minutes=5, name="plattform.probe", registry=ziel)(task(_als("apps.events.schedules", _plattform_lauf)))
    assert [eintrag.name for eintrag in ziel] == ["plattform.probe"]


@override_settings(MANDARI_MODULE=OHNE_PORTAL)
def test_abonnement_eines_abgeschalteten_moduls_registriert_sich_nicht() -> None:
    handler = _als("insight_core.services.indexnow", lambda events, delivery: None)
    registry.subscriber("test.modul_aus", types=["ris.paper.*"])(handler)
    assert "test.modul_aus" not in {spec.name for spec in registry.registered()}


# -- Oberfläche ------------------------------------------------------------------------------


def test_filter_modul_aktiv() -> None:
    vorlage = Template('{% load modul_tags %}{% if "work"|modul_aktiv %}an{% else %}aus{% endif %}')
    assert vorlage.render(Context()) == "an"
    with override_settings(MANDARI_MODULE=OHNE_WORK):
        assert vorlage.render(Context()) == "aus"


def test_robots_nennt_die_sitemap_nur_mit_buergerportal() -> None:
    from insight_core.views import sitemap

    robots_txt = cast(Callable[[HttpRequest], HttpResponse], sitemap.robots_txt)

    anfrage = RequestFactory().get("/robots.txt")
    assert b"Sitemap:" in robots_txt(anfrage).content
    with override_settings(MANDARI_MODULE=OHNE_PORTAL):
        inhalt = robots_txt(anfrage).content
    assert b"Sitemap:" not in inhalt
    assert b"Disallow: /admin/" in inhalt


@override_settings(MANDARI_MODULE=OHNE_PORTAL, INSIGHT_OFFENE_DATEN_URL="", INSIGHT_MCP_ENABLED=True)
def test_drehscheibe_verweist_ohne_buergerportal_nicht_darauf() -> None:
    from hub.api.aggregator import directory_url
    from hub.ris.mapping.bestand import mcp_url
    from insight_ai import mcp

    assert directory_url() == ""
    assert mcp_url() is None
    assert not mcp.enabled()


def test_fehlerseite_404_ohne_buergerportal() -> None:
    from mandari.urls import im_buergerportal

    anfrage = RequestFactory().get("/insight/vorgaenge/")
    assert im_buergerportal(anfrage)
    with override_settings(MANDARI_MODULE=OHNE_PORTAL):
        assert not im_buergerportal(anfrage)
        inhalt = Template('{% include "404.html" %}').render(Context({"request": anfrage}))
    assert "Zum Bürgerportal" not in inhalt
