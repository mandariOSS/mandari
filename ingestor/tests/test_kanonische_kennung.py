"""
Kanonische RIS-Kennungen im Ingestor (ADR docs/adr/20260929-kanonisches-modell.md).

Die Testvektoren liegen im gemeinsamen Paket (``mandari_oparl/ids_testvektoren.json``). Dieselben
Vektoren prüft Django in ``mandari/insight_core/tests/test_kanonische_kennung.py``: Ingestor und Django
müssen für gleiche URIs dieselbe Kennung vergeben.
"""

import json
from importlib import resources
from typing import Any
from uuid import UUID

import pytest
from mandari_oparl import NS_MANDARI_RIS, canonical_id, generate_uuid

from src.sync.processor import OParlProcessor, ProcessedAgendaItem, ProcessedFile, ProcessedLocation

URL_NAMENSRAUM = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
TYP = "https://schema.oparl.org/1.1/"


def _testvektoren() -> dict[str, Any]:
    datei = resources.files("mandari_oparl").joinpath("ids_testvektoren.json")
    return json.loads(datei.read_text(encoding="utf-8"))


VEKTOREN = _testvektoren()["vektoren"]


def test_namensraum_ist_der_url_namensraum() -> None:
    """Der Namensraum darf sich nie ändern, sonst ändern sich alle Kennungen des Bestands."""
    assert NS_MANDARI_RIS == URL_NAMENSRAUM
    assert _testvektoren()["namensraum"] == str(URL_NAMENSRAUM)


def test_testvektoren_decken_die_faelle_ab() -> None:
    """Varianten derselben Adresse bleiben verschiedene Kennungen (keine Normalisierung)."""
    kennungen = [v["kennung"] for v in VEKTOREN]
    assert len(VEKTOREN) >= 10
    assert len(set(kennungen)) == len(kennungen)


@pytest.mark.parametrize("vektor", VEKTOREN, ids=[v["uri"][-40:] for v in VEKTOREN])
def test_kennungsfunktion_liefert_die_testvektoren(vektor: dict[str, str]) -> None:
    erwartet = UUID(vektor["kennung"])
    assert canonical_id(vektor["uri"]) == erwartet
    # Der bisherige Name bleibt als Alias erhalten
    assert generate_uuid(vektor["uri"]) == erwartet
    assert OParlProcessor().generate_uuid(vektor["uri"]) == erwartet


@pytest.mark.parametrize("vektor", VEKTOREN, ids=[v["uri"][-40:] for v in VEKTOREN])
def test_verarbeitete_objekte_tragen_die_kanonische_kennung(vektor: dict[str, str]) -> None:
    """Die Kennung, die der Ingestor speichert, ist die kanonische Kennung der Quell-id."""
    vorlage = OParlProcessor().process({"id": vektor["uri"], "type": f"{TYP}Paper", "name": "Vorlage"})
    assert vorlage is not None
    assert vorlage.external_id == vektor["uri"]
    assert vorlage.id == UUID(vektor["kennung"])


def test_eingebettete_objekte_tragen_die_kanonische_kennung() -> None:
    """Auch eingebettete Tagesordnungspunkte, Dateien und Orte bekommen ihre Kennung aus der eigenen id."""
    basis = "https://ris.beispiel.example/oparl/v1"
    sitzung = OParlProcessor().process(
        {
            "id": f"{basis}/meeting/1",
            "type": f"{TYP}Meeting",
            "name": "Ratssitzung",
            "location": {"id": f"{basis}/location/7", "type": f"{TYP}Location", "description": "Rathaus"},
            "agendaItem": [{"id": f"{basis}/agendaitem/3", "type": f"{TYP}AgendaItem", "name": "TOP 1"}],
            "invitation": {"id": f"{basis}/file/9", "type": f"{TYP}File", "name": "Einladung"},
        },
        f"{basis}/body/1",
    )
    assert sitzung is not None
    assert sitzung.id == canonical_id(f"{basis}/meeting/1")
    eingebettet = {type(e): e for e in sitzung.nested_entities}
    assert eingebettet[ProcessedAgendaItem].id == canonical_id(f"{basis}/agendaitem/3")
    assert eingebettet[ProcessedFile].id == canonical_id(f"{basis}/file/9")
    assert eingebettet[ProcessedLocation].id == canonical_id(f"{basis}/location/7")
