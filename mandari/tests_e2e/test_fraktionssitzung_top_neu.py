# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOP in der laufenden Fraktionssitzung hinzufügen, im Browser (Issue #1166).

Vorsitz schreibt Notizen zu einem TOP und fügt über „TOP hinzufügen …“ in der Tagesordnung einen TOP und einen
Unterpunkt hinzu: Die Notizen sind vorher gesichert, der neue TOP ist geöffnet und markiert, die Adresse merkt ihn.
Dazu axe-core mit offenem Dialog, das Handy (Tagesordnung als Blatt) und Bildschirmfotos für die Sichtprüfung.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting
from apps.work.faction.tests.test_sitzungsansicht import schalter_an
from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "E2e-Passwort-123456"
VORSITZ = [
    "dashboard.view",
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.start",
    "protocols.create",
    "protocols.edit",
    "voting.participate",
]
NOTIZ = "#fs-notiz .ProseMirror"


@pytest.fixture
def sitzung(db: Any, org: Any, make_member: Any) -> tuple[Any, FactionMeeting, list[FactionAgendaItem]]:
    schalter_an(org)
    vorsitz = make_member(org, VORSITZ, email="vorsitz-top@example.org")
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    vorsitz.user.set_password(PASSWORD)
    vorsitz.user.save(update_fields=["password"])
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 25",
        start=timezone.now() - timedelta(minutes=5),
        started_at=timezone.now() - timedelta(minutes=4),
        status="ongoing",
        created_by=vorsitz,
    )
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="present")
    titel = ["Tagesordnung festlegen", "Feuerwehrbedarfsplan 2026–2031", "Sonstiges"]
    tops = [
        FactionAgendaItem.objects.create(
            meeting=meeting, number=str(i + 1), title=t, visibility="public", order=i, is_approval_item=i == 0
        )
        for i, t in enumerate(titel)
    ]
    tops.append(
        FactionAgendaItem.objects.create(
            meeting=meeting, number="NÖ 1", title="Personalangelegenheit", visibility="internal", order=3
        )
    )
    return vorsitz, meeting, tops


def oeffnen(page: Any, live_server: Any, org: Any, meeting: FactionMeeting, top: FactionAgendaItem) -> None:
    page.goto(f"{live_server.url}/work/{org.slug}/faction/{meeting.pk}/?top={top.pk}")
    wait_for_bundle(page)
    page.wait_for_selector(NOTIZ, timeout=15000)


def test_top_hinzufuegen_in_der_laufenden_sitzung(
    page: Any, live_server: Any, login: Any, org: Any, sitzung: Any, axe: Any, screenshot: Any
) -> None:
    vorsitz, meeting, tops = sitzung
    page.set_viewport_size({"width": 1440, "height": 900})
    login(vorsitz.user.email, PASSWORD)
    oeffnen(page, live_server, org, meeting, tops[1])

    # Notizen zum offenen TOP: werden vor dem Wechsel auf den neuen TOP gesichert
    page.locator(NOTIZ).click()
    page.keyboard.type("Antrag auf geänderte Tagesordnung angenommen.")

    tagesordnung = page.locator("#fs-tagesordnung")
    tagesordnung.get_by_role("button", name="TOP hinzufügen …").click()
    dialog = page.get_by_role("dialog", name="TOP hinzufügen")
    expect(dialog).to_be_visible()
    expect(page.locator("#fs-top-neu-titel")).to_be_focused()
    result = axe()
    assert not result.failing, result.describe()
    screenshot("fraktionssitzung-top-neu-1440")

    page.fill("#fs-top-neu-titel", "Dringlichkeitsantrag Schulbau")
    page.fill("#fs-top-neu-beschreibung", "Unter TOP 1 neu aufgenommen.")
    dialog.get_by_role("button", name="Hinzufügen").click()

    expect(page.locator("#fs-top-titel")).to_contain_text("TOP 4")
    expect(page.locator("#fs-top-titel")).to_contain_text("Dringlichkeitsantrag Schulbau")
    expect(page.get_by_role("dialog", name="TOP hinzufügen")).to_be_hidden()
    expect(page.locator("#fs-top-titel")).to_be_focused()
    neu = FactionAgendaItem.objects.get(meeting=meeting, title="Dringlichkeitsantrag Schulbau")
    assert re.search(rf"top={neu.pk}", page.url)
    zeile = tagesordnung.locator(f'[data-top-id="{neu.pk}"]')
    expect(zeile).to_have_attribute("aria-current", "true")
    expect(page.locator("#fs-unten")).to_contain_text("Sonstiges")
    tops[1].refresh_from_db()
    assert "geänderte Tagesordnung" in cast(Any, tops[1]).get_notes_decrypted()

    # Unterpunkt eines TOPs: Nummer unter seinem TOP; das Formular ist wieder leer, die Auswahl kennt den neuen TOP
    tagesordnung.get_by_role("button", name="TOP hinzufügen …").click()
    expect(page.locator("#fs-top-neu-titel")).to_have_value("")
    expect(page.locator("#fs-top-neu-unter option", has_text="Dringlichkeitsantrag Schulbau")).to_have_count(1)
    page.fill("#fs-top-neu-titel", "Stellungnahme der Feuerwehr")
    page.locator("#fs-top-neu-unter").select_option(str(tops[1].pk))
    page.get_by_role("dialog", name="TOP hinzufügen").get_by_role("button", name="Hinzufügen").click()
    expect(page.locator("#fs-top-titel")).to_contain_text("TOP 2.1")
    expect(tagesordnung).to_contain_text("Stellungnahme der Feuerwehr")
    screenshot("fraktionssitzung-top-neu-danach-1440")

    # Sitzung inzwischen beendet: Meldung statt neuem TOP, der Dialog gibt sie frei
    FactionMeeting.objects.filter(pk=meeting.pk).update(status="completed")
    tagesordnung.get_by_role("button", name="TOP hinzufügen …").click()
    page.fill("#fs-top-neu-titel", "Zu spät")
    page.get_by_role("dialog", name="TOP hinzufügen").get_by_role("button", name="Hinzufügen").click()
    expect(page.get_by_text("Die Sitzung läuft nicht mehr.")).to_be_visible()
    expect(page.get_by_role("dialog", name="TOP hinzufügen")).to_be_hidden()
    assert not FactionAgendaItem.objects.filter(title="Zu spät").exists()


def test_top_hinzufuegen_am_handy(
    page: Any, live_server: Any, login: Any, org: Any, sitzung: Any, axe: Any, screenshot: Any
) -> None:
    vorsitz, meeting, tops = sitzung
    page.set_viewport_size({"width": 390, "height": 844})
    login(vorsitz.user.email, PASSWORD)
    oeffnen(page, live_server, org, meeting, tops[1])
    page.locator("#fs-unten").get_by_role("button", name=re.compile("TOP 2")).click()
    tagesordnung = page.locator("#fs-tagesordnung")
    expect(tagesordnung).to_be_visible()
    tagesordnung.get_by_role("button", name="TOP hinzufügen …").click()
    dialog = page.get_by_role("dialog", name="TOP hinzufügen")
    expect(dialog).to_be_visible()
    result = axe()
    assert not result.failing, result.describe()
    screenshot("fraktionssitzung-top-neu-390")
    page.fill("#fs-top-neu-titel", "Vertrauliche Personalfrage")
    page.locator("#fs-top-neu-teil").select_option("internal")
    dialog.get_by_role("button", name="Hinzufügen").click()
    expect(page.locator("#fs-top-titel")).to_contain_text("TOP NÖ 2")
    expect(tagesordnung).to_be_hidden()
    assert FactionAgendaItem.objects.get(title="Vertrauliche Personalfrage").visibility == "internal"
