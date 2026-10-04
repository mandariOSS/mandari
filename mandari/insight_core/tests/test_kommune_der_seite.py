# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kommune der Seite im Rahmen des Bürgerportals (Issue #783, Stufe 2).

Brotkrumen, Herkunft im Fuß, Datenstand und „Zuletzt besucht“ nennen auf Detailseiten die Kommune des Eintrags,
nicht die der Sitzung: beim Erstbesuch über eine Suchmaschine (Sitzung „alle Kommunen“) und wenn bei gewählter
Kommune A ein Vorgang von B geöffnet wird. Die Seitenleiste zeigt weiter die gewählte Kommune.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core import publication
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

ABGLEICH_A = timezone.make_aware(datetime(2026, 10, 1, 8, 15))
ABGLEICH_B = timezone.make_aware(datetime(2026, 10, 3, 17, 40))


def _kommune(nummer: int, name: str, **felder: Any) -> OParlBody:
    source = OParlSource.objects.create(name=f"RIS {name}", url=f"https://ris{nummer}.example.org/system")
    return OParlBody.objects.create(
        external_id=f"https://ris{nummer}.example.org/body/1", source=source, name=name, **felder
    )


@pytest.fixture
def kommunen(db: Any) -> dict[str, Any]:
    a = _kommune(1, "Stadt Beispielstadt", display_name="Beispielstadt", ags="05999001", last_sync=ABGLEICH_A)
    b = _kommune(2, "Stadt Musterhausen", display_name="Musterhausen", ags="05999002", last_sync=ABGLEICH_B)
    vorgang_b = OParlPaper.objects.create(
        external_id="https://ris2.example.org/paper/1", body=b, name="Radweg Musterhausen"
    )
    vorgang_a = OParlPaper.objects.create(
        external_id="https://ris1.example.org/paper/1", body=a, name="Radweg Beispielstadt"
    )
    sitzung_b = OParlMeeting.objects.create(
        external_id="https://ris2.example.org/meeting/1", body=b, name="Rat Musterhausen", start=timezone.now()
    )
    return {"a": a, "b": b, "vorgang_a": vorgang_a, "vorgang_b": vorgang_b, "sitzung_b": sitzung_b}


def _html(client: Client, url: str) -> str:
    antwort = client.get(url)
    assert antwort.status_code == 200
    return antwort.content.decode()


def _brotkrumen(html: str) -> str:
    treffer = re.search(r'<nav aria-label="Brotkrumen"[^>]*>(.*?)</nav>', html, re.S)
    assert treffer, "Brotkrumen in der Kopfzeile fehlen"
    return re.sub(r"\s+", " ", treffer.group(1))


def _fuss(html: str) -> str:
    return re.sub(r"\s+", " ", html[html.index("<footer") : html.index("</footer>")])


def _seitenleiste(html: str) -> str:
    return html[html.index('<aside id="insight-navigation"') : html.index("</aside>")]


def _waehlen(client: Client, body: OParlBody) -> None:
    client.get(reverse("insight_core:insight:set_body", args=[body.id]))


class TestErstbesuchUeberSuchmaschine:
    def test_brotkrumen_fuss_und_zuletzt_besucht_nennen_die_kommune_des_vorgangs(
        self, client: Client, kommunen: dict[str, Any]
    ) -> None:
        b = kommunen["b"]
        html = _html(client, reverse("insight_core:insight:paper_detail", args=[kommunen["vorgang_b"].pk]))
        krumen = _brotkrumen(html)
        assert ">Musterhausen</a>" in krumen and "Alle Kommunen" not in krumen
        assert ">Vorgänge</a>" in krumen
        fuss = _fuss(html)
        assert "Die Daten stammen aus dem Ratsinformationssystem von Musterhausen." in fuss
        assert "Letzter Abgleich: 03.10.2026, 17:40 Uhr." in fuss
        assert 'data-kommune-name="Musterhausen"' in html
        assert f'data-kommune-url="{reverse("insight_core:insight:set_body", args=[b.id])}"' in html

    def test_links_der_brotkrumen_waehlen_erst_die_kommune(self, client: Client, kommunen: dict[str, Any]) -> None:
        b = kommunen["b"]
        krumen = _brotkrumen(
            _html(client, reverse("insight_core:insight:meeting_detail", args=[kommunen["sitzung_b"].pk]))
        )
        waehlen = reverse("insight_core:insight:set_body", args=[b.id])
        assert f'href="{waehlen}?weiter=/insight/"' in krumen
        assert f'href="{waehlen}?weiter=/insight/termine/"' in krumen

        antwort = client.get(f"{waehlen}?weiter=/insight/termine/")
        assert antwort.status_code == 302 and antwort["Location"] == "/insight/termine/"
        assert client.session["active_body_id"] == str(b.id)


class TestFremdeKommune:
    def test_vorgang_von_b_bei_gewaehlter_kommune_a(self, client: Client, kommunen: dict[str, Any]) -> None:
        _waehlen(client, kommunen["a"])
        html = _html(client, reverse("insight_core:insight:paper_detail", args=[kommunen["vorgang_b"].pk]))
        krumen = _brotkrumen(html)
        assert ">Musterhausen</a>" in krumen and "Beispielstadt" not in krumen
        fuss = _fuss(html)
        assert "Ratsinformationssystem von Musterhausen" in fuss and "Beispielstadt" not in fuss
        assert "03.10.2026, 17:40" in fuss and "01.10.2026" not in fuss
        assert 'data-kommune-name="Musterhausen"' in html and 'data-kommune-name="Beispielstadt"' not in html
        # Die Seitenleiste bleibt bei der Wahl: Ihre Bereiche führen zu den Listen von A
        assert "Beispielstadt" in _seitenleiste(html)

    def test_vorgang_mit_einer_zeile_brotkrumen_und_weg_zurueck(self, client: Client, kommunen: dict[str, Any]) -> None:
        _waehlen(client, kommunen["a"])
        html = _html(client, reverse("insight_core:insight:paper_detail", args=[kommunen["vorgang_b"].pk]))
        assert html.count('aria-label="Brotkrumen"') == 1, "nur die Kopfzeile trägt Brotkrumen"
        krumen = _brotkrumen(html)
        assert ">Musterhausen</a>" in krumen and 'aria-current="page"' in krumen and "Radweg Musterhausen" in krumen
        zurueck = re.search(r'<nav aria-label="Zurück"[^>]*>\s*<a href="([^"]*)"', html)
        waehlen = reverse("insight_core:insight:set_body", args=[kommunen["b"].id])
        assert zurueck and zurueck.group(1) == f"{waehlen}?weiter=/insight/vorgaenge/", "am Handy zur Liste von B"

    def test_eigene_kommune_ohne_umweg_ueber_die_wahl(self, client: Client, kommunen: dict[str, Any]) -> None:
        _waehlen(client, kommunen["a"])
        html = _html(client, reverse("insight_core:insight:paper_detail", args=[kommunen["vorgang_a"].pk]))
        krumen = _brotkrumen(html)
        assert 'href="/insight/"' in krumen and 'href="/insight/vorgaenge/"' in krumen and "weiter=" not in krumen
        assert "Ratsinformationssystem von Beispielstadt" in _fuss(html)

    def test_listen_bleiben_bei_der_gewaehlten_kommune(self, client: Client, kommunen: dict[str, Any]) -> None:
        _waehlen(client, kommunen["a"])
        html = _html(client, reverse("insight_core:insight:paper_list"))
        assert "Ratsinformationssystem von Beispielstadt" in _fuss(html)
        assert 'data-kommune-name="Beispielstadt"' in html

    def test_mit_veroeffentlichungsstand_aus_der_middleware(self, client: Client, kommunen: dict[str, Any]) -> None:
        # Hat irgendeine Quelle einen Stand, kennt die Middleware die Kommune des Eintrags schon
        drittquelle = _kommune(3, "Stadt Drittstadt")
        publication.set_source_state(drittquelle.source, publication.ARCHIVED)
        _waehlen(client, kommunen["a"])
        html = _html(client, reverse("insight_core:insight:paper_detail", args=[kommunen["vorgang_b"].pk]))
        assert ">Musterhausen</a>" in _brotkrumen(html)
        assert "Ratsinformationssystem von Musterhausen" in _fuss(html)

    def test_datenstand_hinweis_gilt_der_kommune_der_seite(self, client: Client, kommunen: dict[str, Any]) -> None:
        OParlBody.objects.filter(pk=kommunen["a"].pk).update(last_sync=timezone.now() - timedelta(days=30))
        OParlBody.objects.filter(pk=kommunen["b"].pk).update(last_sync=timezone.now())
        _waehlen(client, kommunen["a"])
        assert "konnten seit" in _html(client, reverse("insight_core:insight:paper_list"))
        fremd = _html(client, reverse("insight_core:insight:paper_detail", args=[kommunen["vorgang_b"].pk]))
        assert "konnten seit" not in fremd, "der veraltete Stand von A gehört nicht auf den Vorgang von B"


class TestWahlMitZiel:
    @pytest.mark.parametrize(
        "ziel",
        [
            "https://fremd.example/",
            "//fremd.example/insight/",
            "/work/",
            "/insight/kommune/alle/",
            "javascript:alert(1)",
        ],
    )
    def test_nur_seiten_des_buergerportals(self, client: Client, kommunen: dict[str, Any], ziel: str) -> None:
        antwort = client.get(reverse("insight_core:insight:set_body", args=[kommunen["b"].id]), {"weiter": ziel})
        assert antwort.status_code == 302 and antwort["Location"] == "/insight/"


def test_fuss_ohne_kommune_nennt_keine_schnittstelle(client: Client, kommunen: dict[str, Any]) -> None:
    client.get(reverse("insight_core:insight:clear_body"))
    fuss = _fuss(_html(client, reverse("insight_core:insight:search")))
    assert "Öffentliche Ratsinformationen der Kommunen aus ihren Ratsinformationssystemen." in fuss
    assert "OParl" not in fuss, "nicht jede Quelle kommt über OParl"
