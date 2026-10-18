# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Strukturierte Daten der Sitzungsseiten als Veranstaltung und erreichbare Brotkrumen (Issue #939).

Die Search Console meldete im Bericht „Veranstaltungen“ Sitzungen ohne ``location`` (ungültig) und Warnungen
zu ``organizer.url``, ``offers``, ``image``, ``performer``, ``location.address`` und ``description``. Eine
Sitzung erscheint deshalb nur noch mit Beginn und einem Ort samt Postanschrift als ``Event``; ohne verlässlichen
Ort trägt die Seite nur die Brotkrumen. Veranstalter ist die Verwaltung mit der Seite der Kommune, Mitwirkende
sind die Gremien mit ihren Seiten, Eintritt frei nur bei einer ausdrücklich öffentlichen Sitzung. Die Brotkrumen
führen nur über Adressen, die eine Suchmaschine ohne gewählte Kommune direkt (Status 200) erreicht.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from insight_core.models import (
    Municipality,
    OParlAgendaItem,
    OParlBody,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_core.services.sitzungsort import anschrift_aus_text

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("site_url")]

SITE = "https://mandari.example"
BASIS = "https://ris.muenster.example/oparl/"
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.S | re.I)
AUSBRUCH = 'Saal "A" & B </script><script>alert(1)</script>'
ISO_MIT_VERSATZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$")


@pytest.fixture
def site_url() -> Iterator[None]:
    with override_settings(SITE_URL=SITE):
        yield


@pytest.fixture
def muenster() -> OParlBody:
    source = OParlSource.objects.create(name="RIS Münster", url=BASIS + "system")
    # Eine zweite gelistete Kommune wie in Produktion: Die Startseite von Insight ist dann die Kommunenauswahl
    OParlBody.objects.create(
        external_id=BASIS + "body/2", source=source, name="Stadt Bonn", display_name="Bonn", slug="bonn"
    )
    return OParlBody.objects.create(
        external_id=BASIS + "body/1",
        source=source,
        name="Stadt Münster",
        display_name="Münster",
        slug="muenster",
        ags="05515000",
    )


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _zeit(stunde: int = 17) -> datetime:
    return timezone.make_aware(datetime(2030, 11, 5, stunde))


def _gremium(body: OParlBody, name: str = "Rat") -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{BASIS}organization/{_id()}", body=body, name=name)


def _sitzung(body: OParlBody, *gremien: OParlOrganization, **felder: Any) -> OParlMeeting:
    werte: dict[str, Any] = {"name": "Sitzung", "start": _zeit()}
    werte.update(felder)
    sitzung = OParlMeeting.objects.create(external_id=f"{BASIS}meeting/{_id()}", body=body, **werte)
    sitzung.organizations.add(*gremien)
    return sitzung


def _punkt(sitzung: OParlMeeting, nummer: str = "1", **felder: Any) -> OParlAgendaItem:
    return OParlAgendaItem.objects.create(
        external_id=f"{BASIS}agendaitem/{_id()}", meeting=sitzung, number=nummer, name=f"Punkt {nummer}", **felder
    )


def _ort(**felder: Any) -> dict[str, Any]:
    """Eingebetteter OParl-Ort einer Sitzung (``Meeting.location``)."""
    return {"id": f"{BASIS}location/{_id()}", "type": "https://schema.oparl.org/1.1/Location", **felder}


def _seite(url: str, client: Client | None = None) -> str:
    antwort = (client or Client()).get(url)
    assert antwort.status_code == 200, url
    return antwort.content.decode()


def _json_ld(seite: str) -> list[dict[str, Any]]:
    return [json.loads(inhalt) for attribute, inhalt in SCRIPT_RE.findall(seite) if "ld+json" in attribute.lower()]


def _event(seite: str) -> dict[str, Any] | None:
    daten = _json_ld(seite)
    events = [d for d in daten if d.get("@type") == "Event"]
    assert len(events) <= 1
    return events[0] if events else None


def _ohne_leere_werte(wert: Any, pfad: str = "") -> None:
    if isinstance(wert, dict):
        for schluessel, inhalt in wert.items():
            _ohne_leere_werte(inhalt, f"{pfad}.{schluessel}")
    elif isinstance(wert, list):
        assert wert, pfad
        for nummer, inhalt in enumerate(wert):
            _ohne_leere_werte(inhalt, f"{pfad}[{nummer}]")
    else:
        assert wert not in (None, ""), pfad


def _absolut(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(f"{SITE}/")


def _pruefe_event(event: dict[str, Any]) -> None:
    """Pflichtangaben für Veranstaltungen in der Google-Suche, offline nachgebildet."""
    assert event["@context"] == "https://schema.org" and event["@type"] == "Event"
    assert isinstance(event["name"], str) and event["name"].strip()
    assert ISO_MIT_VERSATZ.match(event["startDate"]), event["startDate"]
    assert isinstance(event["description"], str) and event["description"].strip()
    ort = event["location"]
    assert ort["@type"] == "Place"
    anschrift = ort["address"]
    assert anschrift["@type"] == "PostalAddress" and anschrift["addressCountry"] == "DE"
    assert anschrift.get("streetAddress") or anschrift.get("addressLocality")
    assert isinstance(ort.get("name"), str) and ort["name"].strip(), "location.name fehlt"
    assert _absolut(event["url"]) and _absolut(event["image"])
    assert event["organizer"]["name"] and _absolut(event["organizer"]["url"])
    for gremium in event.get("performer", []):
        assert gremium["@type"] == "Organization" and gremium["name"] and _absolut(gremium["url"])
    if "offers" in event:
        angebot = event["offers"]
        gueltig_ab = angebot.pop("validFrom")
        assert ISO_MIT_VERSATZ.match(gueltig_ab), gueltig_ab
        assert gueltig_ab <= event["startDate"] or gueltig_ab[:10] <= event["startDate"][:10]
        assert angebot == {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock",
            "url": event["url"],
        }
        assert event["isAccessibleForFree"] is True
    else:
        assert "isAccessibleForFree" not in event
    _ohne_leere_werte(event)


def _pfad(url: str) -> str:
    return urlsplit(url).path


# =============================================================================
# Veranstaltung mit Ort und Postanschrift
# =============================================================================


class TestVeranstaltung:
    def test_vollstaendig_mit_ort_aus_der_quelle(self, muenster: OParlBody) -> None:
        rat = _gremium(muenster)
        sitzung = _sitzung(
            muenster,
            rat,
            name="Rat",
            end=_zeit(20),
            location_name="Rathaus, Festsaal",
            location_address="Musterstraße 1",
            raw_json={
                "location": _ort(
                    description="Rathaus, Festsaal",
                    room="Festsaal",
                    streetAddress="Musterstraße 1",
                    postalCode="48143",
                    locality="Münster",
                )
            },
        )
        _punkt(sitzung, "1", public=True, raw_json={"public": True})
        _punkt(sitzung, "2", public=False, raw_json={"public": False})

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        seite = f"{SITE}/insight/termine/{sitzung.id}/"
        assert event == {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": "Rat am 05.11.2030",
            "description": (
                "Münster: Sitzung im Rat am Dienstag, 05.11.2030, 17:00 Uhr, Rathaus, Festsaal. "
                "Tagesordnung mit 2 Punkten."
            ),
            "url": seite,
            "inLanguage": "de",
            "startDate": "2030-11-05T17:00:00+01:00",
            "endDate": "2030-11-05T20:00:00+01:00",
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "eventStatus": "https://schema.org/EventScheduled",
            "location": {
                "@type": "Place",
                "name": "Rathaus, Festsaal",
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "Musterstraße 1",
                    "postalCode": "48143",
                    "addressLocality": "Münster",
                    "addressCountry": "DE",
                },
            },
            "image": f"{SITE}/static/images/og-default.png",
            "organizer": {"@type": "GovernmentOrganization", "name": "Münster", "url": f"{SITE}/insight/k/muenster/"},
            "performer": [{"@type": "Organization", "name": "Rat", "url": f"{SITE}/insight/gremien/{rat.id}/"}],
            "isAccessibleForFree": True,
            "offers": {
                "@type": "Offer",
                "price": "0",
                "priceCurrency": "EUR",
                "availability": "https://schema.org/InStock",
                "url": seite,
            },
        }

    def test_ort_als_verweis_auf_einen_oparl_ort(self, muenster: OParlBody) -> None:
        verweis = f"{BASIS}location/{_id()}"
        OParlLocation.objects.create(
            external_id=verweis,
            body=muenster,
            room="Sitzungssaal 2",
            street_address="Klemensstraße 10",
            postal_code="48143",
            locality="Münster",
        )
        sitzung = _sitzung(muenster, _gremium(muenster), raw_json={"location": verweis})

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        assert event["location"] == {
            "@type": "Place",
            "name": "Sitzungssaal 2",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "Klemensstraße 10",
                "postalCode": "48143",
                "addressLocality": "Münster",
                "addressCountry": "DE",
            },
        }

    def test_anschrift_aus_dem_text_der_sitzung(self, muenster: OParlBody) -> None:
        # So schreibt der Spiegel von mandari Session den Ort: Gebäude und Anschrift als Text
        OParlBody.objects.filter(pk=muenster.pk).update(ags=None)
        sitzung = _sitzung(
            muenster,
            _gremium(muenster),
            location_name="Stadtweinhaus",
            location_address="Prinzipalmarkt 8-9, 48143 Münster",
        )

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        assert event["location"]["name"] == "Stadtweinhaus"
        assert event["location"]["address"] == {
            "@type": "PostalAddress",
            "streetAddress": "Prinzipalmarkt 8-9",
            "postalCode": "48143",
            "addressLocality": "Münster",
            "addressCountry": "DE",
        }

    def test_ohne_anschrift_der_ort_der_gemeinde(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus, Festsaal")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        assert event["location"] == {
            "@type": "Place",
            "name": "Rathaus, Festsaal",
            "address": {"@type": "PostalAddress", "addressLocality": "Münster", "addressCountry": "DE"},
        }

    def test_ort_der_gemeinde_aus_dem_kommunenverzeichnis(self, muenster: OParlBody) -> None:
        Municipality.objects.create(
            key="055150000000", ags="05515000", name="Münster (Westf.)", district_key="05515", state_key="05"
        )
        sitzung = _sitzung(muenster, _gremium(muenster))

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        assert event["location"]["address"]["addressLocality"] == "Münster (Westf.)"
        # Ohne Raumangabe heißt der Ort wie die Gemeinde (Google erwartet location.name)
        assert event["location"]["name"] == event["location"]["address"]["addressLocality"]

    @pytest.mark.parametrize(
        "koerperschaft",
        [
            {"ags": None},  # Schlüssel unbekannt
            {"ags": "053", "display_name": "Regionalrat Köln"},  # Regierungsbezirk
            {"ags": "05515000", "is_non_territorial": True},  # Zweckverband o. Ä. ohne eigenes Gebiet
            {"ags": "05515000", "display_name": None},  # kein gepflegter Name
        ],
    )
    def test_kein_event_ohne_verlaesslichen_ort(self, muenster: OParlBody, koerperschaft: dict[str, Any]) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(**koerperschaft)
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Sitzungssaal")

        daten = _json_ld(_seite(f"/insight/termine/{sitzung.id}/"))

        assert [d["@type"] for d in daten] == ["BreadcrumbList"]

    def test_kein_event_ohne_beginn(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(muenster, _gremium(muenster), start=None, location_address="Musterstraße 1, 48143 Münster")

        daten = _json_ld(_seite(f"/insight/termine/{sitzung.id}/"))

        assert [d["@type"] for d in daten] == ["BreadcrumbList"]

    def test_bild_ist_das_logo_der_kommune(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(logo="bodies/logos/wappen-muenster.png")
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert event["image"] == f"{SITE}/media/bodies/logos/wappen-muenster.png"

    def test_veranstalter_ohne_einstieg_mit_der_webseite_der_kommune(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(slug=None, website="https://www.stadt-muenster.example/")
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert event["organizer"]["url"] == "https://www.stadt-muenster.example/"

    def test_veranstalter_einer_nicht_gelisteten_kommune(self, muenster: OParlBody) -> None:
        # Ausgeblendete Pilotquelle: Ihre Seiten sind direkt erreichbar, ihr Einstieg /insight/k/<slug>/ nicht
        OParlBody.objects.filter(pk=muenster.pk).update(is_listed=False, website="https://www.stadt-muenster.example/")
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert Client().get("/insight/k/muenster/").status_code == 404
        assert event["organizer"]["url"] == "https://www.stadt-muenster.example/"

    def test_veranstalter_ohne_erreichbare_seite_ohne_url(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(is_listed=False, website=None)
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert event["organizer"] == {"@type": "GovernmentOrganization", "name": "Münster"}

    def test_veranstalter_und_mitwirkende_sind_erreichbar(self, muenster: OParlBody) -> None:
        rat = _gremium(muenster, "Rat")
        ausschuss = _gremium(muenster, "Ausschuss für Umwelt")
        sitzung = _sitzung(muenster, rat, ausschuss, location_name="Rathaus")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert {g["name"] for g in event["performer"]} == {"Rat", "Ausschuss für Umwelt"}
        for url in [event["organizer"]["url"], *(g["url"] for g in event["performer"])]:
            _seite(_pfad(url))

    def test_description_ist_immer_gesetzt(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(muenster, name=None, location_name="Rathaus")

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        assert event["description"].startswith("Münster: Sitzung am Dienstag, 05.11.2030")
        assert "performer" not in event

    def test_sonderzeichen_sicher_serialisiert(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(
            muenster,
            _gremium(muenster, AUSBRUCH),
            location_name=AUSBRUCH,
            location_address="Straße </script> 1, 48143 Münster",
        )

        seite = _seite(f"/insight/termine/{sitzung.id}/")

        assert "</script><script>alert(1)" not in seite
        event = _event(seite)
        assert event is not None
        assert event["location"]["name"] == AUSBRUCH
        assert event["performer"][0]["name"] == AUSBRUCH
        assert event["location"]["address"]["streetAddress"] == "Straße </script> 1"


# =============================================================================
# Eintritt frei nur bei öffentlichen Sitzungen
# =============================================================================


class TestOrtsnameUndGueltigAb:
    @pytest.mark.parametrize("platzhalter", [None, "", "Noch offen", "noch offen.", "N.N."])
    def test_ohne_raum_heisst_der_ort_wie_die_gemeinde(self, muenster: OParlBody, platzhalter: Any) -> None:
        sitzung = _sitzung(muenster, _gremium(muenster), location_name=platzhalter)

        seite = _seite(f"/insight/termine/{sitzung.id}/")
        event = _event(seite)

        assert event is not None
        _pruefe_event(event)
        assert event["location"]["name"] == "Münster"
        assert "Noch offen" not in event["description"] and "N.N." not in event["description"]

    def test_gueltig_ab_aus_created_der_quelle(self, muenster: OParlBody) -> None:
        erstellt = timezone.now() - timedelta(days=20)
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus", oparl_created=erstellt)
        _punkt(sitzung, "1", public=True, raw_json={"public": True})

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert event["offers"]["validFrom"][:10] == timezone.localtime(erstellt).date().isoformat()

    def test_unglaubhaftes_created_faellt_auf_den_abgleich_zurueck(self, muenster: OParlBody) -> None:
        ersatzwert = datetime(1999, 12, 31, tzinfo=timezone.get_current_timezone())
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus", oparl_created=ersatzwert)
        _punkt(sitzung, "1", public=True, raw_json={"public": True})

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        assert not event["offers"]["validFrom"].startswith("1999")
        _pruefe_event(event)


class TestEintrittFrei:
    @pytest.mark.parametrize(
        ("punkte", "abgesagt", "frei"),
        [
            ([{"public": True, "raw_json": {"public": True}}], False, True),
            # Ein öffentlicher Teil genügt
            (
                [{"public": False, "raw_json": {"public": False}}, {"public": True, "raw_json": {"public": True}}],
                False,
                True,
            ),
            # Ohne Angabe der Quelle steht die Spalte nur auf ihrem Standardwert: kein Hinweis auf Öffentlichkeit
            ([{"public": True, "raw_json": {}}], False, False),
            ([{"public": False, "raw_json": {"public": False}}], False, False),
            ([], False, False),
            # Abgesagt: kein Angebot
            ([{"public": True, "raw_json": {"public": True}}], True, False),
        ],
    )
    def test_angebot(self, muenster: OParlBody, punkte: list[dict[str, Any]], abgesagt: bool, frei: bool) -> None:
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus", cancelled=abgesagt)
        for nummer, felder in enumerate(punkte, start=1):
            _punkt(sitzung, str(nummer), **felder)

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None
        _pruefe_event(event)
        assert ("offers" in event) is frei
        assert event.get("isAccessibleForFree") is (True if frei else None)
        assert event["eventStatus"].endswith("EventCancelled" if abgesagt else "EventScheduled")

    def test_nicht_oeffentlicher_punkt_aus_der_tagesordnung_entfernt(self, muenster: OParlBody) -> None:
        sitzung = _sitzung(muenster, _gremium(muenster), location_name="Rathaus")
        _punkt(sitzung, "1", public=True, raw_json={"public": True}, deleted=True)

        event = _event(_seite(f"/insight/termine/{sitzung.id}/"))

        assert event is not None and "offers" not in event


# =============================================================================
# Brotkrumen nur über erreichbare Adressen
# =============================================================================


class TestBrotkrumen:
    def _seiten(self, body: OParlBody) -> dict[str, str]:
        rat = _gremium(body, "Rat")
        person = OParlPerson.objects.create(external_id=f"{BASIS}person/{_id()}", body=body, name="Erika Muster")
        OParlMembership.objects.create(
            external_id=f"{BASIS}membership/{_id()}", person=person, organization=rat, role="Mitglied"
        )
        paper = OParlPaper.objects.create(
            external_id=f"{BASIS}paper/{_id()}", body=body, name="Neubau der Feuerwache", reference="V/1/2030"
        )
        sitzung = _sitzung(body, rat, location_name="Rathaus")
        return {
            "V/1/2030": f"/insight/vorgaenge/{paper.id}/",
            "Rat am 05.11.2030": f"/insight/termine/{sitzung.id}/",
            "Rat": f"/insight/gremien/{rat.id}/",
            "Erika Muster": f"/insight/personen/{person.id}/",
        }

    def test_insight_kommune_objekt_ohne_zwischenebene(self, muenster: OParlBody) -> None:
        for name, url in self._seiten(muenster).items():
            krumen = _json_ld(_seite(url))[-1]

            assert krumen["@type"] == "BreadcrumbList"
            eintraege = krumen["itemListElement"]
            assert [e["name"] for e in eintraege] == ["mandari Insight", "Münster", name], url
            assert [e["position"] for e in eintraege] == [1, 2, 3]
            assert [_pfad(e["item"]) for e in eintraege] == ["/insight/", "/insight/k/muenster/", url]

    def test_jede_ebene_ist_ohne_weiterleitung_erreichbar(self, muenster: OParlBody) -> None:
        for url in self._seiten(muenster).values():
            for eintrag in _json_ld(_seite(url))[-1]["itemListElement"]:
                assert _absolut(eintrag["item"])
                # Suchmaschine ohne gewählte Kommune: frischer Client ohne Sitzung
                antwort = Client().get(_pfad(eintrag["item"]))
                assert antwort.status_code == 200, (url, eintrag["item"], antwort.status_code)

    def test_ohne_einstieg_der_kommune(self, muenster: OParlBody) -> None:
        OParlBody.objects.filter(pk=muenster.pk).update(slug=None)
        paper = OParlPaper.objects.create(external_id=f"{BASIS}paper/{_id()}", body=muenster, reference="V/2/2030")

        krumen = _json_ld(_seite(f"/insight/vorgaenge/{paper.id}/"))[-1]["itemListElement"]

        assert [e["name"] for e in krumen] == ["mandari Insight", "V/2/2030"]
        assert [_pfad(e["item"]) for e in krumen] == ["/insight/", f"/insight/vorgaenge/{paper.id}/"]

    def test_nicht_gelistete_kommune_ohne_kommune_ebene(self, muenster: OParlBody) -> None:
        # Ausgeblendete Pilotquelle: Detailseiten antworten, der Einstieg /insight/k/<slug>/ nicht (404)
        OParlBody.objects.filter(pk=muenster.pk).update(is_listed=False)
        muenster.refresh_from_db()
        # Weiter zwei gelistete Kommunen wie in Produktion (sonst leitet /insight/ auf die einzige weiter)
        OParlBody.objects.create(
            external_id=BASIS + "body/3", source=muenster.source, name="Stadt Köln", display_name="Köln", slug="koeln"
        )

        for name, url in self._seiten(muenster).items():
            krumen = _json_ld(_seite(url))[-1]["itemListElement"]

            assert [e["name"] for e in krumen] == ["mandari Insight", name], url
            for eintrag in krumen:
                assert Client().get(_pfad(eintrag["item"])).status_code == 200, (url, eintrag["item"])


# =============================================================================
# Anschrift aus Text
# =============================================================================


@pytest.mark.parametrize(
    ("text", "nur_mit_plz", "erwartet"),
    [
        ("Musterstraße 1, 48143 Münster", False, ("Musterstraße 1", "48143", "Münster")),
        ("Rathaus, Musterstraße 1, 48143 Münster", False, ("Musterstraße 1", "48143", "Münster")),
        ("Musterstraße 1, D-48143 Münster (Westf.)", False, ("Musterstraße 1", "48143", "Münster (Westf.)")),
        ("48143 Münster", False, ("", "48143", "Münster")),
        ("Musterstraße 1", False, ("Musterstraße 1", "", "")),
        ("Rathaus, Festsaal", True, ("", "", "")),
        ("Raum 12345 Nord", True, ("", "", "")),
        (None, False, ("", "", "")),
        (["kein Text"], False, ("", "", "")),
    ],
)
def test_anschrift_aus_text(text: Any, nur_mit_plz: bool, erwartet: tuple[str, str, str]) -> None:
    assert anschrift_aus_text(text, nur_mit_plz=nur_mit_plz) == erwartet
