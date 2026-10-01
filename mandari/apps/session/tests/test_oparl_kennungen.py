# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Host-unabhängige IDs der Session-OParl-API (ADR docs/adr/20260929-kanonisches-modell.md).

Die IDs sind die Adressen der Session-Objekte und bauen auf ``SITE_URL`` auf, nicht auf dem Host der
Anfrage. Sonst hinge die Kennung eines Objekts im RIS-Bestand davon ab, über welchen Host der Abgleich
lief, und derselbe Beschluss stünde doppelt im Bürgerportal. Die kanonischen Kennungen bilden sich aus
denselben Adressen auf der festgeschriebenen Basis der Installation: Ein Domainwechsel ändert die
Adressen, nicht die Kennungen (Issue #733).
"""

from __future__ import annotations

from io import StringIO
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.common.models import IdentifierBase
from apps.session.api import oparl as schnittstelle
from apps.session.models import SessionPaper, SessionTenant
from apps.session.services import insight_service
from insight_core.models import OParlPaper, OParlSource
from insight_core.services.ris_ids import check_ris_ids
from insight_sync.session_mirror import SessionMirror

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
#: Zwei Wege zur selben Instanz: anderer Host, anderes Schema
HOSTS = [("testserver", False), ("localhost", True)]


@pytest.fixture
def tenant() -> SessionTenant:
    # Freigeschaltete Schnittstelle (Issue #319), sonst antwortet sie mit 404
    tenant = SessionTenant.objects.create(
        name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
    )
    for nummer in range(3):
        SessionPaper.objects.create(
            tenant=tenant, reference=f"V/{nummer}", name=f"Vorlage {nummer}", is_public=True, status="approved"
        )
    return tenant


def _abruf(host: str, secure: bool) -> Any:
    """``fetch`` wie beim Abgleich: Pfad der URL, aber Host und Schema des jeweiligen Zugangs."""
    client = Client()

    def abruf(url: str) -> dict[str, Any]:
        ziel = urlsplit(url)
        pfad = f"{ziel.path}?{ziel.query}" if ziel.query else ziel.path
        antwort = client.get(pfad, headers={"host": host}, secure=secure)
        assert antwort.status_code == 200, url
        return cast(dict[str, Any], antwort.json())

    return abruf


def _schnittstelle(host: str, secure: bool) -> dict[str, Any]:
    abruf = _abruf(host, secure)
    system = abruf(BASIS)
    body = abruf(system["body"])["data"][0]
    seite1 = abruf(body["paper"])
    seite2 = abruf(seite1["links"]["next"])
    vorlage = abruf(seite1["data"][0]["id"])
    return {"system": system, "body": body, "seite1": seite1, "seite2": seite2, "vorlage": vorlage}


def _urls(wert: Any) -> list[str]:
    if isinstance(wert, dict):
        return [url for eintrag in wert.values() for url in _urls(eintrag)]
    if isinstance(wert, list):
        return [url for eintrag in wert for url in _urls(eintrag)]
    return [wert] if isinstance(wert, str) and "://" in wert else []


@override_settings(SITE_URL=SITE, OPARL_API_PAGE_SIZE=2)
def test_ids_und_links_sind_ueber_jeden_host_gleich(tenant: SessionTenant) -> None:
    ueber_testserver = _schnittstelle(*HOSTS[0])
    ueber_localhost = _schnittstelle(*HOSTS[1])

    assert ueber_testserver == ueber_localhost
    assert ueber_testserver["system"]["id"] == BASIS
    assert ueber_testserver["seite1"]["links"]["next"] == f"{BASIS}papers/?page=2"
    eigene = [url for url in _urls(ueber_testserver) if "/api/oparl/" in url]
    assert eigene
    assert all(url.startswith(BASIS) for url in eigene)


@override_settings(SITE_URL=SITE)
def test_system_id_ist_die_registrierte_quelle(tenant: SessionTenant) -> None:
    """Der Ingestor ruft die registrierte Quelle ab; ihre URL ist die ID des System-Objekts."""
    source, _ = insight_service.register_source(tenant)
    assert source.url == BASIS
    assert _abruf("localhost", True)(source.url)["id"] == source.url


@override_settings(SITE_URL=SITE, OPARL_API_PAGE_SIZE=2)
def test_abgleich_ueber_verschiedene_hosts_legt_nichts_doppelt_an(tenant: SessionTenant) -> None:
    source, _ = insight_service.register_source(tenant)
    for host, secure in HOSTS:
        cast(Any, SessionMirror)(source, fetch=_abruf(host, secure)).sync(full=True)

    vorlagen = OParlPaper.objects.filter(body__source=source)
    assert vorlagen.count() == 3
    assert all(
        external_id.startswith(f"{BASIS}paper/") for external_id in vorlagen.values_list("external_id", flat=True)
    )


# =============================================================================
# Domainwechsel: Adressen folgen SITE_URL, Kennungen der festgeschriebenen Basis (Issue #733)
# =============================================================================

NEU = "https://neu.example"
NEU_BASIS = f"{NEU}/session/musterstadt/api/oparl/"


@pytest.fixture
def basis_festgeschrieben() -> None:
    """Installation, deren Kennungen auf SITE gebildet sind (wie in Produktion: Basis = heutige SITE_URL)."""
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})


def _kennungen(source: OParlSource) -> dict[str, Any]:
    """Kennung je Session-Objekt (Pfad nach der Basis der Schnittstelle) im Bürgerportal-Spiegel."""
    zeilen = OParlPaper.objects.filter(body__source=source).values_list("external_id", "id")
    return {external_id.split("/api/oparl/", 1)[1]: kennung for external_id, kennung in zeilen}


def _feed_kennungen(tenant: SessionTenant) -> dict[str, Any]:
    """Kennungen, unter denen der Änderungsfeed die Objekte des Mandanten führt."""
    modul = cast(Any, schnittstelle)  # die Schnittstelle des Fachmoduls ist nicht typisiert
    mapping, feed = modul._feed(tenant.slug)
    vorlagen = {
        f"paper/{pk}/": mapping.uris.canonical_id(mapping.uris.obj("paper", pk))
        for pk in SessionPaper.objects.filter(tenant=tenant).values_list("pk", flat=True)
    }
    return {"body/": feed.body_id, **vorlagen}


@override_settings(OPARL_CHANGES_ENABLED=True)
def test_aenderung_von_site_url_aendert_keine_kennung(tenant: SessionTenant, basis_festgeschrieben: None) -> None:
    """Schnittstelle, Änderungsfeed, Bürgerportal-Spiegel und Prüfbefehl: gleiche Kennungen vor und nach dem Wechsel."""
    with override_settings(SITE_URL=SITE):
        source, _ = insight_service.register_source(tenant)
        cast(Any, SessionMirror)(source, fetch=_abruf("testserver", False)).sync(full=True)
        vorher = _kennungen(source)
        feed_vorher = _feed_kennungen(tenant)
    assert len(vorher) == 3
    assert {pfad: feed_vorher[pfad] for pfad in vorher} == vorher

    with override_settings(SITE_URL=NEU):
        # Adressen folgen der neuen Domain …
        assert _abruf("testserver", False)(NEU_BASIS)["id"] == NEU_BASIS
        # … Kennungen der festgeschriebenen Basis
        assert _feed_kennungen(tenant) == feed_vorher

        # Bestand umziehen (docs/SESSION_OPARL_API.md, „Domainwechsel“), dann wie gewohnt abgleichen
        call_command("move_session_sources", "--yes", stdout=StringIO())
        source.refresh_from_db()
        assert source.url == NEU_BASIS
        assert insight_service.register_source(tenant) == (source, False)
        cast(Any, SessionMirror)(source, fetch=_abruf("testserver", False)).sync(full=True)

        assert _kennungen(source) == vorher
        assert OParlPaper.objects.count() == 3
        assert all(
            adresse.startswith(NEU_BASIS) for adresse in OParlPaper.objects.values_list("external_id", flat=True)
        )
        bericht = check_ris_ids()
        gesamt = bericht.total()
        assert (gesamt.id_deviations, gesamt.uri_deviations, gesamt.collisions) == (0, 0, 0)
        assert bericht.base_deviates
        assert not any(info.id_base_deviates for info in bericht.sources.values())


def test_neue_quelle_traegt_die_basis_der_kennungen(tenant: SessionTenant, basis_festgeschrieben: None) -> None:
    """Ingestor und Spiegel lesen die Basis aus der Quelle; eine einmal eingetragene Basis bleibt."""
    with override_settings(SITE_URL=SITE):
        source, angelegt = insight_service.register_source(tenant)
        assert angelegt
        assert source.sync_config["id_base"] == BASIS
        # Ältere Quelle ohne Eintrag: wird nachgetragen
        source.sync_config = {"source_type": "oparl", "session_tenant": "musterstadt"}
        source.save(update_fields=["sync_config"])
        insight_service.register_source(tenant)
        source.refresh_from_db()
        assert source.sync_config["id_base"] == BASIS
        # Vorhandener Eintrag: bleibt
        source.sync_config = {**source.sync_config, "id_base": "https://alt.example/session/musterstadt/api/oparl/"}
        source.save(update_fields=["sync_config"])
        insight_service.register_source(tenant)
        source.refresh_from_db()
        assert source.sync_config["id_base"] == "https://alt.example/session/musterstadt/api/oparl/"
