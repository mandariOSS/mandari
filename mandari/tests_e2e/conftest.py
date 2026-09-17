# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures für die E2E-Tests: Skip-Schalter, axe-core, Screenshots, Anmeldung."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings

PROJECT_DIR = Path(__file__).resolve().parents[1]
AXE_PATH = PROJECT_DIR / "node_modules" / "axe-core" / "axe.min.js"
MANIFEST_PATH = PROJECT_DIR / "static" / "dist" / "manifest.json"
SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"

#: axe-Regeln, die auf allen Seiten ignoriert werden (mit Begründung)
AXE_IGNORED_RULES: dict[str, str] = {
    # Farbkontrast wird im Design-Token-Schritt (#171) behandelt; bis dahin informativ
    "color-contrast": "Kontraste kommen mit den Design-Tokens (#171)",
}
#: Ab dieser Schwere schlägt ein Test fehl
FAILING_IMPACTS = ("critical", "serious")


def pytest_configure(config: pytest.Config) -> None:
    # Windows: Daphne/Twisted setzen die Selector-Loop-Policy, die keine Subprozesse kann –
    # Playwright braucht die Proactor-Loop, um den Browser zu starten.
    if os.environ.get("MANDARI_E2E") != "1":
        return
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    # Playwrights Sync-API hält im Testthread eine laufende Event-Loop; Django würde ORM-Aufrufe
    # dort als "async unsafe" ablehnen. Für Tests ist das unkritisch (kein echter async Code).
    os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("MANDARI_E2E") == "1":
        # Die Live-Server-Threads nutzen eigene Verbindungen zur Datei-SQLite (settings_test): Testdaten
        # müssen wirklich committet sein, sonst sieht der Server sie nicht.
        for item in items:
            if "tests_e2e" in str(item.fspath):
                item.add_marker(pytest.mark.django_db(transaction=True))
        return
    skip = pytest.mark.skip(reason="E2E nur mit MANDARI_E2E=1 (Playwright + gebaute Assets)")
    for item in items:
        if "tests_e2e" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _e2e_environment() -> None:
    if os.environ.get("MANDARI_E2E") != "1":
        return
    if not MANIFEST_PATH.exists():
        pytest.exit("Vite-Manifest fehlt – vorher `npm run build` ausführen", returncode=3)
    if not AXE_PATH.exists():
        pytest.exit("axe-core fehlt – vorher `npm ci` ausführen", returncode=3)
    # settings_test schaltet django-vite bei MANDARI_E2E=1 auf die gebauten Assets (kein Dev-Server)
    assert settings.DJANGO_VITE["default"]["dev_mode"] is False, "MANDARI_E2E=1 muss vor dem Start gesetzt sein"
    SCREENSHOT_DIR.mkdir(exist_ok=True)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return {**browser_context_args, "viewport": {"width": 1280, "height": 900}, "locale": "de-DE"}


@dataclass
class AxeResult:
    violations: list[dict[str, Any]]

    @property
    def failing(self) -> list[dict[str, Any]]:
        return [v for v in self.violations if v.get("impact") in FAILING_IMPACTS and v["id"] not in AXE_IGNORED_RULES]

    def describe(self) -> str:
        lines = []
        for v in self.violations:
            targets = ", ".join(n["target"][0] for n in v.get("nodes", [])[:3])
            lines.append(f"[{v.get('impact')}] {v['id']}: {v['help']} → {targets}")
        return "\n".join(lines) or "keine Befunde"


@pytest.fixture
def axe(page: Any) -> Callable[[], AxeResult]:
    """Führt axe-core auf der aktuellen Seite aus und liefert die Verstöße."""
    script = AXE_PATH.read_text(encoding="utf-8")

    def run() -> AxeResult:
        page.add_script_tag(content=script)
        result = page.evaluate(
            "async () => await axe.run(document, { resultTypes: ['violations'], "
            "runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
        )
        violations = json.loads(json.dumps(result.get("violations", [])))
        return AxeResult(violations=violations)

    return run


@pytest.fixture
def goto(page: Any, live_server: Any) -> Callable[[str], None]:
    """Öffnet einen Pfad und wartet, bis das Vite-Bundle (Alpine) ausgeführt wurde."""

    def _goto(path: str) -> None:
        page.goto(f"{live_server.url}{path}")
        wait_for_bundle(page)

    return _goto


def wait_for_bundle(page: Any) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined", timeout=15000)


@pytest.fixture
def screenshot(page: Any) -> Callable[[str], Path]:
    """Ganzseitiger Screenshot unter tests_e2e/screenshots/<name>.png (hell) – Dunkelmodus per Name-Suffix."""

    def take(name: str) -> Path:
        target = SCREENSHOT_DIR / f"{name}.png"
        page.screenshot(path=str(target), full_page=True)
        return target

    return take


@pytest.fixture
def dark_mode(page: Any) -> Callable[[bool], None]:
    """Schaltet den Dunkelmodus der Layouts (localStorage 'darkMode') und lädt neu."""

    def toggle(enabled: bool) -> None:
        page.evaluate(f"() => localStorage.setItem('darkMode', '{'true' if enabled else 'false'}')")
        page.reload()
        wait_for_bundle(page)

    return toggle


def login_via_form(page: Any, base_url: str, email: str, password: str) -> None:
    """Meldet einen Nutzer über das Login-Formular des Servers unter ``base_url`` an."""
    page.goto(f"{base_url}/accounts/login/")
    wait_for_bundle(page)
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


@pytest.fixture
def login(page: Any, live_server: Any) -> Callable[[str, str], None]:
    """Meldet einen Nutzer über das Login-Formular an (prüft dabei Alpine/Vite im Browser)."""

    def do_login(email: str, password: str) -> None:
        login_via_form(page, live_server.url, email, password)

    return do_login


class AsgiLiveServer:
    """
    Daphne im Thread desselben Prozesses – für Kollaborationstests mit WebSockets.

    Der Live-Server von pytest-django spricht nur WSGI; die Yjs-Kollaboration braucht
    Channels über ASGI. Läuft der Server im Testprozess, reicht der In-Memory-Channel-Layer
    (kein Redis): HTTP-Views (Reload-Broadcast nach POST-Speichern) und WebSocket-Consumer
    teilen sich dieselbe Event-Loop des Reactors. Die Datenbank ist die Datei-SQLite aus
    settings_test (bzw. die CI-Datenbank); die Server-Threads öffnen eigene Verbindungen,
    darum laufen die E2E-Tests transaktional (siehe pytest_collection_modifyitems).
    """

    host = "localhost"

    def __init__(self) -> None:
        from daphne.server import Server
        from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

        from mandari.asgi import application

        self._ready = threading.Event()
        # Der Static-Handler bedient nur HTTP-Pfade unter STATIC_URL (über die Finder, wie der
        # WSGI-Live-Server) und reicht alles andere – auch WebSockets – an den Router durch.
        self._server = Server(
            application=ASGIStaticFilesHandler(application),
            endpoints=[f"tcp:port=0:interface={self.host}"],
            signal_handlers=False,  # Signale gehören dem Hauptthread (pytest)
            ready_callable=self._ready.set,
        )
        self._thread = threading.Thread(target=self._server.run, name="e2e-asgi-server", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._ready.is_set() and self._server.listening_addresses:
                break
            if not self._thread.is_alive():
                raise RuntimeError("ASGI-Testserver ist beim Start abgebrochen")
            time.sleep(0.05)
        else:
            raise RuntimeError("ASGI-Testserver hat innerhalb von 15 s keinen Port geöffnet")
        self.port: int = self._server.listening_addresses[0][1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        from twisted.internet import reactor

        # Der Reactor läuft im Server-Thread; stop() muss von dort ausgeführt werden.
        cast(Any, reactor).callFromThread(cast(Any, reactor).stop)
        self._thread.join(timeout=10)


@pytest.fixture(scope="session")
def asgi_server(django_db_setup: Any) -> Iterator[AsgiLiveServer]:
    """ASGI-Testserver (Daphne, In-Memory-Channel-Layer) – nur für Kollaborationstests."""
    from channels.layers import InMemoryChannelLayer, get_channel_layer

    # Ohne REDIS_URL wählt settings_test den In-Memory-Layer; mit Redis wäre der Server
    # zwar lauffähig, der Test soll aber ausdrücklich ohne laufen (#289).
    assert isinstance(get_channel_layer(), InMemoryChannelLayer), "Kollaborationstests erwarten den In-Memory-Layer"
    server = AsgiLiveServer()
    yield server
    server.stop()


@pytest.fixture
def member_user(db: Any, org: Any, make_member: Any) -> Iterator[tuple[Any, str]]:
    """Mitglied mit Dashboard-Rechten und bekanntem Passwort."""
    password = "E2e-Passwort-123456"
    membership = make_member(org, ["dashboard.view", "tasks.view", "motions.view"], email="e2e@example.org")
    membership.user.set_password(password)
    membership.user.save(update_fields=["password"])
    yield membership, password


@pytest.fixture
def session_user(db: Any) -> Iterator[tuple[Any, str]]:
    """
    Session-Portal: Mandant mit Gremium, öffentlicher Sitzung und Vorlage sowie ein Nutzer mit
    Standardrolle (Dashboard, Sitzungen, Vorlagen ansehen) – für Barrierefreiheitsprüfungen (#44, #176).
    """
    from django.utils import timezone

    from apps.common.tests.factories import DEFAULT_PASSWORD, UserFactory
    from apps.session.models import (
        SessionMeeting,
        SessionOrganization,
        SessionPaper,
        SessionRole,
        SessionTenant,
        SessionUser,
    )

    tenant = SessionTenant.objects.create(name="E2E-Stadt", slug="e2e-stadt")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=gremium, start=timezone.now(), is_public=True
    )
    SessionPaper.objects.create(tenant=tenant, reference="V/2026/1", name="Haushaltsvorlage", is_public=True)
    role = SessionRole.objects.create(tenant=tenant, name="Lesend")
    user = UserFactory(email="session-e2e@example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    session_user.roles.add(role)
    yield session_user, DEFAULT_PASSWORD
