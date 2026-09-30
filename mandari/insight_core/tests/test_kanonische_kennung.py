# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kanonische RIS-Kennungen in Django (ADR docs/adr/20260929-kanonisches-modell.md).

Die Testvektoren liegen im gemeinsamen Paket (``mandari_oparl/ids_testvektoren.json``). Dieselben
Vektoren prüft der Ingestor in ``ingestor/tests/test_kanonische_kennung.py``: Ingestor und Django
müssen für gleiche URIs dieselbe Kennung vergeben. Bestehende Kennungen dürfen sich nie ändern.
"""

from __future__ import annotations

import json
import uuid
from importlib import resources
from typing import Any, cast

import pytest
from mandari_oparl.ids import NS_MANDARI_RIS, canonical_id

from insight_core.models import (
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from insight_sync.session_mirror import SessionMirror

URL_NAMENSRAUM = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
BASIS = "https://ris.beispiel.example/oparl/v1/"


def _testvektoren() -> dict[str, Any]:
    datei = resources.files("mandari_oparl").joinpath("ids_testvektoren.json")
    return cast(dict[str, Any], json.loads(datei.read_text(encoding="utf-8")))


VEKTOREN = _testvektoren()["vektoren"]
IDS = [v["uri"][-40:] for v in VEKTOREN]


@pytest.fixture
def body(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Beispiel-RIS", url=f"{BASIS}system")
    return OParlBody.objects.create(external_id=f"{BASIS}body/1", source=source, name="Beispielstadt", slug="bsp")


def test_namensraum_ist_der_url_namensraum() -> None:
    """Der Namensraum darf sich nie ändern, sonst ändern sich alle Kennungen des Bestands."""
    assert NS_MANDARI_RIS == URL_NAMENSRAUM
    assert _testvektoren()["namensraum"] == str(URL_NAMENSRAUM)


@pytest.mark.parametrize("vektor", VEKTOREN, ids=IDS)
def test_kennungsfunktion_liefert_die_testvektoren(vektor: dict[str, str]) -> None:
    assert canonical_id(vektor["uri"]) == uuid.UUID(vektor["kennung"])


@pytest.mark.parametrize("vektor", VEKTOREN, ids=IDS)
def test_neue_objekte_tragen_die_kennung_der_testvektoren(body: OParlBody, vektor: dict[str, str]) -> None:
    """Django vergibt für dieselbe URI dieselbe Kennung wie der Ingestor."""
    vorlage = OParlPaper.objects.create(external_id=vektor["uri"], body=body, name="Vorlage")
    assert vorlage.id == uuid.UUID(vektor["kennung"])
    assert OParlPaper.objects.get(external_id=vektor["uri"]).id == uuid.UUID(vektor["kennung"])


def test_kennung_steht_schon_vor_dem_speichern_fest(body: OParlBody) -> None:
    """Objektgeflechte, die vor dem Speichern verknüpft werden, zeigen auf die endgültige Kennung."""
    sitzung = OParlMeeting(external_id=f"{BASIS}meeting/1", body=body, name="Rat")
    assert sitzung.id == canonical_id(f"{BASIS}meeting/1")
    OParlMeeting.objects.bulk_create([sitzung, OParlMeeting(external_id=f"{BASIS}meeting/2", body=body)])
    assert set(OParlMeeting.objects.values_list("id", flat=True)) == {
        canonical_id(f"{BASIS}meeting/1"),
        canonical_id(f"{BASIS}meeting/2"),
    }


def test_nachtraeglich_gesetzte_uri_zaehlt_beim_speichern(body: OParlBody) -> None:
    """Formulare und Verwaltung setzen Felder erst nach dem Erzeugen."""
    person = OParlPerson()
    person.external_id = f"{BASIS}person/5"
    person.body = body
    person.name = "Erika Mustermann"
    person.save()
    assert person.id == canonical_id(f"{BASIS}person/5")


def test_update_or_create_legt_mit_kanonischer_kennung_an(body: OParlBody) -> None:
    gremium, angelegt = OParlOrganization.objects.update_or_create(
        external_id=f"{BASIS}organization/3", defaults={"body": body, "name": "Rat"}
    )
    assert angelegt
    assert gremium.id == canonical_id(f"{BASIS}organization/3")


def test_ausdruecklich_gesetzte_kennung_bleibt(body: OParlBody) -> None:
    eigene = uuid.uuid4()
    assert OParlPaper.objects.create(id=eigene, external_id=f"{BASIS}paper/7", body=body).id == eigene
    andere = uuid.uuid4()
    vorlage = OParlPaper(external_id=f"{BASIS}paper/8", body=body)
    vorlage.id = andere
    vorlage.save()
    assert OParlPaper.objects.get(external_id=f"{BASIS}paper/8").id == andere


def test_bestand_behaelt_seine_kennung(body: OParlBody) -> None:
    """Laden, Ändern und erneutes Abgleichen verändern eine bestehende (zufällige) Kennung nie."""
    alt = uuid.uuid4()
    OParlPaper.objects.create(id=alt, external_id=f"{BASIS}paper/9", body=body, name="Alt")

    geladen = OParlPaper.objects.get(external_id=f"{BASIS}paper/9")
    geladen.name = "Geändert"
    geladen.save()
    vorlage, angelegt = OParlPaper.objects.update_or_create(
        external_id=f"{BASIS}paper/9", defaults={"body": body, "name": "Abgeglichen"}
    )

    assert not angelegt
    assert vorlage.id == alt
    assert list(OParlPaper.objects.values_list("id", "name")) == [(alt, "Abgeglichen")]


def test_ohne_uri_bleibt_die_kennung_zufaellig(body: OParlBody) -> None:
    """Eine leere URI ergibt keine kanonische Kennung (sonst teilten sich alle solche Objekte eine)."""
    person = OParlPerson.objects.create(external_id="", body=body, name="Ohne URI")
    assert person.id != canonical_id("")
    assert person.id.version == 4


def test_session_spiegel_vergibt_kanonische_kennungen() -> None:
    """Der lokale Spiegel (sync_session_insight) schreibt wie der Ingestor."""
    basis = "https://mandari.example/session/nord/api/oparl/"
    source = OParlSource.objects.create(name="Nord (Session)", url=basis)
    mirror = cast(Any, SessionMirror)(source, fetch=lambda url: {})
    body = mirror._upsert_body({"id": f"{basis}body/", "name": "Nord"})
    mirror._upsert_meeting(body, {"id": f"{basis}meeting/1/", "name": "Rat"})

    assert body.id == canonical_id(f"{basis}body/")
    assert OParlMeeting.objects.get(external_id=f"{basis}meeting/1/").id == canonical_id(f"{basis}meeting/1/")


pytestmark = pytest.mark.django_db
