# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-1.1-Konformität des Aggregators (``/oparl/v1/``).

Geprüft werden die Punkte, in denen die Ausgabe von der Spezifikation abwich: ``organizationType`` mit
den Werten der Spezifikation, ``File.date`` als Datum, ``license`` am System-Objekt, ``locationList`` am
Body, der Sitzungsort als Location-Objekt, ``legislativeTerm`` als Pflichtfeld und bedingte Anfragen
(ETag/304). Dazu läuft jede Antwort durch die Typprüfung in ``konformitaet.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from hub.api.tests.konformitaet import ORGANIZATION_TYPES, pruefe, pruefe_liste
from hub.ris.mapping.bestand import organization_type
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlLocation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
API = f"{SITE}/oparl/v1"
RIS = "https://ris.example/oparl"


@pytest.fixture(autouse=True)
def _einstellungen() -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_API_RATE_LIMIT=0):
        yield
    cache.clear()


@pytest.fixture
def welt() -> dict[str, Any]:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt", slug="musterstadt")
    gremien = {
        art: OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/{nummer}", body=body, name=f"Gremium {nummer}", organization_type=art
        )
        for nummer, art in enumerate(["committee", "Fraktion", "fraktion", "Hauptorgan", "Arbeitskreis", None], 1)
    }
    ort = OParlLocation.objects.create(
        external_id=f"{RIS}/location/1", body=body, description="Rathaus", room="Ratssaal", street_address="Markt 1"
    )
    mit_ort = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1",
        body=body,
        name="Rat",
        start=datetime(2026, 10, 1, 17, 0, tzinfo=UTC),
        raw_json={"location": {"id": ort.external_id}},
    )
    OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/1", meeting=mit_ort, name="Eröffnung", order=1)
    nur_text = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/2",
        body=body,
        name="Bauausschuss",
        start=datetime(2026, 10, 2, 17, 0, tzinfo=UTC),
        location_name="Technisches Rathaus",
        location_address="Bauhof 3, 12345 Musterstadt",
    )
    ohne_ort = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/3", body=body, name="Beirat", start=datetime(2026, 10, 3, 17, 0, tzinfo=UTC)
    )
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Radweg", reference="V/1")
    datei = OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=body,
        paper=vorlage,
        name="Begründung",
        access_url=f"{RIS}/file/1/download",
        # Reines Datum der Quelle: Der Ingestor speichert Mitternacht UTC
        file_date=datetime(2026, 9, 30, 0, 0, tzinfo=UTC),
    )
    return {
        "body": body,
        "gremien": gremien,
        "ort": ort,
        "mit_ort": mit_ort,
        "nur_text": nur_text,
        "ohne_ort": ohne_ort,
        "vorlage": vorlage,
        "datei": datei,
    }


def _json(pfad: str, **kopf: str) -> dict[str, Any]:
    antwort = Client().get(pfad, headers=kopf)
    assert antwort.status_code == 200, (pfad, antwort.status_code)
    return cast(dict[str, Any], antwort.json())


def _liste(welt: dict[str, Any], segment: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _json(f"/oparl/v1/body/{welt['body'].id}/{segment}")["data"])


# =============================================================================
# organizationType
# =============================================================================


def test_organization_type_nur_werte_der_spezifikation(welt: dict[str, Any]) -> None:
    arten = {gremium["mandari:originalId"]: gremium for gremium in _liste(welt, "organizations")}
    je_quelle = {art: arten[gremium.external_id] for art, gremium in welt["gremien"].items()}

    assert je_quelle["committee"]["organizationType"] == "Gremium"  # Schlüssel eines Session-Mandanten
    assert je_quelle["Fraktion"]["organizationType"] == "Fraktion"
    assert je_quelle["fraktion"]["organizationType"] == "Fraktion"
    assert je_quelle["Hauptorgan"]["organizationType"] == "Gremium"
    assert je_quelle["Arbeitskreis"]["organizationType"] == "Sonstiges"
    assert "organizationType" not in je_quelle[None]
    assert all(g["organizationType"] in ORGANIZATION_TYPES for g in arten.values() if "organizationType" in g)


def test_abweichende_angabe_der_quelle_bleibt_als_eigenes_feld(welt: dict[str, Any]) -> None:
    arten = {gremium["mandari:originalId"]: gremium for gremium in _liste(welt, "organizations")}
    je_quelle = {art: arten[gremium.external_id] for art, gremium in welt["gremien"].items()}

    assert je_quelle["Arbeitskreis"]["mandari:originalOrganizationType"] == "Arbeitskreis"
    assert je_quelle["committee"]["mandari:originalOrganizationType"] == "committee"
    assert "mandari:originalOrganizationType" not in je_quelle["Fraktion"]


@pytest.mark.parametrize(
    ("angabe", "wert"),
    [
        ("Gremium", "Gremium"),
        ("EXTERNES GREMIUM", "externes Gremium"),
        (" Partei ", "Partei"),
        ("council", "Gremium"),
        ("department", "Verwaltungsbereich"),
        ("other", "Sonstiges"),
        ("Ausschuss", "Gremium"),
        ("Ausschüsse", "Gremium"),
        ("Fraktionen", "Fraktion"),
        # Einordnung nach dem Kommunalrecht, wie sie manche RIS liefern
        ("Hauptorgan", "Gremium"),
        ("Hilfsorgan", "Gremium"),
        ("Amt", "Verwaltungsbereich"),
        ("Organisationseinheit", "Verwaltungsbereich"),
        ("Dienststelle", "Verwaltungsbereich"),
        ("Dienststellen", "Verwaltungsbereich"),
        ("irgendwas", "Sonstiges"),
        ("", None),
        (None, None),
    ],
)
def test_zuordnung_organization_type(angabe: str | None, wert: str | None) -> None:
    assert organization_type(angabe) == wert


def test_verbreitete_angaben_der_quellen_fallen_nicht_auf_sonstiges() -> None:
    """Die im Bestand häufigsten Angaben behalten ihre Bedeutung – sonst verlöre ein Filter auf „Gremium“ die Ausschüsse."""
    verbreitet = ["Amt", "Hilfsorgan", "Organisationseinheit", "Fraktion", "Gremium", "Dienststellen", "Hauptorgan"]
    assert {angabe: organization_type(angabe) for angabe in verbreitet} == {
        "Amt": "Verwaltungsbereich",
        "Hilfsorgan": "Gremium",
        "Organisationseinheit": "Verwaltungsbereich",
        "Fraktion": "Fraktion",
        "Gremium": "Gremium",
        "Dienststellen": "Verwaltungsbereich",
        "Hauptorgan": "Gremium",
    }


# =============================================================================
# File.date, license, Body
# =============================================================================


def test_file_date_ist_ein_datum(welt: dict[str, Any]) -> None:
    datei = _json(f"/oparl/v1/file/{welt['datei'].id}")
    assert datei["date"] == "2026-09-30"
    eingebettet = _json(f"/oparl/v1/paper/{welt['vorlage'].id}")["auxiliaryFile"][0]
    assert eingebettet["date"] == "2026-09-30"


@pytest.mark.parametrize(
    ("quelle", "gespeichert", "tag"),
    [
        # Reines Datum der Quelle: unverändert (der Ingestor speichert Mitternacht UTC)
        ("2026-03-05", datetime(2026, 3, 5, 0, 0, tzinfo=UTC), "2026-03-05"),
        # Zeitpunkt mit lokalem Versatz: derselbe Tag, nicht der Vortag
        ("2026-03-05T00:00:00+01:00", datetime(2026, 3, 4, 23, 0, tzinfo=UTC), "2026-03-05"),
        # Ältere Spiegelung eines Session-Mandanten: Zeitpunkt der Ablage kurz nach Mitternacht Ortszeit
        ("2026-03-04T23:30:00+00:00", datetime(2026, 3, 4, 23, 30, tzinfo=UTC), "2026-03-05"),
        # Ohne Angabe in den Rohdaten bzw. mit unbrauchbarer Angabe: Tag des gespeicherten Zeitpunkts
        (None, datetime(2026, 3, 5, 0, 0, tzinfo=UTC), "2026-03-05"),
        ("2026-13-45", datetime(2026, 3, 5, 0, 0, tzinfo=UTC), "2026-03-05"),
    ],
)
def test_file_date_nennt_den_tag_der_quelle(
    welt: dict[str, Any], quelle: str | None, gespeichert: datetime, tag: str
) -> None:
    datei = welt["datei"]
    datei.raw_json = {"date": quelle} if quelle else {}
    datei.file_date = gespeichert
    datei.save()

    with override_settings(TIME_ZONE="Europe/Berlin"):
        assert _json(f"/oparl/v1/file/{datei.id}")["date"] == tag


def test_system_nennt_lizenz_nur_wenn_festgelegt() -> None:
    assert "license" not in _json("/oparl/v1/system")
    with override_settings(OPARL_LICENSE_URL="https://www.govdata.de/dl-de/zero-2-0"):
        system = _json("/oparl/v1/system")
    assert system["license"] == "https://www.govdata.de/dl-de/zero-2-0"
    assert pruefe(system, "System") == []


def test_body_mit_location_list_und_pflichtfeld_legislative_term(welt: dict[str, Any]) -> None:
    body = _json(f"/oparl/v1/body/{welt['body'].id}")
    orte = f"{API}/body/{welt['body'].id}/locations"
    assert body["locationList"] == orte
    assert body["mandari:locationList"] == orte  # abgekündigt, bleibt vorerst
    assert body["legislativeTerm"] == []  # Pflichtfeld, auch ohne Wahlperiode
    assert pruefe(body, "Body") == []


# =============================================================================
# Meeting.location
# =============================================================================


def test_sitzungsort_der_quelle_wird_eingebettet(welt: dict[str, Any]) -> None:
    sitzung = _json(f"/oparl/v1/meeting/{welt['mit_ort'].id}")
    assert sitzung["location"]["id"] == f"{API}/location/{welt['ort'].id}"
    assert sitzung["location"]["room"] == "Ratssaal"
    assert "mandari:locationName" not in sitzung


def test_sitzungsort_nur_als_text_wird_zum_location_objekt(welt: dict[str, Any]) -> None:
    sitzung = _json(f"/oparl/v1/meeting/{welt['nur_text'].id}")
    ort = sitzung["location"]

    assert ort["id"] == f"{API}/location/{welt['nur_text'].id}"
    assert ort["type"] == "https://schema.oparl.org/1.1/Location"
    assert ort["description"] == "Technisches Rathaus, Bauhof 3, 12345 Musterstadt"
    assert ort["meetings"] == [sitzung["id"]]
    assert pruefe(ort, "Location") == []
    # Die bisherigen Felder bleiben zusätzlich erhalten
    assert sitzung["mandari:locationName"] == "Technisches Rathaus"
    assert sitzung["mandari:locationAddress"] == "Bauhof 3, 12345 Musterstadt"
    # Die ID ist abrufbar und liefert dasselbe Objekt
    assert _json(f"/oparl/v1/location/{welt['nur_text'].id}") == ort


def test_sitzung_ohne_ort_hat_kein_location_objekt(welt: dict[str, Any]) -> None:
    assert "location" not in _json(f"/oparl/v1/meeting/{welt['ohne_ort'].id}")
    gone = _json(f"/oparl/v1/location/{welt['ohne_ort'].id}")
    assert gone["deleted"] is True
    assert pruefe(gone, "Location") == []


def test_ort_einer_geloeschten_sitzung_ist_geloescht(welt: dict[str, Any]) -> None:
    welt["nur_text"].mark_deleted()
    gone = _json(f"/oparl/v1/location/{welt['nur_text'].id}")
    assert gone == {
        "id": f"{API}/location/{welt['nur_text'].id}",
        "type": "https://schema.oparl.org/1.1/Location",
        "created": gone["created"],
        "modified": gone["modified"],
        "deleted": True,
    }


def test_zurueckgenommener_ort_wird_nicht_mehr_ausgegeben(welt: dict[str, Any]) -> None:
    """Ein Ort, den die Quelle zurücknimmt, verschwindet aus Sitzung und Liste; seine Adresse bleibt gekürzt abrufbar."""
    welt["ort"].mark_deleted()

    assert "location" not in _json(f"/oparl/v1/meeting/{welt['mit_ort'].id}")
    assert _liste(welt, "locations") == []
    antwort = Client().get(f"/oparl/v1/location/{welt['ort'].id}")
    assert antwort.status_code == 200
    assert set(antwort.json()) == {"id", "type", "created", "modified", "deleted"}
    assert b"Rathaus" not in antwort.content and b"Markt" not in antwort.content


def test_unbekannter_ort_ergibt_404(welt: dict[str, Any]) -> None:
    antwort = Client().get("/oparl/v1/location/00000000-0000-4000-8000-000000000000")
    assert antwort.status_code == 404


# =============================================================================
# Bedingte Anfragen (ETag / 304)
# =============================================================================


@pytest.mark.parametrize("pfad", ["system", "bodies", "body/{body}", "body/{body}/meetings", "meeting/{sitzung}"])
def test_etag_und_304(welt: dict[str, Any], pfad: str) -> None:
    url = "/oparl/v1/" + pfad.format(body=welt["body"].id, sitzung=welt["mit_ort"].id)
    client = Client()
    erste = client.get(url)
    etag = erste["ETag"]

    assert erste.status_code == 200
    assert etag.startswith('"') and etag.endswith('"')
    assert erste["Cache-Control"] == "no-cache"

    zweite = client.get(url, headers={"if-none-match": etag})
    assert zweite.status_code == 304
    assert zweite.content == b""
    assert zweite["ETag"] == etag
    assert zweite["Access-Control-Allow-Origin"] == "*"

    andere = client.get(url, headers={"if-none-match": '"veraltet"'})
    assert andere.status_code == 200
    assert andere.content == erste.content


def test_etag_aendert_sich_mit_dem_inhalt(welt: dict[str, Any]) -> None:
    url = f"/oparl/v1/meeting/{welt['mit_ort'].id}"
    client = Client()
    etag = client.get(url)["ETag"]

    OParlMeeting.objects.filter(pk=welt["mit_ort"].pk).update(name="Rat (verlegt)")

    antwort = client.get(url, headers={"if-none-match": etag})
    assert antwort.status_code == 200
    assert antwort["ETag"] != etag
    assert antwort.json()["name"] == "Rat (verlegt)"


def test_fehlerantworten_ohne_etag(welt: dict[str, Any]) -> None:
    antwort = Client().get("/oparl/v1/paper/00000000-0000-4000-8000-000000000000")
    assert antwort.status_code == 404
    assert "ETag" not in antwort


# =============================================================================
# Gesamte Ausgabe gegen die Feldtypen der Spezifikation
# =============================================================================


def test_alle_antworten_entsprechen_den_feldtypen(welt: dict[str, Any]) -> None:
    probleme = pruefe(_json("/oparl/v1/system"), "System")
    probleme += pruefe_liste(_json("/oparl/v1/bodies"), "Body")
    for segment, typ in (
        ("organizations", "Organization"),
        ("people", "Person"),
        ("meetings", "Meeting"),
        ("papers", "Paper"),
        ("locations", "Location"),
    ):
        probleme += pruefe_liste(_json(f"/oparl/v1/body/{welt['body'].id}/{segment}"), typ)
    probleme += pruefe(_json(f"/oparl/v1/file/{welt['datei'].id}"), "File")

    assert probleme == []
