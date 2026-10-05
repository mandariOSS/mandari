# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``suchindex``: Abhängigkeiten zwischen Dokumenten, Gremien, Personen, Dateikontext (Issue #821).

Jede Lücke hier ließ den Schattenindex hinter dem Bestand zurück: Sitzung, Tagesordnungspunkt und
Beratung ändern den Kontext der Dateien beratener Vorgänge, ein Gremium die ``organization_names`` von
Sitzungen, Vorgängen und Dateien; Gremien und Personen hatten keine Änderungsereignisse. Der Kontext
der Dateien wird je Block gebündelt aufgelöst (Abfragezähler).
"""

from __future__ import annotations

import math
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.events.dispatch import deliver_batch
from apps.events.registry import Subscriber
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from insight_core.services import search_projection
from insight_core.services.search_documents import file_contexts, file_to_doc
from insight_core.services.search_projection import iter_documents
from insight_search import abonnement
from insight_search.abonnement import Target
from insight_search.tests.fake_es import FakeElasticsearch
from insight_search.tests.test_suchindex_abonnement import Kommune, _abonnement, _datei, _ereignis, _vorgang


def _sitzung(body: OParlBody, name: str = "Rat", **felder: Any) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"https://ris.example/meeting/{uuid.uuid4()}", body=body, name=name, start=timezone.now(), **felder
    )


def _beratung(body: OParlBody, vorgang: OParlPaper, sitzung: OParlMeeting | None, **felder: Any) -> OParlConsultation:
    werte: dict[str, Any] = {
        "external_id": f"https://ris.example/consultation/{uuid.uuid4()}",
        "body": body,
        "paper": vorgang,
        "meeting_external_id": sitzung.external_id if sitzung else None,
    }
    werte.update(felder)
    return OParlConsultation.objects.create(**werte)


def _punkt(sitzung: OParlMeeting, nummer: str, **felder: Any) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id=f"https://ris.example/agendaitem/{uuid.uuid4()}", meeting=sitzung, number=nummer, **felder
    )


def _gremium(body: OParlBody, name: str = "Bauausschuss") -> OParlOrganization:
    return OParlOrganization.objects.create(
        external_id=f"https://ris.example/organization/{uuid.uuid4()}", body=body, name=name
    )


def _datei_doc(es: FakeElasticsearch, datei: OParlFile) -> dict[str, Any]:
    gespeichert = es.doc("schatten-files", datei.pk)
    assert gespeichert is not None, "Datei fehlt im Schattenindex"
    return gespeichert.source


# --- Sitzung, Tagesordnungspunkt, Beratung → Dateien der beratenen Vorgänge ----------------------


@pytest.mark.django_db
def test_sitzung_aktualisiert_die_dateien_der_beratenen_vorgaenge(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    vorgang = _vorgang(body, name="Radweg")
    _beratung(body, vorgang, sitzung)
    anlage = _datei(body, vorgang)
    neuer_beginn = timezone.now() + timedelta(days=7)
    OParlMeeting.objects.filter(pk=sitzung.pk).update(name="Rat (verlegt)", start=neuer_beginn)
    nutzlast = {"meeting": str(sitzung.pk), "changed": ["name", "start"]}
    ereignis = _ereignis("ris.meeting.changed", sitzung, body, payload=nutzlast)

    deliver_batch(spec)

    doc = es.doc("schatten-files", anlage.pk)
    assert doc is not None and doc.version == ereignis.seq
    assert (doc.source["meeting_name"], doc.source["meeting_date"]) == ("Rat (verlegt)", neuer_beginn.isoformat())
    # Felder außerhalb des Kontexts (die eingebettete Tagesordnung) betreffen die Dateien nicht
    tagesordnung = _ereignis(
        "ris.meeting.changed", sitzung, body, payload={"meeting": str(sitzung.pk), "changed": ["agendaItem"]}
    )
    assert abonnement.affected([tagesordnung])[0].keys() == {Target("meetings", sitzung.pk)}
    # Neue Sitzung: Beratungen, die schon auf sie verweisen, bekommen jetzt ihren Kontext
    neu = _ereignis("ris.meeting.scheduled", sitzung, body, payload={"meeting": str(sitzung.pk)})
    assert Target("files", anlage.pk) in abonnement.affected([neu])[0]


@pytest.mark.django_db
def test_tagesordnungspunkt_auch_nichtoeffentlich_aktualisiert_die_nummer(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    punkt = _punkt(sitzung, "N 5", public=False)
    vorgang = _vorgang(body)
    _beratung(body, vorgang, sitzung, agenda_item_external_id=punkt.external_id)
    anlage = _datei(body, vorgang)
    OParlAgendaItem.objects.filter(pk=punkt.pk).update(number="N 5.1")
    basis = {"agenda_item": str(punkt.pk), "meeting": str(sitzung.pk)}
    # Die Quelle veröffentlicht den Punkt als nichtöffentlich gekennzeichnet: Das Ereignis ist nichtöffentlich,
    # das Dokument der Datei entsteht trotzdem nur aus dem Bestand
    ereignis = _ereignis(
        "ris.agendaitem.changed",
        punkt,
        body,
        visibility="nichtoeffentlich",
        payload={**basis, "change": "changed", "changed": ["number"]},
    )

    deliver_batch(spec)

    doc = es.doc("schatten-files", anlage.pk)
    assert doc is not None and doc.version == ereignis.seq
    assert doc.source["agenda_number"] == "N 5.1"
    assert set(es.indizes) == {f"schatten-{index}" for index in search_projection.INDEXES}  # kein Index für Punkte
    assert es.doc("schatten-meetings", sitzung.pk) is None  # der Punkt baut kein Dokument der Sitzung
    # Verschoben oder abgesetzt ohne neue Nummer: Der Kontext der Dateien bleibt
    for aenderung in ({"change": "moved", "previous_meeting": str(uuid.uuid4())}, {"change": "withdrawn"}):
        ohne_nummer = _ereignis("ris.agendaitem.changed", punkt, body, payload={**basis, **aenderung})
        assert abonnement.affected([ohne_nummer])[0] == {}


@pytest.mark.django_db
def test_beratung_aktualisiert_die_dateien_ihres_vorgangs(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body)
    beratung = _beratung(body, vorgang, None)
    anlage = _datei(body, vorgang)
    sitzung = _sitzung(body, name="Bauausschuss")
    OParlConsultation.objects.filter(pk=beratung.pk).update(meeting_external_id=sitzung.external_id)
    basis = {"consultation": str(beratung.pk), "paper": str(vorgang.pk)}
    _ereignis("ris.consultation.changed", beratung, body, payload={**basis, "change": "scheduled"})

    deliver_batch(spec)

    assert _datei_doc(es, anlage)["meeting_name"] == "Bauausschuss"
    rolle = _ereignis(
        "ris.consultation.changed", beratung, body, payload={**basis, "change": "changed", "changed": ["role"]}
    )
    assert abonnement.affected([rolle])[0].keys() == {Target("papers", vorgang.pk)}


# --- Gremien und Personen -------------------------------------------------------------------------


@pytest.mark.django_db
def test_umbenanntes_gremium_aktualisiert_sitzungen_vorgaenge_und_dateien(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    gremium = _gremium(body)
    sitzung = _sitzung(body)
    sitzung.organizations.add(gremium)
    einladung = _datei(body, None, meeting=sitzung, name="Einladung")
    vorgang = _vorgang(body, name="Radweg")
    _beratung(body, vorgang, sitzung, raw_json={"organization": [gremium.external_id]})
    anlage = _datei(body, vorgang)
    OParlOrganization.objects.filter(pk=gremium.pk).update(name="Ausschuss für Bauen")
    basis = {"organization": str(gremium.pk), "change": "changed"}
    ereignis = _ereignis("ris.organization.changed", gremium, body, payload={**basis, "changed": ["name"]})

    deliver_batch(spec)

    neu = ["Ausschuss für Bauen"]
    assert es.doc("schatten-organizations", gremium.pk).source["name"] == "Ausschuss für Bauen"  # type: ignore[union-attr]
    sitzung_doc = es.doc("schatten-meetings", sitzung.pk)
    assert sitzung_doc is not None and sitzung_doc.version == ereignis.seq
    assert sitzung_doc.source["organization_names"] == neu
    assert es.doc("schatten-papers", vorgang.pk).source["organization_names"] == neu  # type: ignore[union-attr]
    for datei in (einladung, anlage):
        assert _datei_doc(es, datei)["organization_names"] == neu
    # Andere Felder des Gremiums betreffen nur sein eigenes Dokument
    kurzname = _ereignis("ris.organization.changed", gremium, body, payload={**basis, "changed": ["shortName"]})
    assert abonnement.affected([kurzname])[0].keys() == {Target("organizations", gremium.pk)}


@pytest.mark.django_db
def test_neues_gremium_aktualisiert_die_vorgaenge_die_es_nennen(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    kennung = f"https://ris.example/organization/{uuid.uuid4()}"
    vorgang = _vorgang(body)
    _beratung(body, vorgang, None, raw_json={"organization": kennung})
    _beratung(body, _vorgang(body), None, raw_json={"organization": [f"{kennung}0"]})  # nur ähnliche Kennung
    gremium = OParlOrganization.objects.create(external_id=kennung, body=body, name="Jugendhilfeausschuss")
    ereignis = _ereignis(
        "ris.organization.changed", gremium, body, payload={"organization": str(gremium.pk), "change": "added"}
    )

    deliver_batch(spec)

    papier = es.doc("schatten-papers", vorgang.pk)
    assert papier is not None and papier.version == ereignis.seq
    assert papier.source["organization_names"] == ["Jugendhilfeausschuss"]
    # Obermenge: Die ähnliche Kennung trifft den zweiten Vorgang mit, sein Dokument bleibt unverändert richtig
    assert {ziel.index for ziel in abonnement.affected([ereignis])[0]} == {"organizations", "papers"}


@pytest.mark.django_db
def test_person_landet_mit_aenderungsereignis_im_schattenindex(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement(settings)
    body = kommune("Beispielstadt")
    person = OParlPerson.objects.create(
        external_id=f"https://ris.example/person/{uuid.uuid4()}", body=body, name="Erika Muster", family_name="Muster"
    )
    ereignis = _ereignis("ris.person.changed", person, body, payload={"person": str(person.pk), "change": "added"})

    deliver_batch(spec)

    doc = es.doc("schatten-persons", person.pk)
    assert doc is not None and doc.version == ereignis.seq
    assert doc.source["family_name"] == "Muster"
    assert abonnement.affected([ereignis])[0].keys() == {Target("persons", person.pk)}


# --- Dateikontext gebündelt ------------------------------------------------------------------------


@pytest.mark.django_db
def test_dateien_ohne_abfragen_je_datei(kommune: Kommune, django_assert_max_num_queries: Any) -> None:
    body = kommune("Beispielstadt")
    gremium = _gremium(body)
    dateien: list[OParlFile] = []
    for nummer in range(4):
        sitzung = _sitzung(body, name=f"Sitzung {nummer}")
        sitzung.organizations.add(gremium)
        dateien.append(_datei(body, None, meeting=sitzung, name="Einladung"))
        for top in range(2):
            punkt = _punkt(sitzung, f"{top + 1}")
            vorgang = _vorgang(body, name=f"Vorlage {nummer}/{top}")
            _beratung(body, vorgang, sitzung, agenda_item_external_id=punkt.external_id, authoritative=True)
            dateien.extend(_datei(body, vorgang, file_name=f"{nummer}-{top}-{teil}.pdf") for teil in range(2))
    kennungen = [datei.pk for datei in dateien]
    # Einzeln aufgelöst wie im Signal und in reindex_elasticsearch: dieselben Dokumente
    erwartet = {pk: file_to_doc(OParlFile.objects.select_related("paper").get(pk=pk)) for pk in kennungen}
    assert all(doc["meeting_name"] and doc["organization_names"] == ["Bauausschuss"] for doc in erwartet.values())

    # Köpfe, Kontext (Beratungen, Sitzungen, ihre Gremien, Punkte), Texte je zehn Dateien
    with django_assert_max_num_queries(1 + 4 + math.ceil(len(kennungen) / 10)):
        dokumente = dict(iter_documents("files", kennungen))

    assert len(kennungen) == 20
    assert dokumente == erwartet


@pytest.mark.django_db
def test_dateikontext_eindeutig_und_ohne_zurueckgenommenes(kommune: Kommune) -> None:
    """Gleichrangige Beratungen: die kleinste Kennung; Federführung geht vor; Zurückgenommenes zählt nicht."""
    body = kommune("Beispielstadt")
    vorgang = _vorgang(body)
    sitzung_b, sitzung_a = _sitzung(body, name="Sitzung B"), _sitzung(body, name="Sitzung A")
    # In umgekehrter Reihenfolge angelegt: Die Datenbank liefert B zuerst, die Kennung entscheidet für A
    _beratung(body, vorgang, sitzung_b, external_id="https://ris.example/consultation/b")
    _beratung(body, vorgang, sitzung_a, external_id="https://ris.example/consultation/a")
    datei = _datei(body, vorgang)
    assert file_contexts([datei])[datei.pk]["meeting_name"] == "Sitzung A"

    # Von mandari Session zurückgenommen (gelöscht, Kennung der Session-Schnittstelle): zählt nicht
    session = "https://mandari.example/api/oparl/session/consultation/1"
    zurueckgenommen = _sitzung(body, name="Sitzung Z")
    _beratung(body, vorgang, zurueckgenommen, external_id=session, authoritative=True, deleted=True)
    assert file_contexts([datei])[datei.pk]["meeting_name"] == "Sitzung A"

    federfuehrend = _sitzung(body, name="Sitzung F")
    _beratung(body, vorgang, federfuehrend, external_id="https://ris.example/consultation/z", authoritative=True)
    assert file_contexts([datei])[datei.pk]["meeting_name"] == "Sitzung F"
    assert file_to_doc(OParlFile.objects.get(pk=datei.pk))["meeting_name"] == "Sitzung F"  # einzeln gleich
