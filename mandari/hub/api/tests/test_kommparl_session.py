# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl je Session-Mandant nach kommParl (Issue #1216): dieselben Formen wie beim Aggregator (``test_kommparl.py``),
Objekte aus Session – nur Öffentliches.

- Der Body nennt ``kommparl:changes`` und ``kommparl:snapshot``, das System ``kommparl:conformsTo``.
- Feed-Seite und abgelaufener Cursor bestehen die Prüfung gegen die Profilschemas, ebenso Kopf und Ende des Snapshots.
- Der Snapshot enthält jedes öffentliche Objekt in einer eigenen Zeile, Nichtöffentliches nicht.
- Die älteren Formen bleiben unter ihren Adressen und sind abgekündigt.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
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
from hub.api import changes, kommparl
from hub.api.tests.ereignisse import ereignis, ruecknahme
from hub.api.tests.konformitaet import pruefe, pruefe_profil, pruefe_snapshot

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/musterstadt/api/oparl/"
HEUTE = date(2026, 9, 30)
MANDANT = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"


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


def _get(adresse: str, **parameter: str) -> Any:
    return Client().get(adresse.removeprefix(SITE), parameter)


def _json(adresse: str, **parameter: str) -> dict[str, Any]:
    antwort = _get(adresse, **parameter)
    assert antwort.status_code == 200, (adresse, antwort.status_code, antwort.content[:500])
    return cast(dict[str, Any], antwort.json())


def _snapshot() -> tuple[Any, list[dict[str, Any]]]:
    antwort = _get(f"{BASIS}body/kommparl/snapshot/")
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    inhalt = b"".join(cast(Any, antwort).streaming_content)
    return antwort, [json.loads(zeile) for zeile in inhalt.decode("utf-8").splitlines()]


def test_body_und_system(welt: dict[str, Any]) -> None:
    body = _json(f"{BASIS}body/")
    assert body["kommparl:changes"] == f"{BASIS}body/kommparl/changes/"
    assert body["kommparl:snapshot"] == f"{BASIS}body/kommparl/snapshot/"
    assert body["mandari:changes"] == f"{BASIS}body/changes/"
    assert body["mandari:snapshot"] == f"{BASIS}body/snapshot/"
    assert pruefe(body, "Body") == []
    system = _json(BASIS)
    assert system["kommparl:conformsTo"] == [kommparl.VERSION]
    assert pruefe(system, "System") == []

    with override_settings(OPARL_CHANGES_ENABLED=False):
        body = _json(f"{BASIS}body/")
        assert _get(f"{BASIS}body/kommparl/changes/").status_code == 404
        assert _get(f"{BASIS}body/kommparl/snapshot/").status_code == 404
    assert not {"kommparl:changes", "kommparl:snapshot", "mandari:changes"} & set(body)


def test_feed_nach_profil(welt: dict[str, Any]) -> None:
    sitzung, vorlage = welt["meeting"], welt["paper"]
    ereignis("ris.paper.released", welt["body"], canonical_id(f"{BASIS}paper/{vorlage.pk}/"), mandant=MANDANT)
    ereignis("ris.meeting.changed", welt["body"], sichtbarkeit="nichtoeffentlich", mandant=MANDANT)
    sitzung.is_public = False
    sitzung.save()
    ruecknahme(
        welt["body"], "Meeting", canonical_id(f"{BASIS}meeting/{sitzung.pk}/"), "nichtoeffentlich", mandant=MANDANT
    )

    seite = _json(f"{BASIS}body/kommparl/changes/")

    assert pruefe_profil(seite, "changes", "ChangeList") == []
    assert [(e["operation"], e["id"], e.get("reason")) for e in seite["data"]] == [
        ("upsert", f"{BASIS}paper/{vorlage.pk}/", None),
        ("delete", f"{BASIS}meeting/{sitzung.pk}/", "nonPublic"),
    ]
    assert seite["links"] == {"self": f"{BASIS}body/kommparl/changes/"}
    # Session-Mandanten führen ihren Bestand selbst: keine Aktualität einer Quelle
    assert "mandari:freshness" not in seite
    assert "GEHEIM" not in json.dumps(seite)


def test_abgelaufener_cursor(welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _json(f"{BASIS}body/kommparl/changes/")["cursor"]
    monkeypatch.setattr(changes, "today", lambda: HEUTE + timedelta(days=changes.retention_days() + 1))

    antwort = _get(f"{BASIS}body/kommparl/changes/", after=cursor)

    assert antwort.status_code == 410
    fehler = antwort.json()
    assert pruefe_profil(fehler, "changes", "CursorExpired") == []
    assert fehler["kommparl:snapshot"] == f"{BASIS}body/kommparl/snapshot/"


def test_snapshot_nach_profil(welt: dict[str, Any]) -> None:
    antwort, zeilen = _snapshot()

    assert pruefe_snapshot(zeilen, body=f"{BASIS}body/") == []
    assert zeilen[0]["cursor"] == antwort["Snapshot-Cursor"]
    objekte = zeilen[1:-1]
    typen = sorted(objekt["type"].rsplit("/", 1)[1] for objekt in objekte)
    # Body, Gremium, Person, Mitgliedschaft, Sitzung, ihr Ort, der öffentliche TOP und die Vorlage
    assert typen == sorted(
        ["Body", "Organization", "Person", "Membership", "Meeting", "Location", "AgendaItem", "Paper"]
    )
    for objekt in objekte:
        assert objekt == _json(objekt["id"]), objekt["id"]
    assert "GEHEIM" not in "\n".join(json.dumps(zeile) for zeile in zeilen)


def test_aeltere_form_abgekuendigt(welt: dict[str, Any]) -> None:
    for alt, neu in (("changes", "kommparl/changes"), ("snapshot", "kommparl/snapshot")):
        antwort = _get(f"{BASIS}body/{alt}/")
        assert antwort.status_code == 200
        assert antwort["Sunset"] == "Wed, 01 Dec 2027 00:00:00 GMT"
        assert antwort["Deprecation"] == "@1793836800"
        assert antwort["Link"] == f'<{BASIS}body/{neu}/>; rel="successor-version"'
    # Inhalt der älteren Form unverändert
    assert set(_json(f"{BASIS}body/changes/")["links"]) == {"self", "next", "snapshot"}
