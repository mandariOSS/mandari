# SPDX-License-Identifier: AGPL-3.0-or-later
"""
htmx ohne Inline-JavaScript (#172): ``allowEval`` ist aus, Nachbearbeitung läuft über
``data-autosave`` und ``data-after-request`` (``frontend/js/htmx-setup.ts``). Geprüft mit dem
echten Bundle auf der Login-Seite; als Ziel dient ``/health/`` (GET, immer erreichbar).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db


def _seite_mit_fragment(page: Any, live_server: Any, html: str) -> None:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    page.evaluate(
        "(html) => { const box = document.createElement('div'); box.id = 'probe'; box.innerHTML = html;"
        " document.body.append(box); window.htmx.process(box) }",
        html,
    )


def test_eval_ist_aus(page: Any, live_server: Any) -> None:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    assert page.evaluate("() => window.htmx.config.allowEval") is False
    assert page.evaluate("() => window.htmx.config.selfRequestsOnly") is True


def test_data_autosave_loest_window_events_aus(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<form id="f" hx-get="/health/" hx-trigger="submit" hx-swap="none" data-autosave="probe">'
        '<button id="b" type="submit">Speichern</button></form>',
    )
    page.evaluate(
        "() => { window._ev = []; for (const n of ['probe-autosaving', 'probe-autosaved'])"
        " window.addEventListener(n, () => window._ev.push(n)) }"
    )
    page.click("#b")
    page.wait_for_function("() => window._ev.length === 2")
    assert page.evaluate("() => window._ev") == ["probe-autosaving", "probe-autosaved"]


def test_data_after_request_reset_und_reload(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<form id="f" hx-get="/health/" hx-trigger="submit" hx-swap="none" data-after-request="reset" data-blur="#t">'
        '<input id="t" name="t" value=""><button id="b" type="submit">Senden</button></form>',
    )
    page.fill("#t", "Text")
    page.focus("#t")
    page.click("#b")
    expect(page.locator("#t")).to_have_value("")
    assert page.evaluate("() => document.activeElement.id") != "t"

    _seite_mit_fragment(
        page, live_server, '<button id="r" hx-get="/health/" hx-swap="none" data-after-request="reload">Neu</button>'
    )
    page.evaluate("() => { window._marker = true }")
    page.click("#r")
    page.wait_for_function("() => window._marker === undefined")
    assert "/accounts/login/" in page.url


def test_data_after_request_notification_read(page: Any, live_server: Any) -> None:
    _seite_mit_fragment(
        page,
        live_server,
        '<div id="eintrag" class="p-4 bg-primary-50/50"><span class="inline-block w-2 h-2"></span>'
        '<button id="b" hx-get="/health/" hx-swap="none" data-after-request="notification-read" data-parent=".p-4">gelesen</button></div>',
    )
    page.click("#b")
    expect(page.locator("#b")).to_have_count(0)
    expect(page.locator("#eintrag .inline-block")).to_have_count(0)
    assert "bg-primary-50/50" not in page.get_attribute("#eintrag", "class")
