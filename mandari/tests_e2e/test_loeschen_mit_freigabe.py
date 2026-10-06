# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Endgültiges Löschen in den Fraktionssitzungen mit ausdrücklicher Freigabe im Browser (Issue #897), in beiden Rahmen.

Geprüft: Der Dialog lädt die Folgen mit Zahlen, „Endgültig löschen“ bleibt gesperrt, bis das Datum der Sitzung bzw.
die Nummer des TOPs eingegeben ist, danach löscht der Knopf (Sitzung: zurück zur Liste ohne Rahmen im Rahmen; TOP:
Dialog schließt, Tagesordnung ohne den TOP). Keine schweren axe-Befunde im Dialog, keine Fehler im Browser.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting, FactionProtocolEntry
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems


def _sitzung(admin: Any, neuer_rahmen: bool) -> FactionMeeting:
    organisation = admin.organization
    organisation.work_new_design = neuer_rahmen
    organisation.save(update_fields=["work_new_design"])
    meeting = FactionMeeting.objects.create(
        organization=organisation,
        title="Fraktionssitzung E2E",
        start=timezone.make_aware(datetime(2030, 11, 3, 18, 0)),
        status="planned",
        created_by=admin,
    )
    haushalt = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt 2031", order=0)
    FactionAgendaItem.objects.create(meeting=meeting, number="2", title="Verkehr", order=1)
    FactionAttendance.objects.create(meeting=meeting, membership=admin, status="confirmed")
    entry: Any = FactionProtocolEntry(meeting=meeting, agenda_item=haushalt, entry_type="note")
    entry.set_content_encrypted("Kämmerei berichtet")
    entry.save()
    return meeting


def _axe_dialog(page: Any, selector: str) -> AxeResult:
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        "async (sel) => await axe.run({ include: [[sel]] }, { resultTypes: ['violations'], "
        "runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })",
        selector,
    )
    return AxeResult(violations=list(ergebnis.get("violations", [])))


@pytest.mark.parametrize("neuer_rahmen", [False, True], ids=["alt", "neu"])
def test_sitzung_loeschen_erst_nach_eingabe_des_datums(
    page: Any,
    goto: Any,
    login: Any,
    admin: Any,
    screenshot: Any,
    problems: BrowserProblems,
    neuer_rahmen: bool,
) -> None:
    meeting = _sitzung(admin, neuer_rahmen)
    slug = admin.organization.slug
    login(admin.user.email, PASSWORD)
    goto(f"/work/{slug}/faction/{meeting.id}/")

    page.get_by_role("button", name="Sitzung löschen …").click()
    dialog = page.locator("#delete-meeting-modal")
    expect(dialog.get_by_text("Wird mitgelöscht")).to_be_visible()
    expect(dialog.locator("dt", has_text="Tagesordnungspunkte").locator("xpath=..").locator("dd")).to_have_text("2")
    expect(dialog.get_by_role("button", name="Sitzung absagen")).to_be_visible()
    knopf = dialog.get_by_role("button", name="Endgültig löschen")
    expect(knopf).to_be_disabled()
    ergebnis = _axe_dialog(page, "#delete-meeting-modal")
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"loeschen-sitzung-dialog-{'neu' if neuer_rahmen else 'alt'}")

    eingabe = dialog.get_by_label("Zur Bestätigung das Datum der Sitzung eingeben")
    eingabe.fill("04.11.2030")
    expect(knopf).to_be_disabled()
    eingabe.fill("03.11.2030")
    expect(knopf).to_be_enabled()
    knopf.click()

    # Der Browser lädt die Liste als ganze Seite (HX-Redirect) statt sie in den Dialog zu tauschen (Issue #895)
    page.wait_for_url(f"**/work/{slug}/faction/")
    expect(page.locator("#delete-meeting-modal")).to_have_count(0)
    assert not FactionMeeting.objects.filter(pk=meeting.pk).exists()
    problems.assert_clean("Sitzung löschen")


@pytest.mark.parametrize("neuer_rahmen", [False, True], ids=["alt", "neu"])
def test_top_mit_protokoll_nur_mit_nummer(
    page: Any,
    goto: Any,
    login: Any,
    admin: Any,
    screenshot: Any,
    problems: BrowserProblems,
    neuer_rahmen: bool,
) -> None:
    meeting = _sitzung(admin, neuer_rahmen)
    haushalt = meeting.agenda_items.get(number="1")
    login(admin.user.email, PASSWORD)
    goto(f"/work/{admin.organization.slug}/faction/{meeting.id}/")

    page.evaluate(
        "(id) => window.dispatchEvent(new CustomEvent('open-delete-item', { detail: { id, title: '' } }))",
        str(haushalt.id),
    )
    dialog = page.locator("#delete-item-modal")
    expect(dialog.locator("dt", has_text="Protokolleinträge")).to_be_visible()
    knopf = dialog.get_by_role("button", name="Endgültig löschen")
    expect(knopf).to_be_disabled()
    screenshot(f"loeschen-top-dialog-{'neu' if neuer_rahmen else 'alt'}")

    dialog.get_by_label("Zur Bestätigung die Nummer des TOPs eingeben").fill("1")
    knopf.click()

    expect(dialog).to_be_hidden()
    expect(page.locator("#agenda-container").get_by_text("Haushalt 2031")).to_have_count(0)
    expect(page.locator("#agenda-container").get_by_text("Verkehr").first).to_be_visible()
    assert not FactionAgendaItem.objects.filter(pk=haushalt.pk).exists()
    problems.assert_clean("TOP löschen")
