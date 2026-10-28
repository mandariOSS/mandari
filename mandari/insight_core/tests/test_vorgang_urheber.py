# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antragstellende Fraktionen mit Datenbank (Issue #1138, ``services/vorgang_urheber.py``).

- Fraktionen und Mitgliedschaften aus OParl, ersatzweise die gepflegte Liste mit Umbenennung und Einzelmandat.
- Speichern mit Herkunft und Stand; der Bestandslauf setzt fort und ist drosselbar.
- Abonnement ``insight.urheber``: doppelte Zustellung ändert nichts.
- CSV-Import der Startdaten für Münster.
- Insight: Anzeige an der Vorlage und Filter in der Liste.

Nur erfundene Namen außer in den Startdaten für Münster.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client

from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription
from apps.events.models import ParkedEvent
from apps.events.registry import Subscriber, get
from apps.events.tests.hilfen import nummeriert
from insight_core import subscribers
from insight_core.models import (
    FraktionsBezeichnung,
    FraktionsZugehoerigkeit,
    OParlBody,
    OParlFile,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    VorgangUrheber,
    VorgangUrheberStand,
)
from insight_core.services import urheber_erkennung, vorgang_urheber

pytestmark = pytest.mark.django_db

RIS = "https://ris.beispielstadt.example/oparl"
_nummer = count()
DATEN = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture
def body() -> OParlBody:
    quelle = OParlSource.objects.create(name="Beispielstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=quelle, name="Beispielstadt", slug="beispiel", is_listed=True
    )


def _org(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=f"{RIS}/org/{next(_nummer)}", body=body, name=name, **felder)


def _person(body: OParlBody, vorname: str, nachname: str) -> OParlPerson:
    return OParlPerson.objects.create(
        external_id=f"{RIS}/person/{next(_nummer)}", body=body, given_name=vorname, family_name=nachname
    )


def _vorgang(body: OParlBody, name: str, **felder: Any) -> OParlPaper:
    felder.setdefault("paper_type", "Antrag")
    felder.setdefault("date", date(2026, 3, 1))
    return OParlPaper.objects.create(external_id=f"{RIS}/paper/{next(_nummer)}", body=body, name=name, **felder)


def _urheber(paper: OParlPaper) -> list[tuple[str, str]]:
    return sorted(VorgangUrheber.objects.filter(paper_id=paper.pk).values_list("bezeichnung", "herkunft"))


@pytest.fixture
def oparl_kommune(body: OParlBody) -> dict[str, Any]:
    cdu = _org(body, "CDU-Fraktion", organization_type="Fraktion", short_name="CDU")
    gruene = _org(body, "Fraktion Bündnis 90/Die Grünen", organization_type="Fraktion", short_name="Grüne")
    _org(body, "Ausschuss für Umwelt", organization_type="Gremium")
    petra = _person(body, "Petra", "Beispiel")
    OParlMembership.objects.create(external_id=f"{RIS}/m/1", person=petra, organization=gruene)
    return {"cdu": cdu, "gruene": gruene, "petra": petra}


# --- Erkennung und Speichern --------------------------------------------------------------------


def test_titel_person_und_unterschrift_werden_mit_herkunft_gespeichert(
    body: OParlBody, oparl_kommune: dict[str, Any]
) -> None:
    titel = _vorgang(body, "Antrag der CDU-Fraktion: Bänke am Hafen")
    person = _vorgang(body, "Bänke am Ufer", raw_json={"originatorPerson": [oparl_kommune["petra"].external_id]})
    unterschrift = _vorgang(body, "Bänke im Park", raw_json={"mainFile": {"id": f"{RIS}/file/1"}})
    OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=body,
        paper=unterschrift,
        text_content="Wir beantragen Bänke.\n" * 400 + "Mit freundlichen Grüßen\nPetra Beispiel\nFraktionssprecherin",
    )
    ohne = _vorgang(body, "Haushalt 2027", paper_type="Beschlussvorlage")

    vorgang_urheber.ermitteln([titel.pk, person.pk, unterschrift.pk, ohne.pk])

    assert _urheber(titel) == [("CDU-Fraktion", "title")]
    assert _urheber(person) == [("Fraktion Bündnis 90/Die Grünen", "person")]
    assert _urheber(unterschrift) == [("Fraktion Bündnis 90/Die Grünen", "signature")]
    assert _urheber(ohne) == []
    zeile = VorgangUrheber.objects.get(paper_id=titel.pk)
    assert zeile.organisation_id == oparl_kommune["cdu"].pk and zeile.anzeigename == "CDU"
    staende = dict(VorgangUrheberStand.objects.values_list("paper_id", "herkunft"))
    assert staende == {titel.pk: "title", person.pk: "person", unterschrift.pk: "signature", ohne.pk: ""}
    assert set(VorgangUrheberStand.objects.values_list("version", flat=True)) == {urheber_erkennung.VERSION}


def test_erneute_ermittlung_ersetzt_das_ergebnis(body: OParlBody, oparl_kommune: dict[str, Any]) -> None:
    vorlage = _vorgang(body, "Antrag der CDU-Fraktion: Bänke")
    vorgang_urheber.ermitteln([vorlage.pk])
    OParlPaper.objects.filter(pk=vorlage.pk).update(name="Antrag der Fraktionen CDU und Grüne: Bänke")
    vorgang_urheber.ermitteln([vorlage.pk])
    assert _urheber(vorlage) == [("CDU-Fraktion", "title"), ("Fraktion Bündnis 90/Die Grünen", "title")]


def test_quelle_mit_originator_organization(body: OParlBody, oparl_kommune: dict[str, Any]) -> None:
    vorlage = _vorgang(
        body, "Antrag der CDU-Fraktion", raw_json={"originatorOrganization": [oparl_kommune["gruene"].external_id]}
    )
    vorgang_urheber.ermitteln([vorlage.pk])
    assert _urheber(vorlage) == [("Fraktion Bündnis 90/Die Grünen", "source")]


def test_fraktionen_anderer_kommunen_zaehlen_nicht(body: OParlBody, oparl_kommune: dict[str, Any]) -> None:
    quelle = OParlSource.objects.create(name="Anderswo", url="https://ris.anderswo.example/oparl/system")
    anderswo = OParlBody.objects.create(external_id="https://ris.anderswo.example/b/1", source=quelle, name="Anderswo")
    vorlage = _vorgang(anderswo, "Antrag der CDU-Fraktion: Bänke")
    vorgang_urheber.ermitteln([vorlage.pk])
    assert _urheber(vorlage) == []


# --- Gepflegte Liste ----------------------------------------------------------------------------


@pytest.fixture
def liste(body: OParlBody) -> None:
    """Wie Münster: keine Fraktionsmitgliedschaften in OParl, dafür die gepflegte Liste."""

    def eintrag(nachname: str, vorname: str, fraktion: str, kurzform: str, art: str, **felder: Any) -> None:
        felder.setdefault("gueltig_ab", date(2025, 11, 1))
        FraktionsZugehoerigkeit.objects.create(
            body=body, nachname=nachname, vorname=vorname, fraktion=fraktion, kurzform=kurzform, art=art, **felder
        )

    eintrag("Beispiel", "Petra", "Fraktion GRÜNE", "Grüne", "fraktion")
    eintrag("Muster", "Max", "FDP", "FDP", "einzelmandat", gueltig_bis=date(2026, 9, 30))
    eintrag("Muster", "Max", "FDP-Ratsfraktion", "FDP", "fraktion", gueltig_ab=date(2026, 10, 1))
    eintrag("Probe", "Lars", "Die PARTEI & MOVE Beispielstadt", "PARTEI & MOVE", "gruppe")
    eintrag("Offen", "Otto", "", "", "fraktion")
    for name, kurz, ab, bis in (
        ("Die PARTEI/ÖDP", "PARTEI/ÖDP", date(2025, 11, 1), date(2026, 9, 30)),
        ("Die PARTEI & MOVE Beispielstadt", "PARTEI & MOVE", date(2026, 10, 1), None),
    ):
        FraktionsBezeichnung.objects.create(
            body=body,
            fraktion="Die PARTEI & MOVE Beispielstadt",
            bezeichnung=name,
            kurzform=kurz,
            gueltig_ab=ab,
            gueltig_bis=bis,
        )


def _brief(body: OParlBody, unterschrift: str, datum: date) -> OParlPaper:
    vorlage = _vorgang(body, "Bänke am Hafen", date=datum)
    OParlFile.objects.create(
        external_id=f"{RIS}/file/{next(_nummer)}",
        body=body,
        paper=vorlage,
        text_content=f"Wir beantragen Bänke.\nMit freundlichen Grüßen\n{unterschrift}",
    )
    return vorlage


def test_liste_mit_umbenennung_und_wechsel_zum_datum(body: OParlBody, liste: None) -> None:
    vorher = _brief(body, "Lars Probe", date(2026, 3, 1))
    nachher = _brief(body, "Lars Probe", date(2026, 10, 15))
    fdp_vorher = _brief(body, "Max Muster", date(2026, 3, 1))
    fdp_nachher = _brief(body, "Max Muster", date(2026, 10, 15))
    titel = _vorgang(body, "Antrag der Fraktion GRÜNE: Radweg", date=date(2019, 5, 1))
    vor_der_liste = _brief(body, "Petra Beispiel", date(2024, 1, 1))

    vorgang_urheber.ermitteln([p.pk for p in (vorher, nachher, fdp_vorher, fdp_nachher, titel, vor_der_liste)])

    assert _urheber(vorher) == [("Die PARTEI/ÖDP", "signature")]
    assert _urheber(nachher) == [("Die PARTEI & MOVE Beispielstadt", "signature")]
    assert VorgangUrheber.objects.get(paper_id=vorher.pk).art == "gruppe"
    assert _urheber(fdp_vorher) == [("FDP", "signature")]
    assert VorgangUrheber.objects.get(paper_id=fdp_vorher.pk).art == "einzelmandat"
    assert _urheber(fdp_nachher) == [("FDP-Ratsfraktion", "signature")]
    # Die Fraktion gab es unter dem Namen schon vor der Liste; Personen gelten erst ab ihrem Beginn
    assert _urheber(titel) == [("Fraktion GRÜNE", "title")]
    assert _urheber(vor_der_liste) == []


def test_liste_gilt_nur_ohne_oparl_mitgliedschaften(
    body: OParlBody, liste: None, oparl_kommune: dict[str, Any]
) -> None:
    vorlage = _brief(body, "Max Muster", date(2026, 3, 1))
    vorgang_urheber.ermitteln([vorlage.pk])
    assert _urheber(vorlage) == []


# --- Bestandslauf -------------------------------------------------------------------------------


def test_bestandslauf_in_stapeln_setzt_fort(body: OParlBody, oparl_kommune: dict[str, Any]) -> None:
    vorgaenge = [_vorgang(body, f"Antrag der CDU-Fraktion: Nummer {n}") for n in range(5)]
    call_command("vorgang_urheber_ermitteln", "--probe", "--pause", "0", stdout=None)
    assert not VorgangUrheber.objects.exists()

    call_command("vorgang_urheber_ermitteln", "--stapel", "2", "--limit", "3", "--pause", "0")
    assert VorgangUrheberStand.objects.count() == 3
    call_command("vorgang_urheber_ermitteln", "--kommune", "beispiel", "--stapel", "2", "--pause", "0")
    assert VorgangUrheberStand.objects.count() == 5
    assert all(_urheber(p) == [("CDU-Fraktion", "title")] for p in vorgaenge)
    assert not vorgang_urheber.ausstehend(body.pk).exists()


# --- Abonnement ---------------------------------------------------------------------------------


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


def _abonnement(settings: Any, modus: str = "aktiv") -> Subscriber:
    settings.URHEBER_SUBSCRIPTION = modus
    assert subscribers.register_urheber()
    spec = get(vorgang_urheber.NAME)
    ensure_subscription(spec)
    return spec


def _ereignis(typ: str, aggregate_type: str, kennung: uuid.UUID) -> None:
    nummeriert(
        type=typ,
        aggregate_type=aggregate_type,
        aggregate_id=kennung,
        tenant_ref=f"source:{uuid.uuid4()}",
        visibility="oeffentlich",
        payload={},
    )


def test_schalter_aus_registriert_nichts(settings: Any, leeres_register: dict[str, Subscriber]) -> None:
    settings.URHEBER_SUBSCRIPTION = "aus"
    assert not subscribers.register_urheber()
    assert vorgang_urheber.NAME not in leeres_register


def test_doppelte_zustellung_ermittelt_einmal(
    settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody, oparl_kommune: dict[str, Any]
) -> None:
    spec = _abonnement(settings)
    vorlage = _vorgang(body, "Antrag der CDU-Fraktion: Bänke")
    anlage = OParlFile.objects.create(external_id=f"{RIS}/file/{next(_nummer)}", body=body, paper=vorlage)
    _ereignis("ris.paper.released", "Paper", vorlage.pk)
    _ereignis("ris.paper.released", "Paper", vorlage.pk)
    _ereignis("ris.file.text_extracted", "File", anlage.pk)
    _ereignis("ris.paper.changed", "Paper", uuid.uuid4())  # unbekannt: ohne Wirkung

    assert deliver_batch(spec).delivered == 4

    assert _urheber(vorlage) == [("CDU-Fraktion", "title")]
    assert VorgangUrheberStand.objects.count() == 1
    assert not ParkedEvent.objects.exists()


def test_schatten_zaehlt_nur(
    settings: Any, leeres_register: dict[str, Subscriber], body: OParlBody, oparl_kommune: dict[str, Any]
) -> None:
    spec = _abonnement(settings, "schatten")
    vorlage = _vorgang(body, "Antrag der CDU-Fraktion: Bänke")
    _ereignis("ris.paper.released", "Paper", vorlage.pk)
    deliver_batch(spec)
    assert not VorgangUrheberStand.objects.exists()


# --- Import der Startdaten ----------------------------------------------------------------------


def test_startdaten_muenster_lassen_sich_einlesen(body: OParlBody) -> None:
    OParlBody.objects.filter(pk=body.pk).update(name="Stadt Münster")
    call_command("fraktionszugehoerigkeiten_import", str(DATEN / "fraktionszugehoerigkeiten_muenster.csv"))
    call_command("fraktionszugehoerigkeiten_import", str(DATEN / "fraktionsbezeichnungen_muenster.csv"))
    anzahl = FraktionsZugehoerigkeit.objects.count()
    assert anzahl > 60 and FraktionsBezeichnung.objects.count() == 2
    # Ein zweiter Lauf ändert nichts
    call_command("fraktionszugehoerigkeiten_import", str(DATEN / "fraktionszugehoerigkeiten_muenster.csv"))
    assert FraktionsZugehoerigkeit.objects.count() == anzahl
    # Wahl-Partei ist nicht Fraktion: Der Oberbürgermeister steht nicht in der Liste
    assert not FraktionsZugehoerigkeit.objects.filter(nachname="Fuchs").exists()

    vorlage = _brief(body, "Sven Konopka", date(2026, 2, 1))
    vorgang_urheber.ermitteln([vorlage.pk])
    assert VorgangUrheber.objects.get(paper_id=vorlage.pk).anzeigename == "Volt"


def test_import_meldet_unbekannte_kommune(tmp_path: Path, body: OParlBody) -> None:
    from django.core.management.base import CommandError

    datei = tmp_path / "liste.csv"
    datei.write_text(
        "kommune,name,vorname,fraktion,gueltig_ab,gueltig_bis,quelle\nNirgendwo,Muster,Max,SPD-Fraktion,,,\n",
        encoding="utf-8",
    )
    with pytest.raises(CommandError, match="Nirgendwo"):
        call_command("fraktionszugehoerigkeiten_import", str(datei))


# --- Insight ------------------------------------------------------------------------------------


def test_vorgangsseite_und_filter(body: OParlBody, oparl_kommune: dict[str, Any]) -> None:
    cdu = _vorgang(body, "Antrag der CDU-Fraktion: Bänke am Hafen")
    andere = _vorgang(body, "Antrag der Fraktion Bündnis 90/Die Grünen: Radweg")
    vorgang_urheber.ermitteln([cdu.pk, andere.pk])
    client = Client()

    seite = client.get(f"/insight/vorgaenge/{cdu.pk}/").content.decode()
    assert "Gestellt von" in seite and f"/insight/gremien/{oparl_kommune['cdu'].pk}/" in seite

    inhalt = client.get("/insight/k/beispiel/vorgaenge/", {"fraktion": "CDU-Fraktion"}).content.decode()
    assert "Alle Fraktionen" in inhalt
    assert "Bänke am Hafen" in inhalt and "Radweg" not in inhalt
