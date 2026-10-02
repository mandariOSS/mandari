# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rücknahmen aus mandari Session erscheinen im Änderungsfeed (Issue #707).

Session nimmt ein Objekt im Moment der Änderung aus dem Bürgerportal (``retract_from_portal``): Die Zeile im
RIS-Bestand ist markiert, bevor der Ingestor die Session-Schnittstelle erneut abgleicht. Der Ingestor meldet
eine Löschmarkierung aber nur beim Übergang ``deleted = false → true`` – ohne eigene Meldung nennte der Feed
die Rücknahme nie, und Abnehmer behielten das Objekt.

Beide Ausgaben lesen dieselbe Kommune im Journal (kanonische Kennung des Body): der Aggregator unter der
Adresse des Bestands, die Session-Schnittstelle unter der Adresse des Session-Objekts. Beide nennen die
Rücknahme mit demselben Grund.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db.models import Max
from django.test import Client, override_settings
from django.utils import timezone

from apps.common.models import IdentifierBase
from apps.events.models import Event
from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionPaper, SessionTenant
from hub.api import changes
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"
API = f"{SITE}/oparl/v1"
HEUTE = date(2026, 10, 2)


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: HEUTE)


@pytest.fixture
def welt() -> Any:
    """Session-Mandant, den das Bürgerportal spiegelt; Spiegel wie ihn der Ingestor anlegt."""
    cache.clear()
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    with override_settings(
        SITE_URL=SITE,
        OPARL_BASE_URL=f"{SITE}/oparl",
        OPARL_API_RATE_LIMIT=0,
        OPARL_API_CACHE_SECONDS=0,
        OPARL_CHANGES_ENABLED=True,
        INGESTOR_EVENTS_ENABLED=True,
    ):
        tenant = SessionTenant.objects.create(
            name="Bezirk Nord", slug="nord", insight_publish=True, oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat")
        sitzung = SessionMeeting.objects.create(
            tenant=tenant, name="Ratssitzung", organization=rat, start=timezone.now(), is_public=True
        )
        vorlage = SessionPaper.objects.create(tenant=tenant, name="Radweg", is_public=True, status="approved")
        top = SessionAgendaItem.objects.create(meeting=sitzung, name="Radweg", number="1", order=1, paper=vorlage)

        quelle = OParlSource.objects.get(sync_config__session_tenant="nord")
        body = OParlBody.objects.create(external_id=f"{BASIS}body/", source=quelle, name="Bezirk Nord")
        m = OParlMeeting.objects.create(external_id=f"{BASIS}meeting/{sitzung.id}/", body=body, name="Ratssitzung")
        t = OParlAgendaItem.objects.create(external_id=f"{BASIS}agendaitem/{top.id}/", meeting=m, name="Radweg")
        p = OParlPaper.objects.create(external_id=f"{BASIS}paper/{vorlage.id}/", body=body, name="Radweg")
        yield {
            "tenant": tenant,
            "quelle": quelle,
            "body": body,
            "sitzung": sitzung,
            "vorlage": vorlage,
            "top": top,
            "m": m,
            "t": t,
            "p": p,
        }
    cache.clear()


def _nummerieren() -> None:
    """Folgenummern vergeben, wie es der Sequenzierer nach dem Commit täte."""
    for pk in Event.objects.filter(seq__isnull=True).order_by("id").values_list("pk", flat=True):
        hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
        Event.objects.filter(pk=pk).update(seq=hoechste + 1)


def _eintraege(pfad: str) -> list[dict[str, Any]]:
    _nummerieren()
    antwort = Client().get(pfad)
    assert antwort.status_code == 200, (antwort.status_code, antwort.content)
    return cast(list[dict[str, Any]], antwort.json()["data"])


def _aggregator(welt: dict[str, Any]) -> list[dict[str, Any]]:
    return _eintraege(f"/oparl/v1/body/{welt['body'].pk}/changes")


def _session() -> list[dict[str, Any]]:
    return _eintraege("/session/nord/api/oparl/body/changes/")


def _kurz(eintraege: list[dict[str, Any]]) -> list[tuple[str, str, str | None]]:
    return [(e["operation"], e["id"], e.get("reason")) for e in eintraege]


def test_nichtoeffentlicher_top_erscheint_in_beiden_feeds(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        welt["top"].is_public = False
        welt["top"].save()

    welt["t"].refresh_from_db()
    assert welt["t"].deleted is True, "sofort aus dem Bürgerportal"
    assert _kurz(_aggregator(welt)) == [("delete", f"{API}/agendaitem/{welt['t'].pk}", "nichtoeffentlich")]
    assert _kurz(_session()) == [("delete", f"{BASIS}agendaitem/{welt['top'].id}/", "nichtoeffentlich")]


def test_der_spaetere_abgleich_meldet_nicht_noch_einmal(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    """Der Ingestor markiert nur unmarkierte Zeilen (``UPDATE … WHERE deleted = false``) und meldet nur dann."""
    with django_capture_on_commit_callbacks(execute=True):
        welt["top"].is_public = False
        welt["top"].save()

    assert OParlAgendaItem.objects.filter(pk=welt["t"].pk, deleted=False).count() == 0
    assert Event.objects.count() == 1


def test_vorlage_zurueck_im_entwurf_ist_zurueckgenommen(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        welt["vorlage"].status = "draft"
        welt["vorlage"].save()

    assert ("delete", f"{API}/paper/{welt['p'].pk}", "zurueckgenommen") in _kurz(_aggregator(welt))
    assert ("delete", f"{BASIS}paper/{welt['vorlage'].id}/", "zurueckgenommen") in _kurz(_session())


def test_geloeschte_sitzung_ist_in_der_quelle_geloescht(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        welt["sitzung"].delete()

    eintraege = _kurz(_aggregator(welt))
    assert ("delete", f"{API}/meeting/{welt['m'].pk}", "quelle_geloescht") in eintraege
    assert ("delete", f"{API}/agendaitem/{welt['t'].pk}", "quelle_geloescht") in eintraege


def test_ohne_schalter_zurueckgenommen_aber_nicht_gemeldet(
    welt: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    with override_settings(INGESTOR_EVENTS_ENABLED=False), django_capture_on_commit_callbacks(execute=True):
        welt["top"].is_public = False
        welt["top"].save()

    welt["t"].refresh_from_db()
    assert welt["t"].deleted is True
    assert not Event.objects.exists()
