# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine Serialisierung für beide Ausgaben der offenen Schnittstelle (Issue #561).

Aggregator (``/oparl/v1/``) und Session-Schnittstelle (``/session/<slug>/api/oparl/``) bilden ihre
Objekte mit ``hub.ris.mapping`` ab und geben sie mit ``hub.api`` aus. Geprüft wird

- der Aufbau: keine eigene Ausgabe-Logik in den Endpunkten, das alte Paket ``oparl_api`` gibt es nicht
  mehr, beide Abbildungen bieten dieselben Objekttypen;
- das Verhalten: Listen-Hülle, Blättern, Zeitfilter, gekürzte Objekte, Fehler und bedingte Anfragen
  sind in beiden Ausgaben gleich.
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.session.api import oparl as session_ausgabe
from apps.session.models import SessionMeeting, SessionOrganization, SessionTenant
from hub.api import aggregator, http, serialization
from hub.ris.mapping.bestand import BestandMapping
from hub.ris.mapping.session import SessionMapping
from insight_core.models import OParlBody, OParlMeeting, OParlSource

SITE = "https://mandari.example"
RIS = "https://ris.example/oparl"
SESSION = "/session/musterstadt/api/oparl"

#: Je Objekttyp des kanonischen Modells eine Methode – in beiden Abbildungen
OBJEKTTYPEN = (
    "system",
    "body",
    "legislative_term",
    "organization",
    "person",
    "membership",
    "meeting",
    "location",
    "agenda_item",
    "paper",
    "consultation",
    "file",
    "file_with_text",
    "tombstone",
)


# =============================================================================
# Aufbau
# =============================================================================


def test_das_alte_paket_ist_in_der_drehscheibe_aufgegangen() -> None:
    assert importlib.util.find_spec("oparl_api") is None
    assert importlib.util.find_spec("hub.api.serialization") is not None


def test_beide_ausgaben_nutzen_dieselbe_serialisierung() -> None:
    # Dieselben Funktionsobjekte, keine Kopien
    for name, herkunft in (
        ("list_response", serialization),
        ("TimeFilters", serialization),
        ("json_response", http),
        ("error_response", http),
        ("endpoint", http),
    ):
        eine = vars(herkunft)[name]
        assert vars(session_ausgabe)[name] is eine, name
        assert vars(aggregator)[name] is eine, name


def _importe(modul: Any) -> set[str]:
    baum = ast.parse(Path(cast(str, modul.__file__)).read_text(encoding="utf-8"))
    namen: set[str] = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            namen |= {alias.name for alias in knoten.names}
        elif isinstance(knoten, ast.ImportFrom) and knoten.module:
            namen.add(knoten.module)
    return namen


@pytest.mark.parametrize("modul", [aggregator, session_ausgabe], ids=["aggregator", "session"])
def test_endpunkte_geben_nicht_selbst_aus(modul: Any) -> None:
    """Blättern, JSON, ETag und Zeitstempel-Parsing stehen nur in ``hub.api``."""
    importe = _importe(modul)

    assert not importe & {"json", "hashlib", "django.core.paginator", "django.utils.cache", "urllib.parse"}
    quelltext = Path(cast(str, modul.__file__)).read_text(encoding="utf-8")
    for eigenes in ("Paginator(", "fromisoformat", '"pagination"', '"links"', "ETag"):
        assert eigenes not in quelltext.split('"""', 2)[2], eigenes


def test_die_drehscheibe_kennt_das_fachmodul_nicht() -> None:
    for modul in (aggregator, http, serialization):
        assert not {name for name in _importe(modul) if name.startswith("apps.session")}


@pytest.mark.parametrize("typ", OBJEKTTYPEN)
def test_beide_abbildungen_bieten_jeden_objekttyp(typ: str) -> None:
    assert callable(getattr(BestandMapping, typ)), typ
    assert callable(getattr(SessionMapping, typ)), typ


def test_beide_abbildungen_nennen_adressen_auf_dieselbe_weise() -> None:
    bestand = BestandMapping(f"{SITE}/oparl", SITE).uris
    session = SessionMapping(None, f"{SITE}{SESSION}/", session_ausgabe.SOURCE).uris

    for uris in (bestand, session):
        assert uris.system().startswith(SITE) and uris.bodies().startswith(SITE)
        assert uris.obj("paper", 7).startswith(SITE) and "/paper/7" in uris.obj("paper", 7)


# =============================================================================
# Verhalten: beide Ausgaben mit je drei Sitzungen, zwei je Seite
# =============================================================================


@pytest.fixture
def welt(db: None) -> Any:
    cache.clear()
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_PAGE_SIZE=2,
        OPARL_API_CACHE_SECONDS=0,
    ):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
        bestand = [
            OParlMeeting.objects.create(external_id=f"{RIS}/meeting/{nummer}", body=body, name=f"Sitzung {nummer}")
            for nummer in range(4)
        ]
        bestand[3].mark_deleted()

        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        sitzungen = [
            SessionMeeting.objects.create(
                tenant=tenant,
                name=f"Sitzung {nummer}",
                organization=rat,
                start=timezone.now() + timedelta(days=nummer),
                is_public=True,
            )
            for nummer in range(4)
        ]
        # Rücknahme: hinterlässt in Session einen Eintrag für Gelöschtes
        sitzungen[3].is_public = False
        sitzungen[3].save()
        yield {
            "aggregator": f"/oparl/v1/body/{body.pk}/meetings",
            "session": f"{SESSION}/meetings/",
            "objekt": {
                "aggregator": f"/oparl/v1/meeting/{bestand[0].pk}",
                "session": f"{SESSION}/meeting/{sitzungen[0].pk}/",
            },
            "geloescht": {
                "aggregator": f"/oparl/v1/meeting/{bestand[3].pk}",
                "session": f"{SESSION}/meeting/{sitzungen[3].pk}/",
            },
        }
    cache.clear()


AUSGABEN = ("aggregator", "session")


def _json(antwort: Any) -> dict[str, Any]:
    return cast(dict[str, Any], antwort.json())


@pytest.mark.parametrize("ausgabe", AUSGABEN)
def test_liste_hat_dieselbe_huelle(welt: dict[str, Any], ausgabe: str) -> None:
    adresse = f"{SITE}{welt[ausgabe]}"
    trenner = "&" if "?" in adresse else "?"
    antwort = Client().get(welt[ausgabe])
    daten = _json(antwort)

    assert antwort.status_code == 200
    assert list(daten) == ["data", "pagination", "links"]
    assert daten["pagination"] == {"totalElements": 3, "elementsPerPage": 2, "currentPage": 1, "totalPages": 2}
    assert daten["links"] == {
        "first": adresse,
        "self": adresse,
        "next": f"{adresse}{trenner}page=2",
        "last": f"{adresse}{trenner}page=2",
    }
    assert antwort["Link"] == (
        f'<{adresse}>; rel="first", <{adresse}{trenner}page=2>; rel="next", <{adresse}{trenner}page=2>; rel="last"'
    )
    assert antwort["Content-Type"] == "application/json; charset=utf-8"
    assert antwort["Access-Control-Allow-Origin"] == "*"
    assert antwort["Cache-Control"] == "no-cache" and antwort["ETag"]
    assert len(daten["data"]) == 2 and not any(eintrag.get("deleted") for eintrag in daten["data"])


@pytest.mark.parametrize("ausgabe", AUSGABEN)
def test_inkrementelle_liste_enthaelt_geloeschtes_als_gekuerztes_objekt(welt: dict[str, Any], ausgabe: str) -> None:
    zeit = "2000-01-01T00:00:00Z"
    erste = _json(Client().get(welt[ausgabe], {"modified_since": zeit}))
    zweite = _json(Client().get(welt[ausgabe], {"modified_since": zeit, "page": "2"}))

    assert erste["pagination"]["totalElements"] == 4
    # Der Filter bleibt in den Blätter-Links erhalten
    assert erste["links"]["next"] == f"{SITE}{welt[ausgabe]}?modified_since=2000-01-01T00%3A00%3A00Z&page=2"
    eintraege = erste["data"] + zweite["data"]
    gekuerzt = [eintrag for eintrag in eintraege if eintrag.get("deleted")]
    assert len(eintraege) == 4 and len(gekuerzt) == 1
    assert list(gekuerzt[0]) == ["id", "type", "created", "modified", "deleted"]
    assert gekuerzt[0]["type"] == "https://schema.oparl.org/1.1/Meeting"
    assert gekuerzt[0]["id"] == f"{SITE}{welt['geloescht'][ausgabe]}"
    # Unter seiner Adresse bleibt es abrufbar – als dasselbe gekürzte Objekt
    assert _json(Client().get(welt["geloescht"][ausgabe])) == gekuerzt[0]


def test_fehler_sind_in_beiden_ausgaben_gleich(welt: dict[str, Any]) -> None:
    faelle = [
        ({"modified_since": "2026-09-01T10:00:00"}, 400),
        ({"created_until": "gestern"}, 400),
        ({"page": "0"}, 400),
        ({"page": "abc"}, 400),
        ({"page": "9"}, 404),
    ]
    for parameter, status in faelle:
        antworten = [Client().get(welt[ausgabe], parameter) for ausgabe in AUSGABEN]
        assert [antwort.status_code for antwort in antworten] == [status, status], parameter
        assert _json(antworten[0]) == _json(antworten[1]), parameter
        assert set(_json(antworten[0])) == {"error", "status"}
        assert "ETag" not in antworten[0] and "ETag" not in antworten[1]


@pytest.mark.parametrize("ausgabe", AUSGABEN)
def test_bedingte_anfrage_und_lesende_methoden(welt: dict[str, Any], ausgabe: str) -> None:
    for adresse in (welt[ausgabe], welt["objekt"][ausgabe]):
        erste = Client().get(adresse)
        unveraendert = Client().get(adresse, headers={"If-None-Match": erste["ETag"]})

        assert unveraendert.status_code == 304 and unveraendert.content == b""
        assert unveraendert["ETag"] == erste["ETag"]
        assert unveraendert["Access-Control-Allow-Origin"] == "*"
        assert Client().post(adresse).status_code == 405
        vorab = Client().options(adresse)
        assert vorab.status_code == 204 and vorab["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"


def test_ratenbegrenzung_gilt_fuer_beide_ausgaben(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    # feste Uhr: Der Zähler gilt je Minute, der Test soll nicht über eine Minutengrenze laufen
    monkeypatch.setattr("hub.api.http.time.time", lambda: 1_790_000_000.0)
    with override_settings(OPARL_API_RATE_LIMIT=2):
        cache.clear()
        stati = [Client().get(welt[ausgabe]).status_code for ausgabe in (*AUSGABEN, *AUSGABEN)]

    # ein Zähler je Adresse des Abnehmers über die ganze Schnittstelle
    assert stati == [200, 200, 429, 429]
