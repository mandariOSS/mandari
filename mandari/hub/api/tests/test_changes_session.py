# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsfeed der Session-Schnittstelle (Issue #562): dieselbe Ausgabe wie beim Aggregator
(``hub.api.changes``), Adressen aus Session.

Ereignisse nennen die kanonische Kennung eines Objekts – ``uuid5`` über seine Adresse in der
Schnittstelle. Die Session-Schnittstelle findet dazu die Adresse unter den Objekten des Mandanten, die
öffentlich sind oder es waren. Was nie öffentlich war, bekommt keinen Eintrag und ist auch an der Antwort
nicht erkennbar – selbst wenn ein Ereignis es fälschlich als öffentlich meldet.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.events.models import Event
from apps.session.models import (
    SessionMeeting,
    SessionOParlTombstone,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)
from hub.api import changes
from hub.api.tests.ereignisse import ereignis, huelle, naechste_nummer, ruecknahme
from hub.contracts import get_registry

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
PFAD = "/session/musterstadt/api/oparl/body/changes/"
HEUTE = date(2026, 9, 30)
MANDANT = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


@pytest.fixture
def welt() -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_API_RATE_LIMIT=0, OPARL_CHANGES_ENABLED=True):
        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        sitzung = SessionMeeting.objects.create(
            tenant=tenant,
            name="Ratssitzung",
            organization=rat,
            start=timezone.now(),
            is_public=True,
            location="Rathaus",
        )
        geheim = SessionMeeting.objects.create(
            tenant=tenant, name="Personalausschuss", organization=rat, start=timezone.now(), is_public=False
        )
        vorlage = SessionPaper.objects.create(
            tenant=tenant, reference="V/2026/1", name="Radweg", is_public=True, status="approved"
        )
        entwurf = SessionPaper.objects.create(tenant=tenant, reference="V/2026/2", name="Entwurf", status="draft")
        yield {
            "tenant": tenant,
            "body": canonical_id(f"{BASIS}body/"),
            "organization": rat,
            "meeting": sitzung,
            "geheim": geheim,
            "paper": vorlage,
            "entwurf": entwurf,
        }
    cache.clear()


def _adresse(art: str, kennung: Any) -> str:
    return f"{BASIS}{art}/{kennung}/"


def _kennung(art: str, kennung: Any) -> uuid.UUID:
    """Kanonische Kennung eines Session-Objekts: ``uuid5`` über seine Adresse in der Schnittstelle."""
    return canonical_id(_adresse(art, kennung))


def _feed(**parameter: str) -> dict[str, Any]:
    antwort = Client().get(PFAD, parameter)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    return cast(dict[str, Any], antwort.json())


# =============================================================================
# Schalter und Adresse
# =============================================================================


def test_ausgeschaltet_gibt_es_den_feed_nicht(welt: dict[str, Any]) -> None:
    with override_settings(OPARL_CHANGES_ENABLED=False):
        assert Client().get(PFAD).status_code == 404
        assert "mandari:changes" not in Client().get("/session/musterstadt/api/oparl/body/").json()


def test_body_nennt_die_adresse_des_feeds(welt: dict[str, Any]) -> None:
    body = Client().get("/session/musterstadt/api/oparl/body/").json()

    assert body["mandari:changes"] == f"{BASIS}body/changes/"
    leer = _feed()
    assert leer["data"] == []
    assert leer["links"] == {
        "self": f"{BASIS}body/changes/",
        "next": f"{BASIS}body/changes/?after={leer['cursor']}",
        "snapshot": f"{BASIS}body/snapshot/",
    }


def test_nicht_freigeschalteter_mandant_hat_keinen_feed(welt: dict[str, Any]) -> None:
    SessionTenant.objects.filter(pk=welt["tenant"].pk).update(oparl_public_since=None)

    assert Client().get(PFAD).status_code == 404
    assert Client().get("/session/gibt-es-nicht/api/oparl/body/changes/").status_code == 404


# =============================================================================
# Einträge mit den Adressen der Schnittstelle
# =============================================================================


def test_eintraege_nennen_die_adressen_der_session_objekte(welt: dict[str, Any]) -> None:
    sitzung, vorlage, rat = welt["meeting"], welt["paper"], welt["organization"]
    ereignis("ris.meeting.changed", welt["body"], _kennung("meeting", sitzung.pk), mandant=MANDANT)
    ereignis("ris.paper.released", welt["body"], _kennung("paper", vorlage.pk), mandant=MANDANT)
    ereignis("ris.source.published", welt["body"], welt["body"], mandant=MANDANT)
    # Der Ort gehört zur Sitzung und trägt deren Kennung
    ruecknahme(welt["body"], "Location", _kennung("location", sitzung.pk), "zurueckgenommen", mandant=MANDANT)
    ruecknahme(welt["body"], "Organization", _kennung("organization", rat.pk), "datenschutz", mandant=MANDANT)

    eintraege = _feed()["data"]

    assert [(e["operation"], e["type"].rsplit("/", 1)[1], e["id"]) for e in eintraege] == [
        ("upsert", "Meeting", _adresse("meeting", sitzung.pk)),
        ("upsert", "Paper", _adresse("paper", vorlage.pk)),
        ("upsert", "Body", f"{BASIS}body/"),
        ("delete", "Location", _adresse("location", sitzung.pk)),
        ("redact", "Organization", _adresse("organization", rat.pk)),
    ]
    # Die Adresse führt zum Objekt dieser Schnittstelle
    assert Client().get(eintraege[1]["id"].removeprefix(SITE)).json()["name"] == "Radweg"


def test_zurueckgenommenes_objekt_behaelt_seine_adresse(welt: dict[str, Any]) -> None:
    """Gelöschtes und nicht mehr Öffentliches findet die Schnittstelle über den Eintrag für Gelöschtes."""
    sitzung, vorlage = welt["meeting"], welt["paper"]
    sitzung.is_public = False
    sitzung.save()
    kennung_vorlage = vorlage.pk
    vorlage.delete()
    assert SessionOParlTombstone.objects.filter(tenant=welt["tenant"]).count() >= 2
    ruecknahme(welt["body"], "Meeting", _kennung("meeting", sitzung.pk), "nichtoeffentlich", mandant=MANDANT)
    ruecknahme(welt["body"], "Paper", _kennung("paper", kennung_vorlage), "quelle_geloescht", mandant=MANDANT)

    eintraege = _feed()["data"]

    assert [(e["operation"], e["reason"], e["id"]) for e in eintraege] == [
        ("delete", "nichtoeffentlich", _adresse("meeting", sitzung.pk)),
        ("delete", "quelle_geloescht", _adresse("paper", kennung_vorlage)),
    ]
    # Unter der Adresse steht das gekürzte Objekt
    assert Client().get(eintraege[0]["id"].removeprefix(SITE)).json()["deleted"] is True


def test_was_nie_oeffentlich_war_bekommt_keinen_eintrag(welt: dict[str, Any]) -> None:
    """
    Meldet ein Ereignis ein nie veröffentlichtes Objekt fälschlich als öffentlich, erscheint es nicht –
    und die Antwort bleibt dieselbe wie zuvor (kein vorgerückter Cursor, gleicher ``ETag``).
    """
    ereignis("ris.paper.released", welt["body"], _kennung("paper", welt["paper"].pk), mandant=MANDANT)
    stand = _feed()["cursor"]
    vorher = [Client().get(PFAD), Client().get(PFAD, {"after": stand})]

    ereignis("ris.meeting.changed", welt["body"], _kennung("meeting", welt["geheim"].pk), mandant=MANDANT)
    ereignis("ris.paper.changed", welt["body"], _kennung("paper", welt["entwurf"].pk), mandant=MANDANT)
    ereignis("ris.paper.changed", welt["body"], uuid.uuid4(), mandant=MANDANT)

    nachher = [Client().get(PFAD), Client().get(PFAD, {"after": stand})]
    for a, b in zip(vorher, nachher, strict=True):
        assert b.content == a.content and b["ETag"] == a["ETag"]
    assert "Personalausschuss" not in nachher[0].content.decode()
    assert str(welt["geheim"].pk) not in nachher[0].content.decode()


def test_eintraege_hinter_uebersprungenen_ereignissen_gehen_nicht_verloren(welt: dict[str, Any]) -> None:
    for _ in range(3):
        ereignis("ris.meeting.changed", welt["body"], _kennung("meeting", welt["geheim"].pk), mandant=MANDANT)
    ereignis("ris.paper.released", welt["body"], _kennung("paper", welt["paper"].pk), mandant=MANDANT)
    for _ in range(2):
        ereignis("ris.paper.changed", welt["body"], uuid.uuid4(), mandant=MANDANT)
    ereignis("ris.meeting.changed", welt["body"], _kennung("meeting", welt["meeting"].pk), mandant=MANDANT)

    erste = _feed(limit="1")
    zweite = _feed(limit="1", after=erste["cursor"])
    dritte = _feed(limit="1", after=zweite["cursor"])

    assert [seite["data"][0]["id"] for seite in (erste, zweite)] == [
        _adresse("paper", welt["paper"].pk),
        _adresse("meeting", welt["meeting"].pk),
    ]
    assert dritte["data"] == []


def test_nur_ereignisse_der_eigenen_kommune(welt: dict[str, Any]) -> None:
    andere = SessionTenant.objects.create(name="Nachbarstadt", slug="nachbarstadt", oparl_public_since=timezone.now())
    fremd = canonical_id(f"{SITE}/session/nachbarstadt/api/oparl/body/")
    ereignis("ris.paper.released", fremd, _kennung("paper", welt["paper"].pk), mandant=MANDANT)
    ereignis("ris.paper.changed", welt["body"], _kennung("paper", welt["paper"].pk), sichtbarkeit="nichtoeffentlich")

    assert _feed()["data"] == []
    assert andere.pk != welt["tenant"].pk
    assert Client().get("/session/nachbarstadt/api/oparl/body/changes/").json()["data"] == []


def test_abgelaufener_cursor_verweist_auf_den_snapshot_der_schnittstelle(welt: dict[str, Any]) -> None:
    from datetime import timedelta

    zu_alt = changes.encode_cursor(welt["body"], 0, HEUTE - timedelta(days=changes.retention_days() + 1))

    antwort = Client().get(PFAD, {"after": zu_alt})

    assert antwort.status_code == 410
    assert antwort["Content-Type"] == "application/problem+json; charset=utf-8"
    assert antwort.json()["snapshot"] == f"{BASIS}body/snapshot/"


def test_adressen_werden_unter_den_zuletzt_geaenderten_objekten_zuerst_gesucht(welt: dict[str, Any]) -> None:
    """Die Suche endet, sobald alle Adressen gefunden sind – das jüngste Objekt zuerst."""
    from apps.session.api import oparl as schnittstelle

    tenant = welt["tenant"]
    vorlagen = [
        SessionPaper.objects.create(
            tenant=tenant, reference=f"V/2026/{nummer}", name=f"Vorlage {nummer}", is_public=True, status="approved"
        )
        for nummer in range(10, 16)
    ]
    juengste = vorlagen[-1]
    modul = cast(Any, schnittstelle)  # die Schnittstelle des Fachmoduls ist nicht typisiert
    adressen = modul._addresses(modul._mapping(tenant))

    gefunden = adressen("paper", {_kennung("paper", juengste.pk), uuid.uuid4()})

    assert gefunden == {_kennung("paper", juengste.pk): _adresse("paper", juengste.pk)}
    assert adressen("paper", set()) == {}
    assert adressen("voting", {uuid.uuid4()}) == {}


def test_feed_bleibt_hinter_einem_block_unauffindbarer_kennungen_nicht_stehen(welt: dict[str, Any]) -> None:
    """
    Mehr Ereignisse ohne Adresse, als eine Anfrage liest (etwa zu Objekten, deren Löschung keinen Eintrag
    für Gelöschtes hinterlassen hat): Der Cursor rückt über den gelesenen Abschnitt vor, und die Änderung
    danach kommt an.
    """
    ereignis("ris.paper.released", welt["body"], _kennung("paper", welt["paper"].pk), mandant=MANDANT)
    stand = _feed()["cursor"]
    muster = huelle("ris.paper.changed", welt["body"], mandant=MANDANT)
    get_registry().validate_event(muster)
    erste = naechste_nummer()
    zeilen = []
    for nummer in range(changes._MIN_EXAMINED + 1):
        kennung = uuid.uuid4()
        zeilen.append(
            Event(
                type=muster.type,
                version=muster.version,
                aggregate_type=muster.aggregate_type,
                aggregate_id=kennung,
                tenant_ref=MANDANT,
                body_id=welt["body"],
                visibility=muster.visibility,
                occurred_at=muster.occurred_at,
                correlation_id=uuid.uuid4(),
                payload={**muster.payload, "paper": str(kennung)},
                seq=erste + nummer,
            )
        )
    Event.objects.bulk_create(zeilen, batch_size=500)
    ereignis("ris.meeting.changed", welt["body"], _kennung("meeting", welt["meeting"].pk), mandant=MANDANT)

    leer = _feed(after=stand)
    seite = _feed(after=leer["cursor"])

    assert leer["data"] == [] and leer["cursor"] != stand
    assert [eintrag["id"] for eintrag in seite["data"]] == [_adresse("meeting", welt["meeting"].pk)]
    assert _feed(after=seite["cursor"])["data"] == []


def test_suche_nach_adressen_durchlaeuft_jeden_typ_je_anfrage_hoechstens_einmal(welt: dict[str, Any]) -> None:
    """Eine Anfrage fragt je Abschnitt des Journals; unauffindbare Kennungen lösen keinen zweiten Durchlauf aus."""
    from apps.session.api import oparl as schnittstelle

    modul = cast(Any, schnittstelle)  # die Schnittstelle des Fachmoduls ist nicht typisiert
    adressen = modul._addresses(modul._mapping(welt["tenant"]))
    vorlage = _kennung("paper", welt["paper"].pk)

    with CaptureQueriesContext(connection) as erster:
        assert adressen("paper", {uuid.uuid4()}) == {}
    with CaptureQueriesContext(connection) as weitere:
        assert adressen("paper", {uuid.uuid4()}) == {}
        assert adressen("paper", {vorlage}) == {vorlage: _adresse("paper", welt["paper"].pk)}

    assert len(erster.captured_queries) >= 2  # öffentliche Objekte und Einträge für Gelöschtes
    assert weitere.captured_queries == []
