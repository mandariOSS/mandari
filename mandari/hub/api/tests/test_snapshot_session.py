# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Snapshot der Session-Schnittstelle (Issue #563): dieselbe Ausgabe wie beim Aggregator
(``hub.api.snapshot``), Objekte aus Session – nur Öffentliches.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.common.models import IdentifierBase
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionTenant,
)
from hub.api import changes
from hub.api.tests.ereignisse import ereignis, ruecknahme
from hub.api.tests.konformitaet import pruefe

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
PFAD = "/session/musterstadt/api/oparl/body/snapshot/"
HEUTE = date(2026, 9, 30)
MANDANT = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
LISTEN = ("organizations", "people", "meetings", "papers")


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


@pytest.fixture
def welt() -> Any:
    cache.clear()
    # Installation, deren Kennungen auf SITE gebildet sind (Basis festgeschrieben, Issue #733)
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    with override_settings(SITE_URL=SITE, OPARL_API_RATE_LIMIT=0, OPARL_CHANGES_ENABLED=True):
        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        person = SessionPerson.objects.create(tenant=tenant, given_name="Petra", family_name="Muster")
        SessionOrganizationMembership.objects.create(organization=rat, person=person)
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
        SessionAgendaItem.objects.create(meeting=sitzung, number="1", name="Radweg", is_public=True, paper=vorlage)
        SessionAgendaItem.objects.create(meeting=sitzung, number="2", name="GEHEIMER-TOP", is_public=False)
        SessionMeeting.objects.create(
            tenant=tenant, name="GEHEIME-SITZUNG", organization=rat, start=timezone.now(), is_public=False
        )
        SessionPaper.objects.create(tenant=tenant, reference="V/2026/2", name="GEHEIMER-ENTWURF", status="draft")
        yield {"tenant": tenant, "body": canonical_id(f"{BASIS}body/"), "meeting": sitzung, "paper": vorlage}
    cache.clear()


def _laden() -> tuple[Any, list[dict[str, Any]]]:
    antwort = Client().get(PFAD)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    return antwort, [json.loads(zeile) for zeile in inhalt.decode("utf-8").splitlines()]


def _json(adresse: str) -> dict[str, Any]:
    antwort = Client().get(adresse.removeprefix(SITE))
    assert antwort.status_code == 200, (adresse, antwort.status_code)
    return cast(dict[str, Any], antwort.json())


def _listen() -> dict[str, dict[str, Any]]:
    body = _json(f"{BASIS}body/")
    stand = {body["id"]: body}
    for segment in LISTEN:
        stand.update({objekt["id"]: objekt for objekt in _json(f"{BASIS}{segment}/")["data"]})
    return stand


def _abnehmer(zeilen: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
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


def test_snapshot_enthaelt_den_oeffentlichen_stand_des_mandanten(welt: dict[str, Any]) -> None:
    antwort, zeilen = _laden()

    assert antwort["Content-Type"] == "application/x-ndjson; charset=utf-8"
    assert zeilen[0]["body"] == f"{BASIS}body/"
    assert zeilen[0]["changes"] == f"{BASIS}body/changes/?after={zeilen[0]['snapshot_cursor']}"
    assert zeilen[0]["snapshot_cursor"] == antwort["Snapshot-Cursor"]
    assert zeilen[1] == _json(f"{BASIS}body/")
    assert {objekt["id"]: objekt for objekt in zeilen[1:]} == _listen()
    assert len(zeilen) == 1 + 1 + 1 + 1 + 1 + 1  # Kopfzeile, Body, Gremium, Person, Sitzung, Vorlage
    assert zeilen[0]["objects"] == len(zeilen) - 1
    for objekt in zeilen[1:]:
        assert pruefe(objekt, objekt["type"].rsplit("/", 1)[1]) == []
    # Tagesordnung und Mitgliedschaften stehen eingebettet, wie in den Listen
    sitzung = next(objekt for objekt in zeilen[1:] if objekt["type"].endswith("/Meeting"))
    assert [punkt["name"] for punkt in sitzung["agendaItem"]] == ["Radweg"]
    person = next(objekt for objekt in zeilen[1:] if objekt["type"].endswith("/Person"))
    assert len(person["membership"]) == 1


def test_nichtoeffentliches_steht_nicht_im_snapshot(welt: dict[str, Any]) -> None:
    antwort = Client().get(PFAD)
    inhalt = b"".join(cast(Any, antwort).streaming_content).decode("utf-8")

    assert "GEHEIM" not in inhalt
    assert "Radweg" in inhalt


def test_cursor_kennt_nur_oeffentliche_ereignisse(welt: dict[str, Any]) -> None:
    """Der Cursor des Snapshots ändert sich nicht, wenn Nichtöffentliches geschieht."""
    ereignis("ris.paper.released", welt["body"], canonical_id(f"{BASIS}paper/{welt['paper'].pk}/"), mandant=MANDANT)
    vorher = Client().head(PFAD)["Snapshot-Cursor"]

    ereignis("ris.meeting.changed", welt["body"], sichtbarkeit="nichtoeffentlich", mandant=MANDANT)
    ereignis("ris.paper.changed", welt["body"], sichtbarkeit="nichtoeffentlich", mandant=MANDANT)

    assert Client().head(PFAD)["Snapshot-Cursor"] == vorher
    assert changes.decode_cursor(welt["body"], vorher).seq == 1


def test_abnehmer_kommt_ueber_snapshot_und_feed_zum_stand_der_schnittstelle(welt: dict[str, Any]) -> None:
    _, zeilen = _laden()
    sitzung, vorlage = welt["meeting"], welt["paper"]

    # Nach dem Snapshot: eine Vorlage ändert sich, die Sitzung wird nichtöffentlich
    SessionPaper.objects.filter(pk=vorlage.pk).update(name="Radweg (Neufassung)")
    ereignis("ris.paper.changed", welt["body"], canonical_id(f"{BASIS}paper/{vorlage.pk}/"), mandant=MANDANT)
    sitzung.is_public = False
    sitzung.save()
    ruecknahme(
        welt["body"], "Meeting", canonical_id(f"{BASIS}meeting/{sitzung.pk}/"), "nichtoeffentlich", mandant=MANDANT
    )

    stand = _abnehmer(zeilen)

    assert stand == _listen()
    assert stand[f"{BASIS}paper/{vorlage.pk}/"]["name"] == "Radweg (Neufassung)"
    assert f"{BASIS}meeting/{sitzung.pk}/" not in stand


def test_ausgeschaltet_und_nicht_freigeschaltet_gibt_es_den_snapshot_nicht(welt: dict[str, Any]) -> None:
    assert _json(f"{BASIS}body/")["mandari:snapshot"] == f"{BASIS}body/snapshot/"
    with override_settings(OPARL_CHANGES_ENABLED=False):
        assert Client().get(PFAD).status_code == 404
        assert "mandari:snapshot" not in _json(f"{BASIS}body/")
    SessionTenant.objects.filter(pk=welt["tenant"].pk).update(oparl_public_since=None)
    assert Client().get(PFAD).status_code == 404
