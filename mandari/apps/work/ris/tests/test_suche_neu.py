# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recherche-Suche in Work im neuen Erscheinungsbild (Issue #853).

Geprüft: Treffer nach Vorgang gruppiert mit Kontextzeile, Stand und Links in Work; Filterleiste wie im Bürgerportal plus
Gremium; alte Links mit Parametern wirken weiter; der Bezug der Fraktion geht als Gewichtung an den Suchdienst und steht
am Treffer; Austausch per HTMX; ohne Suchdienst die Datenbanksuche; die Demo findet „Trinkwasser“; ohne Schalter bleibt
die bisherige Seite.
"""

from __future__ import annotations

import io
import re
from typing import Any

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.work.motions.models import Motion
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlSource
from insight_core.services import search_service

RIS = "https://ris.beispiel.example/oparl"


class _Dienst:
    """Nachbau des Suchdienstes: liefert eine Gruppe je Vorgang und merkt sich die Aufrufe."""

    def __init__(self, vorgaenge: list[OParlPaper]) -> None:
        self.vorgaenge = vorgaenge
        self.aufrufe: list[dict[str, Any]] = []
        self.facetten: list[dict[str, Any]] = []

    def facet_counts(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.facetten.append(kwargs)
        return {"paper_types": {"Beschlussvorlage": 2, "Antrag an den Rat": 1}, "periods": {"12m": 2}}

    def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.aufrufe.append({"query": query, **kwargs})
        gruppen = [
            {
                "kind": "paper",
                "key": str(p.pk),
                "paper": {
                    "id": str(p.pk),
                    "name": p.name,
                    "reference": p.reference,
                    "paper_type": p.paper_type,
                    "organization_names": ["Bauausschuss"],
                    "_formatted": {"name": f'<mark class="x">{p.name}</mark>'},
                },
                "file": {
                    "id": "6d3f1f0a-0000-4000-8000-0000000000f1",
                    "name": "Anlage 1",
                    "text_content": "roh",
                    "_formatted": {"text_content": "der <mark>Radweg</mark>\x07 wird ge- baut"},
                },
                "others": [],
            }
            for p in self.vorgaenge
        ]
        return {
            "groups": gruppen if kwargs.get("page_size", 20) > 1 else [],
            "counts": {"vorgaenge": len(gruppen), "unterlagen": 0, "meetings": 0},
            "page": kwargs.get("page", 1),
            "pages": 1,
            "has_more": False,
            "similar_spelling": False,
            "totals_by_index": {"papers": len(gruppen), "files": 1, "meetings": 0, "persons": 1, "organizations": 0},
        }


@pytest.fixture
def body(org: Any) -> OParlBody:
    quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Beispielstadt")
    OParlOrganization.objects.create(external_id=f"{RIS}/org/bau", body=body, name="Bauausschuss")
    org.body = body
    org.work_new_design = True
    org.save(update_fields=["body", "work_new_design"])
    return body


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["ris.view", "motions.view"], email="recherche@example.org")


@pytest.fixture
def vorgaenge(body: OParlBody) -> list[OParlPaper]:
    return [
        OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{n}", body=body, name=name, reference=f"V/{n}", paper_type="Beschlussvorlage"
        )
        for n, name in enumerate(("Radweg Hauptstraße", "Radweg am Bahnhof"), start=1)
    ]


@pytest.fixture
def dienst(monkeypatch: pytest.MonkeyPatch, vorgaenge: list[OParlPaper]) -> _Dienst:
    fake = _Dienst(vorgaenge)
    monkeypatch.setattr(search_service, "get_search_service", lambda: fake)
    return fake


def _url(org: Any) -> str:
    return reverse("work:ris_search", kwargs={"org_slug": org.slug})


@pytest.mark.django_db
def test_treffer_gruppiert_mit_kontext_bezug_und_links_in_work(
    org: Any, mitglied: Any, client_for: Any, dienst: _Dienst, vorgaenge: list[OParlPaper]
) -> None:
    Motion.objects.create(
        organization=org, author=mitglied, title="Unser Antrag", visibility="organization", related_paper=vorgaenge[1]
    )

    html = client_for(mitglied.user).get(_url(org), {"q": "Radweg"}).content.decode()

    inhalt = html.split('id="inhalt"', 1)[1]
    assert "„Radweg“" in inhalt and "2 Vorgänge" in inhalt
    assert f'href="/work/{org.slug}/ris/papers/{vorgaenge[0].pk}/"' in inhalt
    assert "/insight/vorgaenge/" not in inhalt
    assert "Vorlage" in inhalt and "V/1" in inhalt and "Bauausschuss" in inhalt  # Kontextzeile
    assert "Noch keine Beratung bekannt." in inhalt  # Stand-Satz
    assert "Eigener Antrag" in inhalt
    assert "\x07" not in inhalt and "gebaut" in inhalt  # sauberer Ausschnitt
    # Filterleiste wie im Bürgerportal, Ziel ist die Suche in Work; Gremium-Filter bleibt
    assert f'hx-get="{_url(org)}"' in inhalt and 'name="gremium"' in inhalt and ">Bauausschuss</option>" in inhalt
    assert 'name="period"' in inhalt and 'name="paper_type"' in inhalt and 'name="sort"' in inhalt
    assert "insight_core:insight:search" not in inhalt and 'hx-get="/insight/suche/' not in inhalt
    kennungen = re.findall(r'\sid="([^"]+)"', html)
    assert len(kennungen) == len(set(kennungen))
    assert "<script>" not in inhalt.split("</main>", 1)[0]
    # Bezug geht als Gewichtung an den Suchdienst, nicht als Filter
    boost = dienst.aufrufe[0]["boost"]
    assert {str(vorgaenge[1].pk)} in [ids for ids, _f in boost.papers]
    assert dienst.aufrufe[0]["body_ids"] == [str(org.body_id)]


@pytest.mark.django_db
def test_ergebnisbereich_und_kontext_wie_im_buergerportal(
    org: Any, mitglied: Any, client_for: Any, dienst: _Dienst, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Ein Kontextaufbau und ein Ergebnisbereich für beide Suchseiten (keine Kopie in Work)."""
    from apps.work.ris import suche
    from insight_core.services import search_page

    aufrufe: list[dict[str, Any]] = []
    original = search_page.build_context

    def mitschnitt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        aufrufe.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(suche, "build_context", mitschnitt)
    settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
    client = client_for(mitglied.user)

    antwort = client.get(_url(org), {"q": "Radweg"})
    teil = client.get(_url(org), {"q": "Radweg"}, headers={"HX-Request": "true"})

    assert aufrufe and aufrufe[0]["custom_period"] is True and aufrufe[0]["boost"] is not None
    assert "partials/search_page.html" in [t.name for t in antwort.templates]
    assert [t.name for t in teil.templates][0] == "partials/search_page.html"
    html = antwort.content.decode().split('id="inhalt"', 1)[1]
    # Im Rahmen von Work: Abschnitt ohne Band des Bürgerportals, kein Abo-Link des Bürgerportals
    abschnitt = re.search(r"<section[^>]*treffer-titel[^>]*>", html)
    assert abschnitt and "class=" not in abschnitt.group(0) and "max-w-insight" not in html
    assert "/insight/benachrichtigungen" not in html and "Bei neuen Treffern" not in html
    assert 'value="frei"' in html  # Option „Eigener Zeitraum“


@pytest.mark.django_db
def test_alte_links_mit_parametern_wirken_weiter(org: Any, mitglied: Any, client_for: Any, dienst: _Dienst) -> None:
    antwort = client_for(mitglied.user).get(
        _url(org),
        {
            "q": "Radweg",
            "von": "2025-01-01",
            "bis": "2025-12-31",
            "gremium": "Bauausschuss",
            "art": "Antrag an den Rat",
            "typ": "papers",
        },
    )

    assert antwort.status_code == 200
    aufruf = dienst.aufrufe[0]
    assert (aufruf["date_from"], aufruf["date_to"], aufruf["organization_name"]) == (
        "2025-01-01",
        "2025-12-31",
        "Bauausschuss",
    )
    assert aufruf["paper_type"] == ["Antrag an den Rat"] and aufruf["kinds"] == {"paper"}
    assert aufruf["file_paper_filter"] is not None
    assert dienst.facetten[0]["organization_name"] == "Bauausschuss"
    html = antwort.content.decode()
    assert "Zeitraum: 01.01.2025 bis 31.12.2025" in html and "Gremium: Bauausschuss" in html
    assert "Art: Antrag an den Rat" in html and 'value="2025-01-01"' in html


@pytest.mark.django_db
def test_htmx_tauscht_nur_den_ergebnisbereich(org: Any, mitglied: Any, client_for: Any, dienst: _Dienst) -> None:
    client = client_for(mitglied.user)

    teil = client.get(_url(org), {"q": "Radweg", "period": "12m"}, headers={"HX-Request": "true"})
    assert teil.status_code == 200 and "HX-Request" in teil["Vary"]
    html = teil.content.decode()
    assert (
        "<html" not in html and 'hx-swap-oob="innerHTML:#suche-titel"' in html and "Zeitraum: Letzte 12 Monate" in html
    )

    weiter = client.get(_url(org), {"q": "Radweg", "page": "2"}, headers={"HX-Request": "true"}).content.decode()
    assert "<ol" not in weiter and 'hx-swap-oob="true"' in weiter

    leer = client.get(_url(org), {"q": "R"}, headers={"HX-Request": "true"})
    assert leer.content == b""
    ganz = client.get(_url(org), {"q": "Radweg"}, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert "<html" in ganz.content.decode()


@pytest.mark.django_db
def test_ohne_suchdienst_datenbanksuche_im_selben_format(
    org: Any,
    body: OParlBody,
    mitglied: Any,
    client_for: Any,
    monkeypatch: pytest.MonkeyPatch,
    vorgaenge: list[OParlPaper],
) -> None:
    class Kaputt:
        def facet_counts(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("kein Elasticsearch")

    monkeypatch.setattr(search_service, "get_search_service", lambda: Kaputt())
    Motion.objects.create(
        organization=org, author=mitglied, title="Unser Antrag", visibility="organization", related_paper=vorgaenge[1]
    )
    OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=body, name="Radweg-Begehung")

    html = client_for(mitglied.user).get(_url(org), {"q": "Radweg"}).content.decode()

    assert "Die Volltextsuche ist gerade nicht erreichbar" in html and "3 Treffer in Titeln und Aktenzeichen" in html
    treffer = html.split('id="treffer-liste"', 1)[1]
    # Eigener Antrag zuerst, Links in Work
    assert treffer.index("Radweg am Bahnhof") < treffer.index("Radweg Hauptstraße") < treffer.index("Radweg-Begehung")
    assert f"/work/{org.slug}/ris/papers/{vorgaenge[1].pk}/" in treffer and "Eigener Antrag" in treffer


@pytest.mark.django_db
def test_ohne_schalter_bleibt_die_bisherige_seite(
    org: Any, body: OParlBody, mitglied: Any, client_for: Any, dienst: _Dienst
) -> None:
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])

    antwort = client_for(mitglied.user).get(_url(org), {"q": "Radweg"})

    assert [t.name for t in antwort.templates][0] == "work/ris/search.html"
    assert not dienst.aufrufe  # die bisherige Seite fragt search_all, nicht die gruppierte Suche


@pytest.mark.django_db
def test_demo_findet_trinkwasser_auch_ohne_suchdienst(client_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Demo hat keinen Suchdienst; nach dem nächtlichen Neuaufbau (setup_demo_environment) findet sie trotzdem."""
    from apps.common.management.commands.setup_demo_environment import DEMO_ORG_SLUG
    from apps.tenants.models import Membership

    class Kaputt:
        def facet_counts(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("kein Elasticsearch")

    monkeypatch.setattr(search_service, "get_search_service", lambda: Kaputt())
    call_command("setup_demo_environment", stdout=io.StringIO())
    vorsitz = Membership.objects.get(organization__slug=DEMO_ORG_SLUG, user__email__startswith="demo-vorsitz@")

    html = client_for(vorsitz.user).get(
        reverse("work:ris_search", kwargs={"org_slug": DEMO_ORG_SLUG}), {"q": "Trinkwasser"}
    )

    treffer = html.content.decode().split('id="treffer-liste"', 1)[1]
    assert "Trinkwasserbrunnen" in treffer and "Eigener Antrag" in treffer


@pytest.mark.django_db
def test_ohne_suchdienst_alle_anzeigen_je_art(
    org: Any, body: OParlBody, mitglied: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Datenbanksuche zeigt je Art höchstens zehn Treffer; wie bisher führt „Alle anzeigen“ zur vollen Liste."""

    class Kaputt:
        def facet_counts(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("kein Elasticsearch")

    monkeypatch.setattr(search_service, "get_search_service", lambda: Kaputt())
    for n in range(12):
        OParlPaper.objects.create(
            external_id=f"{RIS}/paper/viele-{n}", body=body, name=f"Radweg Abschnitt {chr(65 + n)}", reference=f"R/{n}"
        )
    OParlMeeting.objects.create(external_id=f"{RIS}/meeting/begehung", body=body, name="Radweg-Begehung")
    client = client_for(mitglied.user)

    html = client.get(_url(org), {"q": "Radweg"}).content.decode()

    treffer = html.split('id="treffer-liste"', 1)[1].split("</ol>", 1)[0]
    assert treffer.count("Radweg Abschnitt") == 10 and "Radweg-Begehung" in treffer
    alle = html.split('id="alle-anzeigen"', 1)[1].split("</p>", 1)[0]
    vorgaenge = f"/work/{org.slug}/ris/papers/?q=Radweg"
    assert "Alle anzeigen" in alle and f'href="{vorgaenge}"' in alle
    assert f'href="/work/{org.slug}/ris/meetings/?q=Radweg"' in alle
    assert "/ris/persons/" not in alle and "/ris/organizations/" not in alle  # ohne Treffer dieser Arten
    # Über „Alle anzeigen“ sind auch die Vorgänge jenseits der ersten zehn erreichbar
    liste = client.get(vorgaenge).content.decode()
    assert all(f"Radweg Abschnitt {chr(65 + n)}" in liste for n in range(12))


@pytest.mark.django_db
def test_filter_reiter_und_feld_in_den_massen_von_work(
    org: Any, mitglied: Any, client_for: Any, dienst: _Dienst
) -> None:
    """Gemeinsame Bausteine mit dicht: Bedienelemente 32 px, Schrift 14 px; das Suchfeld als Hauptaktion 40 px."""
    html = client_for(mitglied.user).get(_url(org), {"q": "Radweg"}).content.decode()

    bereich = html.split('id="suchergebnis"', 1)[1]
    assert "min-h-11" not in bereich
    summary = re.search(r"<summary[^>]*>", bereich)
    assert summary and "min-h-8" in summary.group(0) and "text-sm" in summary.group(0)
    for kennung in ("suche-gremium", "suche-sortierung"):
        feld = re.search(rf'<select id="{kennung}"[^>]*>', bereich, re.S)
        assert feld and "min-h-8" in feld.group(0) and "text-sm" in feld.group(0), kennung
    reiter = bereich.split('aria-label="Arten der Treffer"', 1)[1].split("</nav>", 1)[0]
    assert "min-h-8" in reiter
    eingabe = re.search(r'<input id="suche-eingabe"[^>]*>', html, re.S)
    assert eingabe and "min-h-10" in eingabe.group(0) and "autofocus" not in eingabe.group(0)
    assert html.count('id="suche-form"') == 1
