# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweis je Kommune im Bürgerportal (Issue #734): ``OParlBody.portal_notice``, im Admin gepflegt.

Sichtbar auf Einstieg und Listenseiten der Kommune (auch im eigenen Einstieg /insight/k/<slug>/), nicht
auf Detailseiten und nicht bei anderen Kommunen; leer heißt kein Hinweis. Mit Hinweis entfällt der
automatische Datenstand-Hinweis („nicht erreichbar“), den der Hinweis der Kommune dann richtigstellt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core.models import OParlBody, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

RIS = "https://ris.hinweis.example/oparl"
HINWEIS = (
    "Die Stadt hat die Bereitstellung ihrer Daten über die OParl-Schnittstelle zum 30.09.2026 beendet. "
    "Angezeigt wird der Stand vom 29.09.2026."
)
MARKE = 'data-testid="kommune-hinweis"'
LISTEN = (
    "/insight/",
    "/insight/vorgaenge/",
    "/insight/termine/",
    "/insight/gremien/",
    "/insight/personen/",
    "/insight/beschluesse/",
    "/insight/fragen/",
)


def _kommune(nummer: int, name: str, **felder: Any) -> OParlBody:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "Hinweis-RIS"})
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/{nummer}", source=source, name=name, slug=f"hinweis-{nummer}", is_listed=True, **felder
    )


def _client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


def test_standard_leer() -> None:
    body = _kommune(1, "Ohnestadt")
    body.refresh_from_db()
    assert body.portal_notice == ""
    seite = _client(body).get("/insight/")
    assert seite.status_code == 200
    assert MARKE not in seite.content.decode()


def test_hinweis_auf_einstieg_und_listen() -> None:
    body = _kommune(2, "Darmstadt", portal_notice=HINWEIS)
    client = _client(body)
    for url in LISTEN:
        seite = client.get(url)
        assert seite.status_code == 200, url
        inhalt = seite.content.decode()
        assert MARKE in inhalt, url
        assert "über die OParl-Schnittstelle zum 30.09.2026 beendet" in inhalt, url


def test_hinweis_im_eigenen_einstieg() -> None:
    body = _kommune(3, "Einstiegstadt", portal_notice=HINWEIS)
    inhalt = Client().get(f"/insight/k/{body.slug}/").content.decode()
    assert MARKE in inhalt


def test_nur_bei_der_eigenen_kommune() -> None:
    _kommune(4, "Darmstadt", portal_notice=HINWEIS)
    andere = _kommune(5, "Nachbarstadt")
    inhalt = _client(andere).get("/insight/vorgaenge/").content.decode()
    assert MARKE not in inhalt
    assert "OParl-Schnittstelle" not in inhalt


def test_nicht_auf_detailseiten() -> None:
    body = _kommune(6, "Darmstadt", portal_notice=HINWEIS)
    paper = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Radweg Hauptstraße")
    seite = _client(body).get(f"/insight/vorgaenge/{paper.id}/")
    assert seite.status_code == 200
    assert MARKE not in seite.content.decode()


def test_text_wird_maskiert() -> None:
    body = _kommune(7, "Maskenstadt", portal_notice='<script>alert("x")</script>Zeile 1\nZeile 2')
    inhalt = _client(body).get("/insight/").content.decode()
    assert '<script>alert("x")</script>' not in inhalt
    assert "&lt;script&gt;" in inhalt
    assert "Zeile 1<br>Zeile 2" in inhalt


def test_ersetzt_datenstand_hinweis() -> None:
    alt = timezone.now() - timedelta(days=30)
    ohne = _kommune(8, "Altstadt", last_sync=alt)
    assert "konnten seit" in _client(ohne).get("/insight/vorgaenge/").content.decode()
    mit = _kommune(9, "Darmstadt", last_sync=alt, portal_notice=HINWEIS)
    inhalt = _client(mit).get("/insight/vorgaenge/").content.decode()
    assert MARKE in inhalt
    assert "konnten seit" not in inhalt


def test_im_admin_pflegbar() -> None:
    body = _kommune(10, "Adminstadt")
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)
    url = reverse("admin:insight_core_oparlbody_change", args=[body.pk])

    seite = client.get(url)
    assert seite.status_code == 200
    assert 'name="portal_notice"' in seite.content.decode()

    daten = _formulardaten(seite)
    daten["portal_notice"] = HINWEIS
    antwort = client.post(url, daten)
    assert antwort.status_code == 302, antwort.context["adminform"].form.errors if antwort.context else antwort
    body.refresh_from_db()
    assert body.portal_notice == HINWEIS


def _formulardaten(seite: Any) -> dict[str, Any]:
    """POST-Daten aus dem Admin-Formular so, wie der Browser es unverändert abschicken würde."""
    daten: dict[str, Any] = {}
    for feld in seite.context["adminform"].form:
        wert = feld.value()
        if wert is None or wert is False or (hasattr(wert, "name") and not wert):
            continue
        if wert is True:
            daten[feld.html_name] = "on"
        elif isinstance(wert, list | tuple):
            daten[feld.html_name] = [str(v) for v in wert]
        else:
            daten[feld.html_name] = str(wert)
    for inline in seite.context["inline_admin_formsets"]:
        for feld in inline.formset.management_form:
            daten[feld.html_name] = feld.value()
    return daten
