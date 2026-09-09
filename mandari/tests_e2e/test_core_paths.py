# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kernpfade im Browser: Vite-Bundle aktiv, Komponenten bedienbar, axe-core ohne kritische Befunde,
Screenshots hell/dunkel (Issues #168, #170, #176).
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db(transaction=True)


def _assert_axe_clean(result: Any, page_name: str) -> None:
    failing = result.failing
    assert not failing, f"axe: {len(failing)} kritische/schwere Befunde auf {page_name}:\n{result.describe()}"


class TestLoginPage:
    def test_bundle_and_password_toggle(self, page: Any, goto: Any, axe: Any, screenshot: Any, dark_mode: Any) -> None:
        goto("/accounts/login/")
        # Vite-Bundle ausgeführt: Alpine, HTMX und Icon-Observer sind da
        assert page.evaluate("() => typeof window.Alpine !== 'undefined' && typeof window.htmx !== 'undefined'")
        assert page.locator("svg.lucide").count() > 0, "Lucide-Icons wurden nicht gerendert"
        # Passwortfeld-Komponente: Anzeigen/Verbergen
        password = page.locator("input[name=password]")
        assert password.get_attribute("type") == "password"
        page.click("button[aria-label='Passwort anzeigen']")
        assert password.get_attribute("type") == "text"
        _assert_axe_clean(axe(), "Login")
        screenshot("login-hell")
        dark_mode(True)
        assert page.evaluate("() => document.documentElement.classList.contains('dark')")
        screenshot("login-dunkel")
        dark_mode(False)

    def test_wrong_password_shows_error(self, page: Any, goto: Any, axe: Any) -> None:
        goto("/accounts/login/")
        page.fill("input[name=email]", "niemand@example.org")
        page.fill("input[name=password]", "falsch-falsch-falsch")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert page.locator("[role=alert]").count() >= 1
        _assert_axe_clean(axe(), "Login mit Fehler")

    def test_password_reset_pages(self, page: Any, goto: Any, axe: Any, screenshot: Any) -> None:
        goto("/accounts/password-reset/")
        _assert_axe_clean(axe(), "Passwort zurücksetzen")
        screenshot("passwort-zuruecksetzen")


class TestUiKit:
    def test_components_render_and_are_accessible(
        self, page: Any, goto: Any, axe: Any, screenshot: Any, dark_mode: Any
    ) -> None:
        goto("/dev/ui/")
        assert page.locator("h1", has_text="UI-Kit").count() == 1
        _assert_axe_clean(axe(), "UI-Kit")
        screenshot("ui-kit-hell")
        dark_mode(True)
        screenshot("ui-kit-dunkel")
        dark_mode(False)

    def test_modal_focus_and_escape(self, page: Any, goto: Any) -> None:
        goto("/dev/ui/")
        page.click("text=Modal öffnen")
        dialog = page.locator("dialog#demo-modal")
        expect(dialog).to_be_visible()
        assert dialog.evaluate("el => el.open")
        # Fokus liegt im Dialog (natives <dialog>: Fokusfalle durch den Browser)
        assert page.evaluate("() => document.activeElement.closest('dialog#demo-modal') !== null")
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        page.click("text=Modal öffnen")
        expect(dialog).to_be_visible()
        page.click("dialog#demo-modal button[aria-label='Schließen']")
        expect(dialog).to_be_hidden()

    def test_tabs_keyboard_navigation(self, page: Any, goto: Any) -> None:
        goto("/dev/ui/")
        first = page.locator("[role=tab]#tab-allgemein")
        second = page.locator("[role=tab]#tab-rechte")
        expect(first).to_have_attribute("aria-selected", "true")
        expect(page.locator("#panel-allgemein")).to_be_visible()
        expect(page.locator("#panel-rechte")).to_be_hidden()
        first.focus()
        page.keyboard.press("ArrowRight")
        expect(second).to_have_attribute("aria-selected", "true")
        expect(page.locator("#panel-rechte")).to_be_visible()
        page.keyboard.press("Home")
        expect(first).to_have_attribute("aria-selected", "true")


class TestWorkPortal:
    def test_login_and_dashboard(
        self, page: Any, goto: Any, login: Any, member_user: Any, axe: Any, screenshot: Any
    ) -> None:
        membership, password = member_user
        login(membership.user.email, password)
        assert "/accounts/login" not in page.url, "Anmeldung fehlgeschlagen"
        goto(f"/work/{membership.organization.slug}/dashboard/")
        assert page.locator("body").count() == 1
        assert page.evaluate("() => typeof window.Alpine !== 'undefined'")
        _assert_axe_clean(axe(), "Work-Dashboard")
        screenshot("work-dashboard")
