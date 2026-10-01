# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsfeed und Snapshot des Aggregators nach dem Veröffentlichungsstand der Kommune (Issue #707).

Feed und Snapshot gibt es nur für Kommunen, die das Bürgerportal veröffentlicht und listet; es gelten die
Stände seiner Seiten (``insight_core.publication``, Issue #618):

- nicht gelistet: Die Adressen gibt es nicht (wie bei ausgeschaltetem Feed), der Body nennt sie nicht
- vorübergehend abgeschaltet: ``503`` mit ``Retry-After``; danach gilt der Cursor weiter
- dauerhaft zurückgenommen: ``410``; nach der Wiederherstellung gilt ein Cursor von vorher nicht mehr
  (``410`` mit Verweis auf den Snapshot), denn Rücknahme und Wiederherstellung stehen nicht im Journal

Keine Absage verrät etwas über das Journal der Kommune: Sie bleibt Byte für Byte gleich, was auch
geschieht, und trägt keinen ``ETag`` (Vorbild: ``test_changes.py``, Abschnitt „Nur Öffentliches“).
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.session.models import SessionTenant
from apps.session.services import portal_publication, tenant_provisioning
from hub.api import changes
from hub.api.tests.ereignisse import ereignis
from insight_core import publication
from insight_core.models import OParlBody, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
API = f"{SITE}/oparl/v1"
HEUTE = date(2026, 9, 30)

PAUSED = SessionTenant.PORTAL_END_PAUSED
ARCHIVED = SessionTenant.PORTAL_END_ARCHIVED
WITHDRAWN = SessionTenant.PORTAL_END_WITHDRAWN


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


@pytest.fixture
def welt() -> Any:
    """Ein Session-Mandant, den das Bürgerportal spiegelt: Quelle, Kommune, zwei Vorlagen im Bestand."""
    cache.clear()
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_CACHE_SECONDS=0,
        OPARL_CHANGES_ENABLED=True,
    ):
        tenant = SessionTenant.objects.create(
            name="Bezirk Nord", slug="nord", insight_publish=True, oparl_public_since=timezone.now()
        )
        source = OParlSource.objects.get(sync_config__session_tenant="nord")
        body = OParlBody.objects.create(external_id=f"{source.url}body/", source=source, name="Bezirk Nord")
        vorlagen = [
            OParlPaper.objects.create(external_id=f"{source.url}paper/{nummer}/", body=body, name=f"Vorlage {nummer}")
            for nummer in (1, 2)
        ]
        yield {"tenant": tenant, "source": source, "body": body, "vorlagen": vorlagen}
    cache.clear()


def _frisch(welt: dict[str, Any]) -> SessionTenant:
    return SessionTenant.objects.get(pk=welt["tenant"].pk)


def _pfad(welt: dict[str, Any], segment: str = "changes") -> str:
    return f"/oparl/v1/body/{welt['body'].pk}/{segment}"


def _feed(welt: dict[str, Any], **parameter: str) -> dict[str, Any]:
    antwort = Client().get(_pfad(welt), parameter)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    return cast(dict[str, Any], antwort.json())


def _snapshot_cursor(welt: dict[str, Any]) -> str:
    antwort = Client().get(_pfad(welt, "snapshot"))
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    kopf = json.loads(b"".join(cast(Any, antwort).streaming_content).split(b"\n", 1)[0])
    return cast(str, kopf["snapshot_cursor"])


def _aenderung(welt: dict[str, Any], nummer: int = 0) -> str:
    """Öffentliches Ereignis zu einer Vorlage der Kommune; Adresse des Eintrags, den es ergibt."""
    vorlage = welt["vorlagen"][nummer]
    ereignis("ris.paper.changed", welt["body"].pk, vorlage.pk)
    return f"{API}/paper/{vorlage.pk}"


def _absagen(welt: dict[str, Any], cursor: str) -> list[Any]:
    """Alle Abrufe, die eine nicht angebotene Kommune absagt: Feed mit und ohne Cursor, Snapshot, ``HEAD``."""
    client = Client()
    return [
        client.get(_pfad(welt)),
        client.get(_pfad(welt), {"after": cursor}),
        client.get(_pfad(welt, "snapshot")),
        client.head(_pfad(welt, "snapshot")),
    ]


def _verraten_nichts(welt: dict[str, Any], cursor: str, status: int) -> list[Any]:
    """
    Absagen mit ``status``, ohne ``ETag`` und ohne Cursor – und gleich, ob im Journal der Kommune etwas
    geschieht oder nicht.
    """
    vorher = _absagen(welt, cursor)
    _aenderung(welt)
    ereignis("ris.paper.changed", welt["body"].pk, sichtbarkeit="nichtoeffentlich")
    nachher = _absagen(welt, cursor)
    for a, b in zip(vorher, nachher, strict=True):
        assert a.status_code == b.status_code == status
        assert b.content == a.content
        assert "ETag" not in b and "Snapshot-Cursor" not in b
    return nachher


# =============================================================================
# Veröffentlicht und gelistet
# =============================================================================


def test_veroeffentlichte_gelistete_kommune_bietet_feed_und_snapshot(welt: dict[str, Any]) -> None:
    adresse = _aenderung(welt)
    body = Client().get(f"/oparl/v1/body/{welt['body'].pk}").json()

    assert body["mandari:changes"] == f"{API}{_pfad(welt).removeprefix('/oparl/v1')}"
    assert body["mandari:snapshot"] == f"{API}{_pfad(welt, 'snapshot').removeprefix('/oparl/v1')}"
    assert [eintrag["id"] for eintrag in _feed(welt)["data"]] == [adresse]
    assert _feed(welt, after=_snapshot_cursor(welt))["data"] == []


# =============================================================================
# Nicht gelistet
# =============================================================================


def test_nicht_gelistete_kommune_hat_weder_feed_noch_snapshot(welt: dict[str, Any]) -> None:
    cursor = _feed(welt)["cursor"]
    OParlBody.objects.filter(pk=welt["body"].pk).update(is_listed=False)

    feed, mit_cursor, abzug, _ = _verraten_nichts(welt, cursor, 404)

    # Wie bei ausgeschaltetem Feed: Die Adressen gibt es nicht, und nichts weist auf sie hin
    unbekannt = Client().get(_pfad(welt, "quatsch")).json()["error"]
    assert feed.json()["error"] == mit_cursor.json()["error"] == unbekannt.replace("quatsch", "changes")
    assert abzug.json()["error"] == unbekannt.replace("quatsch", "snapshot")
    body = Client().get(f"/oparl/v1/body/{welt['body'].pk}").json()
    assert "mandari:changes" not in body and "mandari:snapshot" not in body


def test_wieder_gelistet_gilt_der_cursor_weiter(welt: dict[str, Any]) -> None:
    """Die Listung ändert den Bestand nicht: Was in der Zwischenzeit geschah, steht im Feed."""
    cursor = _feed(welt)["cursor"]
    OParlBody.objects.filter(pk=welt["body"].pk).update(is_listed=False)
    zwischendurch = _aenderung(welt)
    OParlBody.objects.filter(pk=welt["body"].pk).update(is_listed=True)

    assert [eintrag["id"] for eintrag in _feed(welt, after=cursor)["data"]] == [zwischendurch]


# =============================================================================
# Vorübergehend abgeschaltet
# =============================================================================


def test_voruebergehend_abgeschaltet_ergibt_503_und_der_cursor_gilt_danach_weiter(welt: dict[str, Any]) -> None:
    _aenderung(welt)
    cursor = _feed(welt)["cursor"]
    portal_publication.end_publication(_frisch(welt), PAUSED)

    for antwort in _verraten_nichts(welt, cursor, 503):
        assert antwort["Retry-After"] == str(publication.RETRY_AFTER_SECONDS)
        assert antwort["Cache-Control"] == "no-store"

    # Wieder veröffentlicht: Es wurde nichts gelöscht, die Änderungen aus der Pause folgen hinter dem Cursor
    portal_publication.resume_publication(_frisch(welt))
    eintraege = _feed(welt, after=cursor)["data"]
    assert [eintrag["id"] for eintrag in eintraege] == [f"{API}/paper/{welt['vorlagen'][0].pk}"]


def test_archiv_bietet_feed_und_snapshot_weiter_an(welt: dict[str, Any]) -> None:
    cursor = _feed(welt)["cursor"]
    portal_publication.end_publication(_frisch(welt), ARCHIVED)

    adresse = _aenderung(welt)

    assert [eintrag["id"] for eintrag in _feed(welt, after=cursor)["data"]] == [adresse]
    assert _feed(welt, after=_snapshot_cursor(welt))["data"] == []


# =============================================================================
# Dauerhaft zurückgenommen
# =============================================================================


def test_dauerhaft_zurueckgenommen_ergibt_410(welt: dict[str, Any]) -> None:
    _aenderung(welt)
    cursor = _feed(welt)["cursor"]
    portal_publication.end_publication(_frisch(welt), WITHDRAWN)

    antworten = _verraten_nichts(welt, cursor, 410)

    for antwort in antworten[:3]:
        assert antwort["Content-Type"] == "application/problem+json; charset=utf-8"
        assert antwort["Cache-Control"] == "no-store"
        problem = antwort.json()
        assert problem["type"] == "https://docs.mandari.de/api/probleme/kommune-zurueckgenommen"
        assert problem["status"] == 410
        # Einen Snapshot gibt es dann auch nicht
        assert "snapshot" not in problem
    assert antworten[2].json()["instance"] == _pfad(welt, "snapshot")


@pytest.mark.parametrize("wiederherstellen", ["wieder_veroeffentlichen", "archiv", "voruebergehend"])
def test_nach_einer_ruecknahme_gilt_ein_alter_cursor_nicht_mehr(welt: dict[str, Any], wiederherstellen: str) -> None:
    """
    Rücknahme und Wiederherstellung ändern den Bestand am Journal vorbei. Ein Abnehmer, der auf die
    Rücknahme hin seine Kopie gelöscht hat, bekäme die wiederhergestellten Einträge über seinen alten
    Cursor nie zurück; er steigt über den Snapshot neu ein.
    """
    _aenderung(welt)
    alt = _feed(welt)["cursor"]
    portal_publication.end_publication(_frisch(welt), WITHDRAWN)

    if wiederherstellen == "wieder_veroeffentlichen":
        portal_publication.resume_publication(_frisch(welt))
    elif wiederherstellen == "archiv":
        portal_publication.end_publication(_frisch(welt), ARCHIVED)
    else:
        portal_publication.end_publication(_frisch(welt), PAUSED)
        portal_publication.resume_publication(_frisch(welt))

    abgelaufen = Client().get(_pfad(welt), {"after": alt})
    assert abgelaufen.status_code == 410
    problem = abgelaufen.json()
    assert problem["type"] == "https://docs.mandari.de/api/probleme/cursor-abgelaufen"
    assert problem["snapshot"] == f"{API}{_pfad(welt, 'snapshot').removeprefix('/oparl/v1')}"

    # Über den Snapshot geht es weiter, auch mit jedem später ausgegebenen Cursor
    neu = _snapshot_cursor(welt)
    assert _feed(welt, after=neu)["data"] == []
    adresse = _aenderung(welt, 1)
    seite = _feed(welt, after=neu)
    assert [eintrag["id"] for eintrag in seite["data"]] == [adresse]
    assert _feed(welt, after=seite["cursor"])["data"] == []
    # Ohne Cursor von vorn geht es weiterhin, solange das Journal vollständig ist
    assert len(_feed(welt)["data"]) == 2


def test_deaktivierter_mandant_wie_eine_ruecknahme(welt: dict[str, Any]) -> None:
    """Deaktivieren nimmt den Bestand ebenfalls zurück (Issue #317): nicht gelistet, danach neuer Abschnitt."""
    _aenderung(welt)
    alt = _feed(welt)["cursor"]

    tenant_provisioning.set_tenant_active(_frisch(welt), False)
    _verraten_nichts(welt, alt, 404)

    tenant_provisioning.set_tenant_active(_frisch(welt), True)
    assert Client().get(_pfad(welt), {"after": alt}).status_code == 410
    assert _feed(welt, after=_snapshot_cursor(welt))["data"] == []


def test_wiederholte_ruecknahme_behaelt_ihren_abschnitt(welt: dict[str, Any]) -> None:
    """Der Abschnitt wechselt mit einer neuen Rücknahme, nicht mit jeder Wiederholung derselben."""
    portal_publication.end_publication(_frisch(welt), WITHDRAWN)
    welt["source"].refresh_from_db()
    zeitpunkt = publication.retracted_at(welt["source"].sync_config)
    assert zeitpunkt

    tenant_provisioning.set_tenant_active(_frisch(welt), False)
    welt["source"].refresh_from_db()
    assert publication.retracted_at(welt["source"].sync_config) == zeitpunkt

    tenant_provisioning.set_tenant_active(_frisch(welt), True)
    portal_publication.resume_publication(_frisch(welt))
    welt["source"].refresh_from_db()
    # Die Wiederherstellung behält den Zeitpunkt: Cursor aus der Zeit danach gelten weiter
    assert publication.retracted_at(welt["source"].sync_config) == zeitpunkt
    cursor = _snapshot_cursor(welt)

    portal_publication.end_publication(_frisch(welt), WITHDRAWN)
    portal_publication.resume_publication(_frisch(welt))
    welt["source"].refresh_from_db()
    assert publication.retracted_at(welt["source"].sync_config) not in ("", zeitpunkt)
    assert Client().get(_pfad(welt), {"after": cursor}).status_code == 410


def test_cursor_eines_abschnitts_gilt_nur_in_diesem() -> None:
    kommune = uuid.uuid4()
    erster = changes.encode_cursor(kommune, 7, HEUTE)
    zweiter = changes.encode_cursor(kommune, 7, HEUTE, "2026-09-30T08:00:00+00:00")

    assert erster != zweiter
    assert changes.decode_cursor(kommune, erster) == changes.Cursor(7, HEUTE)
    assert changes.decode_cursor(kommune, zweiter, "2026-09-30T08:00:00+00:00") == changes.Cursor(7, HEUTE)
    for token, abschnitt in ((erster, "2026-09-30T08:00:00+00:00"), (zweiter, ""), (zweiter, "2026-10-01")):
        with pytest.raises(changes.CursorExpiredError):
            changes.decode_cursor(kommune, token, abschnitt)
