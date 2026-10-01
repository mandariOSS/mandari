# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungscockpit mit zwei Browsern (Issue #140).

Läuft gegen den ASGI-Testserver (Daphne im Thread, In-Memory-Channel-Layer), weil der WSGI-Live-Server keine
WebSockets kann. Browser A gehört der Sitzungsleitung (Recht „Sitzungen leiten“), Browser B einer Person mit
Leserecht (Mitlese-Ansicht):

- Ruft A einen TOP auf, sieht B den Wechsel binnen zwei Sekunden (Hinweis über Channels, Stand per HTMX).
- Ohne WebSocket-Verbindung holt B den Stand im Polling-Rückfall (alle zwei Sekunden).
- Die Mitlese-Ansicht hat keine Bedienelemente; beide Ansichten sind ohne schwere axe-Befunde.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.common.tests.factories import DEFAULT_PASSWORD, UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from tests_e2e.conftest import (
    AXE_PATH,
    SCREENSHOT_DIR,
    AxeResult,
    login_via_form,
    wait_for_bundle,
    wait_for_component,
)

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

WS_COCKPIT = re.compile(r"/ws/session/")
LIVE = "() => window.Alpine.$data(document.querySelector('[x-data=\"meetingCockpit\"]')).live === 'ws'"
#: Akzeptanzkriterium aus Issue #140: Zwei Browser sehen einen TOP-Wechsel binnen zwei Sekunden
ZWEI_SEKUNDEN = 2000


@dataclass
class Cockpit:
    tenant: SessionTenant
    meeting: SessionMeeting
    leitung: SessionUser
    leser: SessionUser

    def url(self, server: Any) -> str:
        return f"{server.url}/session/{self.tenant.slug}/meetings/{self.meeting.pk}/cockpit/"


def _konto(tenant: SessionTenant, email: str, **rechte: bool) -> SessionUser:
    rolle = SessionRole.objects.create(tenant=tenant, name=email, can_view_meetings=True, **rechte)
    konto = SessionUser.objects.create(user=cast(Any, UserFactory)(email=email), tenant=tenant, is_active=True)
    konto.roles.add(rolle)
    return konto


@pytest.fixture
def cockpit() -> Cockpit:
    tenant = SessionTenant.objects.create(name="E2E-Gemeinde", slug="e2e-gemeinde")
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=timezone.now(), is_public=True
    )
    for order, (nummer, name) in enumerate([("1", "Eröffnung"), ("2", "Radweg Hauptstraße"), ("3", "Haushalt")]):
        SessionAgendaItem.objects.create(meeting=meeting, number=nummer, name=name, order=order)
    for name in ("Amsel", "Buche", "Carl"):
        person = SessionPerson.objects.create(tenant=tenant, given_name="P", family_name=name)
        SessionAttendance.objects.create(meeting=meeting, person=person, status="present")
    return Cockpit(
        tenant=tenant,
        meeting=meeting,
        leitung=_konto(tenant, "leitung@example.org", can_conduct_meetings=True, can_edit_protocols=True),
        leser=_konto(tenant, "leser@example.org"),
    )


@pytest.fixture
def zwei_browser(new_context: Any) -> Iterator[tuple[Any, Any]]:
    """Zwei unabhängige Browserkontexte (eigene Cookies/Sitzungen), je eine Seite."""
    yield new_context().new_page(), new_context().new_page()


def _fehler_sammeln(page: Any) -> list[str]:
    fehler: list[str] = []
    page.on("pageerror", lambda error: fehler.append(f"Ausnahme: {error}"))
    page.on("console", lambda msg: fehler.append(msg.text) if "Alpine Expression Error" in msg.text else None)
    return fehler


def _axe(page: Any) -> AxeResult:
    """axe-core auf einer beliebigen Seite (das Fixture ``axe`` prüft nur die Standardseite)."""
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    result = page.evaluate(
        "async () => await axe.run(document, { resultTypes: ['violations'], "
        "runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
    )
    return AxeResult(violations=json.loads(json.dumps(result.get("violations", []))))


def _oeffnen(page: Any, server: Any, konto: SessionUser, url: str, *, live: bool = True) -> None:
    login_via_form(page, server.url, konto.user.email, DEFAULT_PASSWORD)
    page.goto(url)
    wait_for_bundle(page)
    wait_for_component(page, "meetingCockpit")
    if live:
        page.wait_for_function(LIVE, timeout=15000)


def test_top_wechsel_erscheint_im_zweiten_browser(
    asgi_server: Any, cockpit: Cockpit, zwei_browser: tuple[Any, Any]
) -> None:
    leitung, leser = zwei_browser
    fehler = _fehler_sammeln(leitung) + _fehler_sammeln(leser)
    url = cockpit.url(asgi_server)
    _oeffnen(leitung, asgi_server, cockpit.leitung, url)
    _oeffnen(leser, asgi_server, cockpit.leser, url)

    # Mitlese-Ansicht: Stand ohne Bedienelemente
    expect(leser.get_by_test_id("mitlesen")).to_be_visible()
    assert leser.locator("[name=aktion]").count() == 0
    expect(leser.get_by_test_id("aktueller-top")).to_contain_text("Kein Tagesordnungspunkt aufgerufen")

    leitung.get_by_test_id("aufrufen-2").click()
    expect(leitung.get_by_test_id("aktueller-top")).to_contain_text("TOP 2: Radweg Hauptstraße")
    # Rückmeldung genau einmal (HX-Trigger „showToast“ löst htmx auch als „show-toast“ aus)
    expect(leitung.locator('[x-data="toastManager"]').get_by_text("TOP 2 aufgerufen")).to_have_count(1)
    expect(leser.get_by_test_id("aktueller-top")).to_contain_text("TOP 2: Radweg Hauptstraße", timeout=ZWEI_SEKUNDEN)
    expect(leser.get_by_test_id("sitzungsstatus")).to_contain_text("Sitzung läuft", timeout=ZWEI_SEKUNDEN)

    # Nächster TOP über „Weiter mit …“: beide Ansichten folgen
    leitung.get_by_test_id("naechster_top").click()
    expect(leser.get_by_test_id("aktueller-top")).to_contain_text("TOP 3: Haushalt", timeout=ZWEI_SEKUNDEN)
    zwei = SessionAgendaItem.objects.get(meeting=cockpit.meeting, number="2")
    assert zwei.start_time is not None and zwei.end_time is not None

    for page, name in ((leitung, "Cockpit"), (leser, "Mitlese-Ansicht")):
        befunde = _axe(page)
        assert not befunde.failing, f"{name}: {befunde.describe()}"
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    leitung.screenshot(path=str(SCREENSHOT_DIR / "session-cockpit.png"), full_page=True)
    leser.screenshot(path=str(SCREENSHOT_DIR / "session-cockpit-mitlesen.png"), full_page=True)
    assert not fehler, "\n".join(fehler)


def test_polling_rueckfall_ohne_websocket(asgi_server: Any, cockpit: Cockpit, zwei_browser: tuple[Any, Any]) -> None:
    leitung, leser = zwei_browser
    url = cockpit.url(asgi_server)
    # Verbindungsversuche des Lesenden bleiben hängen: kein WebSocket, nur Polling
    leser.route_web_socket(WS_COCKPIT, lambda ws: None)
    _oeffnen(leitung, asgi_server, cockpit.leitung, url)
    _oeffnen(leser, asgi_server, cockpit.leser, url, live=False)
    expect(leser.get_by_test_id("verbindung")).to_contain_text("Aktualisierung alle 2 Sekunden")

    leitung.get_by_test_id("aufrufen-1").click()
    expect(leser.get_by_test_id("aktueller-top")).to_contain_text("TOP 1: Eröffnung", timeout=5000)
