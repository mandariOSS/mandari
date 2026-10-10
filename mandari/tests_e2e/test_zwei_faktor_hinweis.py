# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Empfehlung zum zweiten Faktor im Hinweisband auf Start und die Angabe in der Mitgliederliste im Browser.

Das Band erscheint nur ohne zweiten Faktor, ist barrierefrei (axe-core), „Einrichten“ führt zu Profil › Sicherheit,
„Später“ blendet es aus, auch nach dem Neuladen. Bilder hell, dunkel und am Handy für die Durchsicht.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect


@pytest.fixture
def durchsetzung(settings: Any) -> None:
    """Die Empfehlung gilt wie die Pflicht nur mit Durchsetzung (Produktion)."""
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = []


def _assert_axe_clean(page: Any, selektor: str) -> None:
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        f"async () => await axe.run('{selektor}', {{ resultTypes: ['violations'], runOnly: {{ type: 'tag',"
        " values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
    )
    befunde = AxeResult(violations=json.loads(json.dumps(ergebnis.get("violations", []))))
    assert not befunde.failing, f"axe: kritische/schwere Befunde ({selektor}):\n{befunde.describe()}"


@pytest.mark.usefixtures("durchsetzung")
def test_empfehlung_zeigen_und_spaeter(
    page: Any,
    goto: Any,
    login: Any,
    member_user: tuple[Any, str],
    screenshot: Any,
    dark_mode: Any,
    problems: BrowserProblems,
) -> None:
    membership, password = member_user
    login(membership.user.email, password)
    start = f"/work/{membership.organization.slug}/"

    goto(start)
    band = page.get_by_test_id("hinweisband")
    expect(band).to_be_visible()
    expect(band).to_contain_text("Schützen Sie Ihr Konto mit einem zweiten Faktor")
    expect(band.get_by_role("link", name="Einrichten")).to_have_attribute(
        "href", f"/work/{membership.organization.slug}/profile/security/"
    )
    _assert_axe_clean(page, "[data-testid=hinweisband]")
    screenshot("work-start-zweiter-faktor")
    dark_mode(True)
    screenshot("work-start-zweiter-faktor-dunkel")
    dark_mode(False)
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    expect(page.get_by_test_id("hinweisband")).to_be_visible()
    screenshot("work-start-zweiter-faktor-handy")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.reload()

    with page.expect_response(lambda antwort: "/zwei-faktor-spaeter/" in antwort.url):
        page.get_by_test_id("hinweisband").get_by_role("button", name="Später").click()
    expect(page.get_by_test_id("hinweisband")).to_have_count(0)

    goto(start)
    expect(page.get_by_test_id("hinweisband")).to_have_count(0)
    problems.assert_clean("Empfehlung zum zweiten Faktor auf Start")


def test_mitgliederliste_mit_zweitem_faktor(
    page: Any,
    goto: Any,
    login: Any,
    org: Any,
    make_member: Any,
    screenshot: Any,
    problems: BrowserProblems,
) -> None:
    from apps.accounts.models import TwoFactorDevice

    password = "E2e-Passwort-123456"
    admin = make_member(org, [], email="e2e-admin@example.org", is_admin=True)
    admin.user.set_password(password)
    admin.user.save(update_fields=["password"])
    for name in ("Anna", "Bert"):
        mitglied = make_member(org, ["dashboard.view"], email=f"{name.lower()}@example.org")
        mitglied.user.first_name = name
        mitglied.user.save(update_fields=["first_name"])
        if name == "Anna":
            # Anna hat einen zweiten Faktor; das Konto der Administration bleibt ohne, damit die Anmeldung im
            # Test ohne Code-Schritt auskommt (Durchsetzung ist in den Tests aus)
            TwoFactorDevice.objects.create(user=mitglied.user, secret_encrypted=b"-", is_confirmed=True, is_active=True)

    login(admin.user.email, password)
    goto(f"/work/{org.slug}/organization/members/")
    expect(page.get_by_test_id("zweiter-faktor")).to_have_count(3)
    expect(page.locator('[data-zweiter-faktor="nein"]')).to_have_count(2)
    expect(page.locator('[data-zweiter-faktor="ja"]')).to_have_text("Zweiter Faktor: ja", use_inner_text=True)
    _assert_axe_clean(page, "[data-testid=zweiter-faktor]")
    screenshot("work-mitglieder-zweiter-faktor")
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    expect(page.locator('[data-zweiter-faktor="ja"]')).to_have_text("2FA: ja", use_inner_text=True)
    screenshot("work-mitglieder-zweiter-faktor-handy")
    problems.assert_clean("Mitgliederliste mit zweitem Faktor")
