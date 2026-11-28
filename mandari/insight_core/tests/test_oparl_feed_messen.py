# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``manage.py oparl_feed_messen`` (Issue #707): Ereignismenge und Einträge des Änderungsfeeds messen, bevor er
eingeschaltet wird – auf demselben Weg wie ``…/kommparl/changes``, ohne den Schalter und ohne Ausgabe an Abnehmer.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.events.models import Event
from hub.api import changes
from hub.api.tests.ereignisse import ereignis
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
RIS = "https://ris.example/oparl"
SESSION = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"


@pytest.fixture(autouse=True)
def _heute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "today", lambda: date(2026, 10, 2))


@pytest.fixture
def kommune() -> Any:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_CHANGES_ENABLED=False):
        quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
        body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Musterstadt")
        sitzung = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=body, name="Rat")
        punkt = OParlAgendaItem.objects.create(external_id=f"{RIS}/agendaitem/1", meeting=sitzung, name="TOP 1")
        yield {"body": body, "sitzung": sitzung, "punkt": punkt}
    cache.clear()


def _aufruf(*argumente: str) -> str:
    ausgabe = io.StringIO()
    call_command("oparl_feed_messen", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


def test_zaehlt_ereignisse_und_eintraege_im_zeitraum(kommune: dict[str, Any]) -> None:
    body, sitzung, punkt = kommune["body"], kommune["sitzung"], kommune["punkt"]
    alt = ereignis("ris.meeting.changed", body.pk, sitzung.pk, nutzlast={"changed": ["name"]})
    Event.objects.filter(pk=alt.pk).update(recorded_at=timezone.now() - timedelta(days=3))
    nutzlast = {"meeting": str(sitzung.pk), "change": "changed", "changed": ["name"]}
    ereignis("ris.agendaitem.changed", body.pk, punkt.pk, nutzlast=nutzlast)
    # Was Session selbst meldet, liest der Aggregator nicht
    ereignis("ris.meeting.changed", body.pk, sitzung.pk, nutzlast={"changed": ["name"]}, mandant=SESSION)

    ausgabe = _aufruf()

    assert "alle Kommunen: 1" in ausgabe
    assert "Musterstadt" in ausgabe
    assert "Ereignisse: 1 (ris.agendaitem.changed 1)" in ausgabe
    # Der Tagesordnungspunkt und die Sitzung, die ihn einbettet
    assert "Einträge:   2 (AgendaItem 1, Meeting 1)" in ausgabe or "Einträge:   2 (Meeting 1, AgendaItem 1)" in ausgabe


def test_bestimmte_kommune_und_zeitraum(kommune: dict[str, Any]) -> None:
    body, sitzung = kommune["body"], kommune["sitzung"]
    alt = ereignis("ris.meeting.changed", body.pk, sitzung.pk, nutzlast={"changed": ["name"]})
    Event.objects.filter(pk=alt.pk).update(recorded_at=timezone.now() - timedelta(days=3))

    assert "Ereignisse: 0" in _aufruf("--body", str(body.pk))
    assert "Ereignisse: 1 (ris.meeting.changed 1)" in _aufruf("--body", str(body.pk), "--stunden", "96")


def test_ohne_ereignisse_und_falsche_angaben(kommune: dict[str, Any]) -> None:
    assert "Keine Kommune mit Ereignissen" in _aufruf()
    with pytest.raises(CommandError):
        _aufruf("--body", "keine-uuid")
    with pytest.raises(CommandError):
        _aufruf("--stunden", "0")
