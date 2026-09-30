# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildung des RIS-Bestands auf das kanonische RIS-Modell (``hub/ris/mapping/bestand.py``).

- Die Abbildung selbst: Adressen, Verweise nur auf die eigene Schnittstelle, gekürzte Objekte.
- Der Aggregator gibt genau diese Abbildung aus – er übersetzt nicht selbst.
- Verweise über Kennungen der Quelle kosten je Seite eine Abfrage je Typ, nicht je Objekt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from hub.api import aggregator
from hub.api.tests.konformitaet import pruefe
from hub.ris.mapping import bestand
from hub.ris.mapping.bestand import BestandMapping, BestandUris, RefContext
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
API = f"{SITE}/oparl/v1"
RIS = "https://ris.example/oparl"
ZEIT = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _abbildung() -> BestandMapping:
    return BestandMapping(f"{SITE}/oparl", SITE)


@pytest.fixture
def welt() -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_API_RATE_LIMIT=0, OPARL_LICENSE_URL=""):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        body = OParlBody.objects.create(
            external_id=f"{RIS}/body/1", source=source, name="Stadt Musterstadt", slug="musterstadt"
        )
        periode = OParlLegislativeTerm.objects.create(
            external_id=f"{RIS}/term/1", body=body, name="2025–2030", start_date=date(2025, 11, 1)
        )
        rat = OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/1", body=body, name="Rat", organization_type="Gremium"
        )
        person = OParlPerson.objects.create(
            external_id=f"{RIS}/person/1", body=body, family_name="Muster", given_name="Petra"
        )
        mitgliedschaft = OParlMembership.objects.create(
            external_id=f"{RIS}/membership/1", person=person, organization=rat, role="Mitglied"
        )
        ort = OParlLocation.objects.create(external_id=f"{RIS}/location/1", body=body, description="Rathaus")
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1",
            body=body,
            name="Ratssitzung",
            start=ZEIT,
            raw_json={"location": {"id": ort.external_id}, "invitation": f"{RIS}/file/2"},
        )
        sitzung.organizations.add(rat)
        top = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/agendaitem/1", meeting=sitzung, number="1", order=1, name="Radweg"
        )
        vorlage = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/1",
            body=body,
            name="Radweg",
            reference="V/2026/1",
            raw_json={"mainFile": {"id": f"{RIS}/file/1"}},
        )
        datei = OParlFile.objects.create(
            external_id=f"{RIS}/file/1",
            body=body,
            paper=vorlage,
            name="Begründung",
            access_url=f"{RIS}/file/1/download",
            text_content="Erkannter Text",
        )
        einladung = OParlFile.objects.create(external_id=f"{RIS}/file/2", body=body, meeting=sitzung, name="Einladung")
        beratung = OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/1",
            body=body,
            paper=vorlage,
            paper_external_id=vorlage.external_id,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
            role="Entscheidung",
            authoritative=True,
        )
        yield {
            "body": body,
            "legislativeterm": periode,
            "organization": rat,
            "person": person,
            "membership": mitgliedschaft,
            "location": ort,
            "meeting": sitzung,
            "agendaitem": top,
            "paper": vorlage,
            "file": datei,
            "einladung": einladung,
            "consultation": beratung,
        }
    cache.clear()


def _json(pfad: str) -> dict[str, Any]:
    antwort = Client().get(pfad)
    assert antwort.status_code == 200, (pfad, antwort.status_code)
    return cast(dict[str, Any], antwort.json())


# =============================================================================
# Adressen
# =============================================================================


def test_uris_sind_die_adressen_der_schnittstelle() -> None:
    uris = BestandUris(f"{SITE}/oparl/", f"{SITE}/")

    assert uris.system() == f"{API}/system"
    assert uris.bodies() == f"{API}/bodies"
    assert uris.obj("meeting", 7) == f"{API}/meeting/7"
    assert uris.list(7, "meetings") == f"{API}/body/7/meetings"
    assert uris.web("termine/7/") == f"{SITE}/insight/termine/7/"


def test_system_nennt_die_lizenz_nur_wenn_der_betreiber_sie_festlegt() -> None:
    assert "license" not in _abbildung().system()
    mit = BestandMapping(f"{SITE}/oparl", SITE, license_url="https://www.govdata.de/dl-de/zero-2-0").system()
    assert mit["license"] == "https://www.govdata.de/dl-de/zero-2-0"
    assert mit["id"] == f"{API}/system" and mit["body"] == f"{API}/bodies"
    assert pruefe(mit, "System") == []


# =============================================================================
# Der Aggregator gibt genau diese Abbildung aus
# =============================================================================

#: Objekttyp -> (Segment der Liste je Kommune oder None, Abbildung, Auflösung der Verweise)
ARTEN: dict[str, tuple[str | None, Any, Any]] = {
    "organization": ("organizations", BestandMapping.organization, RefContext.empty),
    "person": ("people", BestandMapping.person, RefContext.empty),
    "membership": (None, BestandMapping.membership, RefContext.empty),
    "legislativeterm": (None, BestandMapping.legislative_term, RefContext.empty),
    "meeting": ("meetings", BestandMapping.meeting, RefContext.for_meetings),
    "agendaitem": (None, BestandMapping.agenda_item, RefContext.for_agenda_items),
    "paper": ("papers", BestandMapping.paper, RefContext.for_papers),
    "consultation": (None, BestandMapping.consultation, RefContext.for_consultations),
    "location": ("locations", BestandMapping.location, RefContext.empty),
}


@pytest.mark.parametrize("art", sorted(ARTEN))
def test_aggregator_gibt_die_abbildung_aus(welt: dict[str, Any], art: str) -> None:
    segment, abbilden, verweise = ARTEN[art]
    objekt = type(welt[art]).objects.get(pk=welt[art].pk)
    erwartet = abbilden(_abbildung(), objekt, verweise([objekt]))

    assert erwartet["id"] == f"{API}/{art}/{objekt.pk}"
    assert _json(f"/oparl/v1/{art}/{objekt.pk}") == erwartet
    if segment:
        assert _json(f"/oparl/v1/body/{welt['body'].pk}/{segment}")["data"] == [erwartet]


def test_datei_mit_text_nur_am_objekt_endpunkt(welt: dict[str, Any]) -> None:
    abbildung = _abbildung()

    assert _json(f"/oparl/v1/file/{welt['file'].pk}") == abbildung.file_with_text(welt["file"])
    assert abbildung.file_with_text(welt["file"])["text"] == "Erkannter Text"
    eingebettet = _json(f"/oparl/v1/paper/{welt['paper'].pk}")["mainFile"]
    assert eingebettet == abbildung.file(welt["file"]) and "text" not in eingebettet


def test_system_und_body_aus_der_abbildung(welt: dict[str, Any]) -> None:
    abbildung = _abbildung()
    body = abbildung.body(welt["body"])

    assert _json("/oparl/v1/system") == abbildung.system()
    assert _json(f"/oparl/v1/body/{welt['body'].pk}") == body
    assert _json("/oparl/v1/bodies")["data"] == [body]
    assert body["legislativeTerm"] == [abbildung.legislative_term(welt["legislativeterm"])]


def test_sitzung_bettet_ort_einladung_und_tagesordnung_ein(welt: dict[str, Any]) -> None:
    abbildung = _abbildung()
    sitzung = _json(f"/oparl/v1/meeting/{welt['meeting'].pk}")

    assert sitzung["location"] == abbildung.location(welt["location"])
    assert sitzung["invitation"] == abbildung.file(welt["einladung"])
    assert "auxiliaryFile" not in sitzung  # die Einladung steht nicht noch einmal unter den Anlagen
    assert [top["id"] for top in sitzung["agendaItem"]] == [f"{API}/agendaitem/{welt['agendaitem'].pk}"]
    assert sitzung["agendaItem"][0]["consultation"] == f"{API}/consultation/{welt['consultation'].pk}"
    assert sitzung["organization"] == [f"{API}/organization/{welt['organization'].pk}"]


def _ohne_erweiterungen(wert: Any) -> Any:
    if isinstance(wert, dict):
        return {name: _ohne_erweiterungen(inhalt) for name, inhalt in wert.items() if not name.startswith("mandari:")}
    if isinstance(wert, list):
        return [_ohne_erweiterungen(inhalt) for inhalt in wert]
    return wert


@pytest.mark.parametrize("art", [*sorted(ARTEN), "file", "body"])
def test_verweise_zeigen_nur_auf_die_eigene_schnittstelle(welt: dict[str, Any], art: str) -> None:
    objekt = _json(f"/oparl/v1/{art}/{welt[art].pk}")

    assert objekt["mandari:originalId"] == welt[art].external_id
    # Außerhalb der gekennzeichneten Erweiterungen steht keine Adresse der Quelle
    assert RIS not in str(_ohne_erweiterungen(objekt))


def test_geloeschtes_objekt_traegt_nur_kennung_und_zeiten(welt: dict[str, Any]) -> None:
    geloescht = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    vorlage = welt["paper"]
    OParlPaper.objects.filter(pk=vorlage.pk).update(oparl_created=ZEIT)
    vorlage.refresh_from_db()
    vorlage.mark_deleted(when=geloescht)

    gekuerzt = _abbildung().tombstone("paper", vorlage)

    assert gekuerzt == {
        "id": f"{API}/paper/{vorlage.pk}",
        "type": "https://schema.oparl.org/1.1/Paper",
        "created": "2026-09-01T08:00:00+00:00",
        "modified": "2026-09-02T09:00:00+00:00",
        "deleted": True,
    }
    assert _json(f"/oparl/v1/paper/{vorlage.pk}") == gekuerzt
    # Eingebettet wird Gelöschtes nie: Die Beratung der gelöschten Vorlage bleibt, die Vorlage selbst nicht
    assert _json(f"/oparl/v1/body/{welt['body'].pk}/papers")["data"] == []
    assert _json(f"/oparl/v1/body/{welt['body'].pk}/papers?modified_since=2026-01-01T00:00:00Z")["data"] == [gekuerzt]


def test_ort_einer_sitzung_ohne_location_objekt_traegt_die_kennung_der_sitzung(welt: dict[str, Any]) -> None:
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/2", body=welt["body"], name="Ausschuss", location_name="Bürgerhaus"
    )

    ort = _abbildung().meeting_location(sitzung)

    assert ort is not None and ort["id"] == f"{API}/location/{sitzung.pk}"
    assert ort["meetings"] == [f"{API}/meeting/{sitzung.pk}"]
    assert _json(f"/oparl/v1/location/{sitzung.pk}") == ort
    assert _abbildung().meeting_location(welt["meeting"]) is None  # ohne Textangabe kein Ort aus Text


# =============================================================================
# Verweise je Seite, nicht je Objekt
# =============================================================================


def test_listen_kosten_keine_abfrage_je_objekt(welt: dict[str, Any]) -> None:
    pfade = [f"/oparl/v1/body/{welt['body'].pk}/{segment}" for segment in ("meetings", "papers", "people")]

    def abfragen() -> dict[str, int]:
        anzahl = {}
        for pfad in pfade:
            cache.clear()
            with CaptureQueriesContext(connection) as erfasst:
                _json(pfad)
            anzahl[pfad] = len(erfasst)
        return anzahl

    vorher = abfragen()
    for nummer in range(2, 7):
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/{nummer}",
            body=welt["body"],
            name=f"Sitzung {nummer}",
            raw_json={"location": welt["location"].external_id},
        )
        top = OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/{nummer}", meeting=sitzung, name="TOP")
        vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/{nummer}", body=welt["body"], name="Vorlage")
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/{nummer}",
            body=welt["body"],
            paper=vorlage,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
        )
        OParlFile.objects.create(
            external_id=f"{RIS}/file/{nummer + 10}", body=welt["body"], paper=vorlage, name="Anlage"
        )
        person = OParlPerson.objects.create(external_id=f"{RIS}/person/{nummer}", body=welt["body"], family_name="X")
        OParlMembership.objects.create(
            external_id=f"{RIS}/membership/{nummer}", person=person, organization=welt["organization"]
        )

    assert len(_json(pfade[0])["data"]) == 6
    assert abfragen() == vorher


# =============================================================================
# Eine Stelle für die Übersetzung
# =============================================================================


def test_aggregator_uebersetzt_nicht_selbst() -> None:
    """Typ-URLs und Feldnamen des Modells stehen nur in der Abbildung, nicht in den Endpunkten."""
    quelltext = Path(aggregator.__file__).read_text(encoding="utf-8")
    assert "schema_type" not in quelltext
    assert "schema.oparl.org/1.1/" not in quelltext.replace('"oparlVersion": "https://schema.oparl.org/1.1/"', "")
    assert '"type":' not in quelltext
    assert "mandari:" not in quelltext


def test_abbildung_nennt_ihre_version() -> None:
    assert bestand.VERSION >= 1
