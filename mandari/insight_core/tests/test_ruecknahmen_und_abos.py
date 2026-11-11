# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zurückgenommenes und Wartendes bestimmt keine öffentliche Ausgabe mehr, und Abo-Formulare verraten nichts.

- Gremienliste: zurückgenommene Sitzungen liefern kein Datum und machen ein Gremium nicht „aktiv“.
- Personen- und Fragenfilter treffen nur über bestehende Mitgliedschaften; das Etikett des Personenfilters nennt
  nur sichtbare Personen der Kommune.
- Die Fragensuche und die Sortierung „beantwortet“ beachten nur veröffentlichte Antworten.
- Die Suche über „alle Kommunen“ findet nichts, wenn keine Kommune durchsuchbar ist.
- Das Abo-Formular antwortet für jede Adresse gleich; ein abgemeldetes Abo wird erst mit der Bestätigung wieder
  aktiv.
- Die Verortung liest nur Anlagen, die noch veröffentlicht sind; das Straßenverzeichnis bleibt bei vielen
  Nennungen schnell.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from insight_core.models import (
    InsightSubscriber,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    PublicQuestion,
)
from insight_core.services import georef_runner
from insight_core.services.gazetteer import StreetGazetteer
from insight_core.services.georeferencing import collect_paper_text
from insight_core.services.search_service import ElasticsearchService
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON
from insight_core.views.subscriptions import ABO_ANTWORT

pytestmark = pytest.mark.django_db

RIS = "https://ris.ruecknahme.example/oparl"


def _kennung(art: str) -> str:
    return f"{RIS}/{art}/{uuid.uuid4()}"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Rücknahme-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Rücknahmestadt", slug="ruecknahmestadt", is_listed=True
    )


@pytest.fixture
def client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


def _gremium(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=_kennung("organization"), body=body, name=name, **felder)


def _person(body: OParlBody, name: str, **felder: Any) -> OParlPerson:
    return OParlPerson.objects.create(
        external_id=_kennung("person"), body=body, name=name, family_name=name.split()[-1], **felder
    )


def _mitglied(person: OParlPerson, gremium: OParlOrganization, **felder: Any) -> OParlMembership:
    return OParlMembership.objects.create(
        external_id=_kennung("membership"), person=person, organization=gremium, **felder
    )


def _frage(body: OParlBody, person: OParlPerson, betreff: str, **felder: Any) -> PublicQuestion:
    return PublicQuestion.objects.create(
        body=body,
        recipient=person,
        questioner_name="Bert",
        questioner_email="bert@example.org",
        subject=betreff,
        question_text="Wann geht es weiter?",
        status="published",
        published_at=felder.pop("published_at", timezone.now() - timedelta(days=5)),
        **felder,
    )


# --- Gremienliste -------------------------------------------------------------------------------------


def test_gremienliste_ohne_daten_zurueckgenommener_sitzungen(body: OParlBody, client: Client) -> None:
    nur_zurueckgenommen = _gremium(body, "Ausschuss A")
    mit_sitzung = _gremium(body, "Ausschuss B")
    jetzt = timezone.now()
    for gremium, deleted in ((nur_zurueckgenommen, True), (mit_sitzung, False)):
        for start in (jetzt + timedelta(days=3), jetzt - timedelta(days=3)):
            sitzung = OParlMeeting.objects.create(
                external_id=_kennung("meeting"), body=body, name="Sitzung", start=start, deleted=deleted
            )
            sitzung.organizations.add(gremium)

    alle = {
        o.pk: o
        for o in client.get(reverse("insight_core:insight:organization_list"), {"tab": "all"}).context["organizations"]
    }
    aktiv = [o.pk for o in client.get(reverse("insight_core:insight:organization_list")).context["organizations"]]

    assert alle[mit_sitzung.pk].next_meeting is not None and alle[mit_sitzung.pk].last_meeting is not None
    if nur_zurueckgenommen.pk in alle:
        assert alle[nur_zurueckgenommen.pk].next_meeting is None
        assert alle[nur_zurueckgenommen.pk].last_meeting is None
        assert alle[nur_zurueckgenommen.pk].has_meetings is False
    assert mit_sitzung.pk in aktiv and nur_zurueckgenommen.pk not in aktiv


# --- Filter über Mitgliedschaften ---------------------------------------------------------------------


def test_personensuche_trifft_nur_ueber_bestehende_mitgliedschaften(body: OParlBody, client: Client) -> None:
    bestehend = _person(body, "Anna Aktiv")
    geloescht = _person(body, "Bert Früher")
    aufgeloest = _person(body, "Cem Aufgelöst")
    ausschuss = _gremium(body, "Sonderausschuss Hafen")
    weg = _gremium(body, "Sonderausschuss Hafen alt", deleted=True)
    _mitglied(bestehend, ausschuss, role="Beispielrolle")
    _mitglied(geloescht, ausschuss, role="Beispielrolle", deleted=True)
    _mitglied(aufgeloest, weg, role="Beispielrolle")
    url = reverse("insight_core:insight:person_list")

    nach_rolle = {p.pk for p in client.get(url, {"q": "Beispielrolle"}).context["persons"]}
    nach_gremium = {p.pk for p in client.get(url, {"q": "Sonderausschuss"}).context["persons"]}

    assert nach_rolle == {bestehend.pk}
    assert nach_gremium == {bestehend.pk}


def test_fragenfilter_nach_fraktion_nur_ueber_bestehende_mitgliedschaften(body: OParlBody, client: Client) -> None:
    fraktion = _gremium(body, "Fraktion Mitte", organization_type="Fraktion")
    dabei = _person(body, "Anna Dabei")
    frueher = _person(body, "Bert Früher")
    _mitglied(dabei, fraktion)
    _mitglied(frueher, fraktion, deleted=True)
    frage_dabei = _frage(body, dabei, "Frage an Anna")
    _frage(body, frueher, "Frage an Bert")
    url = reverse("insight_core:insight:question_portal")

    treffer = [f.pk for f in client.get(url, {"fraktion": str(fraktion.pk)}).context["questions"]]

    assert treffer == [frage_dabei.pk]
    # Ungültige Kennungen treffen nichts und führen zu keinem Serverfehler
    for parameter in ("fraktion", "person"):
        antwort = client.get(url, {parameter: "keine-kennung"})
        assert antwort.status_code == 200 and list(antwort.context["questions"]) == []


def test_etikett_des_personenfilters_nur_fuer_sichtbare_personen_der_kommune(body: OParlBody, client: Client) -> None:
    sichtbar = _person(body, "Anna Sichtbar")
    zurueckgenommen = _person(body, "Bert Geheimname", deleted=True)
    fremde_quelle = OParlSource.objects.create(name="Fremd", url="https://ris.fremd.example/oparl/system")
    fremde_kommune = OParlBody.objects.create(
        external_id="https://ris.fremd.example/oparl/body/1", source=fremde_quelle, name="Fremdstadt", slug="fremd"
    )
    fremd = _person(fremde_kommune, "Cem Fremdname")
    url = reverse("insight_core:insight:question_portal")

    assert client.get(url, {"person": str(sichtbar.pk)}).context["filter_person_obj"] == sichtbar
    for person in (zurueckgenommen, fremd):
        antwort = client.get(url, {"person": str(person.pk)})
        assert antwort.context.get("filter_person_obj") is None
        assert str(person.name) not in antwort.content.decode()


# --- Fragen: wartende Antworten -----------------------------------------------------------------------


def test_wartende_antwort_beeinflusst_weder_suche_noch_reihenfolge(body: OParlBody, client: Client) -> None:
    anna = _person(body, "Anna Rat")
    jetzt = timezone.now()
    veroeffentlicht = _frage(
        body,
        anna,
        "Park",
        answer_text="Ja, im Frühjahr kommt die Beleuchtung.",
        answer_status="published",
        answered_at=jetzt - timedelta(days=2),
        published_at=jetzt - timedelta(days=9),
    )
    wartend = _frage(
        body,
        anna,
        "Schule",
        answer_text="Vertraulicher Entwurf mit dem Wort Zitronenfalter.",
        answer_status="pending",
        answered_at=jetzt - timedelta(hours=1),
        published_at=jetzt - timedelta(days=8),
    )
    url = reverse("insight_core:insight:question_portal")

    assert [f.pk for f in client.get(url, {"q": "Zitronenfalter"}).context["questions"]] == []
    assert [f.pk for f in client.get(url, {"q": "Beleuchtung"}).context["questions"]] == [veroeffentlicht.pk]
    # Die wartende Antwort ist jünger, zählt für „beantwortet“ aber nicht: Die veröffentlichte steht vorn
    reihenfolge = [f.pk for f in client.get(url, {"sort": "beantwortet"}).context["questions"]]
    assert reihenfolge == [veroeffentlicht.pk, wartend.pk]


# --- Suche über alle Kommunen ---------------------------------------------------------------------------


def test_leere_kommunenliste_ergibt_einen_filter_ohne_treffer() -> None:
    service: Any = ElasticsearchService.__new__(ElasticsearchService)

    assert service._filter_clauses(None, "papers", body_ids=[]) == [{"terms": {"body_id": []}}]
    # Ohne Angabe bleibt es bei „keine Einschränkung“, eine Kommune allein beim einzelnen Filter
    assert service._filter_clauses(None, "papers", body_ids=None) == []
    assert service._filter_clauses("b1", "papers", body_ids=[]) == [{"term": {"body_id": "b1"}}]
    assert service._filter_clauses(None, "papers", body_ids=["b1", "b2"]) == [{"terms": {"body_id": ["b1", "b2"]}}]


# --- Abo-Formular --------------------------------------------------------------------------------------


class TestAboFormular:
    @pytest.fixture(autouse=True)
    def _abos_an(self, settings: Any) -> None:
        settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True

    def _abonnieren(self, client: Client, email: str, **felder: str) -> str:
        daten = {"email": email, "keyword": "Radweg", "digest_frequency": "weekly", **felder}
        antwort = client.post(reverse("insight_core:insight:notifications"), daten)
        assert antwort.status_code == 200
        return antwort.content.decode()

    def test_antwort_ist_fuer_jede_adresse_gleich(self, body: OParlBody, client: Client) -> None:
        InsightSubscriber.objects.create(
            email="aktiv@example.org", body=body, confirmed=True, keyword_active=True, keyword="Schule"
        )
        mail.outbox.clear()

        neu = self._abonnieren(client, "neu@example.org")
        aktiv = self._abonnieren(client, "aktiv@example.org")

        assert ABO_ANTWORT in neu
        assert neu == aktiv
        # Das bestehende Abo bleibt unverändert und bekommt keine Mail
        bestand = InsightSubscriber.objects.get(email="aktiv@example.org")
        assert (bestand.keyword, bestand.is_active) == ("Schule", True)
        assert [m.to for m in mail.outbox] == [["neu@example.org"]]

    def test_abgemeldetes_abo_wird_erst_mit_der_bestaetigung_wieder_aktiv(
        self, body: OParlBody, client: Client
    ) -> None:
        abo = InsightSubscriber.objects.create(
            email="abgemeldet@example.org",
            body=body,
            confirmed=True,
            keyword_active=True,
            keyword="Schule",
            unsubscribed_at=timezone.now() - timedelta(days=30),
        )
        mail.outbox.clear()

        self._abonnieren(client, "abgemeldet@example.org", keyword="Fremdes Thema")

        abo.refresh_from_db()
        assert abo.is_active is False and abo.confirmed is False
        assert len(mail.outbox) == 1


# --- Verortung -----------------------------------------------------------------------------------------


def _anlage(vorgang: OParlPaper, text: str, **felder: Any) -> OParlFile:
    return OParlFile.objects.create(
        external_id=_kennung("file"),
        body=vorgang.body,
        paper=vorgang,
        name="Anlage",
        text_content=text,
        text_extraction_status="completed",
        **felder,
    )


def test_verortung_liest_nur_veroeffentlichte_anlagen(body: OParlBody) -> None:
    vorgang = OParlPaper.objects.create(external_id=_kennung("paper"), body=body, name="Bauantrag")
    _anlage(vorgang, "Offener Text zur Hafenstraße 1.")
    _anlage(vorgang, "Zurückgenommener Text zur Geheimgasse 7.", deleted=True)
    _anlage(vorgang, "Gesperrter Text zum Sperrweg 9.", source_missing_since=timezone.now())

    text = collect_paper_text(vorgang)

    assert "Hafenstraße" in text
    assert "Geheimgasse" not in text and "Sperrweg" not in text

    nur_gesperrt = OParlPaper.objects.create(external_id=_kennung("paper"), body=body, name="Nur gesperrt")
    _anlage(nur_gesperrt, "Text", deleted=True)
    mit_text = set(OParlPaper.objects.filter(georef_runner.has_text_q()).values_list("pk", flat=True))
    assert mit_text == {vorgang.pk}


def test_strassenverzeichnis_bleibt_bei_vielen_nennungen_schnell(body: OParlBody, make_street: Any) -> None:
    make_street(body, "Hafenstraße", CENTER_LAT, CENTER_LON)
    make_street(body, "Am Hafen", CENTER_LAT, CENTER_LON)
    verzeichnis = StreetGazetteer(body)
    wenig = verzeichnis.find_in_text("Der Antrag betrifft die Hafenstraße 12 und Am Hafen.")
    viel_text = "Die Hafenstraße 12 und dann Am Hafen 3. " * 60_000

    start = time.perf_counter()
    viel = verzeichnis.find_in_text(viel_text)
    dauer = time.perf_counter() - start

    assert {eintrag["normalized"] for eintrag in viel} == {eintrag["normalized"] for eintrag in wenig}
    assert len(wenig) == 2
    assert dauer < 20
