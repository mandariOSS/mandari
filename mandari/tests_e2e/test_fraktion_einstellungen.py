# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstellungen › Fraktionssitzungen nach dem Muster im Browser (Issue #1071, Schritt 4).

Vorsitz: Schalter und Textfeld speichern sofort, der Stand im Kopf zeigt „Gespeichert“; ein ungültiger Wert zeigt
„Nicht gespeichert“ mit Grund, die Eingabe bleibt stehen und wird nach der Korrektur gespeichert. Mitglied: dieselbe
Seite gesperrt mit Hinweis. Dazu axe-core und Bildschirmfotos (390, 1280, 1440, 1920) für die Sichtprüfung.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

import pytest

from apps.work.faction.models import FactionMeetingException, FactionMeetingSchedule, FactionStandardAgendaItem
from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "E2e-Passwort-123456"


def _fraktion(org: Any) -> dict[str, Any]:
    org.refresh_from_db()
    return dict((org.settings or {}).get("faction", {}))


@pytest.fixture
def einstellungen(db: Any, org: Any, make_member: Any) -> tuple[Any, Any]:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    vorsitz = make_member(
        org,
        ["dashboard.view", "faction.manage", "faction.view_public", "protocols.publish"],
        email="vorsitz-e2e@example.org",
    )
    mitglied = make_member(org, ["dashboard.view", "faction.view_public"], email="mitglied-e2e@example.org")
    for m in (vorsitz, mitglied):
        m.user.set_password(PASSWORD)
        m.user.save(update_fields=["password"])
    reihe = FactionMeetingSchedule.objects.create(
        organization=org,
        name="Wöchentliche Fraktionssitzung",
        weekday=0,
        time=time(18, 0),
        default_location="Fraktionsbüro, Rathausplatz 1",
        default_video_link="https://video.example.org/fraktion",
        auto_invite=True,
        auto_invite_weekday=4,
        auto_invite_time=time(18, 0),
    )
    FactionMeetingException.objects.create(
        schedule=reihe,
        original_date=date.today() + timedelta(days=60),
        end_date=date.today() + timedelta(days=72),
        exception_type="cancelled",
        reason="Weihnachtspause",
    )
    for i, titel in enumerate(["Beschlüsse", "Politische Arbeit", "Termine", "Presse & Social Media", "Sonstiges"]):
        FactionStandardAgendaItem.objects.create(organization=org, title=titel, visibility="public", order=i + 1)
    return vorsitz, mitglied


def _oeffnen(page: Any, live_server: Any, org: Any, email: str) -> None:
    from tests_e2e.conftest import login_via_form

    login_via_form(page, live_server.url, email, PASSWORD)
    from django.urls import reverse

    page.goto(live_server.url + reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug}))
    wait_for_bundle(page)


def test_vorsitz_speichert_sofort(
    page: Any, live_server: Any, org: Any, einstellungen: Any, axe: Any, screenshot: Any
) -> None:
    vorsitz, _mitglied = einstellungen
    page.set_viewport_size({"width": 1440, "height": 900})
    _oeffnen(page, live_server, org, vorsitz.user.email)
    status = page.get_by_test_id("fs-einst-status")
    expect(status).to_contain_text("Gespeichert", use_inner_text=True)

    # Schalter: sofort gespeichert
    page.get_by_role("switch", name="Erinnerung verschicken").check()
    expect(status).to_contain_text("Gespeichert", use_inner_text=True)
    page.wait_for_timeout(500)
    assert _fraktion(org).get("agenda_reminder_enabled") is True

    # Textfeld: nach kurzer Pause gespeichert, ohne Knopf
    titel = page.get_by_label("Titel ohne vorherige Sitzung")
    titel.fill("Tagesordnung beschließen")
    page.wait_for_timeout(1500)
    expect(status).to_contain_text("Gespeichert", use_inner_text=True)
    assert _fraktion(org).get("first_agenda_title_no_previous") == "Tagesordnung beschließen"

    # Ungültiger Wert: Hinweis mit Grund, Eingabe bleibt, nach Korrektur gespeichert
    vorlauf = page.get_by_label("Vorlauf in Stunden vor Beginn")
    vorlauf.fill("0")
    expect(status).to_contain_text("Nicht gespeichert", use_inner_text=True, timeout=5000)
    expect(page.get_by_role("alert")).to_contain_text("Einladungsvorlauf")
    expect(vorlauf).to_have_value("0")
    vorlauf.fill("48")
    expect(status).to_contain_text("Gespeichert", use_inner_text=True, timeout=5000)
    expect(page.get_by_role("alert")).to_be_hidden()
    assert _fraktion(org).get("invitation_lead_hours") == 48

    result = axe()
    assert not result.failing, result.describe()
    screenshot("fraktion-einstellungen-vorsitz-1440")
    for breite, hoehe in ((1280, 800), (1920, 1080), (390, 844)):
        page.set_viewport_size({"width": breite, "height": hoehe})
        screenshot(f"fraktion-einstellungen-vorsitz-{breite}")
    result = axe()
    assert not result.failing, result.describe()


def test_mitglied_sieht_nur(
    page: Any, live_server: Any, org: Any, einstellungen: Any, axe: Any, screenshot: Any
) -> None:
    _vorsitz, mitglied = einstellungen
    page.set_viewport_size({"width": 1440, "height": 900})
    _oeffnen(page, live_server, org, mitglied.user.email)
    expect(page.get_by_role("note")).to_contain_text("Sie sehen sie zur Information.")
    expect(page.get_by_role("switch", name="Erinnerung verschicken")).to_be_disabled()
    expect(page.get_by_label("Vorlauf in Stunden vor Beginn")).to_be_disabled()
    result = axe()
    assert not result.failing, result.describe()
    screenshot("fraktion-einstellungen-mitglied-1440")
