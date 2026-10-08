# SPDX-License-Identifier: AGPL-3.0-or-later
"""Werkzeuge des KI-Assistenten (Issue #899): öffentliche Ratsdaten der gewählten Kommune, kompakt, mit Links."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core.cache import cache

from insight_ai.services import chat_tools
from insight_ai.services.chat_tools import TOOL_NAMES, TOOLS, ToolContext, context_for, run_tool, select_sources
from insight_core import publication
from insight_core.models import OParlOrganization

from .musterstadt import JETZT, FakeSuche, Musterstadt, baue_musterstadt, kennung

pytestmark = pytest.mark.django_db


@pytest.fixture
def stadt() -> Musterstadt:
    return baue_musterstadt()


@pytest.fixture
def ctx(stadt: Musterstadt) -> ToolContext:
    kontext = context_for(stadt.body.pk, now=JETZT)
    assert kontext is not None
    return kontext


@pytest.fixture
def suche(monkeypatch: pytest.MonkeyPatch) -> FakeSuche:
    fake = FakeSuche()
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: fake)
    return fake


def rufe(ctx: ToolContext, werkzeug: str, /, **args: Any) -> dict[str, Any]:
    ergebnis = json.loads(run_tool(werkzeug, json.dumps(args), ctx))
    assert isinstance(ergebnis, dict)
    return ergebnis


def link(name: str, pk: object) -> str:
    return {"meeting": f"/insight/termine/{pk}/", "paper": f"/insight/vorgaenge/{pk}/"}[name]


# --- Beschreibung -------------------------------------------------------------------------------------


def test_werkzeuge_sind_openai_funktionen() -> None:
    assert {
        "sitzungen_im_zeitraum",
        "sitzung",
        "vorgaenge_suchen",
        "vorgang",
        "gremien",
        "personen",
        "dokumente_suchen",
        "dokument_abschnitt",
    } == TOOL_NAMES
    for tool in TOOLS:
        assert tool["type"] == "function"
        parameter = tool["function"]["parameters"]
        assert parameter["type"] == "object"
        assert set(parameter["required"]) <= set(parameter["properties"])
    # Die Beschreibung geht mit jeder Runde an das Modell: knapp halten
    assert len(json.dumps(TOOLS, ensure_ascii=False)) < 3500


# --- Sitzungen ------------------------------------------------------------------------------------------


def test_sitzungen_diese_woche_mit_links(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "sitzungen_im_zeitraum", von="2026-10-05", bis="2026-10-11")

    assert ergebnis["spalten"] == "Beginn | Gremium | Ort | Link | abgesagt"
    assert ergebnis["sitzungen"] == [
        f"Mi 07.10.2026 17:00 | Rat | Rathaus, Ratssaal | {link('meeting', stadt.rat_sitzung.pk)}",
        f"Do 08.10.2026 16:00 | Ausschuss für Umwelt und Verkehr | - | {link('meeting', stadt.ausschuss_sitzung.pk)}",
        "Fr 09.10.2026 09:00 | Ausschuss für Umwelt und Verkehr | - | "
        f"{link('meeting', stadt.abgesagte_sitzung.pk)} | abgesagt",
    ]
    assert ergebnis["anzahl"] == 3
    assert ergebnis["kommune"] == "Stadt Musterstadt"
    # Gelöschte Sitzungen, spätere Wochen und andere Kommunen fehlen
    text = json.dumps(ergebnis, ensure_ascii=False)
    assert str(stadt.geloeschte_sitzung.pk) not in text
    assert str(stadt.spaetere_sitzung.pk) not in text
    assert str(stadt.fremde_sitzung.pk) not in text


def test_sitzungen_eines_gremiums(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "sitzungen_im_zeitraum", von="2026-10-07", bis="2026-10-07", gremium="rat")
    assert [s.split(" | ")[3] for s in ergebnis["sitzungen"]] == [link("meeting", stadt.rat_sitzung.pk)]
    assert ergebnis["gremium"] == ["Rat"]

    unbekannt = rufe(ctx, "sitzungen_im_zeitraum", von="2026-10-07", bis="2026-10-07", gremium="Kulturbeirat")
    assert "fehler" in unbekannt


def test_tagesordnungen_gleich_mitliefern(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "sitzungen_im_zeitraum", von="2026-10-07", bis="2026-10-07", gremium="Rat", tagesordnung=True)
    (tagesordnung,) = ergebnis["tagesordnungen"]
    assert tagesordnung["link"] == link("meeting", stadt.rat_sitzung.pk)
    assert tagesordnung["tagesordnung"][2] == "TOP 3: nichtöffentlich"

    woche = rufe(ctx, "sitzungen_im_zeitraum", von="2026-09-01", bis="2026-10-31", tagesordnung=True)
    assert "tagesordnungen" not in woche
    assert "einzeln" in woche["hinweis_tagesordnung"]


def test_zeitraum_wird_begrenzt_und_geprueft(ctx: ToolContext) -> None:
    lang = rufe(ctx, "sitzungen_im_zeitraum", von="2026-01-01", bis="2026-12-31")
    assert lang["zeitraum"] == "01.01.2026–03.04.2026"
    assert "gekürzt" in lang["hinweis"]
    assert "fehler" in rufe(ctx, "sitzungen_im_zeitraum", von="morgen", bis="2026-10-11")
    assert "fehler" in rufe(ctx, "sitzungen_im_zeitraum", bis="2026-10-11")


def test_tagesordnung_ohne_nichtoeffentliche_inhalte(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "sitzung", id=link("meeting", stadt.rat_sitzung.pk))

    assert ergebnis["titel"] == "Rat"
    assert ergebnis["name"] == "Sitzung des Rates"
    # Gleicher Titel wie der Punkt: bei der Vorlage nur Drucksachennummer und Link
    assert ergebnis["tagesordnung"] == [
        "TOP 1: Eröffnung der Sitzung",
        f"TOP 2: Radweg an der Musterstraße | Vorlage V/2026/0123 {link('paper', stadt.vorlage.pk)}",
        "TOP 3: nichtöffentlich",
    ]
    text = json.dumps(ergebnis, ensure_ascii=False)
    assert "Parzelle 7" not in text
    assert "beschlossen" not in text


def test_sitzung_anderer_kommune_oder_geloescht_nicht_gefunden(ctx: ToolContext, stadt: Musterstadt) -> None:
    assert rufe(ctx, "sitzung", id=str(stadt.fremde_sitzung.pk)) == {"fehler": "Sitzung nicht gefunden."}
    assert rufe(ctx, "sitzung", id=str(stadt.geloeschte_sitzung.pk)) == {"fehler": "Sitzung nicht gefunden."}
    assert "fehler" in rufe(ctx, "sitzung", id="keine-kennung")


# --- Vorgänge -------------------------------------------------------------------------------------------


def test_vorgang_nach_drucksachennummer_gleich_mit_einzelheiten(
    ctx: ToolContext, stadt: Musterstadt, suche: FakeSuche
) -> None:
    ergebnis = rufe(ctx, "vorgaenge_suchen", text="v/2026/0123")

    assert ergebnis["treffer"] == "Drucksachennummer"
    vorgang = ergebnis["vorgang"]
    assert vorgang["link"] == link("paper", stadt.vorlage.pk) and vorgang["az"] == "V/2026/0123"
    assert vorgang["stand"].startswith("Am 15.09.2026 im Ausschuss für Umwelt und Verkehr")
    assert "Nächste Beratung am 07.10.2026 im Rat" in vorgang["stand"]
    assert len(vorgang["beratungsfolge"]) == 2
    # Genauer Treffer in der Kommune (die gleiche Nummer im Nachbarort zählt nicht): keine Volltextsuche nötig
    assert suche.aufrufe == []


def test_vorgaenge_mit_stand_aus_der_volltextsuche(ctx: ToolContext, stadt: Musterstadt, suche: FakeSuche) -> None:
    suche.treffer["papers"] = [{"id": str(stadt.vorlage.pk)}]
    ergebnis = rufe(ctx, "vorgaenge_suchen", text="Radweg")

    (treffer,) = ergebnis["vorgaenge"]
    assert treffer["link"] == link("paper", stadt.vorlage.pk)
    assert treffer["stand"].startswith("Am 15.09.2026 im Ausschuss für Umwelt und Verkehr einstimmig empfohlen.")
    # Die Suche ist auf die Kommune beschränkt
    assert suche.aufrufe[0]["body_id"] == str(stadt.body.pk)
    assert suche.aufrufe[0]["index_names"] == ["papers"]


def test_vorgaenge_aus_der_volltextsuche_nur_oeffentlich_und_eigene_kommune(
    ctx: ToolContext, stadt: Musterstadt, suche: FakeSuche
) -> None:
    stadt.grundstueck.mark_deleted()
    suche.treffer["papers"] = [
        {"id": str(stadt.fremde_vorlage.pk)},
        {"id": str(stadt.grundstueck.pk)},
        {"id": str(stadt.vorlage.pk)},
    ]
    ergebnis = rufe(ctx, "vorgaenge_suchen", text="Radweg", gremium="Ausschuss für Umwelt und Verkehr")
    assert [v["link"] for v in ergebnis["vorgaenge"]] == [link("paper", stadt.vorlage.pk)]
    assert suche.aufrufe[0]["organization_name"] == "Ausschuss für Umwelt und Verkehr"


def test_vorgaenge_ohne_volltextsuche_aus_der_datenbank(monkeypatch: pytest.MonkeyPatch, ctx: ToolContext) -> None:
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: FakeSuche(fehler=True))
    ergebnis = rufe(ctx, "vorgaenge_suchen", text="Radweg Musterstraße")
    assert [v["titel"] for v in ergebnis["vorgaenge"]] == ["Radweg an der Musterstraße"]
    leer = rufe(ctx, "vorgaenge_suchen", text="Schwimmbad")
    assert leer["anzahl"] == 0 and "hinweis" in leer


def test_vorgang_mit_beratungsfolge_und_beschluss(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "vorgang", id=str(stadt.vorlage.pk))

    assert ergebnis["link"] == link("paper", stadt.vorlage.pk)
    assert ergebnis["beratungsfolge"] == [
        "Di 15.09.2026 16:00 | Ausschuss für Umwelt und Verkehr | TOP 4 | Vorberatung | "
        f"Ergebnis: einstimmig empfohlen | {link('meeting', stadt.vergangene_sitzung.pk)}",
        f"Mi 07.10.2026 17:00 | Rat | TOP 2 | Entscheidung | entscheidend | {link('meeting', stadt.rat_sitzung.pk)}",
    ]
    assert ergebnis["beschluss"].startswith("Der Ausschuss empfiehlt dem Rat")
    assert "Nächste Beratung am 07.10.2026 im Rat" in ergebnis["stand"]
    # Entfernte Dokumente erscheinen nicht, Volltext nie
    assert ergebnis["dokumente"] == [{"id": str(stadt.datei.pk), "name": "Begründung"}]
    assert "Radweg erhält eine rote Markierung" not in json.dumps(ergebnis, ensure_ascii=False)


def test_vorgang_nichtoeffentlich_beraten_ohne_ergebnis(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "vorgang", id=str(stadt.grundstueck.pk))
    (schritt,) = ergebnis["beratungsfolge"]
    assert "| nichtöffentlich |" in schritt
    assert "Ergebnis" not in schritt
    assert "beschluss" not in ergebnis
    assert "beschlossen" not in json.dumps(ergebnis, ensure_ascii=False)


def test_vorgang_anderer_kommune_nicht_gefunden(ctx: ToolContext, stadt: Musterstadt) -> None:
    assert rufe(ctx, "vorgang", id=str(stadt.fremde_vorlage.pk)) == {"fehler": "Vorgang nicht gefunden."}


# --- Gremien und Personen -------------------------------------------------------------------------------


def test_gremien_nur_bestehende(ctx: ToolContext, stadt: Musterstadt) -> None:
    beirat = OParlOrganization.objects.create(
        external_id=kennung("organizations"), body=stadt.body, name="Seniorenvertretung", classification="Beirat"
    )
    ergebnis = rufe(ctx, "gremien")
    # Die Art nur, wenn der Name sie nicht schon nennt
    assert ergebnis["gremien"] == [
        f"Ausschuss für Umwelt und Verkehr | /insight/gremien/{stadt.ausschuss.pk}/",
        f"Rat | /insight/gremien/{stadt.rat.pk}/",
        f"Seniorenvertretung (Beirat) | /insight/gremien/{beirat.pk}/",
    ]
    assert rufe(ctx, "gremien", suchtext="umwelt")["anzahl"] == 1


def test_personen_ohne_kontaktdaten(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "personen", name="Mustermann")
    person = ergebnis["personen"][0]
    assert person["name"] == "Erika Mustermann"
    assert person["link"] == f"/insight/personen/{stadt.person.pk}/"
    # Nur laufende Mitgliedschaften
    assert person["mitgliedschaften"] == [f"Rat (Vorsitz) | /insight/gremien/{stadt.rat.pk}/"]
    text = json.dumps(ergebnis, ensure_ascii=False)
    assert "@" not in text and "56789" not in text
    assert "fehler" in rufe(ctx, "personen", name="E")


# --- Dokumente ------------------------------------------------------------------------------------------


def test_dokumentsuche_liefert_kurze_ausschnitte(ctx: ToolContext, stadt: Musterstadt, suche: FakeSuche) -> None:
    suche.treffer["files"] = [
        {"id": str(stadt.fremde_datei.pk), "text_content": "Fremd"},
        {"id": str(stadt.datei_entfernt.pk), "text_content": "Entfernter Text"},
        {
            "id": str(stadt.datei.pk),
            "text_content": stadt.datei.text_content,
            "_formatted": {"text_content": 'Der <mark class="x">Radweg</mark> an der Musterstraße wird verbreitert'},
        },
    ]
    ergebnis = rufe(ctx, "dokumente_suchen", text="Radweg")

    assert ergebnis["dokumente"] == [
        {
            "id": str(stadt.datei.pk),
            "name": "Begründung",
            "vorgang": "Radweg an der Musterstraße",
            "az": "V/2026/0123",
            "link": link("paper", stadt.vorlage.pk),
            "ausschnitt": "Der Radweg an der Musterstraße wird verbreitert",
        }
    ]
    assert suche.aufrufe[0]["index_names"] == ["files"]
    assert suche.aufrufe[0]["body_id"] == str(stadt.body.pk)


def test_dokumentsuche_ohne_suchdienst(monkeypatch: pytest.MonkeyPatch, ctx: ToolContext) -> None:
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: FakeSuche(fehler=True))
    assert "fehler" in rufe(ctx, "dokumente_suchen", text="Radweg")


def test_dokument_abschnitt_laedt_nur_den_passenden_teil(ctx: ToolContext, stadt: Musterstadt) -> None:
    ergebnis = rufe(ctx, "dokument_abschnitt", id=str(stadt.datei.pk), frage="Wie breit wird der Radweg?")

    texte = " ".join(abschnitt["text"] for abschnitt in ergebnis["abschnitte"])
    assert "auf 2,50 m verbreitert" in texte
    assert len(texte) <= chat_tools.MAX_PASSAGE_TOTAL
    assert ergebnis["laenge_zeichen"] == len(stadt.datei.text_content or "")
    assert ergebnis["link"] == link("paper", stadt.vorlage.pk)

    kosten = rufe(ctx, "dokument_abschnitt", id=str(stadt.datei.pk), frage="Was kostet die Maßnahme?")
    assert "480.000 Euro" in " ".join(abschnitt["text"] for abschnitt in kosten["abschnitte"])


def test_dokument_abschnitt_nur_oeffentliche_dateien_der_kommune(ctx: ToolContext, stadt: Musterstadt) -> None:
    nicht_gefunden = {"fehler": "Dokument nicht gefunden."}
    assert rufe(ctx, "dokument_abschnitt", id=str(stadt.fremde_datei.pk), frage="x") == nicht_gefunden
    assert rufe(ctx, "dokument_abschnitt", id=str(stadt.datei_entfernt.pk), frage="x") == nicht_gefunden
    stadt.vorlage.mark_deleted()
    assert rufe(ctx, "dokument_abschnitt", id=str(stadt.datei.pk), frage="x") == nicht_gefunden


# --- Rahmen ---------------------------------------------------------------------------------------------


def test_fehler_haben_feste_texte(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    assert json.loads(run_tool("unbekannt", "{}", ctx)) == {"fehler": "Unbekanntes Werkzeug."}
    assert json.loads(run_tool("gremien", "{kaputt", ctx)) == {"fehler": "Argumente sind kein gültiges JSON."}

    def kaputt(ctx: ToolContext, args: Any) -> dict[str, Any]:
        raise RuntimeError("geheimer interner Text")

    monkeypatch.setitem(chat_tools.HANDLERS, "gremien", kaputt)
    assert json.loads(run_tool("gremien", "{}", ctx)) == {"fehler": "Das Werkzeug ist gerade nicht verfügbar."}


def test_kein_kontext_fuer_abgeschaltete_oder_unbekannte_kommune(stadt: Musterstadt) -> None:
    assert context_for("keine-kennung", now=JETZT) is None
    publication.set_source_state(stadt.body.source, publication.PAUSED)
    cache.clear()
    try:
        assert context_for(stadt.body.pk, now=JETZT) is None
    finally:
        publication.set_source_state(stadt.body.source, None)
        cache.clear()
    assert context_for(stadt.body.pk, now=JETZT) is not None


def test_quellen_sind_die_verlinkten_eintraege(ctx: ToolContext, stadt: Musterstadt) -> None:
    rufe(ctx, "sitzungen_im_zeitraum", von="2026-10-05", bis="2026-10-11")
    rufe(ctx, "vorgang", id=str(stadt.vorlage.pk))
    antwort = f"Am Mittwoch tagt der [Rat]({link('meeting', stadt.rat_sitzung.pk)})."
    assert [q["url"] for q in select_sources(ctx, antwort)] == [link("meeting", stadt.rat_sitzung.pk)]
    # Ohne Links: die Einträge der Detailwerkzeuge
    assert [q["url"] for q in select_sources(ctx, "Keine Links.")] == [link("paper", stadt.vorlage.pk)]


def test_links_mit_adresse_fuer_fremde_werkzeuge(stadt: Musterstadt) -> None:
    kontext = context_for(stadt.body.pk, now=JETZT, link_base="https://insight.example")
    assert kontext is not None
    ergebnis = rufe(kontext, "sitzung", id=str(stadt.rat_sitzung.pk))
    assert ergebnis["link"] == f"https://insight.example/insight/termine/{stadt.rat_sitzung.pk}/"
