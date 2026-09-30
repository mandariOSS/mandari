# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Host-unabhängige IDs der Session-OParl-API (ADR docs/adr/20260929-kanonisches-modell.md).

Die IDs sind die kanonischen URIs der Session-Objekte und bauen auf ``SITE_URL`` auf, nicht auf dem
Host der Anfrage. Sonst hinge die Kennung eines Objekts im RIS-Bestand davon ab, über welchen Host
der Abgleich lief, und derselbe Beschluss stünde doppelt im Bürgerportal.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from django.test import Client, override_settings

from apps.session.models import SessionPaper, SessionTenant
from apps.session.services import insight_service
from insight_core.models import OParlPaper
from insight_sync.session_mirror import SessionMirror

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
#: Zwei Wege zur selben Instanz: anderer Host, anderes Schema
HOSTS = [("testserver", False), ("localhost", True)]


@pytest.fixture
def tenant() -> SessionTenant:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
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
