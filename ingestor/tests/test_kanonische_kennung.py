"""
Kanonische RIS-Kennungen im Ingestor (ADR docs/adr/20260929-kanonisches-modell.md).

Die Testvektoren liegen im gemeinsamen Paket (``mandari_oparl/ids_testvektoren.json``). Dieselben
Vektoren prüft Django in ``mandari/insight_core/tests/test_kanonische_kennung.py``: Ingestor und Django
müssen für gleiche URIs dieselbe Kennung vergeben.
"""

import json
from importlib import resources
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from mandari_oparl import (
    NS_MANDARI_RIS,
    SOURCE_ID_BASE_KEY,
    IdBases,
    canonical_id,
    canonical_uri,
    generate_uuid,
    source_id_base,
)

from src.sync.orchestrator import SyncOrchestrator
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


# --- Umgezogene Quellen (Issue #733) ------------------------------------------------------------------------

UMZUEGE = _testvektoren()["umzuege"]
ALT = "https://mandari.example/session/musterstadt/api/oparl/"
NEU = "https://neu.example/session/musterstadt/api/oparl/"


@pytest.mark.parametrize("vektor", UMZUEGE, ids=[f"{v['uri'][-30:]}|{v['basis'][-12:]}" for v in UMZUEGE])
def test_umzug_aendert_keine_kennung(vektor: dict[str, str]) -> None:
    """Dieselben Vektoren prüft Django: Adresse unter der neuen Domain, Kennung der festgeschriebenen Basis."""
    erwartet = UUID(vektor["kennung"])
    assert canonical_uri(vektor["uri"], vektor["adresse"], vektor["basis"]) == vektor["kanonisch"]
    assert canonical_id(vektor["kanonisch"]) == erwartet
    basen = IdBases({vektor["adresse"]: vektor["basis"]})
    assert basen.id(vektor["uri"]) == erwartet
    assert OParlProcessor(basen).generate_uuid(vektor["uri"]) == erwartet


def test_umgezogene_quelle_behaelt_kennungen_auch_eingebettet() -> None:
    """Objekt und eingebettete Objekte tragen nach dem Umzug die Kennungen von vorher; die Adresse ist neu."""
    vorher = OParlProcessor().process(
        {"id": f"{ALT}paper/1/", "type": f"{TYP}Paper", "mainFile": {"id": f"{ALT}file/2/", "type": f"{TYP}File"}}
    )
    nachher = OParlProcessor(IdBases({NEU: ALT})).process(
        {"id": f"{NEU}paper/1/", "type": f"{TYP}Paper", "mainFile": {"id": f"{NEU}file/2/", "type": f"{TYP}File"}}
    )
    assert vorher is not None and nachher is not None
    assert nachher.id == vorher.id == canonical_id(f"{ALT}paper/1/")
    assert nachher.external_id == f"{NEU}paper/1/"
    assert [e.id for e in nachher.nested_entities] == [e.id for e in vorher.nested_entities]
    assert [e.external_id for e in nachher.nested_entities] == [f"{NEU}file/2/"]


def test_basen_der_kennungen() -> None:
    basen = IdBases()
    assert not basen
    assert basen.add("https://neu.example/a/", "https://alt.example/a/") is True
    assert basen
    # Gleich nach Ergänzung des Schrägstrichs: keine Änderung
    assert basen.add("https://neu.example/a", "https://alt.example/a") is False
    assert basen.uri("https://neu.example/a/x/") == "https://alt.example/a/x/"
    # Präfixgrenze: /ab/ liegt nicht unter /a/
    assert basen.uri("https://neu.example/ab/x/") == "https://neu.example/ab/x/"
    # Verschachtelte Präfixe: der längste gilt
    basen.add("https://neu.example/a/b/", "https://dritte.example/")
    assert basen.uri("https://neu.example/a/b/c/") == "https://dritte.example/c/"
    assert basen.uri("https://neu.example/a/c/") == "https://alt.example/a/c/"
    # Adresse gleich Basis bzw. ohne Basis: Eintrag entfällt
    assert basen.add("https://neu.example/a/", "https://neu.example/a/") is True
    assert basen.add("https://neu.example/a/b/", "") is True
    assert not basen
    assert basen.add("", "https://alt.example/") is False


def test_basis_aus_der_konfiguration_der_quelle() -> None:
    assert SOURCE_ID_BASE_KEY == "id_base"
    assert source_id_base({"session_tenant": "x", "id_base": ALT}) == ALT
    assert source_id_base({"id_base": 5}) == ""
    assert source_id_base({}) == ""
    assert source_id_base(None) == ""


def test_orchestrator_uebernimmt_die_basis_der_quelle() -> None:
    """Je Quelle gilt ``sync_config["id_base"]``; ein Wechsel verwirft zwischengespeicherte Kennungen."""
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = cast(Any, SimpleNamespace(id_bases=IdBases()))
    orchestrator.processor = OParlProcessor(orchestrator.storage.id_bases)
    adresse = f"{NEU}paper/1/"
    assert orchestrator.processor.generate_uuid(adresse) == canonical_id(adresse)

    orchestrator._register_id_base(NEU, SimpleNamespace(sync_config={"session_tenant": "x", "id_base": ALT}))
    assert orchestrator.processor.generate_uuid(adresse) == canonical_id(f"{ALT}paper/1/")

    # Ohne Eintrag (oder ohne Quelle) sind die Adressen wieder kanonisch
    orchestrator._register_id_base(NEU, SimpleNamespace(sync_config={}))
    assert orchestrator.processor.generate_uuid(adresse) == canonical_id(adresse)
    orchestrator._register_id_base(NEU, None)
    assert not orchestrator.storage.id_bases
