# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionszuordnung für Personen ohne OParl-Fraktion (Issue #916).

- Regeln der automatischen Übernahme aus der Einblendung der Live-Übertragung (``fraktion_aus_einblendung``):
  eindeutig → bestätigt, sonst Vorschlag; nie überschreiben, von Hand Gepflegtes geht vor, Funktionsbezeichnungen
  sind keine Fraktion.
- Ereignis ``ris.person.faction_assigned`` nur mit Kennungen und Codes, in derselben Transaktion.
- Anzeige in Personenliste und Personenseite mit Quellenhinweis, ohne zusätzliche Abfrage je Person.
- Pflege im Admin (von Hand anlegen, bestätigen, ablehnen) und Schutz vor stillem Löschen.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event
from insight_core.models import (
    OParlBody,
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    OParlSource,
    PersonFraktion,
)
from insight_core.services import fraktionen
from insight_core.services.body_deletion import delete_body_data
from insight_core.services.external_references import split_by_references
from insight_core.services.personen_liste import angaben_fuer

pytestmark = pytest.mark.django_db

RIS = "https://ris.musterstadt.example/oparl"
HEUTE = date(2026, 10, 7)
#: Zeitpunkt einer Lesung (16:20 Uhr Ortszeit)
LESUNG = datetime(2026, 10, 7, 14, 20, tzinfo=UTC)
TYP = "ris.person.faction_assigned"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt", slug="musterstadt")


@pytest.fixture
def ereignisse(settings: Any) -> None:
    settings.INGESTOR_EVENTS_ENABLED = True


def _person(body: OParlBody, key: str = "1", name: str = "Erika Muster") -> OParlPerson:
    vorname, nachname = name.split(" ", 1)
    return OParlPerson.objects.create(
        external_id=f"{RIS}/person/{key}", body=body, name=name, given_name=vorname, family_name=nachname
    )


@pytest.fixture
def person(body: OParlBody) -> OParlPerson:
    return _person(body)


def _lesen(
    person: OParlPerson,
    bezeichnung: str = "Fraktion A",
    *,
    eindeutig: bool = True,
    zeitpunkt: datetime = LESUNG,
) -> PersonFraktion | None:
    return fraktionen.fraktion_aus_einblendung(
        person=person, body=person.body, bezeichnung=bezeichnung, zeitpunkt=zeitpunkt, eindeutig=eindeutig
    )


def _hand(person: OParlPerson, bezeichnung: str, **felder: Any) -> PersonFraktion:
    return PersonFraktion.objects.create(
        person=person, body=person.body, bezeichnung=bezeichnung, quelle=PersonFraktion.QUELLE_HAND, **felder
    )


def _ereignisse() -> list[Event]:
    return list(Event.objects.filter(type=TYP).order_by("id"))


# =============================================================================
# Regeln der automatischen Übernahme
# =============================================================================


class TestEinblendung:
    def test_eindeutig_gelesen_wird_bestaetigt(self, person: OParlPerson) -> None:
        zuordnung = _lesen(person)

        assert zuordnung is not None
        assert (zuordnung.status, zuordnung.quelle, zuordnung.bezeichnung) == (
            "bestaetigt",
            "einblendung",
            "Fraktion A",
        )
        assert (zuordnung.gueltig_ab, zuordnung.gueltig_bis, zuordnung.belege) == (HEUTE, None, 1)
        assert zuordnung.zuletzt_gesehen == LESUNG
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) == zuordnung

    def test_unsicher_gelesen_wird_vorschlag_und_zaehlt_hoch(self, person: OParlPerson) -> None:
        erste = _lesen(person, eindeutig=False)
        zweite = _lesen(person, eindeutig=False, zeitpunkt=LESUNG + timedelta(seconds=10))

        assert erste is not None and zweite is not None
        assert erste.pk == zweite.pk
        assert (zweite.status, zweite.belege) == ("vorschlag", 2)
        assert PersonFraktion.objects.count() == 1
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) is None

    def test_vorschlag_wird_bei_eindeutiger_lesung_bestaetigt(self, person: OParlPerson) -> None:
        vorschlag = _lesen(person, eindeutig=False)
        bestaetigt = _lesen(person, eindeutig=True, zeitpunkt=LESUNG + timedelta(minutes=1))

        assert vorschlag is not None and bestaetigt is not None
        assert bestaetigt.pk == vorschlag.pk
        assert (bestaetigt.status, bestaetigt.belege, bestaetigt.gueltig_ab) == ("bestaetigt", 2, HEUTE)

    def test_gleiche_lesung_zaehlt_nur_hoch(self, person: OParlPerson) -> None:
        _lesen(person)
        spaeter = LESUNG + timedelta(minutes=5)
        _lesen(person, "  fraktion   a |", zeitpunkt=spaeter)
        # Eine ältere Aufzeichnung (Nachverarbeitung) setzt den Zeitpunkt nicht zurück
        zuordnung = _lesen(person, zeitpunkt=LESUNG - timedelta(days=30))

        assert zuordnung is not None
        assert (zuordnung.belege, zuordnung.zuletzt_gesehen) == (3, spaeter)
        assert PersonFraktion.objects.count() == 1

    def test_abweichende_lesung_wird_vorschlag_und_ueberschreibt_nie(self, person: OParlPerson) -> None:
        bestehend = _lesen(person)
        abweichend = _lesen(person, "Fraktion B", eindeutig=True)

        assert bestehend is not None and abweichend is not None
        assert (abweichend.status, abweichend.bezeichnung) == ("vorschlag", "Fraktion B")
        bestehend.refresh_from_db()
        assert (bestehend.status, bestehend.bezeichnung, bestehend.gueltig_bis) == ("bestaetigt", "Fraktion A", None)
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) == bestehend

    def test_von_hand_gepflegt_geht_immer_vor(self, person: OParlPerson) -> None:
        hand = _hand(person, "Fraktion A", gueltig_ab=date(2025, 1, 1))

        vorschlag = _lesen(person, "Fraktion B", eindeutig=True)

        assert vorschlag is not None and vorschlag.status == "vorschlag"
        hand.refresh_from_db()
        assert (hand.bezeichnung, hand.quelle, hand.status, hand.gueltig_bis) == (
            "Fraktion A",
            "hand",
            "bestaetigt",
            None,
        )
        # Gelten zwei bestätigte Zuordnungen zugleich, zeigt die Anzeige die von Hand gepflegte
        PersonFraktion.objects.create(
            person=person,
            body=person.body,
            bezeichnung="Fraktion C",
            quelle=PersonFraktion.QUELLE_EINBLENDUNG,
            gueltig_ab=HEUTE,
            gueltig_bis=HEUTE + timedelta(days=30),
        )
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) == hand
        assert angaben_fuer([person], stichtag=HEUTE)[person.pk].fraktion_lokal == hand

    @pytest.mark.parametrize(
        "funktion",
        [
            "Oberbürgermeisterin",
            "Bürgermeister",
            "Beigeordneter",
            "Stadtkämmerin",
            "Stadtkämmerer",
            # Lesefehler der Texterkennung: Umlaut verloren, als Ersatzzeichen, „ü“ als „ii“, „mm“ als „rnm“
            "Stadtkammerer",
            "Stadtka�mmerin",
            "Oberbiirgermeister",
            "Oberbu�rgermeisterin",
            "Stadtkämrnerin",
            "Stadträtin",
            "Erster Beigeordneter",
        ],
    )
    def test_funktionsbezeichnungen_sind_keine_fraktion(self, person: OParlPerson, funktion: str) -> None:
        assert _lesen(person, funktion) is None
        assert _lesen(person, " | – ") is None
        assert not PersonFraktion.objects.exists()

    def test_abgelehnt_bleibt_abgelehnt(self, person: OParlPerson) -> None:
        vorschlag = _lesen(person, eindeutig=False)
        assert vorschlag is not None
        fraktionen.ablehnen(vorschlag)

        zuordnung = _lesen(person, eindeutig=True)

        assert zuordnung is not None
        assert (zuordnung.pk, zuordnung.status, zuordnung.belege) == (vorschlag.pk, "abgelehnt", 2)
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) is None

    def test_beendete_zuordnung_kommt_nicht_von_selbst_zurueck(self, person: OParlPerson) -> None:
        _hand(person, "Fraktion A", gueltig_ab=date(2025, 1, 1), gueltig_bis=HEUTE - timedelta(days=7))

        zuordnung = _lesen(person, "Fraktion A", eindeutig=True)

        assert zuordnung is not None and zuordnung.status == "vorschlag"

    def test_zeitpunkt_braucht_zeitzone(self, person: OParlPerson) -> None:
        with pytest.raises(ValueError, match="Zeitzone"):
            _lesen(person, zeitpunkt=datetime(2026, 10, 7, 16, 20))  # absichtlich ohne Zeitzone

    def test_gleichzeitige_erste_lesung_zaehlt_die_andere_hoch(
        self, person: OParlPerson, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eine zweite Lesung legt die Zuordnung zwischen Lesen und Anlegen an: neuer Versuch statt Fehler."""
        echt = fraktionen._verbuchen
        aufrufe: list[int] = []

        def mit_konkurrenz(*args: Any) -> PersonFraktion:
            aufrufe.append(1)
            if len(aufrufe) == 1:
                with transaction.atomic():
                    echt(*args)  # die andere Lesung
                raise IntegrityError("personfraktion_eine_laufende")
            return echt(*args)

        monkeypatch.setattr(fraktionen, "_verbuchen", mit_konkurrenz)
        zuordnung = _lesen(person)

        assert zuordnung is not None
        assert len(aufrufe) == 2
        assert (zuordnung.status, zuordnung.belege) == ("bestaetigt", 1)
        assert PersonFraktion.objects.count() == 1

    def test_datenbank_verhindert_doppelte_laufende_und_doppelte_vorschlaege(self, person: OParlPerson) -> None:
        _hand(person, "Fraktion A")
        with pytest.raises(IntegrityError), transaction.atomic():
            _hand(person, "Fraktion B")
        PersonFraktion.objects.create(person=person, body=person.body, bezeichnung="Fraktion C", status="vorschlag")
        with pytest.raises(IntegrityError), transaction.atomic():
            PersonFraktion.objects.create(person=person, body=person.body, bezeichnung="FRAKTION C", status="vorschlag")


# =============================================================================
# Unscharfer Abgleich gelesener Bezeichnungen (keine Dubletten durch Lesefehler)
# =============================================================================


class TestAbgleich:
    @pytest.mark.parametrize(
        ("bekannt", "gelesen"),
        [
            ("Bürgerliste Süd", "Burgerliste Sud"),
            ("Bürgerliste Süd", "Bu�rgerliste Su�d"),
            ("Bürgerliste Süd", "Biirgerliste Süd"),
            ("Fraktion Grünwald", "Grünwald"),
            # abgeschnitten, auch zusätzlich verlesen
            ("Internationale Fraktion Musterpartei", "Internationale Fraktion Mus"),
            ("Internationale Fraktion Musterpartei", "Intemationale Fraktion Muster"),
        ],
    )
    def test_lesefehler_und_abgeschnittene_sind_dieselbe_fraktion(self, bekannt: str, gelesen: str) -> None:
        assert fraktionen.gleiche_fraktion(bekannt, gelesen)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Fraktion A", "Fraktion B"),
            ("Bürgerliste Süd", "Bürgerliste Nord"),
            # Ein anderes Wort bleibt ein Unterschied, auch wenn der Rest lang und gleich ist
            ("Wählergemeinschaft Musterstadt Nord", "Wählergemeinschaft Musterstadt Süd"),
        ],
    )
    def test_andere_woerter_sind_andere_fraktionen(self, a: str, b: str) -> None:
        assert not fraktionen.gleiche_fraktion(a, b)

    @pytest.mark.parametrize("text", ["Bürgerliste Süd", "Stadtratsfraktion A", "Fraktion A", "Bürgerforum Mitte"])
    def test_fraktionen_sind_keine_funktion(self, text: str) -> None:
        assert not fraktionen.ist_funktionsbezeichnung(text)

    def test_verlesene_lesungen_anderer_personen_ergeben_keine_dublette(self, body: OParlBody) -> None:
        erste, zweite, dritte = (_person(body, str(n), f"Person Muster{n}") for n in range(3))
        _lesen(erste, "Bürgerliste Süd")
        _lesen(erste, "Internationale Fraktion Musterpartei", eindeutig=False)

        verlesen = _lesen(zweite, "Bu�rgerliste Sud")
        abgeschnitten = _lesen(dritte, "Internationale Fraktion Mus…", eindeutig=False)

        assert verlesen is not None and abgeschnitten is not None
        assert (verlesen.status, verlesen.bezeichnung) == ("bestaetigt", "Bürgerliste Süd")
        assert abgeschnitten.bezeichnung == "Internationale Fraktion Musterpartei"
        assert set(PersonFraktion.objects.values_list("bezeichnung", flat=True)) == {
            "Bürgerliste Süd",
            "Internationale Fraktion Musterpartei",
        }

    def test_verlesen_zaehlt_die_bestaetigte_hoch(self, person: OParlPerson) -> None:
        bestaetigt = _lesen(person, "Fraktion Grünwald")
        nochmal = _lesen(person, "FRAKTION GRUNWALD", zeitpunkt=LESUNG + timedelta(minutes=3))

        assert bestaetigt is not None and nochmal is not None
        assert (nochmal.pk, nochmal.bezeichnung, nochmal.belege) == (bestaetigt.pk, "Fraktion Grünwald", 2)
        assert PersonFraktion.objects.count() == 1

    def test_vorschlag_uebernimmt_die_bessere_schreibweise(self, body: OParlBody) -> None:
        erste, zweite = _person(body, "1", "Erika Muster"), _person(body, "2", "Max Beispiel")
        ohne_umlaut = _lesen(erste, "Burgerliste Sud", eindeutig=False)
        abgeschnitten = _lesen(zweite, "Internationale Fraktion Mus", eindeutig=False)

        mit_umlaut = _lesen(erste, "Bürgerliste Süd", eindeutig=False)
        vollstaendig = _lesen(zweite, "Internationale Fraktion Musterpartei", eindeutig=False)

        assert ohne_umlaut is not None and mit_umlaut is not None
        assert abgeschnitten is not None and vollstaendig is not None
        assert (mit_umlaut.pk, mit_umlaut.bezeichnung, mit_umlaut.belege) == (ohne_umlaut.pk, "Bürgerliste Süd", 2)
        assert (vollstaendig.pk, vollstaendig.bezeichnung) == (abgeschnitten.pk, "Internationale Fraktion Musterpartei")
        assert PersonFraktion.objects.count() == 2

    def test_von_hand_gepflegte_schreibweise_gilt_fuer_alle(self, body: OParlBody) -> None:
        _hand(_person(body, "1", "Erika Muster"), "Burgerliste Süd")

        gelesen = _lesen(_person(body, "2", "Max Beispiel"), "Bürgerliste Süd")

        assert gelesen is not None and gelesen.bezeichnung == "Burgerliste Süd"

    def test_von_hand_bearbeiteter_vorschlag_wird_nicht_umbenannt(self, body: OParlBody) -> None:
        """Ein Vorschlag, dessen Schreibweise jemand im Admin gesetzt hat, behält sie; auch für andere Lesungen."""
        erste, zweite = _person(body, "1", "Erika Muster"), _person(body, "2", "Max Beispiel")
        bearbeitet = _hand(erste, "Burgerliste Sud", status=PersonFraktion.STATUS_VORSCHLAG)

        nochmal = _lesen(erste, "Bürgerliste Süd", eindeutig=False)
        andere = _lesen(zweite, "Bürgerliste Süd", eindeutig=False)

        assert nochmal is not None and andere is not None
        assert (nochmal.pk, nochmal.bezeichnung, nochmal.quelle) == (bearbeitet.pk, "Burgerliste Sud", "hand")
        assert andere.bezeichnung == "Burgerliste Sud"
        assert PersonFraktion.objects.count() == 2


# =============================================================================
# Ereignis an die Datendrehscheibe
# =============================================================================


@pytest.mark.usefixtures("ereignisse")
class TestEreignis:
    def test_bestaetigte_zuordnung_meldet_nur_kennungen_und_codes(self, person: OParlPerson) -> None:
        zuordnung = _lesen(person)
        assert zuordnung is not None

        (ereignis,) = _ereignisse()
        assert (ereignis.aggregate_type, ereignis.aggregate_id, ereignis.body_id) == (
            "Person",
            person.pk,
            person.body_id,
        )
        assert (ereignis.version, ereignis.visibility) == (1, "oeffentlich")
        assert ereignis.tenant_ref == f"source:{person.body.source_id}"
        assert ereignis.payload == {
            "assignment": str(zuordnung.pk),
            "person": str(person.pk),
            "source": "einblendung",
            "status": "bestaetigt",
            "valid_from": "2026-10-07",
        }
        assert "Fraktion" not in json.dumps(ereignis.payload)

    def test_vorschlaege_und_weitere_lesungen_melden_nichts(self, person: OParlPerson) -> None:
        _lesen(person, eindeutig=False)
        assert _ereignisse() == []
        _lesen(person, eindeutig=True)
        _lesen(person, eindeutig=True)
        assert len(_ereignisse()) == 1

    def test_ohne_schalter_oder_bei_ausgenommener_quelle_kein_ereignis(
        self, person: OParlPerson, settings: Any
    ) -> None:
        settings.INGESTOR_EVENTS_ENABLED = False
        assert _lesen(person) is not None
        settings.INGESTOR_EVENTS_ENABLED = True
        OParlSource.objects.filter(pk=person.body.source_id).update(sync_config={"events_enabled": False})
        assert _lesen(_person(person.body, "2", "Max Beispiel")) is not None

        assert PersonFraktion.objects.filter(status="bestaetigt").count() == 2
        assert _ereignisse() == []

    def test_scheitert_das_ereignis_bleibt_auch_die_zuordnung_aus(
        self, person: OParlPerson, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def kaputt(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Journal nicht erreichbar")

        monkeypatch.setattr("hub.ris.faction_assignment.publish", kaputt)
        with pytest.raises(RuntimeError):
            _lesen(person)
        assert not PersonFraktion.objects.exists()


# =============================================================================
# Anzeige
# =============================================================================


def _client(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


class TestAnzeige:
    def test_angaben_nehmen_die_zuordnung_nur_ohne_oparl_fraktion(self, body: OParlBody) -> None:
        ohne_oparl = _person(body, "1", "Erika Muster")
        mit_oparl = _person(body, "2", "Max Beispiel")
        nur_vorschlag = _person(body, "3", "Paula Probe")
        beendet = _person(body, "4", "Otto Ehemalig")
        fraktion = OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/f",
            body=body,
            name="Fraktion Mitte",
            short_name="Mitte",
            organization_type="Fraktion",
        )
        OParlMembership.objects.create(
            external_id=f"{RIS}/membership/1", person=mit_oparl, organization=fraktion, role="Mitglied"
        )
        _hand(ohne_oparl, "Fraktion A")
        _hand(mit_oparl, "Fraktion B")
        _lesen(nur_vorschlag, eindeutig=False)
        _hand(beendet, "Fraktion A", gueltig_ab=date(2024, 1, 1), gueltig_bis=date(2025, 1, 1))

        angaben = angaben_fuer([ohne_oparl, mit_oparl, nur_vorschlag, beendet], stichtag=HEUTE)

        assert (angaben[ohne_oparl.pk].fraktion_name, angaben[ohne_oparl.pk].fraktion_hinweis) == (
            "Fraktion A",
            "redaktionell gepflegt",
        )
        assert (angaben[mit_oparl.pk].fraktion_name, angaben[mit_oparl.pk].fraktion_hinweis) == ("Mitte", "")
        assert angaben[mit_oparl.pk].fraktion_lokal is None
        assert angaben[nur_vorschlag.pk].fraktion_name == ""
        assert angaben[beendet.pk].fraktion_name == ""

    def test_zuordnung_einer_anderen_koerperschaft_zaehlt_nicht(self, body: OParlBody, person: OParlPerson) -> None:
        andere = OParlBody.objects.create(
            external_id=f"{RIS}/body/2", source=body.source, name="Nachbarstadt", slug="nachbar"
        )
        PersonFraktion.objects.create(person=person, body=andere, bezeichnung="Fraktion A", quelle="hand")
        assert angaben_fuer([person], stichtag=HEUTE)[person.pk].fraktion_lokal is None

    def test_eine_abfrage_fuer_alle_zuordnungen(self, body: OParlBody, django_assert_num_queries: Any) -> None:
        personen = [_person(body, str(nummer), f"Person Muster{nummer}") for nummer in range(10)]
        for person in personen:
            _hand(person, "Fraktion A")
        # Mitgliedschaften und Zuordnungen: je eine Abfrage für die ganze Seite
        with django_assert_num_queries(2):
            angaben = angaben_fuer(personen, stichtag=HEUTE)
        assert all(angaben[p.pk].fraktion_name == "Fraktion A" for p in personen)

    def test_personenliste_zeigt_fraktion_mit_quellenhinweis(self, person: OParlPerson) -> None:
        _lesen(person, zeitpunkt=timezone.now())

        html = _client(person.body).get("/insight/personen/").content.decode()

        assert ">Fraktion</th>" in html
        assert 'title="laut Einblendung der Live-Übertragung">Fraktion A' in html
        assert '<span class="sr-only"> (laut Einblendung der Live-Übertragung)</span>' in html

    def test_personenliste_ohne_bestaetigte_zuordnung_ohne_spalte(self, person: OParlPerson) -> None:
        _lesen(person, eindeutig=False, zeitpunkt=timezone.now())
        html = _client(person.body).get("/insight/personen/").content.decode()
        assert ">Fraktion</th>" not in html
        assert "Fraktion A" not in html

    def test_personenseite_nennt_fraktion_und_quelle(self, person: OParlPerson) -> None:
        _hand(person, "Fraktion A")

        html = _client(person.body).get(f"/insight/personen/{person.pk}/").content.decode()

        kopf = html.split('<header class="rounded-2xl', 1)[1].split("</header>", 1)[0]
        assert "Fraktion A" in kopf and "redaktionell gepflegt" in kopf


# =============================================================================
# Pflege im Admin
# =============================================================================


@pytest.fixture
def admin_client(db: Any) -> Client:
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)
    return client


def _aktion(admin_client: Client, aktion: str, *zuordnungen: PersonFraktion) -> Any:
    return admin_client.post(
        reverse("admin:insight_core_personfraktion_changelist"),
        {"action": aktion, "_selected_action": [str(z.pk) for z in zuordnungen]},
        follow=True,
    )


@pytest.mark.usefixtures("ereignisse")
class TestAdmin:
    def test_liste_mit_filtern_und_suche(self, admin_client: Client, person: OParlPerson) -> None:
        _lesen(person, eindeutig=False)
        url = reverse("admin:insight_core_personfraktion_changelist")
        antwort = admin_client.get(url, {"status__exact": "vorschlag", "q": "Muster"})
        assert antwort.status_code == 200
        assert "Fraktion A" in antwort.content.decode()

    def test_von_hand_anlegen_beendet_die_bisherige(self, admin_client: Client, person: OParlPerson) -> None:
        alt = _lesen(person)
        assert alt is not None

        antwort = admin_client.post(
            reverse("admin:insight_core_personfraktion_add"),
            {
                "person": str(person.pk),
                "body": "",
                "bezeichnung": "Fraktion B",
                "partei": "",
                "organisation": "",
                "gueltig_ab": "2026-11-01",
                "gueltig_bis": "",
            },
        )

        assert antwort.status_code == 302, antwort.content.decode()[:2000]
        neu = PersonFraktion.objects.get(bezeichnung="Fraktion B")
        assert (neu.quelle, neu.status, neu.body_id, neu.gueltig_ab) == (
            "hand",
            "bestaetigt",
            person.body_id,
            date(2026, 11, 1),
        )
        alt.refresh_from_db()
        assert (alt.status, alt.gueltig_bis) == ("bestaetigt", date(2026, 10, 31))
        # Ereignisse: die neue Zuordnung der automatischen, dann das Ende der alten und die neue
        assert [e.payload.get("valid_until") for e in _ereignisse()] == [None, "2026-10-31", None]

    def test_bestaetigen_beendet_die_bisherige_laufende(self, admin_client: Client, person: OParlPerson) -> None:
        alt = _hand(person, "Fraktion A", gueltig_ab=date(2025, 1, 1))
        vorschlag = _lesen(person, "Fraktion B", eindeutig=True)
        assert vorschlag is not None and vorschlag.status == "vorschlag"

        antwort = _aktion(admin_client, "bestaetigen_auswahl", vorschlag)

        assert antwort.status_code == 200
        vorschlag.refresh_from_db()
        alt.refresh_from_db()
        assert (vorschlag.status, vorschlag.gueltig_ab, vorschlag.quelle) == ("bestaetigt", HEUTE, "einblendung")
        assert alt.gueltig_bis == HEUTE - timedelta(days=1)
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) == vorschlag
        assert {e.payload["assignment"] for e in _ereignisse()} == {str(alt.pk), str(vorschlag.pk)}

    def test_ablehnen_nimmt_eine_bestaetigte_zuruck(self, admin_client: Client, person: OParlPerson) -> None:
        zuordnung = _lesen(person)
        assert zuordnung is not None

        _aktion(admin_client, "ablehnen_auswahl", zuordnung)

        zuordnung.refresh_from_db()
        assert zuordnung.status == "abgelehnt"
        assert fraktionen.aktuelle_fraktion(person, person.body, stichtag=HEUTE) is None
        assert [e.payload["status"] for e in _ereignisse()] == ["bestaetigt", "abgelehnt"]

    def test_inhaltliche_aenderung_macht_die_zuordnung_zur_handpflege(
        self, admin_client: Client, person: OParlPerson
    ) -> None:
        zuordnung = _lesen(person)
        assert zuordnung is not None
        url = reverse("admin:insight_core_personfraktion_change", args=[zuordnung.pk])

        antwort = admin_client.post(
            url,
            {
                "person": str(person.pk),
                "body": str(person.body_id),
                "bezeichnung": "Fraktion A (neu)",
                "partei": "Partei X",
                "organisation": "",
                "gueltig_ab": "2026-10-07",
                "gueltig_bis": "",
            },
        )

        assert antwort.status_code == 302, antwort.content.decode()[:2000]
        zuordnung.refresh_from_db()
        assert (zuordnung.quelle, zuordnung.bezeichnung, zuordnung.partei) == ("hand", "Fraktion A (neu)", "Partei X")


# =============================================================================
# Kein stilles Löschen (ADR Fremdschlüssel auf den RIS-Bestand)
# =============================================================================


class TestLoeschschutz:
    def test_person_mit_zuordnung_bleibt_beim_aufraeumen(self, person: OParlPerson) -> None:
        _hand(person, "Fraktion A")
        OParlPerson.objects.filter(pk=person.pk).update(deleted=True, deleted_at=timezone.now())

        loeschbar, geschuetzt = split_by_references(OParlPerson.objects.filter(deleted=True))

        assert list(loeschbar) == []
        assert list(geschuetzt) == [person]

    def test_kommune_mit_zuordnungen_wird_nicht_geloescht(self, person: OParlPerson) -> None:
        _hand(person, "Fraktion A")

        ergebnis = delete_body_data(str(person.body_id))

        assert ergebnis["deleted"] == 0
        assert any("insight_core.PersonFraktion" in eintrag for eintrag in ergebnis["blocked_by"])
        assert OParlPerson.objects.filter(pk=person.pk).exists()
