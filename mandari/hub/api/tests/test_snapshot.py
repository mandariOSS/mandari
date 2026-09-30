# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Snapshot einer Kommune mit Cursor-Übergabe (Issue #563, ``docs/adr/20260929-aenderungsfeed-format.md``).

- NDJSON: erste Zeile mit dem ``snapshot_cursor``, danach dieselben Objekte wie die externen Listen
- Übergabe ohne Verlust: Der Cursor wird vor dem Lesen festgehalten. Ein Abnehmer, der den Snapshot lädt
  und danach den Feed ab dem Cursor liest, hat am Ende denselben Stand wie die Schnittstelle – auch wenn
  sich während des Snapshots etwas ändert.
- Betrieb: nur die Anfrage liest die Datenbank, begrenzte Zahl gleichzeitiger Snapshots, Übertragung in
  Blöcken (auch unter ASGI)
"""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.db import connection
from django.test import AsyncClient, AsyncRequestFactory, Client, override_settings
from django.test.utils import CaptureQueriesContext

from hub.api import aggregator, changes, snapshot
from hub.api.tests.ereignisse import ereignis, ruecknahme
from hub.api.tests.konformitaet import pruefe
from insight_core import publication
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
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
HEUTE = date(2026, 9, 30)
LISTEN = ("organizations", "people", "meetings", "papers", "locations")


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


@pytest.fixture
def welt() -> Any:
    cache.clear()
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_CACHE_SECONDS=0,
        OPARL_CHANGES_ENABLED=True,
    ):
        source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
        body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
        rat = OParlOrganization.objects.create(external_id=f"{RIS}/organization/1", body=body, name="Rat")
        person = OParlPerson.objects.create(external_id=f"{RIS}/person/1", body=body, family_name="Muster")
        OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=rat)
        ort = OParlLocation.objects.create(external_id=f"{RIS}/location/1", body=body, description="Rathaus")
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/1", body=body, name="Ratssitzung", raw_json={"location": ort.external_id}
        )
        top = OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/1", meeting=sitzung, name="Radweg", order=1)
        vorlagen = [
            OParlPaper.objects.create(external_id=f"{RIS}/paper/{nummer}", body=body, name=f"Vorlage {nummer}")
            for nummer in range(1, 4)
        ]
        OParlFile.objects.create(external_id=f"{RIS}/file/1", body=body, paper=vorlagen[0], name="Begründung")
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/1",
            body=body,
            paper=vorlagen[0],
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
        )
        geloescht = OParlPaper.objects.create(external_id=f"{RIS}/paper/9", body=body, name="Gelöscht")
        geloescht.mark_deleted()
        # Eine andere Kommune: nichts von ihr gehört in den Snapshot
        andere = OParlBody.objects.create(external_id=f"{RIS}/body/2", source=source, name="Nachbarstadt")
        OParlPaper.objects.create(external_id=f"{RIS}/paper/fremd", body=andere, name="Fremde Vorlage")
        yield {"body": body, "vorlagen": vorlagen, "geloescht": geloescht, "sitzung": sitzung}
    cache.clear()


def _pfad(welt: dict[str, Any]) -> str:
    return f"/oparl/v1/body/{welt['body'].pk}/snapshot"


def _laden(pfad: str) -> tuple[Any, list[dict[str, Any]]]:
    antwort = Client().get(pfad)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    assert inhalt.endswith(b"\n")
    return antwort, [json.loads(zeile) for zeile in inhalt.decode("utf-8").splitlines()]


def _json(adresse: str) -> dict[str, Any]:
    antwort = Client().get(adresse.removeprefix(SITE))
    assert antwort.status_code == 200, (adresse, antwort.status_code)
    return cast(dict[str, Any], antwort.json())


def _listen(welt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Stand der Schnittstelle: der Body und alle Objekte der externen Listen, nach Adresse."""
    body = _json(f"{API}/body/{welt['body'].pk}")
    stand = {body["id"]: body}
    for segment in LISTEN:
        adresse: str | None = f"{API}/body/{welt['body'].pk}/{segment}"
        while adresse:
            seite = _json(adresse)
            stand.update({objekt["id"]: objekt for objekt in seite["data"]})
            adresse = seite["links"].get("next")
    return stand


# =============================================================================
# Form
# =============================================================================


def test_snapshot_ist_ndjson_mit_dem_cursor_in_der_ersten_zeile(welt: dict[str, Any]) -> None:
    antwort = Client().get(_pfad(welt))
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    zeilen = [json.loads(zeile) for zeile in inhalt.decode("utf-8").splitlines()]

    assert antwort.status_code == 200
    assert antwort["Content-Type"] == "application/x-ndjson; charset=utf-8"
    assert antwort["Content-Length"] == str(len(inhalt))
    assert antwort["Cache-Control"] == "no-store" and "ETag" not in antwort
    assert antwort["Access-Control-Allow-Origin"] == "*"
    assert "Snapshot-Cursor" in antwort["Access-Control-Expose-Headers"]
    kopf = zeilen[0]
    assert set(kopf) == {"snapshot_cursor", "body", "changes", "created"}
    assert kopf["snapshot_cursor"] == antwort["Snapshot-Cursor"]
    assert kopf["body"] == f"{API}/body/{welt['body'].pk}"
    assert kopf["changes"] == f"{API}/body/{welt['body'].pk}/changes?after={kopf['snapshot_cursor']}"
    assert kopf["created"].endswith("+00:00")
    # Jede weitere Zeile ist ein Objekt mit Adresse und Typ
    assert all(
        zeile["id"].startswith(API) and zeile["type"].startswith("https://schema.oparl.org/1.1/")
        for zeile in zeilen[1:]
    )


def test_snapshot_enthaelt_dieselben_objekte_wie_die_listen(welt: dict[str, Any]) -> None:
    _, zeilen = _laden(_pfad(welt))
    objekte = zeilen[1:]

    assert objekte[0] == _json(f"{API}/body/{welt['body'].pk}")
    assert {objekt["id"]: objekt for objekt in objekte} == _listen(welt)
    assert len({objekt["id"] for objekt in objekte}) == len(objekte), "jedes Objekt genau einmal"
    assert len(objekte) == 1 + 1 + 1 + 1 + 3 + 1  # Body, Gremium, Person, Sitzung, drei Vorlagen, Ort
    # Gelöschtes und Fremdes gehören nicht zum Gesamtstand
    text = json.dumps(objekte, ensure_ascii=False)
    assert "Gelöscht" not in text and "Fremde Vorlage" not in text and '"deleted"' not in text
    # Eingebettet wie in den Listen
    sitzung = next(objekt for objekt in objekte if objekt["type"].endswith("/Meeting"))
    assert sitzung["agendaItem"][0]["name"] == "Radweg" and sitzung["location"]["description"] == "Rathaus"
    typen = {"Body", "Organization", "Person", "Meeting", "Paper", "Location"}
    for objekt in objekte:
        typ = objekt["type"].rsplit("/", 1)[1]
        assert typ in typen and pruefe(objekt, typ) == []


def test_viele_objekte_werden_seitenweise_gelesen(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    for nummer in range(10, 17):
        OParlPaper.objects.create(external_id=f"{RIS}/paper/{nummer}", body=welt["body"], name=f"Vorlage {nummer}")
    erwartet = _listen(welt)

    def abfragen(seite: int) -> tuple[int, list[dict[str, Any]]]:
        monkeypatch.setattr(snapshot, "PAGE_SIZE", seite)
        with CaptureQueriesContext(connection) as erfasst:
            _, zeilen = _laden(_pfad(welt))
        return len(erfasst), zeilen[1:]

    klein, objekte = abfragen(3)
    gross, dieselben = abfragen(200)

    assert {objekt["id"]: objekt for objekt in objekte} == erwartet
    assert len(objekte) == len(erwartet) == len(dieselben), "kein Objekt doppelt, keines verloren"
    # Abfragen je Seite, nicht je Objekt: zehn Vorlagen in vier statt einer Seite, je Seite die Vorlagen
    # und ihre vorgeladenen Beziehungen
    assert gross < klein <= gross + 3 * 4


# =============================================================================
# Übergabe an den Feed
# =============================================================================


def _vorlage_aendern(welt: dict[str, Any], vorlage: OParlPaper, name: str) -> None:
    """Eine Änderung, wie sie ein Erzeuger schreibt: Bestand und Ereignis zusammen."""
    OParlPaper.objects.filter(pk=vorlage.pk).update(name=name)
    ereignis("ris.paper.changed", welt["body"].pk, vorlage.pk)


def _abnehmer(zeilen: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Ein Abnehmer: lädt den Snapshot, liest danach den Feed ab dem Cursor bis zur leeren Seite und holt
    jedes gemeldete Objekt neu.
    """
    stand = {objekt["id"]: objekt for objekt in zeilen[1:]}
    adresse = zeilen[0]["changes"]
    while True:
        seite = _json(adresse)
        if not seite["data"]:
            return stand
        for eintrag in seite["data"]:
            if eintrag["operation"] == "upsert":
                stand[eintrag["id"]] = _json(eintrag["id"])
            else:
                stand.pop(eintrag["id"], None)
        adresse = seite["links"]["next"]


def test_cursor_setzt_den_feed_nahtlos_fort(welt: dict[str, Any]) -> None:
    for vorlage in welt["vorlagen"]:
        ereignis("ris.paper.changed", welt["body"].pk, vorlage.pk)
    ereignis("ris.paper.changed", welt["body"].pk, sichtbarkeit="nichtoeffentlich")

    _, zeilen = _laden(_pfad(welt))
    cursor = zeilen[0]["snapshot_cursor"]

    # Stand des Feeds beim Snapshot: das neueste öffentliche Ereignis der Kommune, heute ausgegeben
    assert changes.decode_cursor(welt["body"].pk, cursor) == changes.Cursor(seq=3, day=HEUTE)
    assert _json(zeilen[0]["changes"])["data"] == []
    _vorlage_aendern(welt, welt["vorlagen"][0], "Vorlage 1 (neu)")
    (eintrag,) = _json(zeilen[0]["changes"])["data"]
    assert eintrag["id"] == f"{API}/paper/{welt['vorlagen'][0].pk}"


def test_ohne_ereignisse_beginnt_der_feed_am_anfang(welt: dict[str, Any]) -> None:
    _, zeilen = _laden(_pfad(welt))

    assert changes.decode_cursor(welt["body"].pk, zeilen[0]["snapshot_cursor"]).seq == 0
    assert _abnehmer(zeilen) == _listen(welt)


def test_aenderung_nach_dem_festhalten_des_cursors_geht_nicht_verloren(
    welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zwischen Cursor und Lesen: Der Snapshot enthält die Änderung schon, der Feed meldet sie trotzdem."""
    vorlage = welt["vorlagen"][0]
    stand_lesen = changes.head

    def stand_dann_aenderung(body_id: uuid.UUID) -> int:
        stand = stand_lesen(body_id)
        _vorlage_aendern(welt, vorlage, "Geändert vor dem Lesen")
        return stand

    monkeypatch.setattr(changes, "head", stand_dann_aenderung)
    _, zeilen = _laden(_pfad(welt))
    monkeypatch.setattr(changes, "head", stand_lesen)

    im_snapshot = next(objekt for objekt in zeilen[1:] if objekt["id"].endswith(str(vorlage.pk)))
    assert im_snapshot["name"] == "Geändert vor dem Lesen"
    (eintrag,) = _json(zeilen[0]["changes"])["data"]
    assert eintrag["id"] == im_snapshot["id"], "die Änderung steht hinter dem Cursor"
    assert _abnehmer(zeilen) == _listen(welt)


def test_aenderung_waehrend_des_lesens_geht_nicht_verloren(
    welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Nach dem Lesen des Objekts, vor dem Ende des Snapshots: Der Snapshot enthält den alten Stand, der
    Feed meldet die Änderung hinter dem Cursor – der Abnehmer hat am Ende den neuen.
    """
    geaendert, entfernt = welt["vorlagen"][0], welt["vorlagen"][1]
    lesen = snapshot.objects

    def lesen_dann_aenderung(abzug: snapshot.Snapshot) -> Iterator[dict[str, Any]]:
        yield from lesen(abzug)
        _vorlage_aendern(welt, geaendert, "Geändert während des Snapshots")
        entfernt.mark_deleted()
        ruecknahme(welt["body"].pk, "Paper", entfernt.pk, "quelle_geloescht")
        OParlPaper.objects.create(external_id=f"{RIS}/paper/neu", body=welt["body"], name="Neu während des Snapshots")
        ereignis("ris.paper.released", welt["body"].pk, OParlPaper.objects.get(external_id=f"{RIS}/paper/neu").pk)

    monkeypatch.setattr(snapshot, "objects", lesen_dann_aenderung)
    _, zeilen = _laden(_pfad(welt))
    monkeypatch.setattr(snapshot, "objects", lesen)

    namen = {objekt["id"]: objekt.get("name") for objekt in zeilen[1:]}
    assert namen[f"{API}/paper/{geaendert.pk}"] == "Vorlage 1", "der Snapshot hat den Stand vor der Änderung"
    assert f"{API}/paper/{entfernt.pk}" in namen
    assert "Neu während des Snapshots" not in namen.values()
    # Alle drei Änderungen stehen hinter dem Cursor
    gemeldet = [(eintrag["operation"], eintrag["id"]) for eintrag in _json(zeilen[0]["changes"])["data"]]
    assert gemeldet == [
        ("upsert", f"{API}/paper/{geaendert.pk}"),
        ("delete", f"{API}/paper/{entfernt.pk}"),
        ("upsert", f"{API}/paper/{OParlPaper.objects.get(name='Neu während des Snapshots').pk}"),
    ]
    stand = _abnehmer(zeilen)
    assert stand == _listen(welt)
    assert stand[f"{API}/paper/{geaendert.pk}"]["name"] == "Geändert während des Snapshots"
    assert f"{API}/paper/{entfernt.pk}" not in stand


def test_abgelaufener_cursor_fuehrt_ueber_den_snapshot_zurueck_in_den_feed(welt: dict[str, Any]) -> None:
    from datetime import timedelta

    ereignis("ris.paper.changed", welt["body"].pk, welt["vorlagen"][0].pk)
    zu_alt = changes.encode_cursor(welt["body"].pk, 0, HEUTE - timedelta(days=changes.retention_days() + 1))

    abgelaufen = Client().get(f"/oparl/v1/body/{welt['body'].pk}/changes", {"after": zu_alt})

    assert abgelaufen.status_code == 410
    _, zeilen = _laden(abgelaufen.json()["snapshot"].removeprefix(SITE))
    assert _json(zeilen[0]["changes"])["data"] == []
    assert _abnehmer(zeilen) == _listen(welt)


# =============================================================================
# Schalter, Adressen, Fehler
# =============================================================================


def test_ausgeschaltet_gibt_es_den_snapshot_nicht(welt: dict[str, Any]) -> None:
    with override_settings(OPARL_CHANGES_ENABLED=False):
        antwort = Client().get(_pfad(welt))
        unbekannt = Client().get(f"/oparl/v1/body/{welt['body'].pk}/quatsch")

        assert antwort.status_code == 404
        assert antwort.json()["error"] == unbekannt.json()["error"].replace("quatsch", "snapshot")
        assert "mandari:snapshot" not in _json(f"{API}/body/{welt['body'].pk}")


def test_body_nennt_die_adresse_des_snapshots(welt: dict[str, Any]) -> None:
    body = _json(f"{API}/body/{welt['body'].pk}")

    assert body["mandari:snapshot"] == f"{API}/body/{welt['body'].pk}/snapshot"
    assert _json(body["mandari:changes"])["links"]["snapshot"] == body["mandari:snapshot"]
    assert pruefe(body, "Body") == []


def test_unbekannte_kommune_abgeschaltete_veroeffentlichung_und_lesende_methoden(welt: dict[str, Any]) -> None:
    assert Client().get(f"/oparl/v1/body/{uuid.uuid4()}/snapshot").status_code == 404
    assert Client().post(_pfad(welt)).status_code == 405
    assert Client().get(f"{_pfad(welt)}/").status_code == 301
    publication.set_source_state(welt["body"].source, publication.PAUSED)

    pausiert = Client().get(_pfad(welt))

    assert pausiert.status_code == 503 and pausiert["Retry-After"]


def test_zurueckgenommene_kommune_steht_als_gekuerztes_objekt_im_snapshot(welt: dict[str, Any]) -> None:
    for modell in (OParlOrganization, OParlPerson, OParlMeeting, OParlPaper, OParlLocation):
        for objekt in modell.objects.filter(body=welt["body"], deleted=False):
            objekt.mark_deleted()
    welt["body"].mark_deleted()

    _, zeilen = _laden(_pfad(welt))

    assert len(zeilen) == 2
    assert zeilen[1]["deleted"] is True and set(zeilen[1]) == {"id", "type", "created", "modified", "deleted"}


# =============================================================================
# Betrieb
# =============================================================================


def test_head_nennt_den_cursor_ohne_den_bestand_zu_lesen(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def nicht_lesen(abzug: snapshot.Snapshot) -> Iterator[dict[str, Any]]:
        raise AssertionError("HEAD darf den Bestand nicht lesen")

    monkeypatch.setattr(snapshot, "objects", nicht_lesen)

    antwort = Client().head(_pfad(welt))

    assert antwort.status_code == 200 and antwort.content == b""
    assert changes.decode_cursor(welt["body"].pk, antwort["Snapshot-Cursor"]).seq == 0
    assert antwort["Content-Type"] == "application/x-ndjson; charset=utf-8"


def test_nur_wenige_snapshots_entstehen_gleichzeitig(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    lesen = snapshot.objects
    waehrenddessen: list[Any] = []

    def lesen_und_zweite_anfrage(abzug: snapshot.Snapshot) -> Iterator[dict[str, Any]]:
        # Während dieser Snapshot entsteht, fragt ein zweiter Abnehmer an
        waehrenddessen.append(Client().get(_pfad(welt)))
        yield from lesen(abzug)

    with override_settings(OPARL_SNAPSHOT_PARALLEL=1):
        monkeypatch.setattr(snapshot, "objects", lesen_und_zweite_anfrage)
        erste, _ = _laden(_pfad(welt))
        monkeypatch.setattr(snapshot, "objects", lesen)

        (abgewiesen,) = waehrenddessen
        assert erste.status_code == 200
        assert abgewiesen.status_code == 503
        assert abgewiesen["Retry-After"] == "30" and abgewiesen["Cache-Control"] == "no-store"
        assert "error" in abgewiesen.json()
        # Danach ist der Platz wieder frei
        assert Client().get(_pfad(welt)).status_code == 200


def test_platz_wird_auch_nach_einem_fehler_frei(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    lesen = snapshot.objects

    def scheitern(abzug: snapshot.Snapshot) -> Iterator[dict[str, Any]]:
        raise RuntimeError("Datenbank nicht erreichbar")
        yield {}

    with override_settings(OPARL_SNAPSHOT_PARALLEL=1):
        monkeypatch.setattr(snapshot, "objects", scheitern)
        with pytest.raises(RuntimeError):
            Client().get(_pfad(welt))
        monkeypatch.setattr(snapshot, "objects", lesen)

        assert Client().get(_pfad(welt)).status_code == 200


def test_temporaere_datei_wird_nach_der_uebertragung_geschlossen(
    welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    dateien: list[Any] = []
    anlegen: Callable[[], Any] = tempfile.TemporaryFile

    def merken() -> Any:
        dateien.append(anlegen())
        return dateien[-1]

    monkeypatch.setattr("hub.api.snapshot.tempfile.TemporaryFile", merken)

    antwort = Client().get(_pfad(welt))
    assert not dateien[0].closed
    b"".join(cast(Any, antwort).streaming_content)

    assert dateien[0].closed


def test_unter_asgi_wird_in_bloecken_uebertragen(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Django läse einen synchronen Iterator unter ASGI ganz in den Speicher; hier liefert die Antwort Blöcke."""
    monkeypatch.setattr(snapshot, "BLOCK_SIZE", 64)
    _, erwartet = _laden(_pfad(welt))
    anfrage = AsyncRequestFactory().get(_pfad(welt))

    antwort = aggregator.body_snapshot(anfrage, pk=welt["body"].pk)

    async def sammeln() -> list[bytes]:
        return [block async for block in cast(Any, antwort).streaming_content]

    bloecke = async_to_sync(sammeln)()
    assert len(bloecke) > 3 and all(len(block) <= 64 for block in bloecke)
    zeilen = [json.loads(zeile) for zeile in b"".join(bloecke).decode("utf-8").splitlines()]
    assert zeilen[1:] == erwartet[1:]
    assert antwort["Content-Length"] == str(sum(len(block) for block in bloecke))


def test_snapshot_durch_den_ganzen_asgi_stapel(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Wie im Betrieb: Anfrage über den ASGI-Weg samt Middleware, Inhalt als asynchroner Strom."""
    monkeypatch.setattr(snapshot, "BLOCK_SIZE", 256)
    _, erwartet = _laden(_pfad(welt))

    async def abrufen() -> tuple[Any, list[bytes]]:
        antwort = await AsyncClient().get(_pfad(welt))
        return antwort, [block async for block in cast(Any, antwort).streaming_content]

    antwort, bloecke = async_to_sync(abrufen)()

    assert antwort.status_code == 200
    assert antwort["Content-Type"] == "application/x-ndjson; charset=utf-8"
    assert antwort["Content-Length"] == str(sum(len(block) for block in bloecke))
    assert len(bloecke) > 1
    zeilen = [json.loads(zeile) for zeile in b"".join(bloecke).decode("utf-8").splitlines()]
    assert zeilen[0]["snapshot_cursor"] == antwort["Snapshot-Cursor"]
    assert zeilen[1:] == erwartet[1:]
