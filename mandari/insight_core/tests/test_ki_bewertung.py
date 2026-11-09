# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Daumen hoch/runter zu KI-Zusammenfassungen (Issue #1221).

- Abgeben ohne Anmeldung, gespeichert ohne Adresse und Uhrzeit, mit Modell aus dem Anbieterhinweis.
- Eine je Browser und Fassung: Mit dem Zeichen ändert ein zweiter Klick dieselbe Bewertung.
- Neue Fassung: altes Zeichen und alte Prüfsumme gelten nicht mehr (409).
- Schalter nur im Bürgerportal unter einer vorhandenen Zusammenfassung.
- Auswertung je Modell, Kommune und Woche; ERP-Abruf nur mit Schlüssel.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from insight_core.models import OParlBody, OParlPaper, OParlSource, ZusammenfassungBewertung
from insight_core.services import ki_bewertung

pytestmark = pytest.mark.django_db

HINWEIS = "Claude von Anthropic (claude-haiku-5-5); Verarbeitungsort: weltweit, auch in den USA (außerhalb der EU)"
SCHLUESSEL = "erp-test-schluessel-0123456789abcdefghijkl"


@pytest.fixture(autouse=True)
def _sauber() -> None:
    cache.clear()


@pytest.fixture
def vorgang() -> OParlPaper:
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt Münster"
    )
    return OParlPaper.objects.create(
        external_id="https://ris.example.org/oparl/paper/1",
        body=body,
        name="Spielplatz am Markt",
        reference="V/0001/2026",
        summary="Die Stadt plant einen Spielplatz.",
        summary_hinweis=HINWEIS,
    )


def _bewerten(client: Client, vorgang: OParlPaper, wert: str, **daten: str) -> Any:
    url = reverse("insight_core:insight:paper_summary_bewertung", args=[vorgang.pk])
    return client.post(url, {"wert": wert, "fassung": ki_bewertung.fassung(vorgang.summary), **daten})


def test_abgeben_und_aendern(client: Client, vorgang: OParlPaper) -> None:
    antwort = _bewerten(client, vorgang, "hoch")
    assert antwort.status_code == 200 and antwort.json()["hilfreich"] is True
    bewertung = ZusammenfassungBewertung.objects.get()
    assert bewertung.modell == "claude-haiku-5-5" and bewertung.hinweis == HINWEIS
    assert bewertung.body_id == vorgang.body_id and bewertung.fassung == ki_bewertung.fassung(vorgang.summary)
    # Gleicher Browser mit Zeichen: dieselbe Zeile wird geändert
    zeichen = antwort.json()["zeichen"]
    assert _bewerten(client, vorgang, "runter", zeichen=zeichen).json()["hilfreich"] is False
    assert ZusammenfassungBewertung.objects.count() == 1
    assert ZusammenfassungBewertung.objects.get().hilfreich is False
    # Ohne Zeichen (anderer Browser): neue Bewertung
    _bewerten(client, vorgang, "hoch")
    assert ZusammenfassungBewertung.objects.count() == 2


def test_fremdes_oder_falsches_zeichen_neue_zeile(client: Client, vorgang: OParlPaper) -> None:
    _bewerten(client, vorgang, "hoch", zeichen="gefälscht")
    assert ZusammenfassungBewertung.objects.count() == 1


def test_neue_fassung(client: Client, vorgang: OParlPaper) -> None:
    zeichen = _bewerten(client, vorgang, "hoch").json()["zeichen"]
    alte_fassung = ki_bewertung.fassung(vorgang.summary)
    OParlPaper.objects.filter(pk=vorgang.pk).update(summary="Neue Zusammenfassung.")
    url = reverse("insight_core:insight:paper_summary_bewertung", args=[vorgang.pk])
    assert client.post(url, {"wert": "runter", "fassung": alte_fassung, "zeichen": zeichen}).status_code == 409
    vorgang.refresh_from_db()
    # Neue Fassung: das alte Zeichen ändert nichts, es entsteht eine neue Zeile
    assert _bewerten(client, vorgang, "runter", zeichen=zeichen).status_code == 200
    assert ZusammenfassungBewertung.objects.count() == 2


@pytest.mark.parametrize("wert", ["", "vielleicht"])
def test_ungueltiger_wert(client: Client, vorgang: OParlPaper, wert: str) -> None:
    assert _bewerten(client, vorgang, wert).status_code == 400


def test_ohne_zusammenfassung(client: Client, vorgang: OParlPaper) -> None:
    OParlPaper.objects.filter(pk=vorgang.pk).update(summary=None)
    url = reverse("insight_core:insight:paper_summary_bewertung", args=[vorgang.pk])
    assert client.post(url, {"wert": "hoch", "fassung": "x"}).status_code == 404


def test_grenze(client: Client, vorgang: OParlPaper, monkeypatch: Any) -> None:
    monkeypatch.setattr(ki_bewertung, "GRENZE_JE_STUNDE", 2)
    assert [_bewerten(client, vorgang, "hoch").status_code for _ in range(3)] == [200, 200, 429]


def test_schalter_unter_zusammenfassung(client: Client, vorgang: OParlPaper) -> None:
    html = client.get(reverse("insight_core:insight:paper_summary", args=[vorgang.pk])).content.decode()
    assert 'x-data="kiBewertung"' in html and "War die Kurzfassung hilfreich?" in html and "Hilfreich?" in html
    assert f'data-fassung="{ki_bewertung.fassung(vorgang.summary)}"' in html
    assert 'aria-label="Ja, hilfreich"' in html and 'aria-label="Nein, nicht hilfreich"' in html


def test_auswertung(client: Client, vorgang: OParlPaper) -> None:
    _bewerten(client, vorgang, "hoch")
    _bewerten(client, vorgang, "hoch")
    _bewerten(client, vorgang, "runter")
    tabellen = ki_bewertung.auswertung()
    (modell,) = tabellen["Modell"]
    assert (modell.gruppe, modell.gesamt, modell.hilfreich, modell.prozent) == ("claude-haiku-5-5", 3, 2, 67)
    assert tabellen["Kommune"][0].gruppe == "Stadt Münster" and len(tabellen["Woche"]) == 1


class TestErp:
    URL = "/api/insight/ki-bewertungen/"

    def test_ohne_schluessel_unsichtbar(self, client: Client, settings: Any) -> None:
        settings.PROVISIONING_V2_API_KEY = ""
        assert client.get(self.URL).status_code == 404

    def test_falscher_schluessel(self, client: Client, settings: Any) -> None:
        settings.PROVISIONING_V2_API_KEY = SCHLUESSEL
        assert client.get(self.URL, HTTP_AUTHORIZATION="Bearer falsch").status_code == 401

    def test_abruf_seitenweise(self, client: Client, settings: Any, vorgang: OParlPaper) -> None:
        settings.PROVISIONING_V2_API_KEY = SCHLUESSEL
        for wert in ("hoch", "runter", "hoch"):
            _bewerten(client, vorgang, wert)
        kopf = {"Authorization": f"Bearer {SCHLUESSEL}"}
        erste = client.get(self.URL, {"anzahl": 2}, headers=kopf).json()
        assert [eintrag["hilfreich"] for eintrag in erste["bewertungen"]] == [True, False]
        eintrag = erste["bewertungen"][0]
        assert eintrag["modell"] == "claude-haiku-5-5" and eintrag["vorgang_nummer"] == "V/0001/2026"
        assert eintrag["kommune_name"] == "Stadt Münster" and "ip" not in eintrag
        zweite = client.get(self.URL, {"anzahl": 2, "nach": erste["naechste"]}, headers=kopf).json()
        assert len(zweite["bewertungen"]) == 1 and zweite["naechste"] is None
        assert client.get(self.URL, {"seit": "kein-datum"}, headers=kopf).status_code == 400


def test_admin_auswertung(client: Client, vorgang: OParlPaper) -> None:
    from django.contrib.auth import get_user_model

    _bewerten(client, vorgang, "hoch")
    nutzer = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    nutzer.set_unusable_password()
    nutzer.save()
    admin = Client()
    admin.force_login(nutzer)
    html = admin.get(reverse("admin:insight_core_zusammenfassungbewertung_changelist")).content.decode()
    assert "Je Modell (12 Wochen)" in html and "claude-haiku-5-5" in html and "100 %" in html
