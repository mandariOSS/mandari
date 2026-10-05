# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``manage.py suchindex_schatten``: Vollbau, Vergleich, Stand und Aufräumen des Schattenindex (Issue #526).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.events.models import ParkedEvent, Subscription, SubscriptionState
from apps.events.tests.hilfen import nummeriert
from insight_core.models import OParlBody, OParlMeeting, OParlPaper
from insight_core.services.search_documents import meeting_to_doc
from insight_search.indices import index_configs, synonyms
from insight_search.schatten import differing_fields
from insight_search.tests.fake_es import FakeElasticsearch

Kommune = Callable[[str], OParlBody]


def _befehl(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("suchindex_schatten", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def _vorgang(body: OParlBody, name: str = "Vorlage") -> OParlPaper:
    return OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body, name=name)


def _sitzung(body: OParlBody, name: str = "Rat") -> OParlMeeting:
    return OParlMeeting.objects.create(external_id=f"https://ris.example/meeting/{uuid.uuid4()}", body=body, name=name)


# --- aufbauen -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_vollbau_schreibt_gewaehlte_kommune_mit_hoechster_folgenummer(
    settings: Any, es: FakeElasticsearch, kommune: Kommune
) -> None:
    body, andere = kommune("Gewählt"), kommune("Andere")
    vorgang, fremd = _vorgang(body), _vorgang(andere)
    OParlPaper.objects.create(external_id="https://ris.example/paper/weg", body=body, name="weg", deleted=True)
    letzte = nummeriert()
    settings.SEARCH_INDEX_SHADOW_BODIES = [str(body.pk)]

    ausgabe = _befehl("aufbauen", "--index", "papers")

    assert "Vollbau abgeschlossen" in ausgabe
    docs = es.indizes["schatten-papers"].docs
    assert set(docs) == {str(vorgang.pk)}  # ohne Löschmarkierte und ohne andere Kommunen
    assert docs[str(vorgang.pk)].version == letzte.seq
    assert str(fremd.pk) not in docs
    assert es.indizes["schatten-papers"].mappings == index_configs(synonyms())["papers"]["mappings"]


@pytest.mark.django_db
def test_vollbau_trocken_und_grenzen(settings: Any, es: FakeElasticsearch, kommune: Kommune) -> None:
    body = kommune("Beispielstadt")
    _vorgang(body), _vorgang(body)

    with pytest.raises(CommandError, match="--alle"):
        _befehl("aufbauen")

    ausgabe = _befehl("aufbauen", "--kommune", str(body.pk), "--trocken")
    assert "Gesamt 2 Dokumente" in ausgabe and "Trockenlauf" in ausgabe
    assert not es.indizes

    settings.SEARCH_INDEX_SHADOW_MAX_DOCS = 1
    with pytest.raises(CommandError, match="Obergrenze"):
        _befehl("aufbauen", "--kommune", str(body.pk))
    assert not es.indizes

    with pytest.raises(CommandError, match="UUID"):
        _befehl("aufbauen", "--kommune", "keine-kennung")


# --- vergleichen --------------------------------------------------------------------------------


def test_abweichende_felder_ohne_reihenfolge_von_listen() -> None:
    live = {"id": "1", "organization_names": ["Rat", "Ausschuss"], "name": "A"}
    assert differing_fields(live, {"id": "1", "organization_names": ["Ausschuss", "Rat"], "name": "A"}) == []
    assert differing_fields(live, {"id": "1", "organization_names": [], "name": "B", "neu": None}) == [
        "name",
        "neu",
        "organization_names",
    ]


@pytest.mark.django_db
def test_vergleich_zeigt_fehlende_ueberzaehlige_und_abweichende(
    settings: Any, es: FakeElasticsearch, kommune: Kommune
) -> None:
    body = kommune("Beispielstadt")
    kennung = str(body.pk)
    gleich, abweichend, fehlt = _sitzung(body), _sitzung(body, "Ausschuss"), _sitzung(body)
    for sitzung in (gleich, abweichend, fehlt):
        es.ablegen("meetings", meeting_to_doc(sitzung))
    for sitzung in (gleich, abweichend):
        es.ablegen("schatten-meetings", meeting_to_doc(sitzung))
    es.indizes["schatten-meetings"].docs[str(abweichend.pk)].source["name"] = "anders"
    es.ablegen("schatten-meetings", {"id": str(uuid.uuid4()), "body_id": kennung})

    daten = json.loads(_befehl("vergleichen", "--index", "meetings", "--json"))

    ergebnis = daten["indizes"][0]["kommunen"][0]
    assert daten["abweichung"] is True
    assert ergebnis["kommune"] == kennung  # ohne Einstellung: die Kommunen im Schattenindex
    assert (ergebnis["bestand"], ergebnis["live"], ergebnis["schatten"]) == (3, 3, 3)
    assert (ergebnis["fehlt"], ergebnis["ueberzaehlig"]) == (1, 1)
    assert ergebnis["beispiele_fehlt"] == [str(fehlt.pk)]
    assert (ergebnis["abweichend"], ergebnis["stichprobe"]) == (1, 2)
    assert ergebnis["felder"] == {"name": 1}
    assert ergebnis["beispiele_abweichend"] == [str(abweichend.pk)]
    # Der Bestand sagt „Ausschuss“: Veraltet ist der Schatten
    assert ergebnis["schatten_veraltet"] == {"name": 1}

    text = _befehl("vergleichen", "--index", "meetings", "--kommune", kennung)
    assert "fehlt 1  überzählig 1  abweichend 1/2" in text
    assert "Felder: name 1" in text
    assert "Schatten weicht vom Bestand ab: name 1" in text
    with pytest.raises(SystemExit):
        _befehl("vergleichen", "--index", "meetings", "--streng")


@pytest.mark.django_db
def test_vergleich_erkennt_veralteten_live_index(es: FakeElasticsearch, kommune: Kommune) -> None:
    """Issue #821: Abhängige Felder zieht nur das Abonnement nach; dann ist der Live-Index veraltet, nicht der Schatten."""
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body, "Bauausschuss")
    es.ablegen("schatten-meetings", meeting_to_doc(sitzung))
    es.ablegen("meetings", {**meeting_to_doc(sitzung), "organization_names": ["Alter Name"]})
    weg = _sitzung(body, "Abgesagt")
    es.ablegen("meetings", meeting_to_doc(weg))
    es.ablegen("schatten-meetings", {**meeting_to_doc(weg), "name": "anders"})
    OParlMeeting.objects.filter(pk=weg.pk).update(deleted=True)

    daten = json.loads(_befehl("vergleichen", "--index", "meetings", "--json"))

    ergebnis = daten["indizes"][0]["kommunen"][0]
    assert ergebnis["felder"] == {"name": 1, "organization_names": 1}
    # Gehört ein Dokument nicht mehr in den Index, ist der Schatten veraltet (er hätte es löschen müssen)
    assert ergebnis["schatten_veraltet"] == {"(nicht im Bestand)": 1}

    OParlMeeting.objects.filter(pk=weg.pk).update(deleted=False, name="anders")
    text = _befehl("vergleichen", "--index", "meetings")
    assert "Schatten entspricht dem Bestand (Live-Index veraltet)" in text


@pytest.mark.django_db
def test_vergleich_meldet_abweichende_abbildung(es: FakeElasticsearch, kommune: Kommune) -> None:
    body = kommune("Beispielstadt")
    abbildung = index_configs(synonyms())["persons"]["mappings"]
    es.anlegen("persons", {"properties": {**abbildung["properties"], "title": {"type": "keyword"}}})
    es.anlegen("schatten-persons", abbildung)
    es.ablegen("schatten-persons", {"id": str(uuid.uuid4()), "body_id": str(body.pk)})

    text = _befehl("vergleichen", "--index", "persons")

    assert "Abbildung abweichend: title" in text


# --- status und loeschen ------------------------------------------------------------------------


@pytest.mark.django_db
def test_status_nennt_abonnement_rueckstand_und_groesse(settings: Any, es: FakeElasticsearch) -> None:
    settings.SEARCH_INDEX_SUBSCRIPTION = "schatten"
    Subscription.objects.create(name="suchindex", cursor_seq=0, state=SubscriptionState.SCHATTEN)
    nummeriert(type="ris.meeting.changed")
    nummeriert(type="ris.voting.recorded")  # gehört nicht zum Abonnement
    es.ablegen("schatten-papers", {"id": "1", "name": "x"})

    ausgabe = _befehl("status")

    assert "Schalter SEARCH_INDEX_SUBSCRIPTION: schatten" in ausgabe
    assert "Cursor 0 von 2, offen 1 Ereignisse" in ausgabe
    assert "schatten-papers" in ausgabe and "1 Dokumente" in ausgabe
    assert "schatten-files" in ausgabe and "fehlt" in ausgabe
    assert "Heap Elasticsearch: 512.0 MB von 1024.0 MB" in ausgabe


@pytest.mark.django_db
def test_loeschen_nur_wenn_das_abonnement_nicht_mehr_schreibt(settings: Any, es: FakeElasticsearch) -> None:
    settings.SEARCH_INDEX_SUBSCRIPTION = "schatten"
    Subscription.objects.create(name="suchindex", cursor_seq=5, state=SubscriptionState.SCHATTEN)
    ParkedEvent.objects.create(
        subscription="suchindex", event_seq=3, aggregate_id=uuid.uuid4(), state="wiederholen", attempts=1
    )
    es.ablegen("schatten-papers", {"id": "1"})
    es.ablegen("papers", {"id": "1"})

    with pytest.raises(CommandError, match="--ja"):
        _befehl("loeschen")
    with pytest.raises(CommandError, match="SEARCH_INDEX_SUBSCRIPTION=aus"):
        _befehl("loeschen", "--ja")

    settings.SEARCH_INDEX_SUBSCRIPTION = "aus"
    ausgabe = _befehl("loeschen", "--ja", "--abonnement")

    assert "Gelöscht: schatten-papers" in ausgabe
    assert set(es.indizes) == {"papers"}  # der Live-Index bleibt
    assert not Subscription.objects.filter(name="suchindex").exists()
    assert not ParkedEvent.objects.filter(subscription="suchindex").exists()


# --- Obergrenze beim Erweitern -------------------------------------------------------------------


@pytest.mark.django_db
def test_vollbau_beim_erweitern_zaehlt_den_vorhandenen_schattenindex(
    settings: Any, es: FakeElasticsearch, kommune: Kommune
) -> None:
    erste, zweite = kommune("Erste"), kommune("Zweite")
    for _ in range(3):
        _vorgang(erste), _vorgang(zweite)
    settings.SEARCH_INDEX_SHADOW_MAX_DOCS = 4

    _befehl("aufbauen", "--kommune", str(erste.pk), "--index", "papers")
    assert len(es.indizes["schatten-papers"].docs) == 3

    # Die zweite Kommune allein (3) läge unter der Grenze, zusammen mit der ersten (6) nicht
    with pytest.raises(CommandError, match=r"Obergrenze \(6 > 4"):
        _befehl("aufbauen", "--kommune", str(zweite.pk), "--index", "papers")
    with pytest.raises(CommandError, match="Obergrenze"):
        _befehl("aufbauen", "--kommune", str(zweite.pk), "--index", "papers", "--trocken")
    assert len(es.indizes["schatten-papers"].docs) == 3

    # Neu aufbauen ersetzt die eigenen Dokumente und zählt sie nicht doppelt
    ausgabe = _befehl("aufbauen", "--kommune", str(erste.pk), "--index", "papers")
    assert "Schattenindex danach etwa 3 Dokumente (0 bleiben" in ausgabe
    assert len(es.indizes["schatten-papers"].docs) == 3

    # Dokumente anderer Indizes zählen mit
    es.ablegen("schatten-meetings", {"id": str(uuid.uuid4()), "body_id": str(erste.pk)})
    es.ablegen("schatten-meetings", {"id": str(uuid.uuid4()), "body_id": str(erste.pk)})
    with pytest.raises(CommandError, match=r"Obergrenze \(5 > 4"):
        _befehl("aufbauen", "--kommune", str(erste.pk), "--index", "papers")


# --- Vergleich ohne Auswahl ----------------------------------------------------------------------


@pytest.mark.django_db
def test_vergleich_ohne_auswahl_meldet_kommune_die_im_schattenindex_fehlt(
    settings: Any, es: FakeElasticsearch, kommune: Kommune
) -> None:
    settings.SEARCH_INDEX_SHADOW_BODIES = []  # alle Kommunen im Schattenbetrieb
    da, fehlt = kommune("Da"), kommune("Fehlt")
    sitzung, vergessen = _sitzung(da), _sitzung(fehlt)
    for dokument in (meeting_to_doc(sitzung), meeting_to_doc(vergessen)):
        es.ablegen("meetings", dokument)
    es.ablegen("schatten-meetings", meeting_to_doc(sitzung))

    daten = json.loads(_befehl("vergleichen", "--index", "meetings", "--json"))

    kommunen = {eintrag["kommune"]: eintrag for eintrag in daten["indizes"][0]["kommunen"]}
    assert set(kommunen) == {str(da.pk), str(fehlt.pk)}
    assert (kommunen[str(fehlt.pk)]["fehlt"], kommunen[str(fehlt.pk)]["schatten"]) == (1, 0)
    assert kommunen[str(da.pk)]["fehlt"] == 0
    assert daten["abweichung"] is True


# --- Schutz beim Löschen, Sicherheitsprotokoll ----------------------------------------------------


@pytest.mark.django_db
def test_loeschen_schuetzt_auch_bei_schalter_aktiv_und_protokolliert(settings: Any, es: FakeElasticsearch) -> None:
    from apps.accounts.models import SecurityAuditLog

    settings.SEARCH_INDEX_SUBSCRIPTION = "aktiv"
    Subscription.objects.create(name="suchindex", cursor_seq=7, state=SubscriptionState.SCHATTEN)
    es.ablegen("schatten-papers", {"id": "1"})

    # Zustand "schatten" in der Datenbank: Der Handler schreibt weiter in den Schattenindex
    with pytest.raises(CommandError, match="schreibt noch"):
        _befehl("loeschen", "--ja")

    # Nach dem Umschalten (Zustand aktiv) dürfen die Schattenindizes weg, das Abonnement aber nicht
    Subscription.objects.filter(name="suchindex").update(state=SubscriptionState.AKTIV)
    with pytest.raises(CommandError, match="noch zugestellt"):
        _befehl("loeschen", "--ja", "--abonnement")
    assert "schatten-papers" in es.indizes
    assert not SecurityAuditLog.objects.exists()

    assert "Gelöscht: schatten-papers" in _befehl("loeschen", "--ja")
    assert Subscription.objects.filter(name="suchindex").exists()

    settings.SEARCH_INDEX_SUBSCRIPTION = "aus"
    ParkedEvent.objects.create(
        subscription="suchindex", event_seq=3, aggregate_id=uuid.uuid4(), state="wiederholen", attempts=1
    )
    _befehl("loeschen", "--ja", "--abonnement")

    eintraege = list(SecurityAuditLog.objects.filter(event="betrieb").order_by("created_at"))
    assert [eintrag.details["aktion"] for eintrag in eintraege] == ["suchindex_schatten_loeschen"] * 2
    assert eintraege[0].details["indizes"] == ["schatten-papers"]
    assert eintraege[0].details["quelle"] == "kommandozeile" and eintraege[0].user_ref is None
    assert eintraege[1].details["abonnement_entfernt"] is True
    assert (eintraege[1].details["cursor"], eintraege[1].details["geparkt_entfernt"]) == (7, 1)
