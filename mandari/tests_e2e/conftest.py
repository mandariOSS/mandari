# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures für die E2E-Tests: Skip-Schalter, axe-core, Screenshots, Anmeldung."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@pytest.fixture
def login(page: Any, live_server: Any) -> Callable[[str, str], None]:
    """Meldet einen Nutzer über das Login-Formular an (prüft dabei Alpine/Vite im Browser)."""

    def do_login(email: str, password: str) -> None:
        page.goto(f"{live_server.url}/accounts/login/")
        wait_for_bundle(page)
        page.fill("input[name=email]", email)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

    return do_login


@pytest.fixture
def member_user(db: Any, org: Any, make_member: Any) -> Iterator[tuple[Any, str]]:
    """Mitglied mit Dashboard-Rechten und bekanntem Passwort."""
    password = "E2e-Passwort-123456"
    membership = make_member(org, ["dashboard.view", "tasks.view", "motions.view"], email="e2e@example.org")
    membership.user.set_password(password)
    membership.user.save(update_fields=["password"])
    yield membership, password
