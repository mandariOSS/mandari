# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ankündigung im Hinweisband auf Start im Browser (Issue #857).

Das Band zeigt die neueste ungelesene Ankündigung, ist barrierefrei (axe-core) und verschwindet nach dem
Wegklicken – auch nach dem Neuladen. Bilder hell, dunkel und am Handy für die Durchsicht.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect


def _ankuendigen() -> None:
    call_command(
        "work_ankuendigung",
        "--schluessel",
        "e2e-update",
        "--titel",
        "Was ist neu in Work",
        "--text",
        "In der Hilfe lesen Sie, was sich in Work zuletzt geändert hat.",
        "--link",
        "https://docs.mandari.de/work/was-ist-neu/",
        "--linktext",
        "Was ist neu",
        "--rueckmeldung",
        stdout=StringIO(),
    )


def _assert_axe_clean(page: Any) -> None:
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        "async () => await axe.run('[data-testid=hinweisband]', { resultTypes: ['violations'], runOnly: { type: 'tag',"
        " values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
    )
    befunde = AxeResult(violations=json.loads(json.dumps(ergebnis.get("violations", []))))
    assert not befunde.failing, f"axe: kritische/schwere Befunde im Hinweisband:\n{befunde.describe()}"


def test_band_zeigen_und_wegklicken(
    page: Any,
    goto: Any,
    login: Any,
    member_user: tuple[Any, str],
    screenshot: Any,
    dark_mode: Any,
    problems: BrowserProblems,
) -> None:
    membership, password = member_user
    _ankuendigen()
    login(membership.user.email, password)
    start = f"/work/{membership.organization.slug}/"

    goto(start)
    band = page.get_by_test_id("hinweisband")
    expect(band).to_be_visible()
    expect(band).to_contain_text("Was ist neu in Work")
    expect(band.get_by_role("link", name="Was ist neu")).to_have_attribute(
        "href", "https://docs.mandari.de/work/was-ist-neu/"
    )
    expect(band.get_by_role("link", name="Rückmeldung geben")).to_be_visible()
    _assert_axe_clean(page)
    screenshot("work-start-hinweisband")
    dark_mode(True)
    screenshot("work-start-hinweisband-dunkel")
    dark_mode(False)
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    expect(page.get_by_test_id("hinweisband")).to_be_visible()
    screenshot("work-start-hinweisband-handy")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.reload()

    with page.expect_response(lambda antwort: "/mark-read/" in antwort.url):
        page.get_by_role("button", name="Hinweis ausblenden").click()
    expect(page.get_by_test_id("hinweisband")).to_have_count(0)

    goto(start)
    expect(page.get_by_test_id("hinweisband")).to_have_count(0)
    problems.assert_clean("Hinweisband auf Start")
