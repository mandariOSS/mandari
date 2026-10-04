# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``suchindex`` im Schattenbetrieb (Issue #526).

Die Ereignisse laufen über die echte Zustellung (``deliver_batch``); Elasticsearch ist ein Ersatz im
Speicher mit externer Version (``fake_es``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.utils import timezone
from elastic_transport import ConnectionError as EsConnectionError

from apps.events import Delivery, TargetUnavailableError
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent, Subscription, SubscriptionState
from apps.events.registry import Subscriber, get
from apps.events.tests.hilfen import nummeriert
from insight_core.models import OParlBody, OParlConsultation, OParlFile, OParlMeeting, OParlPaper
from insight_core.services.search_documents import paper_to_doc
from insight_core.services.search_projection import iter_documents
from insight_search import abonnement, subscribers
from insight_search.indices import index_configs, shadow_name, synonyms
from insight_search.tests.fake_es import FakeElasticsearch

Kommune = Callable[[str], OParlBody]


def _abonnement(settings: Any, modus: str = "schatten") -> Subscriber:
    settings.SEARCH_INDEX_SUBSCRIPTION = modus
    assert subscribers.register()
    spec = get(abonnement.NAME)
    ensure_subscription(spec)
    return spec


def _ereignis(typ: str, objekt: Any, body: OParlBody | None, **felder: Any) -> Event:
    aggregate_type = {
        OParlMeeting: "Meeting",
        OParlPaper: "Paper",
        OParlFile: "File",
        OParlConsultation: "Consultation",
    }[type(objekt)]
    daten: dict[str, Any] = {
        "type": typ,
        "aggregate_type": aggregate_type,
        "aggregate_id": objekt.pk,
        "tenant_ref": f"source:{uuid.uuid4()}",
        "body_id": body.pk if body else None,
        "visibility": "oeffentlich",
        "payload": {},
    }
    daten.update(felder)
    return nummeriert(**daten)


def _sitzung(body: OParlBody, **felder: Any) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"https://ris.example/meeting/{uuid.uuid4()}", body=body, name="Rat", start=timezone.now(), **felder
    )


def _vorgang(body: OParlBody, **felder: Any) -> OParlPaper:
    return OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body, **felder)


def _datei(body: OParlBody, paper: OParlPaper | None, **felder: Any) -> OParlFile:
    werte: dict[str, Any] = {
        "external_id": f"https://ris.example/file/{uuid.uuid4()}",
        "body": body,
        "paper": paper,
        "name": "Anlage",
        "file_name": "anlage.pdf",
        "text_content": "Spielplatz an der Ringstraße",
        "text_extraction_status": "completed",
    }
    werte.update(felder)
    return OParlFile.objects.create(**werte)


# --- Schalter -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_schalter_aus_registriert_nichts(settings: Any, leeres_register: dict[str, Subscriber]) -> None:
    settings.SEARCH_INDEX_SUBSCRIPTION = "aus"
    assert subscribers.register() is False
    assert abonnement.NAME not in leeres_register


@pytest.mark.django_db
def test_schalter_schatten_registriert_externes_abonnement(
    settings: Any, leeres_register: dict[str, Subscriber]
) -> None:
    spec = _abonnement(settings)
    assert spec.shadow and not spec.transactional
    assert spec.queue == "index"
    assert spec.matches("ris.meeting.changed") and spec.matches("ris.object.depublished")
    assert not spec.matches("ris.agendaitem.changed")
    assert Subscription.objects.get(name=abonnement.NAME).state == SubscriptionState.SCHATTEN


# --- Schattenbetrieb ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_sitzung_landet_im_schattenindex_mit_folgenummer_als_version(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    ereignis = _ereignis("ris.meeting.changed", sitzung, body)

    assert deliver_batch(spec).delivered == 1

    doc = es.doc("schatten-meetings", sitzung.pk)
    assert doc is not None
    assert doc.version == ereignis.seq
    assert doc.source["name"] == "Rat" and doc.source["body_id"] == str(body.pk)
    assert "meetings" not in es.indizes  # der Live-Index bleibt unberührt
    # Die Schattenindizes bekommen die Abbildung der Live-Indizes, nicht erratene Feldtypen
    assert es.indizes["schatten-papers"].mappings == index_configs(synonyms())["papers"]["mappings"]


@pytest.mark.django_db
def test_datei_aktualisiert_auch_ihren_vorgang(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body, name="Spielplatz")
    datei = _datei(body, vorgang)
    ereignis = _ereignis("ris.file.changed", datei, body, payload={"file": str(datei.pk), "change": "added"})

    deliver_batch(spec)

    assert es.doc("schatten-files", datei.pk) is not None
    papier = es.doc("schatten-papers", vorgang.pk)
    assert papier is not None and papier.version == ereignis.seq
    assert "Ringstraße" in papier.source["file_contents_preview"]


@pytest.mark.django_db
def test_beratung_aktualisiert_ihren_vorgang(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body, name="Haushalt")
    beratung = OParlConsultation.objects.create(
        external_id=f"https://ris.example/consultation/{uuid.uuid4()}", body=body, paper=vorgang
    )
    _ereignis("ris.consultation.changed", beratung, body, payload={"consultation": str(beratung.pk)})

    deliver_batch(spec)

    assert es.doc("schatten-papers", vorgang.pk) is not None


@pytest.mark.django_db
def test_wiederholung_und_veralteter_stand_schaden_nicht(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    erstes = _ereignis("ris.meeting.changed", sitzung, body)
    deliver_batch(spec)
    # Ein neuerer Stand (etwa aus dem Vollbau) steht schon im Index
    es.ablegen("schatten-meetings", {"id": str(sitzung.pk), "name": "neuer"}, version=(erstes.seq or 0) + 100)

    rewind(abonnement.NAME, erstes.seq or 1)  # nachspielen
    assert deliver_batch(spec).delivered == 1

    doc = es.doc("schatten-meetings", sitzung.pk)
    assert doc is not None and doc.source["name"] == "neuer"
    assert not ParkedEvent.objects.exists()


@pytest.mark.django_db
def test_geloeschtes_objekt_verlaesst_den_schattenindex(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body, name="Alt")
    es.ablegen("schatten-papers", {"id": str(vorgang.pk), "body_id": str(body.pk)}, version=0)
    OParlPaper.objects.filter(pk=vorgang.pk).update(deleted=True)
    _ereignis(
        "ris.object.depublished",
        vorgang,
        body,
        operation="delete",
        payload={"object_type": "Paper", "object": str(vorgang.pk), "reason": "quelle_geloescht"},
    )

    deliver_batch(spec)

    assert es.doc("schatten-papers", vorgang.pk) is None


@pytest.mark.django_db
def test_nur_gewaehlte_kommunen_und_oeffentliche_ereignisse(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    gewaehlt, andere = kommune("Gewählt"), kommune("Andere")
    settings.SEARCH_INDEX_SHADOW_BODIES = [str(gewaehlt.pk)]
    spec = _abonnement(settings)
    drin, draussen, intern = _sitzung(gewaehlt), _sitzung(andere), _sitzung(gewaehlt)
    _ereignis("ris.meeting.changed", drin, gewaehlt)
    _ereignis("ris.meeting.changed", draussen, andere)
    _ereignis("ris.meeting.changed", intern, gewaehlt, visibility="nichtoeffentlich")

    assert deliver_batch(spec).delivered == 3

    assert set(es.indizes["schatten-meetings"].docs) == {str(drin.pk)}


@pytest.mark.django_db
def test_obergrenze_legt_keine_neuen_dokumente_an(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    settings.SEARCH_INDEX_SHADOW_MAX_DOCS = 1
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorhanden, neu = _sitzung(body, location_name="Rathaus"), _sitzung(body)
    es.ablegen("schatten-meetings", {"id": str(vorhanden.pk)}, version=0)
    _ereignis("ris.meeting.changed", vorhanden, body)
    _ereignis("ris.meeting.scheduled", neu, body)

    deliver_batch(spec)

    docs = es.indizes["schatten-meetings"].docs
    assert set(docs) == {str(vorhanden.pk)}
    assert docs[str(vorhanden.pk)].source["location_name"] == "Rathaus"  # vorhandenes weiter aktualisiert


@pytest.mark.django_db
def test_elasticsearch_nicht_erreichbar_parkt_nichts(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    cursor = Subscription.objects.get(name=abonnement.NAME).cursor_seq
    _ereignis("ris.meeting.changed", _sitzung(body), body)
    es.fehler = EsConnectionError("weg")

    with pytest.raises(TargetUnavailableError):
        deliver_batch(spec)

    assert Subscription.objects.get(name=abonnement.NAME).cursor_seq == cursor
    assert not ParkedEvent.objects.exists()
    assert deliver_batch(spec).delivered == 1  # wieder erreichbar: derselbe Batch


@pytest.mark.django_db
def test_abgelehntes_dokument_parkt_nur_sein_ereignis(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    gut, schlecht = _sitzung(body), _sitzung(body)
    _ereignis("ris.meeting.changed", gut, body)
    abgelehnt = _ereignis("ris.meeting.changed", schlecht, body)
    es.ablehnen[str(schlecht.pk)] = 400

    ergebnis = deliver_batch(spec)

    assert ergebnis.delivered == 1 and ergebnis.parked == 1
    assert list(ParkedEvent.objects.values_list("event_seq", flat=True)) == [abgelehnt.seq]
    assert es.doc("schatten-meetings", gut.pk) is not None


@pytest.mark.django_db
def test_schalter_schatten_schreibt_nie_den_live_index(settings: Any, es: FakeElasticsearch, kommune: Kommune) -> None:
    settings.SEARCH_INDEX_SUBSCRIPTION = "schatten"
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    ereignis = _ereignis("ris.meeting.changed", sitzung, body)

    # Auch wenn das Abonnement in der Datenbank auf "aktiv" steht
    abonnement.suchindex([ereignis], Delivery(subscription=abonnement.NAME, shadow=False))

    assert es.doc("schatten-meetings", sitzung.pk) is not None
    assert "meetings" not in es.indizes


@pytest.mark.django_db
def test_schalter_aktiv_schreibt_den_live_index(settings: Any, es: FakeElasticsearch, kommune: Kommune) -> None:
    settings.SEARCH_INDEX_SUBSCRIPTION = "aktiv"
    settings.SEARCH_INDEX_SHADOW_BODIES = [str(uuid.uuid4())]  # gilt nur für den Schatten
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    ereignis = _ereignis("ris.meeting.changed", sitzung, body)

    abonnement.suchindex([ereignis], Delivery(subscription=abonnement.NAME, shadow=False))

    doc = es.doc("meetings", sitzung.pk)
    assert doc is not None and doc.version == ereignis.seq
    assert shadow_name("meetings") not in es.indizes


@pytest.mark.parametrize(("status", "nicht_erreichbar"), [(429, True), (503, True), (400, False), (404, False)])
def test_ueberlast_gilt_als_nicht_erreichbar(status: int, nicht_erreichbar: bool) -> None:
    from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig
    from elasticsearch import ApiError

    meta = ApiResponseMeta(
        status=status, http_version="1.1", headers=HttpHeaders(), duration=0.0, node=NodeConfig("http", "es", 9200)
    )
    assert abonnement._unavailable(ApiError("x", meta=meta, body={})) is nicht_erreichbar
    assert abonnement._unavailable(EsConnectionError("weg")) is True
    assert abonnement._unavailable(RuntimeError("anderes")) is False


# --- Sichtbarkeit je Typ, abhängige Dokumente --------------------------------------------------


@pytest.mark.django_db
def test_texterkennung_ist_intern_und_aktualisiert_datei_und_vorgang(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body, name="Radweg")
    datei = _datei(body, vorgang, text_content="Radweg entlang der Bahnstrecke")
    nutzlast = {"file": str(datei.pk), "method": "pypdf"}
    # Vertrag ris.file.text_extracted: x-visibility "intern" (Anreicherung ohne Inhalt)
    ereignis = _ereignis("ris.file.text_extracted", datei, body, visibility="intern", payload=nutzlast)

    assert deliver_batch(spec).delivered == 1

    doc = es.doc("schatten-files", datei.pk)
    assert doc is not None and doc.version == ereignis.seq
    assert "Bahnstrecke" in doc.source["text_content"]
    papier = es.doc("schatten-papers", vorgang.pk)
    assert papier is not None and "Bahnstrecke" in papier.source["file_contents_preview"]


@pytest.mark.django_db
def test_intern_gilt_nur_fuer_die_texterkennung(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    datei = _datei(body, None)
    _ereignis("ris.file.changed", datei, body, visibility="intern", payload={"file": str(datei.pk)})
    _ereignis("ris.file.text_extracted", datei, body, visibility="nichtoeffentlich", payload={"file": str(datei.pk)})
    _ereignis("ris.meeting.changed", _sitzung(body), body, visibility="intern")

    assert deliver_batch(spec).delivered == 3

    assert not any(index.docs for index in es.indizes.values())


@pytest.mark.django_db
def test_vorgang_aktualisiert_seine_dateien(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body, name="Alter Titel", reference="V/1")
    mit_text = _datei(body, vorgang)
    ohne_text = _datei(body, vorgang, text_content=None, text_extraction_status="pending")
    OParlPaper.objects.filter(pk=vorgang.pk).update(name="Neuer Titel")
    ereignis = _ereignis("ris.paper.changed", vorgang, body, payload={"paper": str(vorgang.pk)})

    deliver_batch(spec)

    doc = es.doc("schatten-files", mit_text.pk)
    assert doc is not None and doc.version == ereignis.seq
    assert (doc.source["paper_name"], doc.source["paper_reference"]) == ("Neuer Titel", "V/1")
    # Dateien, die nicht in den Index gehören, betrifft die Änderung des Vorgangs nicht
    assert es.doc("schatten-files", ohne_text.pk) is None
    assert abonnement.affected([ereignis])[0].keys() == {
        abonnement.Target("papers", vorgang.pk),
        abonnement.Target("files", mit_text.pk),
    }


@pytest.mark.django_db
def test_sitzung_aktualisiert_die_dateien_an_ihr(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    einladung = _datei(body, None, meeting=sitzung, name="Einladung")
    OParlMeeting.objects.filter(pk=sitzung.pk).update(name="Rat (verschoben)")
    _ereignis("ris.meeting.changed", sitzung, body)

    deliver_batch(spec)

    doc = es.doc("schatten-files", einladung.pk)
    assert doc is not None and doc.source["meeting_name"] == "Rat (verschoben)"


@pytest.mark.django_db
def test_loeschen_eines_fehlenden_dokuments_zaehlt_nicht_als_geloescht(es: FakeElasticsearch) -> None:
    tally = abonnement.Tally()
    es.ablegen("schatten-files", {"id": "vorhanden"}, version=1)

    abonnement.write(
        es,
        [
            abonnement.Operation("schatten-files", "vorhanden", 5, None),
            abonnement.Operation("schatten-files", "fehlt", 5, None),
        ],
        tally,
    )

    assert (tally.deleted, tally.absent) == (1, 1)


@pytest.mark.django_db
def test_vorgaenge_ohne_abfragen_je_vorgang(kommune: Kommune, django_assert_max_num_queries: Any) -> None:
    body = kommune("Beispielstadt")
    vorgaenge = []
    for nummer in range(5):
        vorgang = _vorgang(body, name=f"Vorlage {nummer}")
        _datei(body, vorgang, file_name=f"a{nummer}.pdf", text_content="A" * 6000 + " Ende")
        _datei(body, vorgang, file_name=f"b{nummer}.pdf", text_content="  kurz  ")
        _datei(body, vorgang, file_name="weg.pdf", deleted=True)
        OParlConsultation.objects.create(
            external_id=f"https://ris.example/consultation/{uuid.uuid4()}", body=body, paper=vorgang
        )
        vorgaenge.append(vorgang)
    erwartet = {vorgang.pk: paper_to_doc(OParlPaper.objects.get(pk=vorgang.pk)) for vorgang in vorgaenge}

    # Vorgänge, Beratungen, Dateien: je eine Abfrage für den ganzen Abschnitt
    with django_assert_max_num_queries(3):
        dokumente = dict(iter_documents("papers", [vorgang.pk for vorgang in vorgaenge]))

    assert dokumente == erwartet  # dieselben Dokumente wie ohne Vorladen
    vorschau = erwartet[vorgaenge[0].pk]["file_contents_preview"]
    assert "A" * 5000 in vorschau and "Ende" not in vorschau and "kurz" in vorschau
