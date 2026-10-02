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
    SOURCE_ID_ADDRESS_KEY,
    SOURCE_ID_BASE_KEY,
    SOURCE_ID_RULES_KEY,
    IdBases,
    canonical_id,
    canonical_uri,
    generate_uuid,
    id_rules,
    source_id_address,
    source_id_base,
    source_id_rules,
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


# --- Umzug mit neuer Form der Adressen (ALLRIS: /public/oparl/<typ>?id=N -> /oparl/<typ>/N) ---------------

REGELSATZ = _testvektoren()["regelsatz"]
REGELN = _testvektoren()["regeln"]
REGEL_ADRESSE = REGELSATZ["adresse"]
REGEL_BASIS = REGELSATZ["basis"]
REGEL_KONFIGURATION = {
    SOURCE_ID_ADDRESS_KEY: REGEL_ADRESSE,
    SOURCE_ID_BASE_KEY: REGEL_BASIS,
    SOURCE_ID_RULES_KEY: REGELSATZ["regeln"],
}


@pytest.mark.parametrize("vektor", REGELN, ids=[v["uri"][-35:] for v in REGELN])
def test_regeln_ergeben_die_bisherige_kennung(vektor: dict[str, str]) -> None:
    """Dieselben Vektoren prüft Django: neue Adressform, Kennung der bisherigen Adresse."""
    erwartet = UUID(vektor["kennung"])
    assert canonical_uri(vektor["uri"], REGEL_ADRESSE, REGEL_BASIS, REGELSATZ["regeln"]) == vektor["kanonisch"]
    assert canonical_id(vektor["kanonisch"]) == erwartet
    basen = IdBases.for_source(f"{REGEL_ADRESSE}system", REGEL_KONFIGURATION)
    assert basen.id(vektor["uri"]) == erwartet
    assert OParlProcessor(basen).generate_uuid(vektor["uri"]) == erwartet


def test_regelvektoren_decken_die_faelle_ab() -> None:
    """Abgebildete Adressen, Adressen ohne passende Regel, Präfixgrenze und Altbestand unter der Basis."""
    abgebildet = [v for v in REGELN if v["uri"] != v["kanonisch"]]
    unveraendert = [v for v in REGELN if v["uri"] == v["kanonisch"]]
    assert len(abgebildet) >= 8 and len(unveraendert) >= 5
    assert all(v["kanonisch"].startswith(REGEL_BASIS) for v in abgebildet)
    kennungen = [v["kennung"] for v in REGELN]
    assert len(set(kennungen)) == len(kennungen)


@pytest.mark.parametrize(
    "regeln",
    [
        r"papers/(\d+)",
        {"papers": "x"},
        [[r"papers/(\d+)"]],
        [["", "x"]],
        [[1, "x"]],
        [["(", "x"]],
        [[r"papers/(\d+)", r"papers?id=\2"]],
        [[r"(?P<n>\d+)", r"\g<m>"]],
        [[r"papers/(\d+)", r"papers?id=\q"]],
        [["a", "b"]] * 51,
        [["a" * 301, "b"]],
    ],
)
def test_ungueltige_regeln_werden_abgewiesen(regeln: Any) -> None:
    """Ungültige Regeln fallen auf, statt Objekte mit anderen Kennungen anzulegen."""
    with pytest.raises(ValueError):
        id_rules(regeln)
    with pytest.raises(ValueError):
        IdBases().add_source(f"{REGEL_ADRESSE}system", {SOURCE_ID_RULES_KEY: regeln})


def test_gueltige_regeln() -> None:
    assert id_rules(None) == ()
    assert id_rules([]) == ()
    assert id_rules([("a(b)", r"\g<1>"), ["(?P<n>c)", r"\g<n>"]]) == (("a(b)", r"\g<1>"), ("(?P<n>c)", r"\g<n>"))
    assert source_id_rules({}) == () and source_id_rules(None) == ()
    assert source_id_address({SOURCE_ID_ADDRESS_KEY: REGEL_ADRESSE}) == REGEL_ADRESSE
    assert source_id_address({SOURCE_ID_ADDRESS_KEY: 5}) == ""


def test_basen_mit_regeln() -> None:
    regel = [["papers/([0-9]+)", r"papers?id=\1"]]
    basen = IdBases()
    # Gleicher Präfix, andere Form: Der Eintrag bleibt, auch ohne eigene Basis
    assert basen.add("https://a.example/oparl/", "", regel) is True
    assert basen.uri("https://a.example/oparl/papers/5") == "https://a.example/oparl/papers?id=5"
    assert basen.uri("https://a.example/oparl/files/5") == "https://a.example/oparl/files/5"
    assert basen.add("https://a.example/oparl", "https://a.example/oparl/", regel) is False
    # Ohne Regeln und mit Basis gleich Adresse entfällt der Eintrag
    assert basen.add("https://a.example/oparl/", "") is True
    assert not basen


def test_orchestrator_uebernimmt_adresse_und_regeln_der_quelle() -> None:
    """Quelle unter ``…/oparl/system``, Objekte unter ``…/oparl/<typ>/N``: Kennungen der bisherigen Adressen."""
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = cast(Any, SimpleNamespace(id_bases=IdBases()))
    orchestrator.processor = OParlProcessor(orchestrator.storage.id_bases)
    quelle = f"{REGEL_ADRESSE}system"
    vorlage = f"{REGEL_ADRESSE}papers/2025706"
    datei = f"{REGEL_ADRESSE}files/3080129"
    assert orchestrator.processor.generate_uuid(vorlage) == canonical_id(vorlage)

    orchestrator._register_id_base(quelle, SimpleNamespace(sync_config=REGEL_KONFIGURATION))
    assert orchestrator.processor.generate_uuid(vorlage) == canonical_id(f"{REGEL_BASIS}papers?id=2025706")
    # Ohne passende Regel bleibt die Adresse kanonisch
    assert orchestrator.processor.generate_uuid(datei) == canonical_id(datei)

    # Ungültige Regeln: Der Abgleich der Quelle bricht ab (sync_all meldet den Fehler je Quelle)
    with pytest.raises(ValueError):
        orchestrator._register_id_base(quelle, SimpleNamespace(sync_config={SOURCE_ID_RULES_KEY: "x"}))


def test_ereignisse_verweisen_auf_die_bisherigen_kennungen() -> None:
    """Verweise aus URLs (Gremien, Sitzung, Tagesordnungspunkt, Vorlage) treffen die Objekte im Bestand."""
    from src.storage import ris_events

    basen = IdBases.for_source(f"{REGEL_ADRESSE}system", REGEL_KONFIGURATION)
    beratung = ris_events.consultation_events(
        canonical_id(f"{REGEL_BASIS}consultations?id=9402&bi=5"),
        {
            "id": f"{REGEL_ADRESSE}consultations/9402",
            "paper": f"{REGEL_ADRESSE}papers/2025706",
            "meeting": f"{REGEL_ADRESSE}meetings/2005833",
            "agendaitem": f"{REGEL_ADRESSE}agendaItems/2069142",
            "organization": [f"{REGEL_ADRESSE}organizations/gr/6"],
        },
        None,
        ids=basen,
    )
    assert beratung[0].payload["paper"] == str(canonical_id(f"{REGEL_BASIS}papers?id=2025706"))
    assert beratung[0].payload["meeting"] == str(canonical_id(f"{REGEL_BASIS}meetings?id=2005833"))
    assert beratung[0].payload["agenda_item"] == str(canonical_id(f"{REGEL_BASIS}agendaItems?id=2069142"))
    assert beratung[0].payload["organization"] == str(canonical_id(f"{REGEL_BASIS}organizations?typ=gr&id=6"))

    sitzung = ris_events.meeting_events(
        canonical_id(f"{REGEL_BASIS}meetings?id=2005833"),
        {"id": f"{REGEL_ADRESSE}meetings/2005833", "organization": [f"{REGEL_ADRESSE}organizations/gr/6"]},
        None,
        ids=basen,
    )
    assert sitzung[0].payload["organizations"] == [str(canonical_id(f"{REGEL_BASIS}organizations?typ=gr&id=6"))]
