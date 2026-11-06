# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl-Ausgaben nach kommParl (Issue #1216): übernommene Schemas, Beschreibung, Änderungsfeed und Snapshot nach den
Profilen, ``kommparl:conformsTo`` – hier für den Aggregator; die Session-Schnittstelle prüft
``test_kommparl_session.py``.

- Die Schemas liegen unverändert mit ihrer Herkunft im Repository; Beschreibung und Konformitätsprüfung nutzen sie.
- Körperschaften nennen ``kommparl:changes`` und ``kommparl:snapshot``; ``mandari:changes`` und ``mandari:snapshot``
  bleiben mit ihren Adressen und sind abgekündigt (Beschreibung und Header).
- Feed-Seite, abgelaufener Cursor, Snapshot-Kopf und -Ende bestehen die Prüfung gegen die Profilschemas.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from jsonschema import Draft202012Validator

from apps.events import pruning
from apps.events.models import Event
from hub.api import changes, kommparl
from hub.api.entry import description
from hub.api.openapi import oparl as oparl_openapi
from hub.api.tests.ereignisse import T0
from hub.api.tests.konformitaet import MUSTER, pruefe, pruefe_profil, pruefe_snapshot
from hub.api.tests.test_changes import ereignis, ruecknahme
from insight_core import publication
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

SITE = "https://mandari.example"
API = f"{SITE}/oparl/v1"
RIS = "https://ris.example/oparl"
HEUTE = date(2026, 9, 30)
PAPER = "https://schema.oparl.org/1.1/Paper"


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


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


def _get(adresse: str, **parameter: str) -> Any:
    return Client().get(adresse.removeprefix(SITE), parameter)


def _json(adresse: str, **parameter: str) -> dict[str, Any]:
    antwort = _get(adresse, **parameter)
    assert antwort.status_code == 200, (adresse, antwort.status_code, antwort.content[:500])
    return cast(dict[str, Any], antwort.json())


def _feed(body: OParlBody, **parameter: str) -> dict[str, Any]:
    return _json(f"{API}/body/{body.pk}/kommparl/changes", **parameter)


def _snapshot(adresse: str) -> tuple[Any, list[dict[str, Any]]]:
    antwort = _get(adresse)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    return antwort, [json.loads(zeile) for zeile in inhalt.decode("utf-8").splitlines()]


# =============================================================================
# Übernommene Schemas
# =============================================================================


class TestUebernommeneSchemas:
    def test_unveraendert_mit_herkunft(self) -> None:
        herkunft = kommparl.provenance()
        assert herkunft["repository"] == "https://github.com/mandariOSS/kommParl"
        assert len(herkunft["commit"]) == 40
        assert herkunft["version"] == kommparl.FASSUNG
        assert herkunft["license"] == "CC-BY-SA-4.0"
        assert datetime.fromisoformat(herkunft["commitDate"]).tzinfo is not None
        vorhanden = {
            pfad.relative_to(kommparl.ROOT).as_posix()
            for pfad in kommparl.ROOT.rglob("*.json")
            if pfad.name != kommparl.PROVENANCE
        }
        assert vorhanden == set(herkunft["files"])
        for name, pruefsumme in herkunft["files"].items():
            inhalt = (kommparl.ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            assert hashlib.sha256(inhalt).hexdigest() == pruefsumme, f"{name} weicht vom übernommenen Stand ab"

    def test_alle_objekttypen_und_profile(self) -> None:
        assert set(kommparl.object_types()) == set(oparl_openapi.KINDS.values())
        assert set(kommparl.profile_schemas()) == {
            ("changes", "ChangeList"),
            ("changes", "CursorExpired"),
            ("snapshot", "SnapshotEnd"),
            ("snapshot", "SnapshotHeader"),
            # Profil Einreichung (#1228)
            ("submission", "AttendanceResponse"),
            ("submission", "AttendanceResponseRequest"),
            ("submission", "InvitationReceipt"),
            ("submission", "Problem"),
            ("submission", "Submission"),
            ("submission", "SubmissionList"),
            ("submission", "SubmissionRequest"),
            ("submission", "SubmissionRules"),
            ("submission", "SubmissionUpload"),
            # Profil Nichtöffentliches (#565)
            ("nonpublic", "NonPublicProblem"),
            ("nonpublic", "ProtectedResourceMetadata"),
        }

    def test_lizenz_in_reuse_toml(self) -> None:
        reuse = (Path(__file__).resolve().parents[4] / "REUSE.toml").read_text(encoding="utf-8")
        assert "mandari/hub/api/kommparl/*/schema/*.json" in reuse
        assert 'SPDX-License-Identifier = "CC-BY-SA-4.0"' in reuse
        assert (Path(__file__).resolve().parents[4] / "LICENSES" / "CC-BY-SA-4.0.txt").is_file()


class TestUmwandlung:
    def test_formate_einbettungen_und_typ(self) -> None:
        body = kommparl.type_schema("Body")
        eigenschaften = body["properties"]
        assert eigenschaften["type"] == {"const": "https://schema.oparl.org/1.1/Body"}
        assert eigenschaften["id"] == {"type": "string", "format": "uri"}
        assert eigenschaften["licenseValidSince"] == {"type": "string", "format": "date-time"}
        assert eigenschaften["name"] == {"type": "string", "minLength": 1}
        assert eigenschaften["location"] == {"$ref": "#/components/schemas/OParlLocation"}
        assert eigenschaften["legislativeTerm"] == {
            "type": "array",
            "items": {"$ref": "#/components/schemas/OParlLegislativeTerm"},
        }
        # Externe Listen sind in der Ausgabe URLs
        assert eigenschaften["organization"] == {"type": "string", "format": "uri"}
        assert eigenschaften["kommparl:changes"] == {"type": "string", "format": "uri"}
        assert body["required"] == kommparl.object_schema("Body")["required"]
        assert body["additionalProperties"] is False
        assert kommparl.type_schema("File")["properties"]["date"] == {"type": "string", "format": "date"}
        version = kommparl.type_schema("System")["properties"]["oparlVersion"]
        assert version == {"type": "string", "pattern": "^https://schema\\.oparl\\.org/1\\.(0|1)/$", "minLength": 1}

    def test_alle_typen_sind_json_schema_2020_12(self) -> None:
        for typ in kommparl.object_types():
            Draft202012Validator.check_schema(kommparl.type_schema(typ))

    def test_profile_ohne_eigene_basis_und_ohne_defs(self) -> None:
        for profil, name in kommparl.profile_schemas():
            komponente = kommparl.profile_component(profil, name)
            text = json.dumps(komponente)
            assert "$defs" not in text and '"$id"' not in text and "$schema" not in text
            assert komponente["x-kommparl-schema"] == kommparl.profile_schema(profil, name)["$id"]
            Draft202012Validator.check_schema(komponente)
        liste = kommparl.profile_component("changes", "ChangeList")
        assert liste["properties"]["cursor"]["pattern"] == "^[A-Za-z0-9._~-]{1,512}$"
        assert liste["properties"]["data"]["items"]["required"] == ["cursor", "operation", "type", "id", "modified"]

    def test_unbekanntes_format_faellt_auf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        schemas = {"X": {"title": "X", "required": [], "properties": {"a": {"type": "string", "format": "email"}}}}
        monkeypatch.setattr(kommparl, "_object_schemas", lambda: schemas)
        with pytest.raises(ValueError, match="unbekanntes Format"):
            kommparl.type_schema("X")


# =============================================================================
# Beschreibung
# =============================================================================


@pytest.fixture(scope="module")
def beschreibung() -> dict[str, Any]:
    return description()


class TestBeschreibung:
    def test_schemas_aller_objekttypen_aus_kommparl(self, beschreibung: dict[str, Any]) -> None:
        schemas = beschreibung["components"]["schemas"]
        for typ in kommparl.object_types():
            schema = schemas[f"OParl{typ}"]
            uebernommen = kommparl.type_schema(typ, vendor_property=oparl_openapi.NAMESPACED)["properties"]
            assert set(uebernommen) <= set(schema["properties"]), typ
            assert schema["required"] == kommparl.object_schema(typ)["required"]
        for _profil, name in kommparl.profile_schemas():
            assert f"Kommparl{name}" in schemas
        assert "kommParl" in beschreibung["info"]["description"]
        assert kommparl.provenance()["commit"][:12] in beschreibung["info"]["description"]

    @pytest.mark.parametrize(
        ("alt", "neu"),
        [
            ("/oparl/v1/body/{id}/changes", "/oparl/v1/body/{id}/kommparl/changes"),
            ("/oparl/v1/body/{id}/snapshot", "/oparl/v1/body/{id}/kommparl/snapshot"),
            (
                "/session/{tenant_slug}/api/oparl/body/changes/",
                "/session/{tenant_slug}/api/oparl/body/kommparl/changes/",
            ),
            (
                "/session/{tenant_slug}/api/oparl/body/snapshot/",
                "/session/{tenant_slug}/api/oparl/body/kommparl/snapshot/",
            ),
        ],
    )
    def test_aeltere_form_abgekuendigt_neue_nach_profil(self, beschreibung: dict[str, Any], alt: str, neu: str) -> None:
        abgekuendigt = beschreibung["paths"][alt]["get"]
        assert abgekuendigt["deprecated"] is True
        assert abgekuendigt["x-sunset"] == "2027-12-01"
        assert {"Deprecation", "Sunset", "Link"} <= set(abgekuendigt["responses"]["200"]["headers"])
        nachfolger = beschreibung["paths"][neu]["get"]
        assert "deprecated" not in nachfolger
        if neu.endswith("changes") or neu.endswith("changes/"):
            assert nachfolger["responses"]["200"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/KommparlChangeList"
            }
            assert nachfolger["responses"]["410"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/KommparlCursorExpired"
            }

    def test_abgekuendigte_eigenschaften_am_body(self, beschreibung: dict[str, Any]) -> None:
        eigenschaften = beschreibung["components"]["schemas"]["OParlBody"]["properties"]
        for name in ("mandari:changes", "mandari:snapshot"):
            assert eigenschaften[name]["deprecated"] is True
            assert eigenschaften[name]["x-sunset"] == "2027-12-01"
        assert "deprecated" not in eigenschaften["kommparl:changes"]


# =============================================================================
# Körperschaft und System
# =============================================================================


@pytest.mark.django_db
class TestKoerperschaftUndSystem:
    def test_body_nennt_beide_formen(self, kommune: OParlBody) -> None:
        body = _json(f"{API}/body/{kommune.pk}")

        assert body["kommparl:changes"] == f"{API}/body/{kommune.pk}/kommparl/changes"
        assert body["kommparl:snapshot"] == f"{API}/body/{kommune.pk}/kommparl/snapshot"
        # Abgekündigt, aber mit derselben Adresse wie bisher
        assert body["mandari:changes"] == f"{API}/body/{kommune.pk}/changes"
        assert body["mandari:snapshot"] == f"{API}/body/{kommune.pk}/snapshot"
        assert pruefe(body, "Body") == []
        assert _json(f"{API}/bodies")["data"][0]["kommparl:changes"] == body["kommparl:changes"]

    def test_ausgeschaltet_nennt_der_body_keinen_feed(self, kommune: OParlBody) -> None:
        with override_settings(OPARL_CHANGES_ENABLED=False):
            body = _json(f"{API}/body/{kommune.pk}")
            assert _get(f"{API}/body/{kommune.pk}/kommparl/changes").status_code == 404
            assert _get(f"{API}/body/{kommune.pk}/kommparl/snapshot").status_code == 404
        assert not {"kommparl:changes", "kommparl:snapshot", "mandari:changes"} & set(body)

    def test_conforms_to_an_system_und_einstieg(self, kommune: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        system = _json(f"{API}/system")
        assert system["kommparl:conformsTo"] == [kommparl.VERSION]
        assert _json(f"{SITE}/api/")["kommparl:conformsTo"] == system["kommparl:conformsTo"]
        assert pruefe(system, "System") == []

        # Ein erfüllt erklärtes Profil nennen beide nur, wenn die Installation es anbietet
        monkeypatch.setattr(kommparl, "PROFILES_FULFILLED", (kommparl.PROFILE_CHANGES, kommparl.PROFILE_SNAPSHOT))
        mit = [kommparl.VERSION, kommparl.PROFILE_CHANGES, kommparl.PROFILE_SNAPSHOT]
        assert _json(f"{API}/system")["kommparl:conformsTo"] == mit
        assert _json(f"{SITE}/api/")["kommparl:conformsTo"] == mit
        with override_settings(OPARL_CHANGES_ENABLED=False):
            assert _json(f"{API}/system")["kommparl:conformsTo"] == [kommparl.VERSION]
            assert _json(f"{SITE}/api/")["kommparl:conformsTo"] == [kommparl.VERSION]

    def test_conforms_to_nur_mit_zeitpunkten_nach_der_spezifikation(self, kommune: OParlBody) -> None:
        """
        Kapitel 5: ``kommparl:conformsTo`` nennt nur vollständig erfüllte Kennungen. Die Fassung verlangt Zeitpunkte als
        ``yyyy-mm-ddThh:mm:ss±hh:mm`` (Kapitel „JSON-Ausgabe“, muss): Sekundenbruchteile der eigenen Zeitstempel und
        der Quelle entfallen, und die Prüfung lässt keine Abweichung durch.
        """
        bruchteil = datetime(2026, 9, 30, 8, 15, 10, 123456, tzinfo=UTC)
        OParlBody.objects.filter(pk=kommune.pk).update(updated_at=bruchteil, oparl_created=bruchteil)
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/9", body=kommune, name="Rat", start=bruchteil, oparl_modified=bruchteil
        )

        system = _json(f"{API}/system")
        body = _json(f"{API}/body/{kommune.pk}")
        termin = _json(f"{API}/meeting/{sitzung.pk}")

        assert kommparl.VERSION in system["kommparl:conformsTo"]
        assert (body["created"], body["modified"]) == ("2026-09-30T08:15:10+00:00", "2026-09-30T08:15:10+00:00")
        assert (termin["start"], termin["modified"]) == ("2026-09-30T08:15:10+00:00", "2026-09-30T08:15:10+00:00")
        for objekt, typ in ((system, "System"), (body, "Body"), (termin, "Meeting")):
            assert pruefe(objekt, typ) == [], typ
        # Die Prüfung ist so streng wie die der Spezifikation (Muster wie im Profil Änderungsfeed)
        eintrag = kommparl.profile_component("changes", "ChangeList")["properties"]["data"]["items"]
        assert MUSTER["date-time"] == eintrag["properties"]["modified"]["pattern"]
        for falsch in (
            "2026-09-30T08:15:10.123456+00:00",
            "2026-09-30T08:15:10Z",
            "2026-09-30T08:15+00:00",
            "2026-09-30 08:15:10+00:00",
            "2026-09-30T08:15:10",
        ):
            assert pruefe({**body, "modified": falsch}, "Body"), falsch

    def test_heute_kein_profil_genannt(self) -> None:
        """Muss-Anforderungen des Profils Änderungsfeed sind noch offen („Vollständigkeit“); siehe PROFILES_FULFILLED."""
        assert kommparl.conforms_to(feed_offered=True) == [kommparl.VERSION]

    def test_profile_der_session_ausgabe(self) -> None:
        """Einreichung (#1228) und Nichtöffentliches (#565) nennt nur eine Ausgabe, die sie anbietet, beide zusammen."""
        assert kommparl.conforms_to(feed_offered=False, submission_offered=True) == [
            kommparl.VERSION,
            kommparl.PROFILE_SUBMISSION,
        ]
        assert kommparl.conforms_to(feed_offered=False, nonpublic=True) == [
            kommparl.VERSION,
            kommparl.PROFILE_NONPUBLIC,
        ]
        assert kommparl.conforms_to(feed_offered=True, submission_offered=True, nonpublic=True) == [
            kommparl.VERSION,
            kommparl.PROFILE_SUBMISSION,
            kommparl.PROFILE_NONPUBLIC,
        ]


# =============================================================================
# Änderungsfeed nach dem Profil
# =============================================================================


@pytest.mark.django_db
class TestFeed:
    def test_seite_nach_profil_mit_gruenden_des_profils(self, kommune: OParlBody) -> None:
        vorlage, datei, person, alt, weg = (uuid.uuid4() for _ in range(5))
        ereignis("ris.paper.changed", kommune.pk, vorlage, zeit=T0)
        ruecknahme(kommune.pk, "File", datei, "nichtoeffentlich", zeit=T0 + timedelta(hours=1))
        ruecknahme(kommune.pk, "Person", person, "datenschutz", zeit=T0 + timedelta(hours=2))
        ruecknahme(kommune.pk, "Paper", alt, "zurueckgenommen", zeit=T0 + timedelta(hours=3))
        ereignis("ris.paper.changed", kommune.pk, weg, operation="delete", zeit=T0 + timedelta(hours=4))

        seite = _feed(kommune)

        assert pruefe_profil(seite, "changes", "ChangeList") == []
        assert [(e["operation"], e.get("reason"), e["modified"]) for e in seite["data"]] == [
            ("upsert", None, "2026-09-01T08:00:00+00:00"),
            ("delete", "nonPublic", "2026-09-01T09:00:00+00:00"),
            ("redact", "privacy", "2026-09-01T10:00:00+00:00"),
            ("delete", "withdrawn", "2026-09-01T11:00:00+00:00"),
            ("delete", "deleted", "2026-09-01T12:00:00+00:00"),
        ]
        assert seite["data"][0] == {
            "cursor": seite["data"][0]["cursor"],
            "operation": "upsert",
            "type": PAPER,
            "id": f"{API}/paper/{vorlage}",
            "modified": "2026-09-01T08:00:00+00:00",
        }
        # Letzte Seite: kein next; die Aktualität der Quelle unter eigenem Namensraum
        assert seite["links"] == {"self": f"{API}/body/{kommune.pk}/kommparl/changes"}
        assert seite["cursor"] == seite["data"][-1]["cursor"]
        assert seite["mandari:freshness"] == {"synced_at": None, "full_synced_at": None}

    def test_next_nur_solange_weitere_eintraege(self, kommune: OParlBody) -> None:
        for stunde in range(3):
            ereignis("ris.paper.changed", kommune.pk, zeit=T0 + timedelta(hours=stunde))

        erste = _feed(kommune, limit="2")
        assert len(erste["data"]) == 2
        assert erste["cursor"] == erste["data"][-1]["cursor"]
        assert erste["links"]["next"] == f"{API}/body/{kommune.pk}/kommparl/changes?after={erste['cursor']}&limit=2"
        zweite = _json(erste["links"]["next"])
        assert len(zweite["data"]) == 1 and "next" not in zweite["links"]
        dritte = _feed(kommune, after=zweite["cursor"])
        assert dritte["data"] == []
        assert dritte["links"] == {"self": f"{API}/body/{kommune.pk}/kommparl/changes?after={zweite['cursor']}"}
        for seite in (erste, zweite, dritte):
            assert pruefe_profil(seite, "changes", "ChangeList") == []
        # Genau volle Seite ohne weitere Einträge: kein next
        assert "next" not in _feed(kommune, after=erste["cursor"], limit="1")["links"]

    def test_cursor_gilt_in_beiden_formen(self, kommune: OParlBody) -> None:
        ereignis("ris.paper.changed", kommune.pk)
        alt = _json(f"{API}/body/{kommune.pk}/changes")
        ereignis("ris.paper.changed", kommune.pk)

        neu = _feed(kommune, after=alt["cursor"])
        assert len(neu["data"]) == 1
        assert _json(f"{API}/body/{kommune.pk}/changes", after=neu["cursor"])["data"] == []

    def test_abgelaufener_cursor(self, kommune: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        cursor = _feed(kommune)["cursor"]
        monkeypatch.setattr(changes, "today", lambda: HEUTE + timedelta(days=changes.retention_days() + 1))

        antwort = _get(f"{API}/body/{kommune.pk}/kommparl/changes", after=cursor)

        assert antwort.status_code == 410
        assert antwort["Content-Type"].startswith("application/json")
        assert antwort["Cache-Control"] == "no-store"
        fehler = antwort.json()
        assert pruefe_profil(fehler, "changes", "CursorExpired") == []
        assert fehler["type"] == "https://schema.oparl.org/1.1/Error"
        assert fehler["kommparl:snapshot"] == f"{API}/body/{kommune.pk}/kommparl/snapshot"
        # Ein Cursor einer anderen Kommune ist ebenso unbekannt
        fremd = changes.encode_cursor(uuid.uuid4(), 1, HEUTE)
        monkeypatch.setattr(changes, "today", lambda: HEUTE)
        assert _get(f"{API}/body/{kommune.pk}/kommparl/changes", after=fremd).status_code == 410

    def test_zurueckgenommene_kommune(self, kommune: OParlBody) -> None:
        publication.set_source_state(kommune.source, publication.WITHDRAWN)

        for pfad in ("kommparl/changes", "kommparl/snapshot"):
            antwort = _get(f"{API}/body/{kommune.pk}/{pfad}")
            assert antwort.status_code == 410, pfad
            fehler = antwort.json()
            assert pruefe_profil(fehler, "changes", "CursorExpired") == []
            assert "kommparl:snapshot" not in fehler

    def test_ohne_after_ab_dem_aeltesten_aufbewahrten_eintrag(self, kommune: OParlBody) -> None:
        """
        Profil Änderungsfeed, „Abruf“: Ohne ``after`` beginnt die Antwort beim ältesten Eintrag, den der Server noch
        aufbewahrt – auch wenn das Journal aufgeräumt ist. Die ältere Form antwortet dann weiter mit ``410``.
        """
        erste = ereignis("ris.paper.changed", kommune.pk, zeit=T0)
        zweite = ereignis("ris.paper.changed", kommune.pk, zeit=T0 + timedelta(hours=1))
        Event.objects.filter(pk=erste.pk).delete()
        pruning.record(erste.seq or 0, datetime(2026, 6, 1, tzinfo=UTC))

        seite = _feed(kommune)

        assert pruefe_profil(seite, "changes", "ChangeList") == []
        assert [eintrag["id"] for eintrag in seite["data"]] == [f"{API}/paper/{zweite.aggregate_id}"]
        assert _feed(kommune, after=seite["cursor"])["data"] == []
        assert _get(f"{API}/body/{kommune.pk}/changes").status_code == 410

    def test_ungueltige_parameter(self, kommune: OParlBody) -> None:
        assert _get(f"{API}/body/{kommune.pk}/kommparl/changes", after="kein-cursor").status_code == 400
        assert _get(f"{API}/body/{kommune.pk}/kommparl/changes", limit="0").status_code == 400


# =============================================================================
# Snapshot nach dem Profil
# =============================================================================


@pytest.fixture
def bestand(kommune: OParlBody) -> dict[str, Any]:
    """Je Objekttyp ein Objekt der Kommune, dazu Gelöschtes, das nicht in den Snapshot gehört."""
    OParlLegislativeTerm.objects.create(external_id=f"{RIS}/term/1", body=kommune, name="2025–2030")
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/1", body=kommune, name="Rat")
    person = OParlPerson.objects.create(external_id=f"{RIS}/person/1", body=kommune, name="Petra Muster")
    OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=rat)
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1",
        body=kommune,
        name="Rat",
        start=datetime(2026, 10, 1, 17, tzinfo=UTC),
        location_name="Rathaus",
    )
    OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/1", meeting=sitzung, name="Eröffnung", order=0)
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=kommune, name="Radweg", reference="V/1")
    OParlConsultation.objects.create(external_id=f"{RIS}/consultation/1", body=kommune, paper=vorlage, role="Beschluss")
    OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=kommune,
        paper=vorlage,
        name="Begründung",
        access_url=f"{RIS}/file/1/download",
        text_content="GEHEIMER-VOLLTEXT",
    )
    OParlLocation.objects.create(external_id=f"{RIS}/location/1", body=kommune, description="Bürgerhaus")
    OParlPaper.objects.create(external_id=f"{RIS}/paper/2", body=kommune, name="Alt", deleted=True)
    return {"sitzung": sitzung}


@pytest.mark.django_db
class TestSnapshot:
    def test_kopf_objekte_und_ende_nach_profil(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        antwort, zeilen = _snapshot(f"{API}/body/{kommune.pk}/kommparl/snapshot")

        assert antwort["Content-Type"] == "application/x-ndjson; charset=utf-8"
        assert pruefe_snapshot(zeilen, body=f"{API}/body/{kommune.pk}") == []
        kopf, ende = zeilen[0], zeilen[-1]
        assert set(kopf) == {"type", "body", "cursor", "generated"}
        assert kopf["cursor"] == antwort["Snapshot-Cursor"]
        assert kopf["generated"].endswith("+00:00") and "." not in kopf["generated"]
        assert ende == {"type": kommparl.SNAPSHOT_END, "count": len(zeilen) - 2}
        laenge = sum(len(json.dumps(zeile, ensure_ascii=False, separators=(",", ":")).encode()) + 1 for zeile in zeilen)
        assert int(antwort["Content-Length"]) == laenge

    def test_jedes_objekt_in_eigener_zeile(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        _, zeilen = _snapshot(f"{API}/body/{kommune.pk}/kommparl/snapshot")
        objekte = zeilen[1:-1]
        typen = sorted(objekt["type"].rsplit("/", 1)[1] for objekt in objekte)

        assert set(typen) == set(kommparl.object_types()) - {"System"}
        # Zwei Orte: das eigene Objekt der Quelle und der Sitzungsort, der nur als Text vorlag
        orte = [objekt["id"] for objekt in objekte if objekt["type"].endswith("/Location")]
        assert f"{API}/location/{bestand['sitzung'].pk}" in orte and len(orte) == 2
        # Jedes Objekt wie unter seiner Adresse; Dateien ohne Inhalt (auch ohne erkannten Text)
        for objekt in objekte:
            if not objekt["type"].endswith("/File"):
                assert objekt == _json(objekt["id"]), objekt["id"]
        datei = next(objekt for objekt in objekte if objekt["type"].endswith("/File"))
        assert "text" not in datei
        text = "\n".join(json.dumps(zeile) for zeile in zeilen)
        assert "GEHEIMER-VOLLTEXT" not in text and f"{RIS}/paper/2" not in text

    def test_feed_setzt_am_cursor_des_kopfes_fort(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        ereignis("ris.paper.changed", kommune.pk)
        _, zeilen = _snapshot(f"{API}/body/{kommune.pk}/kommparl/snapshot")
        assert _feed(kommune, after=zeilen[0]["cursor"])["data"] == []
        ereignis("ris.paper.changed", kommune.pk)
        assert len(_feed(kommune, after=zeilen[0]["cursor"])["data"]) == 1

    def test_geloeschter_body_hat_keinen_snapshot(self, kommune: OParlBody, bestand: dict[str, Any]) -> None:
        """Profil Snapshot: Gelöschte Objekte sind nicht enthalten. Ist der Body selbst gelöscht, gibt es keinen."""
        ereignis("ris.body.changed", kommune.pk, kommune.pk, operation="delete")
        OParlBody.objects.filter(pk=kommune.pk).update(deleted=True)

        antwort = _get(f"{API}/body/{kommune.pk}/kommparl/snapshot")

        assert antwort.status_code == 410
        assert antwort["Cache-Control"] == "no-store"
        fehler = antwort.json()
        assert pruefe_profil(fehler, "changes", "CursorExpired") == []
        assert (fehler["type"], fehler["debug"]) == (kommparl.OPARL_ERROR, "kommune-geloescht")
        # Der Feed nennt die Löschung weiter; die ältere Form bleibt, wie sie war
        assert [e["operation"] for e in _feed(kommune)["data"]] == ["delete"]
        assert _get(f"{API}/body/{kommune.pk}/snapshot").status_code == 200

    def test_head_ohne_bestand(self, kommune: OParlBody) -> None:
        antwort = Client().head(f"/oparl/v1/body/{kommune.pk}/kommparl/snapshot")
        assert antwort.status_code == 200 and antwort["Snapshot-Cursor"]


# =============================================================================
# Ältere Form: unverändert, abgekündigt
# =============================================================================


@pytest.mark.django_db
class TestAeltereForm:
    def test_feed_und_snapshot_tragen_die_abkuendigung(self, kommune: OParlBody) -> None:
        for alt, neu in (("changes", "kommparl/changes"), ("snapshot", "kommparl/snapshot")):
            antwort = _get(f"{API}/body/{kommune.pk}/{alt}")
            assert antwort.status_code == 200
            assert antwort["Deprecation"] == "@1793836800"  # 05.11.2026
            assert antwort["Sunset"] == "Wed, 01 Dec 2027 00:00:00 GMT"
            assert antwort["Link"] == f'<{API}/body/{kommune.pk}/{neu}>; rel="successor-version"'
            assert {"Deprecation", "Sunset", "Link"} <= {
                teil.strip() for teil in antwort["Access-Control-Expose-Headers"].split(",")
            }
        nachfolger = _get(f"{API}/body/{kommune.pk}/kommparl/changes")
        assert "Deprecation" not in nachfolger and "Sunset" not in nachfolger

    def test_auch_bei_304_und_410(self, kommune: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
        erste = _get(f"{API}/body/{kommune.pk}/changes")
        zweite = Client().get(f"/oparl/v1/body/{kommune.pk}/changes", headers={"If-None-Match": erste["ETag"]})
        assert zweite.status_code == 304
        assert zweite["Deprecation"] == erste["Deprecation"] and zweite["Sunset"] == erste["Sunset"]
        monkeypatch.setattr(changes, "today", lambda: HEUTE + timedelta(days=changes.retention_days() + 1))
        abgelaufen = _get(f"{API}/body/{kommune.pk}/changes", after=erste.json()["cursor"])
        assert abgelaufen.status_code == 410 and abgelaufen["Sunset"]
        # Die ältere Form bleibt, wie sie war: Problem nach RFC 9457 mit snapshot
        assert abgelaufen.json()["snapshot"] == f"{API}/body/{kommune.pk}/snapshot"

    def test_nicht_vorhandene_adresse_ohne_abkuendigung(self, kommune: OParlBody) -> None:
        with override_settings(OPARL_CHANGES_ENABLED=False):
            antwort = _get(f"{API}/body/{kommune.pk}/changes")
        assert antwort.status_code == 404 and "Deprecation" not in antwort


# =============================================================================
# Antworten passen zur Beschreibung
# =============================================================================


@pytest.mark.django_db
def test_antworten_passen_zur_beschreibung(kommune: OParlBody) -> None:
    dokument = description()
    ereignis("ris.paper.changed", kommune.pk)

    def schema(pfad: str, status: str = "200") -> dict[str, Any]:
        inhalt = dokument["paths"][pfad]["get"]["responses"][status]["content"]["application/json"]["schema"]
        return {**inhalt, "components": dokument["components"]}

    Draft202012Validator(schema("/oparl/v1/body/{id}/kommparl/changes")).validate(_feed(kommune))
    Draft202012Validator(schema("/oparl/v1/system")).validate(_json(f"{API}/system"))
    Draft202012Validator(schema("/oparl/v1/body/{id}")).validate(_json(f"{API}/body/{kommune.pk}"))
