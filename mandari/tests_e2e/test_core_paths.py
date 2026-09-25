# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kernpfade im Browser: Vite-Bundle aktiv, Komponenten bedienbar, axe-core ohne kritische Befunde,
Screenshots hell/dunkel (Issues #168, #170, #176).
"""

from __future__ import annotations

from typing import Any

import pytest

# Ohne installiertes Playwright (z. B. im normalen Test-Job) wird das Modul übersprungen statt zu scheitern
playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

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

    def test_alpine_modal_traps_focus_and_restores_it(self, page: Any, goto: Any) -> None:
        """Alpine-Modal (#176): role=dialog, Fokusfalle, Escape schließt, Fokus kehrt zum Auslöser zurück."""
        goto("/dev/ui/")
        # CSS-Locator statt Rolle: während der Dialog offen ist, liegt der Auslöser unter aria-hidden
        trigger = page.locator("button", has_text="Alpine-Modal öffnen")
        trigger.click()
        dialog = page.locator("[role=dialog][aria-modal=true]", has_text="Beispiel-Dialog")
        expect(dialog).to_be_visible()
        # Fokus liegt im Dialog und bleibt beim Tabben darin (x-trap)
        expect(dialog.locator(":focus")).to_have_count(1)
        for _ in range(6):
            page.keyboard.press("Tab")
            assert page.evaluate("() => document.activeElement.closest('[role=dialog]') !== null"), (
                "Fokus verließ den Dialog"
            )
        # Hintergrund ist für Hilfstechnik verborgen (x-trap.inert setzt aria-hidden außerhalb des Dialogs)
        assert trigger.evaluate("el => !!el.closest('[aria-hidden=\"true\"]')"), "Hintergrund nicht verborgen"
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(trigger).to_be_focused()

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
        goto(f"/work/{membership.organization.slug}/documents/")
        _assert_axe_clean(axe(), "Dokumentenliste")
        goto(f"/work/{membership.organization.slug}/tasks/")
        _assert_axe_clean(axe(), "Aufgaben")


class TestSessionPortal:
    def test_dashboard_sitzungen_vorlagen_barrierefrei(
        self, page: Any, goto: Any, login: Any, session_user: Any, axe: Any, screenshot: Any
    ) -> None:
        """Session-Portal in der axe-Prüfung (#44, #176): Dashboard, Sitzungsliste, Vorlagenliste."""
        su, password = session_user
        login(su.user.email, password)
        assert "/accounts/login" not in page.url, "Anmeldung fehlgeschlagen"
        for pfad, name in (
            ("", "Session-Dashboard"),
            ("meetings/", "Session-Sitzungen"),
            ("papers/", "Session-Vorlagen"),
        ):
            goto(f"/session/{su.tenant.slug}/{pfad}")
            assert page.locator("body").count() == 1
            _assert_axe_clean(axe(), name)
        screenshot("session-dashboard")

    def test_leitstelle_barrierefrei(
        self, page: Any, goto: Any, login: Any, session_user: Any, axe: Any, screenshot: Any, dark_mode: Any
    ) -> None:
        """Leitstellen-Übersicht einer Mandantengruppe (Issue #317): axe ohne schwere Befunde, Screenshots hell/dunkel."""
        from apps.session.models import SessionTenant, SessionTenantGroup, SessionTenantGroupMembership

        su, password = session_user
        gruppe = SessionTenantGroup.objects.create(name="E2E-Bezirke", slug="e2e-bezirke")
        gruppe.tenant_links.create(tenant=su.tenant)
        gruppe.tenant_links.create(tenant=SessionTenant.objects.create(name="E2E-Nachbarbezirk", slug="e2e-nachbar"))
        SessionTenantGroupMembership.objects.create(group=gruppe, user=su.user)
        login(su.user.email, password)
        goto("/session/leitstelle/e2e-bezirke/")
        expect(page.get_by_test_id("leitstelle-tabelle")).to_contain_text("E2E-Nachbarbezirk")
        _assert_axe_clean(axe(), "Leitstelle")
        screenshot("session-leitstelle")
        dark_mode(True)
        screenshot("session-leitstelle-dunkel")
        dark_mode(False)


class TestInsightPortal:
    def test_startseite_barrierefrei(self, page: Any, goto: Any, axe: Any, screenshot: Any) -> None:
        goto("/")
        assert page.locator("body").count() == 1
        _assert_axe_clean(axe(), "Insight-Startseite")
        screenshot("insight-start")
