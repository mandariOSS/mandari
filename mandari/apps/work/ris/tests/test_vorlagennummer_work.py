# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suche nach Vorlagennummern in Work (Issue #1102): Vorgänge-Liste und Recherche-Suche nutzen dieselbe Funktion der
Drehscheibe wie das Bürgerportal (``hub.ris.selectors.search_papers_in``, ``hub.ris.vorlagennummer``).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from django.urls import reverse

from insight_core.models import OParlBody, OParlPaper, OParlSource
from insight_core.services import search_service

pytestmark = pytest.mark.django_db

RIS = "https://ris.nummer.example/oparl"


@pytest.fixture
def body(org: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Nummernstadt")
    org.body = body
    org.work_new_design = True
    org.save(update_fields=["body", "work_new_design"])
    return body


@pytest.fixture
def vorgaenge(body: OParlBody) -> dict[str, OParlPaper]:
    def anlegen(reference: str, name: str, tag: int) -> OParlPaper:
        return OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{reference}", body=body, reference=reference, name=name, date=date(2026, 3, tag)
        )

    return {
        "vorlage": anlegen("V/0989/2017", "Sanierung Stadthalle", 1),
        "folge": anlegen("V/0989/2017/1", "Sanierung Stadthalle, Ergänzung", 9),
        "andere": anlegen("A-R/0052/2026", "Anfrage Radweg", 5),
    }


@pytest.fixture
def client(org: Any, make_member: Any, client_for: Any) -> Any:
    return client_for(make_member(org, ["ris.view"], email="nummer@example.org").user)


def _liste(org: Any) -> str:
    return reverse("work:ris_papers", kwargs={"org_slug": org.slug})


@pytest.mark.parametrize("eingabe", ["V/0989/2017", "V 0989 2017", "V/989/2017", "V0989/2017", "0989/2017"])
def test_vorgaenge_finden_jede_schreibweise(org: Any, client: Any, vorgaenge: dict[str, Any], eingabe: str) -> None:
    html = client.get(_liste(org), {"q": eingabe}).content.decode()

    liste = html.split('data-testid="vorgangsliste"', 1)[1]
    titel = re.findall(r">\s*(Sanierung Stadthalle[^<]*?)\s*</a>", liste)
    assert titel == ["Sanierung Stadthalle", "Sanierung Stadthalle, Ergänzung"]
    assert "Anfrage Radweg" not in liste
    assert len(re.findall(r"bg-primary-50/70", liste)) == 1


def test_suchen_oeffnet_den_einzigen_treffer(org: Any, client: Any, vorgaenge: dict[str, Any]) -> None:
    direkt = client.get(_liste(org), {"q": "ar 52 2026", "direkt": "1"})

    ziel = reverse("work:ris_paper_detail", kwargs={"org_slug": org.slug, "paper_id": vorgaenge["andere"].pk})
    assert direkt.status_code == 302 and direkt["Location"] == ziel
    # Auswahl von Art oder Jahr (ohne den Button) und mehrere Treffer: die Liste
    assert client.get(_liste(org), {"q": "ar 52 2026"}).status_code == 200
    assert client.get(_liste(org), {"q": "V 989 2017", "direkt": "1"}).status_code == 200
    html = client.get(_liste(org)).content.decode()
    assert re.search(r'<button type="submit"[^>]*name="direkt" value="1"', html)


def test_leerzustand_bei_fehlender_nummer(org: Any, client: Any, vorgaenge: dict[str, Any]) -> None:
    html = client.get(_liste(org), {"q": "V/0591/2026"}).content.decode()

    assert "Die Vorlage V/0591/2026 liegt uns noch nicht vor." in html
    assert "Für diese Auswahl gibt es keine Vorgänge" not in html
    jahr = client.get(_liste(org), {"q": "V/0591/2026", "year": "2026"}).content.decode()
    assert "liegt uns noch nicht vor" not in jahr and "Für diese Auswahl gibt es keine Vorgänge" in jahr


class _Dienst:
    """Suchdienst ohne die Nummer: findet nur einen Vorgang über den Text."""

    def __init__(self, treffer: list[OParlPaper]) -> None:
        self.treffer = treffer

    def facet_counts(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"paper_types": {}, "periods": {}}

    def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
        gruppen = [
            {"kind": "paper", "key": str(p.pk), "paper": {"id": str(p.pk), "name": p.name, "reference": p.reference}}
            for p in self.treffer
        ]
        return {
            "groups": gruppen,
            "counts": {"vorgaenge": len(gruppen), "unterlagen": 0, "meetings": 0},
            "page": 1,
            "pages": 1,
            "has_more": False,
            "similar_spelling": False,
            "totals_by_index": {"papers": len(gruppen), "files": 0, "meetings": 0, "persons": 0, "organizations": 0},
        }


def test_work_suche_heftet_die_nummer_an(
    org: Any, client: Any, vorgaenge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_service, "get_search_service", lambda: _Dienst([vorgaenge["andere"]]))

    html = client.get(reverse("work:ris_search", kwargs={"org_slug": org.slug}), {"q": "V0989/2017"}).content.decode()

    liste = html.split('id="treffer-liste"', 1)[1].split("</ol>", 1)[0]
    assert liste.index("Sanierung Stadthalle</a>") < liste.index("Ergänzung") < liste.index("Anfrage Radweg")
    assert liste.count("Gesuchte Vorlagennummer") == 1
    ziel = reverse("work:ris_paper_detail", kwargs={"org_slug": org.slug, "paper_id": vorgaenge["vorlage"].pk})
    assert f'href="{ziel}"' in liste


def test_work_datenbanksuche_findet_die_nummer(
    org: Any, client: Any, vorgaenge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def kaputt() -> Any:
        raise ConnectionError("Suchdienst aus")

    monkeypatch.setattr(search_service, "get_search_service", kaputt)

    html = client.get(reverse("work:ris_search", kwargs={"org_slug": org.slug}), {"q": "V 989 2017"}).content.decode()

    assert "Treffer in Titeln und Aktenzeichen" in html
    liste = html.split('id="treffer-liste"', 1)[1].split("</ol>", 1)[0]
    assert liste.index("Sanierung Stadthalle</a>") < liste.index("Ergänzung")
    assert "Gesuchte Vorlagennummer" in liste
