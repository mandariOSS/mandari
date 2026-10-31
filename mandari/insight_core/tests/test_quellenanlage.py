# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``add_oparl_source``: Neuanlage inaktiv und ungelistet, keine Reaktivierung (Issue #555), Kommunenliste über alle
Seiten (Issue #869), keine Kommune wandert auf eine andere Quelle. Dazu das Formular „Quelle hinzufügen“ im Admin,
das „Aktiv“ nicht mehr vorbelegt.

Bisher setzte der Befehl jede Quelle per ``update_or_create`` auf aktiv, auch eine im Admin abgeschaltete, und neue
Kommunen erschienen sofort im Bürgerportal. Jetzt ist eine neue Quelle inaktiv, eine vorhandene behält ihren Status,
neue Kommunen sind nicht gelistet und vorhandene behalten ihren. Antworten aus ``httpx.MockTransport``, keine Abrufe.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from io import StringIO
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse

from insight_core.models import OParlBody, OParlSource
from insight_core.services import host_pacing

pytestmark = pytest.mark.django_db


def _quelle() -> tuple[str, str, Callable[[int], dict[str, Any]]]:
    """Eigene Adressen je Test: System, Kommunenliste und eine Kommune je Nummer."""
    base = f"https://rat.example.de/oparl/{uuid.uuid4().hex[:8]}"

    def kommune(nummer: int) -> dict[str, Any]:
        return {"id": f"{base}/body/{nummer}", "type": "https://schema.oparl.org/1.1/Body", "name": f"Kommune {nummer}"}

    return f"{base}/system", f"{base}/bodies", kommune


class Server:
    """System mit Kommunenliste über zwei Seiten (Kommunen 1 und 2, dann 3); die zweite Seite lässt sich stören."""

    def __init__(self, system: str, liste: str, kommune: Callable[[int], dict[str, Any]]) -> None:
        self.system = system
        self.liste = liste
        self.kommune = kommune
        self.zweite_seite: httpx.Response | None = None
        self.anfragen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.anfragen.append(url)
        if url.rstrip("/") == self.system:
            return httpx.Response(
                200, json={"id": self.system, "type": "https://schema.oparl.org/1.1/System", "body": self.liste}
            )
        if url == self.liste:
            seite = {"data": [self.kommune(1), self.kommune(2)], "links": {"next": f"{self.liste}?page=2"}}
            return httpx.Response(200, json=seite)
        if url == f"{self.liste}?page=2":
            return self.zweite_seite or httpx.Response(200, json={"data": [self.kommune(3)], "links": {}})
        if url == self.kommune(1)["id"]:
            return httpx.Response(200, json=self.kommune(1))
        return httpx.Response(404)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    system, liste, kommune = _quelle()
    antworten = Server(system, liste, kommune)
    echter_client = httpx.Client
    monkeypatch.setattr(
        "insight_core.management.commands.add_oparl_source.httpx.Client",
        lambda **kw: echter_client(transport=httpx.MockTransport(antworten), **kw),
    )
    # Takt je Host ohne Warten (die Drossel selbst prüft test_drossel_je_host)
    monkeypatch.setattr(host_pacing, "wait", lambda url, **kw: True)
    return antworten


def _anlegen(server: Server) -> str:
    ausgabe = StringIO()
    call_command("add_oparl_source", server.system, stdout=ausgabe)
    return ausgabe.getvalue()


def test_neue_quelle_ist_inaktiv_und_neue_kommunen_sind_nicht_gelistet(server: Server) -> None:
    ausgabe = _anlegen(server)

    source = OParlSource.objects.get(url=server.system)
    assert source.is_active is False
    bodies = OParlBody.objects.filter(source=source).order_by("external_id")
    assert [b.external_id for b in bodies] == [server.kommune(n)["id"] for n in (1, 2, 3)]
    assert {b.is_listed for b in bodies} == {False}
    assert "Quelle inaktiv angelegt" in ausgabe
    assert "Nicht gelistet" in ausgabe


@pytest.mark.parametrize("aktiv", [False, True])
def test_vorhandene_quelle_behaelt_ihren_status(server: Server, aktiv: bool) -> None:
    OParlSource.objects.create(name="Alt", url=server.system, is_active=aktiv)

    ausgabe = _anlegen(server)

    source = OParlSource.objects.get(url=server.system)
    # Eine abgeschaltete Quelle wird nie wieder aktiv; eine aktive bleibt aktiv. Die Angaben werden aktualisiert.
    assert source.is_active is aktiv
    assert source.name == "Unbenannte Quelle"
    assert f"Status unverändert: {'aktiv' if aktiv else 'inaktiv'}" in ausgabe


def test_vorhandene_kommunen_behalten_ihre_sichtbarkeit(server: Server) -> None:
    source = OParlSource.objects.create(name="Alt", url=server.system, is_active=True)
    OParlBody.objects.create(source=source, external_id=server.kommune(1)["id"], name="Alt 1", is_listed=True)
    OParlBody.objects.create(source=source, external_id=server.kommune(2)["id"], name="Alt 2", is_listed=False)

    _anlegen(server)

    gelistet = dict(OParlBody.objects.filter(source=source).values_list("external_id", "is_listed"))
    assert gelistet == {
        server.kommune(1)["id"]: True,
        server.kommune(2)["id"]: False,
        server.kommune(3)["id"]: False,  # neu entdeckt
    }
    assert OParlBody.objects.get(external_id=server.kommune(1)["id"]).name == "Kommune 1"


def test_abgebrochene_kommunenliste_wird_gemeldet(server: Server) -> None:
    server.zweite_seite = httpx.Response(500)

    ausgabe = _anlegen(server)

    source = OParlSource.objects.get(url=server.system)
    assert OParlBody.objects.filter(source=source).count() == 2
    assert f"Kommunenliste unvollständig ab Seite {server.liste}?page=2" in ausgabe


#: Einstellungen einer vorhandenen Quelle, die eine zweite Quelle nicht hätte
EINSTELLUNGEN = {"file_downloads": False, "request_interval": 2.0}


@pytest.mark.parametrize(
    ("registriert", "aufgerufen"),
    [
        pytest.param("system", "system/", id="Schrägstrich am Ende"),
        pytest.param("liste", "system", id="System statt Kommunenliste"),
        pytest.param("system", "liste", id="Kommunenliste statt System"),
        pytest.param("system", "kommune", id="Adresse der Kommune"),
    ],
)
def test_andere_schreibweise_laesst_die_kommune_an_ihrer_quelle(
    server: Server, registriert: str, aufgerufen: str
) -> None:
    adressen = {
        "system": server.system,
        "system/": f"{server.system}/",
        "liste": server.liste,
        "kommune": server.kommune(1)["id"],
    }
    source = OParlSource.objects.create(
        name="Vorhanden", url=adressen[registriert], is_active=True, sync_config=EINSTELLUNGEN
    )
    body = OParlBody.objects.create(
        source=source, external_id=server.kommune(1)["id"], name="Vorhanden 1", is_listed=True
    )

    with pytest.raises(CommandError, match="schon an einer anderen Quelle") as abbruch:
        call_command("add_oparl_source", adressen[aufgerufen], stdout=StringIO())

    assert source.url in str(abbruch.value)
    # Nichts geschrieben: keine zweite Quelle, keine weitere Kommune, Quelle und Kommune unverändert
    assert not OParlSource.objects.filter(url=adressen[aufgerufen]).exists()
    assert list(OParlBody.objects.filter(external_id__contains=server.system.rsplit("/", 1)[0])) == [body]
    body.refresh_from_db()
    assert (body.source_id, body.name, body.is_listed) == (source.pk, "Vorhanden 1", True)
    source.refresh_from_db()
    assert (source.name, source.is_active, source.sync_config) == ("Vorhanden", True, EINSTELLUNGEN)


def test_registrierte_quelle_holt_keine_kommune_einer_anderen(server: Server) -> None:
    andere = OParlSource.objects.create(name="Andere", url=f"{server.liste}/andere", is_active=True)
    OParlBody.objects.create(source=andere, external_id=server.kommune(2)["id"], name="Vorhanden 2")
    eigene = OParlSource.objects.create(name="Alt", url=server.system, is_active=False)

    with pytest.raises(CommandError, match=andere.url):
        _anlegen(server)

    eigene.refresh_from_db()
    assert eigene.name == "Alt"
    assert OParlBody.objects.get(external_id=server.kommune(2)["id"]).source_id == andere.pk
    assert not OParlBody.objects.filter(source=eigene).exists()


def _haken_gesetzt(antwort: Any) -> bool:
    """Ob das Formular den Haken „Aktiv“ gesetzt ausliefert: das Attribut ``checked``, nicht die CSS-Klasse."""
    assert antwort.status_code == 200
    return re.search(r"\schecked[\s>=]", str(antwort.context["adminform"].form["is_active"])) is not None


def test_formular_belegt_aktiv_nicht_vor(admin_client: Client) -> None:
    assert not _haken_gesetzt(admin_client.get(reverse("admin:insight_core_oparlsource_add")))


@pytest.mark.parametrize(("haken", "aktiv"), [({}, False), ({"is_active": "on"}, True)])
def test_formular_legt_nur_mit_gesetztem_haken_aktiv_an(
    admin_client: Client, haken: dict[str, str], aktiv: bool
) -> None:
    url = f"https://rat.example.de/oparl/{uuid.uuid4().hex[:8]}/system"
    daten = {"name": "Neue Quelle", "url": url, "sync_config": "{}", "raw_json": "{}", **haken}

    antwort = admin_client.post(reverse("admin:insight_core_oparlsource_add"), daten)

    assert antwort.status_code == 302
    assert OParlSource.objects.get(url=url).is_active is aktiv


def test_formular_einer_vorhandenen_quelle_zeigt_ihren_status(admin_client: Client) -> None:
    """Nur die Neuanlage ist nicht vorbelegt: Eine aktive Quelle verliert beim Bearbeiten nicht ihren Haken."""
    source = OParlSource.objects.create(name="Aktiv", url="https://rat.example.de/oparl/aktiv/system", is_active=True)

    assert _haken_gesetzt(admin_client.get(reverse("admin:insight_core_oparlsource_change", args=[source.pk])))
