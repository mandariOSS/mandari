# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsfeed nach dem Profil von kommParl: Einträge für einbettende Objekte (Issue #1229), in beiden Ausgaben.

kommParl 7-01, „Vollständigkeit und Reihenfolge“: Ändert sich ein Objekt, das in einem anderen intern ausgegeben wird,
muss der Feed einen Eintrag für das Objekt selbst und einen für das einbettende enthalten.

- Die Einbettungen jeder Ausgabe (``EMBEDDINGS`` ihrer Abbildung) sind die, die sie tatsächlich ausgibt.
- Je Einbettung: Änderung und Rücknahme des eingebetteten Objekts ergeben auch einen Eintrag für das einbettende,
  über zwei Stufen. Ein gelöschtes oder nie öffentliches einbettendes Objekt bekommt keinen.
- Der Sitzungsort unter der Kennung der Sitzung ändert sich mit ihr.
- Keine Doppelungen in einer Seite, feste Reihenfolge, ein eigener Cursor je Eintrag; dieselbe Anfrage ergibt dieselbe
  Antwort. Die ältere Form bleibt, wie sie ist.
- Die Zahl der Abfragen je Seite hängt nicht von der Zahl der Einträge ab.
"""

from __future__ import annotations

import gc
import json
import re
import uuid
import weakref
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.common.models import IdentifierBase
from apps.session.api import oparl as session_oparl
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionTenant,
)
from hub.api import changes
from hub.api.tests.ereignisse import T0, ereignis
from hub.api.tests.konformitaet import pruefe_profil
from hub.ris.canonical import TYPE_SCHEMA
from hub.ris.mapping import bestand as bestand_mapping
from hub.ris.mapping import session as session_mapping
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
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
HEUTE = date(2026, 9, 30)
MANDANT = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
#: Typ-URL -> Objekttyp in den Adressen (``Meeting`` -> ``meeting``)
ARTEN = {name: kind for kind, name in TYPE_SCHEMA.items()}
CURSOR = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


def _get(adresse: str, **parameter: str) -> Any:
    return Client().get(adresse.removeprefix(SITE), parameter)


def _json(adresse: str, **parameter: str) -> dict[str, Any]:
    antwort = _get(adresse, **parameter)
    assert antwort.status_code == 200, (adresse, antwort.status_code, antwort.content[:500])
    return cast(dict[str, Any], antwort.json())


def _zeilen(adresse: str) -> list[dict[str, Any]]:
    antwort = _get(adresse)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    return [json.loads(zeile) for zeile in inhalt.decode("utf-8").splitlines()]


def _art(objekt: dict[str, Any]) -> str:
    return ARTEN[str(objekt["type"]).rsplit("/", 1)[1]]


def _eingebettet(objekt: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """(eingebetteter Typ, einbettender Typ) für jedes Objekt, das ``objekt`` intern ausgibt – auf allen Stufen."""
    for wert in objekt.values():
        for kind in wert if isinstance(wert, list) else [wert]:
            if isinstance(kind, dict) and "type" in kind and "id" in kind:
                yield _art(kind), _art(objekt)
                yield from _eingebettet(kind)


def _einbettungen(zeilen: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {paar for objekt in zeilen[1:-1] for paar in _eingebettet(objekt)}


def _tabelle(einbettungen: dict[str, frozenset[str]]) -> set[tuple[str, str]]:
    return {(kind, parent) for kind, parents in einbettungen.items() for parent in parents}


# =============================================================================
# Aggregator
# =============================================================================


@pytest.fixture
def kommune() -> Any:
    cache.clear()
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_CACHE_SECONDS=0,
        OPARL_CHANGES_ENABLED=True,
    ):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        yield OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
    cache.clear()


@pytest.fixture
def bestand(kommune: OParlBody) -> dict[str, Any]:
    """Je Einbettung des Bestands ein Paar; der Ort steht in zwei Sitzungen (als Adresse und eingebettet)."""
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/1", body=kommune, name="Rat")
    person = OParlPerson.objects.create(external_id=f"{RIS}/person/1", body=kommune, name="Petra Muster")
    ort = OParlLocation.objects.create(external_id=f"{RIS}/location/1", body=kommune, description="Bürgerhaus")
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1", body=kommune, name="Rat", raw_json={"location": ort.external_id}
    )
    ausschuss = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/2",
        body=kommune,
        name="Ausschuss",
        raw_json={"location": {"id": ort.external_id, "type": "https://schema.oparl.org/1.1/Location"}},
    )
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=kommune, name="Radweg", reference="V/1")
    return {
        "kommune": kommune,
        "periode": OParlLegislativeTerm.objects.create(external_id=f"{RIS}/term/1", body=kommune, name="2025–2030"),
        "rat": rat,
        "person": person,
        "mitglied": OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=rat),
        "ort": ort,
        "sitzung": sitzung,
        "ausschuss": ausschuss,
        # Ortsangabe nur als Text: Der Ort gehört zur Sitzung und trägt ihre Kennung
        "bezirk": OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/3", body=kommune, name="Bezirk", location_name="Rathaus, Raum 1"
        ),
        "top": OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/1", meeting=sitzung, name="TOP 1"),
        "top2": OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/2", meeting=sitzung, name="TOP 2"),
        "vorlage": vorlage,
        "beratung": OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/1", body=kommune, paper=vorlage, role="Beschluss"
        ),
        "anlage": OParlFile.objects.create(
            external_id=f"{RIS}/file/1", body=kommune, paper=vorlage, name="Begründung", access_url=f"{RIS}/f/1"
        ),
        "einladung": OParlFile.objects.create(
            external_id=f"{RIS}/file/2", body=kommune, meeting=sitzung, name="Einladung", access_url=f"{RIS}/f/2"
        ),
    }


ADRESSART = {
    "kommune": "body",
    "periode": "legislativeterm",
    "rat": "organization",
    "person": "person",
    "mitglied": "membership",
    "ort": "location",
    "sitzung": "meeting",
    "ausschuss": "meeting",
    "bezirk": "meeting",
    "top": "agendaitem",
    "top2": "agendaitem",
    "vorlage": "paper",
    "beratung": "consultation",
    "anlage": "file",
    "einladung": "file",
}


def _namen(bestand: dict[str, Any]) -> dict[str, str]:
    """Adresse -> Name im Test; der Sitzungsort der Bezirkssitzung heißt ``bezirksort``."""
    namen = {f"{API}/{ADRESSART[name]}/{obj.pk}": name for name, obj in bestand.items()}
    namen[f"{API}/location/{bestand['bezirk'].pk}"] = "bezirksort"
    return namen


def _feed(kommune: OParlBody, **parameter: str) -> dict[str, Any]:
    seite = _json(f"{API}/body/{kommune.pk}/kommparl/changes", **parameter)
    assert pruefe_profil(seite, "changes", "ChangeList") == []
    return seite


def _kurz(seite: dict[str, Any], namen: dict[str, str]) -> list[tuple[str, str, str | None]]:
    return [(e["operation"], namen.get(e["id"], e["id"]), e.get("reason")) for e in seite["data"]]


def _e(typ: str, body: uuid.UUID, objekt: uuid.UUID, *, zeit: datetime = T0, **nutzlast: Any) -> None:
    """Öffentliches Ereignis mit genau dieser Nutzlast (die Kennung des Objekts setzt ``ereignis``)."""
    inhalt = {key: str(wert) if isinstance(wert, uuid.UUID) else wert for key, wert in nutzlast.items()}
    ereignis(typ, body, objekt, nutzlast=inhalt, zeit=zeit)


def _zurueck(
    body: uuid.UUID, objekttyp: str, objekt: uuid.UUID, grund: str = "quelle_geloescht", **nutzlast: Any
) -> None:
    inhalt = {"object_type": objekttyp, "object": str(objekt), "reason": grund}
    inhalt.update({key: str(wert) for key, wert in nutzlast.items()})
    operation = "redact" if grund == "datenschutz" else "delete"
    ereignis("ris.object.depublished", body, objekt, nutzlast=inhalt, operation=operation)


class TestAggregatorEinbettungen:
    def test_tabelle_wie_die_ausgabe(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        """Was der Bestand intern ausgibt, steht in ``EMBEDDINGS`` – nicht mehr und nicht weniger."""
        zeilen = _zeilen(f"{API}/body/{kommune.pk}/kommparl/snapshot")
        assert _einbettungen(zeilen) == _tabelle(bestand_mapping.EMBEDDINGS)
        assert _tabelle(bestand_mapping.EMBEDDINGS) <= _tabelle(changes.OPARL_EMBEDDINGS)

    def test_tagesordnungspunkt_in_der_sitzung(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        b = bestand
        _e(
            "ris.agendaitem.changed",
            kommune.pk,
            b["top"].pk,
            meeting=b["sitzung"].pk,
            change="changed",
            changed=["name"],
        )

        seite = _feed(kommune)

        assert _kurz(seite, _namen(b)) == [("upsert", "top", None), ("upsert", "sitzung", None)]
        assert [e["type"].rsplit("/", 1)[1] for e in seite["data"]] == ["AgendaItem", "Meeting"]
        # Beide Einträge tragen den Zeitpunkt der Änderung
        assert {e["modified"] for e in seite["data"]} == {"2026-09-01T08:00:00+00:00"}

    def test_verschobener_punkt_nennt_beide_sitzungen(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        b = bestand
        _e(
            "ris.agendaitem.changed",
            kommune.pk,
            b["top"].pk,
            meeting=b["ausschuss"].pk,
            previous_meeting=b["sitzung"].pk,
            change="moved",
        )

        eintraege = _kurz(_feed(kommune), _namen(b))

        assert eintraege[0] == ("upsert", "top", None)
        assert sorted(eintraege[1:]) == [("upsert", "ausschuss", None), ("upsert", "sitzung", None)]

    @pytest.mark.parametrize(
        ("name", "objekttyp", "einbettend"),
        [
            ("top", "AgendaItem", "sitzung"),
            ("beratung", "Consultation", "vorlage"),
            ("anlage", "File", "vorlage"),
            ("einladung", "File", "sitzung"),
            ("mitglied", "Membership", "person"),
        ],
    )
    def test_ruecknahme_ohne_angabe_aus_dem_bestand(
        self, kommune: OParlBody, bestand: dict[str, Any], name: str, objekttyp: str, einbettend: str
    ) -> None:
        """Rücknahmen des Ingestors nennen das einbettende Objekt nicht; der Bestand behält die Zeile und kennt es."""
        objekt = bestand[name]
        type(objekt).objects.filter(pk=objekt.pk).update(deleted=True)
        _zurueck(kommune.pk, objekttyp, objekt.pk)

        assert _kurz(_feed(kommune), _namen(bestand)) == [("delete", name, "deleted"), ("upsert", einbettend, None)]

    @pytest.mark.parametrize(
        ("typ", "name", "nutzlast", "einbettend"),
        [
            (
                "ris.consultation.changed",
                "beratung",
                {"paper": "vorlage", "change": "changed", "changed": ["role"]},
                ["vorlage"],
            ),
            (
                "ris.membership.changed",
                "mitglied",
                {"person": "person", "change": "changed", "changed": ["role"]},
                ["person"],
            ),
            ("ris.legislativeterm.changed", "periode", {"change": "changed", "changed": ["name"]}, ["kommune"]),
            ("ris.file.changed", "anlage", {"paper": "vorlage", "change": "renamed"}, ["vorlage"]),
            ("ris.file.changed", "einladung", {"meeting": "sitzung", "change": "replaced"}, ["sitzung"]),
            # Im Bestand gibt der Tagesordnungspunkt keine Dateien aus: die Vorlage aus der Zeile, kein TOP
            ("ris.file.changed", "anlage", {"agenda_item": "top", "change": "added"}, ["vorlage"]),
        ],
    )
    def test_je_einbettung_ein_eintrag(
        self,
        kommune: OParlBody,
        bestand: dict[str, Any],
        typ: str,
        name: str,
        nutzlast: dict[str, str],
        einbettend: list[str],
    ) -> None:
        inhalt: dict[str, Any] = {
            key: bestand[wert].pk if isinstance(wert, str) and wert in bestand else wert
            for key, wert in nutzlast.items()
        }
        _e(typ, kommune.pk, bestand[name].pk, **inhalt)

        eintraege = _kurz(_feed(kommune), _namen(bestand))

        assert eintraege == [("upsert", name, None), *(("upsert", objekt, None) for objekt in einbettend)]

    @pytest.mark.parametrize(
        ("nutzlast", "einbettend"),
        [
            # Die Zeile hängt an Vorlage und Sitzung (``upsert_file`` behält beide), das Ereignis nennt nur die Vorlage
            ({"paper": "vorlage"}, ["sitzung", "vorlage"]),
            # Das Ereignis nennt eine andere Sitzung als die Zeile (``_first_reference`` im Ingestor)
            ({"meeting": "ausschuss"}, ["ausschuss", "sitzung", "vorlage"]),
        ],
    )
    def test_nutzlast_und_zeile_zusammen(
        self, kommune: OParlBody, bestand: dict[str, Any], nutzlast: dict[str, str], einbettend: list[str]
    ) -> None:
        """Nennt das Ereignis nur einen Teil der einbettenden Objekte, ergänzt die Ausgabe die übrigen."""
        b = bestand
        OParlFile.objects.filter(pk=b["anlage"].pk).update(meeting=b["sitzung"])
        bezuege = {key: b[name].pk for key, name in nutzlast.items()}
        _e("ris.file.changed", kommune.pk, b["anlage"].pk, change="renamed", **bezuege)

        eintraege = _kurz(_feed(kommune), _namen(b))

        assert eintraege[0] == ("upsert", "anlage", None)
        assert sorted(eintraege[1:]) == [("upsert", objekt, None) for objekt in einbettend]

    def test_ort_in_allen_sitzungen(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        """Ein Ort nennt seine Sitzungen nicht; der Bestand findet sie über die Kennung der Quelle (Adresse und Objekt)."""
        b = bestand
        _e("ris.location.changed", kommune.pk, b["ort"].pk, change="changed", changed=["description"])
        _zurueck(kommune.pk, "Location", b["ort"].pk)

        eintraege = _kurz(_feed(kommune), _namen(b))

        # In einer Seite steht jedes einbettende Objekt einmal, an der spätesten Stelle
        assert eintraege[0] == ("upsert", "ort", None)
        assert eintraege[1] == ("delete", "ort", "deleted")
        assert sorted(eintraege[2:]) == [("upsert", "ausschuss", None), ("upsert", "sitzung", None)]

    def test_sitzungsort_aendert_sich_mit_der_sitzung(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        b = bestand
        _e("ris.meeting.changed", kommune.pk, b["bezirk"].pk, changed=["name"])
        # Eine Sitzung mit eigenem Ort der Quelle hat keinen Ort unter ihrer Kennung
        _e("ris.meeting.changed", kommune.pk, b["sitzung"].pk, changed=["name"])

        assert _kurz(_feed(kommune), _namen(b)) == [
            ("upsert", "bezirk", None),
            ("upsert", "bezirksort", None),
            ("upsert", "sitzung", None),
        ]

    def test_sitzungsort_entfaellt(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        b = bestand
        OParlMeeting.objects.filter(pk=b["bezirk"].pk).update(location_name=None)
        _e("ris.meeting.changed", kommune.pk, b["bezirk"].pk, changed=["location"])
        _e("ris.meeting.changed", kommune.pk, b["bezirk"].pk, changed=["name"])

        assert _kurz(_feed(kommune), _namen(b)) == [
            ("delete", "bezirksort", "deleted"),
            ("upsert", "bezirk", None),
        ]
        # Unter der Adresse steht das gekürzte Objekt
        assert _json(f"{API}/location/{b['bezirk'].pk}")["deleted"] is True

    def test_geloeschte_sitzung_mit_ihrem_ort(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        b = bestand
        OParlMeeting.objects.filter(pk=b["bezirk"].pk).update(deleted=True)
        _zurueck(kommune.pk, "Meeting", b["bezirk"].pk, "nichtoeffentlich")

        assert _kurz(_feed(kommune), _namen(b)) == [
            ("delete", "bezirk", "nonPublic"),
            ("delete", "bezirksort", "nonPublic"),
        ]

    def test_abstimmung_und_umsetzung_am_punkt_in_der_sitzung(
        self, kommune: OParlBody, bestand: dict[str, Any]
    ) -> None:
        b = bestand
        abstimmung = uuid.uuid4()
        _e(
            "ris.voting.recorded",
            kommune.pk,
            abstimmung,
            agenda_item=b["top"].pk,
            meeting=b["sitzung"].pk,
            method="open",
            result="approved",
        )
        # Der Umsetzungsstand nennt die Sitzung nicht: Der Bestand kennt sie
        _e("ris.resolution.implementation_changed", kommune.pk, b["top2"].pk, status="done")

        assert _kurz(_feed(kommune), _namen(b)) == [
            ("upsert", "top", None),
            ("upsert", "top2", None),
            ("upsert", "sitzung", None),
        ]

    def test_geloeschtes_einbettendes_objekt_bekommt_keinen_eintrag(
        self, kommune: OParlBody, bestand: dict[str, Any]
    ) -> None:
        """Mit der Sitzung werden ihre Punkte gelöscht: Hinter dem ``delete`` der Sitzung folgt kein ``upsert``."""
        b = bestand
        OParlMeeting.objects.filter(pk=b["sitzung"].pk).update(deleted=True)
        OParlAgendaItem.objects.filter(pk=b["top"].pk).update(deleted=True)
        _zurueck(kommune.pk, "Meeting", b["sitzung"].pk)
        _zurueck(kommune.pk, "AgendaItem", b["top"].pk, meeting=b["sitzung"].pk)

        assert _kurz(_feed(kommune), _namen(b)) == [("delete", "sitzung", "deleted"), ("delete", "top", "deleted")]


class TestAggregatorSeiten:
    def _ereignisse(self, kommune: OParlBody, b: dict[str, Any]) -> None:
        """Zwei Punkte derselben Sitzung, dann eine Datei an Vorlage und Sitzung: 2 + 2 + 3 Einträge."""
        for stunde, name in enumerate(("top", "top2")):
            zeit = T0 + timedelta(hours=stunde)
            _e("ris.agendaitem.changed", kommune.pk, b[name].pk, zeit=zeit, meeting=b["sitzung"].pk, change="changed")
        anlage, vorlage, sitzung = b["anlage"].pk, b["vorlage"].pk, b["sitzung"].pk
        zeit = T0 + timedelta(hours=2)
        _e("ris.file.changed", kommune.pk, anlage, zeit=zeit, paper=vorlage, meeting=sitzung, change="added")

    def test_keine_doppelungen_in_der_seite(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        self._ereignisse(kommune, bestand)

        seite = _feed(kommune)

        # Die Sitzung steht einmal, an der spätesten Stelle; die Seite endet mit ihrem letzten Eintrag
        assert _kurz(seite, _namen(bestand)) == [
            ("upsert", "top", None),
            ("upsert", "top2", None),
            ("upsert", "anlage", None),
            ("upsert", "sitzung", None),
            ("upsert", "vorlage", None),
        ]
        assert seite["cursor"] == seite["data"][-1]["cursor"]
        assert "next" not in seite["links"]

    def test_eigener_cursor_je_eintrag(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        """Seiten zu je einem Eintrag: jeder Eintrag einmal, in derselben Reihenfolge, auch innerhalb eines Ereignisses."""
        self._ereignisse(kommune, bestand)
        namen = _namen(bestand)

        gelesen: list[tuple[str, str, str | None]] = []
        cursor: list[str] = []
        seite = _feed(kommune, limit="1")
        while True:
            gelesen += _kurz(seite, namen)
            cursor += [e["cursor"] for e in seite["data"]]
            assert seite["data"] and seite["cursor"] == seite["data"][-1]["cursor"]
            if "next" not in seite["links"]:
                break
            seite = _feed(kommune, limit="1", after=seite["cursor"])

        assert gelesen == [
            ("upsert", "top", None),
            ("upsert", "sitzung", None),
            ("upsert", "top2", None),
            ("upsert", "sitzung", None),
            ("upsert", "anlage", None),
            ("upsert", "sitzung", None),
            ("upsert", "vorlage", None),
        ]
        assert len(set(cursor)) == len(cursor) and all(CURSOR.fullmatch(c) for c in cursor)
        # Der Cursor des letzten Eintrags eines Ereignisses ist der bisherige (ohne Stelle im Ereignis)
        assert [len(c) for c in cursor] == [60, 38, 60, 38, 60, 60, 38]
        assert _feed(kommune, after=cursor[-1])["data"] == []
        # Dieselbe Anfrage ergibt dieselbe Antwort, Byte für Byte
        for after in cursor[:3]:
            erste = _get(f"{API}/body/{kommune.pk}/kommparl/changes", after=after, limit="2")
            zweite = _get(f"{API}/body/{kommune.pk}/kommparl/changes", after=after, limit="2")
            assert erste.content == zweite.content and erste["ETag"] == zweite["ETag"]

    def test_cursor_innerhalb_eines_ereignisses_in_beiden_formen(
        self, kommune: OParlBody, bestand: dict[str, Any]
    ) -> None:
        self._ereignisse(kommune, bestand)
        mitten = _feed(kommune, limit="1")["cursor"]  # hinter dem TOP, vor seiner Sitzung
        assert len(mitten) == 60

        assert _kurz(_feed(kommune, after=mitten, limit="1"), _namen(bestand)) == [("upsert", "sitzung", None)]
        # Die ältere Form kennt keine einbettenden Objekte: Für sie ist das Ereignis gesehen
        alt = _json(f"{API}/body/{kommune.pk}/changes", after=mitten)
        assert [_namen(bestand)[e["id"]] for e in alt["data"]] == ["top2", "anlage"]

    def test_aeltere_form_bleibt_unveraendert(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        self._ereignisse(kommune, bestand)
        _zurueck(kommune.pk, "AgendaItem", bestand["top"].pk, meeting=bestand["sitzung"].pk)

        alt = _json(f"{API}/body/{kommune.pk}/changes")

        assert [(_namen(bestand)[e["id"]], e["operation"]) for e in alt["data"]] == [
            ("top", "upsert"),
            ("top2", "upsert"),
            ("anlage", "upsert"),
            ("top", "delete"),
        ]
        assert {len(e["cursor"]) for e in alt["data"]} == {38}
        assert set(alt["links"]) == {"self", "next", "snapshot"}


def _abfragen(adresse: str) -> int:
    with CaptureQueriesContext(connection) as abfragen:
        antwort = _get(adresse)
    assert antwort.status_code == 200, antwort.content
    return len(abfragen.captured_queries)


def test_aggregator_abfragen_je_seite_haengen_nicht_an_der_zahl_der_eintraege(kommune: OParlBody) -> None:
    """Je Abschnitt des Journals eine Abfrage je Typ und Stufe, keine je Eintrag (keine N+1-Abfragen)."""
    ort = OParlLocation.objects.create(external_id=f"{RIS}/location/9", body=kommune, description="Halle")

    def runde(nummer: int) -> None:
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/r{nummer}",
            body=kommune,
            location_name="Saal",
            raw_json={"location": ort.external_id},
        )
        top = OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/r{nummer}", meeting=sitzung)
        vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/r{nummer}", body=kommune)
        datei = OParlFile.objects.create(external_id=f"{RIS}/file/r{nummer}", body=kommune, paper=vorlage)
        beratung = OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/r{nummer}", body=kommune, paper=vorlage
        )
        _e("ris.agendaitem.changed", kommune.pk, top.pk, meeting=sitzung.pk, change="changed", changed=["name"])
        _e("ris.meeting.changed", kommune.pk, sitzung.pk, changed=["name"])
        _zurueck(kommune.pk, "File", datei.pk)
        _zurueck(kommune.pk, "Consultation", beratung.pk)
        _e("ris.resolution.implementation_changed", kommune.pk, top.pk, status="open")
        _e("ris.location.changed", kommune.pk, ort.pk, change="changed", changed=["room"])

    adresse = f"{API}/body/{kommune.pk}/kommparl/changes"
    runde(0)
    runde(1)
    _get(adresse)  # einmalige Abfragen (Zwischenspeicher der Installation) zählen nicht
    wenige = _abfragen(adresse)
    eintraege = len(_json(adresse)["data"])
    for nummer in range(2, 8):
        runde(nummer)
    viele = _abfragen(adresse)

    assert len(_json(adresse)["data"]) > 2 * eintraege
    assert viele == wenige


# =============================================================================
# Session-Schnittstelle
# =============================================================================


@pytest.fixture
def welt() -> Any:
    cache.clear()
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    with override_settings(SITE_URL=SITE, OPARL_API_RATE_LIMIT=0, OPARL_CHANGES_ENABLED=True):
        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        person = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Muster")
        sitzung = SessionMeeting.objects.create(
            tenant=tenant,
            name="Ratssitzung",
            organization=rat,
            start=timezone.now(),
            is_public=True,
            location="Rathaus",
        )
        vorlage = SessionPaper.objects.create(
            tenant=tenant, reference="V/2026/1", name="Radweg", is_public=True, status="approved"
        )
        top = SessionAgendaItem.objects.create(
            meeting=sitzung, number="1", name="Radweg", is_public=True, paper=vorlage
        )
        objekte = {
            "tenant": tenant,
            "body": canonical_id(f"{BASIS}body/"),
            "periode": SessionLegislativeTerm.objects.create(
                tenant=tenant, name="2025–2030", start_date=date(2025, 11, 1)
            ),
            "rat": rat,
            "person": person,
            "mitglied": SessionOrganizationMembership.objects.create(organization=rat, person=person),
            "sitzung": sitzung,
            "vorlage": vorlage,
            "top": top,
            "beratung": SessionConsultation.objects.create(paper=vorlage, organization=rat),
            "anlage": SessionFile.objects.create(tenant=tenant, name="Begründung", paper=vorlage),
            "einladung": SessionFile.objects.create(tenant=tenant, name="Einladung", meeting=sitzung),
            "top_anlage": SessionFile.objects.create(tenant=tenant, name="Plan", agenda_item=top),
            "geheim": SessionMeeting.objects.create(
                tenant=tenant, name="GEHEIME-SITZUNG", organization=rat, start=timezone.now(), is_public=False
            ),
        }
        yield objekte
    cache.clear()


SESSION_ART = {
    "periode": "legislativeterm",
    "rat": "organization",
    "person": "person",
    "mitglied": "membership",
    "sitzung": "meeting",
    "vorlage": "paper",
    "top": "agendaitem",
    "beratung": "consultation",
    "anlage": "file",
    "einladung": "file",
    "top_anlage": "file",
    "geheim": "meeting",
}


def _adresse(art: str, pk: Any) -> str:
    return f"{BASIS}{art}/{pk}/"


def _k(welt: dict[str, Any], name: str) -> uuid.UUID:
    """Kanonische Kennung eines Session-Objekts der Welt."""
    if name == "body":
        return cast(uuid.UUID, welt["body"])
    if name == "sitzungsort":
        return canonical_id(_adresse("location", welt["sitzung"].pk))
    return canonical_id(_adresse(SESSION_ART[name], welt[name].pk))


def _session_namen(welt: dict[str, Any]) -> dict[str, str]:
    namen = {_adresse(art, welt[name].pk): name for name, art in SESSION_ART.items()}
    namen[f"{BASIS}body/"] = "body"
    namen[_adresse("location", welt["sitzung"].pk)] = "sitzungsort"
    return namen


def _session_feed(**parameter: str) -> dict[str, Any]:
    seite = _json(f"{BASIS}body/kommparl/changes/", **parameter)
    assert pruefe_profil(seite, "changes", "ChangeList") == []
    return seite


def _s(welt: dict[str, Any], typ: str, name: str, **nutzlast: Any) -> None:
    inhalt = {
        key: str(_k(welt, wert)) if isinstance(wert, str) and (wert in SESSION_ART or wert == "sitzungsort") else wert
        for key, wert in nutzlast.items()
    }
    ereignis(typ, welt["body"], _k(welt, name), nutzlast=inhalt, mandant=MANDANT)


def _s_zurueck(
    welt: dict[str, Any], objekttyp: str, objekt: uuid.UUID, grund: str = "quelle_geloescht", **nutzlast: str
) -> None:
    inhalt = {"object_type": objekttyp, "object": str(objekt), "reason": grund}
    inhalt.update({key: str(_k(welt, wert)) for key, wert in nutzlast.items()})
    ereignis("ris.object.depublished", welt["body"], objekt, nutzlast=inhalt, operation="delete", mandant=MANDANT)


class TestSessionEinbettungen:
    def test_tabelle_wie_die_ausgabe(self, welt: dict[str, Any]) -> None:
        zeilen = _zeilen(f"{BASIS}body/kommparl/snapshot/")
        assert _einbettungen(zeilen) == _tabelle(session_mapping.EMBEDDINGS)
        assert _tabelle(session_mapping.EMBEDDINGS) <= _tabelle(changes.OPARL_EMBEDDINGS)

    @pytest.mark.parametrize(
        ("typ", "name", "nutzlast", "erwartet"),
        [
            (
                "ris.agendaitem.changed",
                "top",
                {"meeting": "sitzung", "change": "changed", "changed": ["name"]},
                ["top", "sitzung"],
            ),
            (
                "ris.consultation.changed",
                "beratung",
                {"paper": "vorlage", "change": "changed", "changed": ["role"]},
                ["beratung", "vorlage"],
            ),
            ("ris.file.changed", "anlage", {"paper": "vorlage", "change": "renamed"}, ["anlage", "vorlage"]),
            ("ris.file.changed", "einladung", {"meeting": "sitzung", "change": "renamed"}, ["einladung", "sitzung"]),
            # Neue Zugehörigkeit: Die frühere Sitzung gibt die Datei nicht mehr aus und bekommt auch einen Eintrag
            (
                "ris.file.changed",
                "anlage",
                {"paper": "vorlage", "previous_meeting": "sitzung", "change": "added"},
                ["anlage", "sitzung", "vorlage"],
            ),
            # Zwei Stufen: Datei im Tagesordnungspunkt in der Sitzung (die Sitzung kennt die Schnittstelle)
            (
                "ris.file.changed",
                "top_anlage",
                {"agenda_item": "top", "change": "replaced"},
                ["top_anlage", "sitzung", "top"],
            ),
            (
                "ris.membership.changed",
                "mitglied",
                {"person": "person", "change": "changed", "changed": ["role"]},
                ["mitglied", "person"],
            ),
            ("ris.legislativeterm.changed", "periode", {"change": "changed", "changed": ["name"]}, ["periode", "body"]),
            ("ris.meeting.changed", "sitzung", {"changed": ["location"]}, ["sitzung", "sitzungsort"]),
            # Die öffentliche Fassung der Niederschrift ist eine neue Datei der Sitzung
            (
                "ris.protocol.published",
                "sitzung",
                {"protocol": "8d0f7780-8536-51ef-a55c-f18fd2fa1bf8", "change": "published", "file": "einladung"},
                ["sitzung", "einladung", "sitzungsort"],
            ),
        ],
    )
    def test_je_einbettung_ein_eintrag(
        self, welt: dict[str, Any], typ: str, name: str, nutzlast: dict[str, Any], erwartet: list[str]
    ) -> None:
        _s(welt, typ, name, **nutzlast)

        seite = _session_feed()

        assert [(e["operation"], _session_namen(welt)[e["id"]]) for e in seite["data"]] == [
            ("upsert", objekt) for objekt in erwartet
        ]

    def test_ruecknahme_nennt_das_einbettende_objekt_auch_ohne_zeile(self, welt: dict[str, Any]) -> None:
        """Session löscht die Zeile; die Rücknahme nennt das einbettende Objekt (``embedded_in``)."""
        namen, kennung = _session_namen(welt), _k(welt, "anlage")
        welt["anlage"].delete()
        _s_zurueck(welt, "File", kennung, paper="vorlage")

        seite = _session_feed()

        assert [(e["operation"], namen[e["id"]], e.get("reason")) for e in seite["data"]] == [
            ("delete", "anlage", "deleted"),
            ("upsert", "vorlage", None),
        ]

    def test_ruecknahme_ohne_angabe_aus_der_zeile(self, welt: dict[str, Any]) -> None:
        """Ältere Rücknahmen ohne Angabe: Gibt es die Zeile noch (nichtöffentlich gestellt), kennt die Ausgabe es."""
        top = welt["top"]
        top.is_public = False
        top.save()
        _s_zurueck(welt, "AgendaItem", _k(welt, "top"), "nichtoeffentlich")

        namen = _session_namen(welt)
        assert [(e["operation"], namen[e["id"]]) for e in _session_feed()["data"]] == [
            ("delete", "top"),
            ("upsert", "sitzung"),
        ]

    def test_sitzungsort_zurueckgenommen(self, welt: dict[str, Any]) -> None:
        SessionMeeting.objects.filter(pk=welt["sitzung"].pk).update(location="")
        _s_zurueck(welt, "Location", _k(welt, "sitzungsort"))

        namen = _session_namen(welt)
        assert [(e["operation"], namen[e["id"]]) for e in _session_feed()["data"]] == [
            ("delete", "sitzungsort"),
            ("upsert", "sitzung"),
        ]

    def test_nichtoeffentliches_einbettendes_objekt_bleibt_unsichtbar(self, welt: dict[str, Any]) -> None:
        """Nennt ein Ereignis eine nie öffentliche Sitzung, erscheint sie nicht: kein Eintrag, keine Kennung."""
        _s(welt, "ris.agendaitem.changed", "top", meeting="geheim", previous_meeting="sitzung", change="moved")

        seite = _session_feed()

        namen = _session_namen(welt)
        assert [(e["operation"], namen[e["id"]]) for e in seite["data"]] == [("upsert", "top"), ("upsert", "sitzung")]
        assert "GEHEIM" not in json.dumps(seite) and str(welt["geheim"].pk) not in json.dumps(seite)


def test_offene_suche_endet_mit_der_anfrage(welt: dict[str, Any]) -> None:
    """
    Die Suche nach Adressen hält eine offene Abfrage (unter PostgreSQL ein serverseitiger Cursor). Sie darf den Index
    nicht in einem Kreis halten: Sonst schlösse erst die Speicherbereinigung den Cursor, womöglich in einer späteren
    Transaktion, und bräche sie ab.
    """
    abbildung, feed = cast(Any, session_oparl)._feed("musterstadt", profile=True)
    assert feed.addresses("meeting", [_k(welt, "sitzung")])
    index = cast(Any, feed.addresses).__self__
    suche = index._searches["meeting"]
    assert suche.gi_frame is not None, "die Suche ist noch offen"
    verweis = weakref.ref(index)

    gc.disable()
    try:
        del abbildung, feed, index, suche
        assert verweis() is None
    finally:
        gc.enable()


def test_session_abfragen_je_seite_haengen_nicht_an_der_zahl_der_eintraege(welt: dict[str, Any]) -> None:
    tenant, rat = welt["tenant"], welt["rat"]

    def runde(nummer: int) -> None:
        sitzung = SessionMeeting.objects.create(
            tenant=tenant,
            name=f"Sitzung {nummer}",
            organization=rat,
            start=timezone.now(),
            is_public=True,
            location="Saal",
        )
        top = SessionAgendaItem.objects.create(meeting=sitzung, number=str(nummer), name="Punkt", is_public=True)
        datei = SessionFile.objects.create(tenant=tenant, name="Plan", agenda_item=top)
        sitzung_k = canonical_id(_adresse("meeting", sitzung.pk))
        top_k = canonical_id(_adresse("agendaitem", top.pk))
        nutzlast = {"meeting": str(sitzung_k), "change": "changed", "changed": ["name"]}
        ereignis("ris.agendaitem.changed", welt["body"], top_k, nutzlast=nutzlast, mandant=MANDANT)
        ereignis("ris.meeting.changed", welt["body"], sitzung_k, nutzlast={"changed": ["name"]}, mandant=MANDANT)
        datei_k = canonical_id(_adresse("file", datei.pk))
        nutzlast = {"agenda_item": str(top_k), "change": "renamed"}
        ereignis("ris.file.changed", welt["body"], datei_k, nutzlast=nutzlast, mandant=MANDANT)

    adresse = f"{BASIS}body/kommparl/changes/"
    runde(0)
    runde(1)
    _get(adresse)  # einmalige Abfragen (Zwischenspeicher der Installation) zählen nicht
    wenige = _abfragen(adresse)
    eintraege = len(_json(adresse)["data"])
    for nummer in range(2, 8):
        runde(nummer)
    viele = _abfragen(adresse)

    assert len(_json(adresse)["data"]) > 2 * eintraege
    assert viele == wenige


def test_zeit_der_eintraege_ist_die_des_ereignisses(kommune: OParlBody, bestand: dict[str, Any]) -> None:
    zeit = datetime(2026, 9, 2, 10, 30, 15, 123456, tzinfo=UTC)
    ereignis(
        "ris.agendaitem.changed",
        kommune.pk,
        bestand["top"].pk,
        nutzlast={"meeting": str(bestand["sitzung"].pk), "change": "changed", "changed": ["name"]},
        zeit=zeit,
    )
    assert [e["modified"] for e in _feed(kommune)["data"]] == ["2026-09-02T10:30:15+00:00"] * 2
