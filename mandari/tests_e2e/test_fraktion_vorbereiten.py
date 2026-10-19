# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnung der Fraktionssitzung vor der Sitzung (Issue #1071) im Browser: Ziehen am Griff, Verschieben mit den
Pfeiltasten am Griff (Fokus bleibt, Ansage) und sofort speichern von Titel und Beschreibung beim Verlassen des Felds,
auch wenn der Server kurz nicht erreichbar ist (Eingabe bleibt im Browser, wird erneut gesendet).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone
from playwright.sync_api import expect

from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems, wait_for_component

SICHERUNGEN = "() => Object.keys(localStorage).filter((k) => k.startsWith('mandari.eingaben.'))"


def _sitzung(admin: Any) -> FactionMeeting:
    organisation = admin.organization
    organisation.work_new_design = True
    organisation.save(update_fields=["work_new_design"])
    meeting = FactionMeeting.objects.create(
        organization=organisation,
        title="Fraktionssitzung E2E",
        start=timezone.make_aware(datetime(2030, 11, 3, 18, 0)),
        status="planned",
        created_by=admin,
    )
    for nr, titel in enumerate(("Haushalt 2031", "Verkehr", "Presse"), start=1):
        FactionAgendaItem.objects.create(meeting=meeting, number=str(nr), title=titel, order=nr)
    FactionAttendance.objects.create(meeting=meeting, membership=admin, status="invited")
    return meeting


def _titel(meeting: FactionMeeting) -> list[str]:
    return list(
        FactionAgendaItem.objects.filter(meeting=meeting, parent__isnull=True)
        .order_by("order")
        .values_list("title", flat=True)
    )


def _oeffnen(page: Any, goto: Any, login: Any, admin: Any, meeting: FactionMeeting, top: str = "") -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    login(admin.user.email, PASSWORD)
    goto(f"/work/{admin.organization.slug}/faction/{meeting.id}/" + (f"?ansicht=tagesordnung&top={top}" if top else ""))
    wait_for_component(page, "fsTagesordnung")


def test_tops_ziehen_und_mit_pfeiltasten_verschieben(
    page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems, screenshot: Any
) -> None:
    meeting = _sitzung(admin)
    _oeffnen(page, goto, login, admin, meeting)
    presse = FactionAgendaItem.objects.get(meeting=meeting, title="Presse")

    # Tastatur: Griff fokussieren, Pfeil hoch – der Fokus bleibt am Griff, die Ansage nennt die neue Nummer
    griff = page.locator(f'[data-griff="{presse.pk}"]')
    griff.focus()
    page.keyboard.press("ArrowUp")
    expect(page.locator(".fs-teil").first.locator(".fs-top-titel").nth(1)).to_have_text("Presse")
    expect(page.locator(f'[data-griff="{presse.pk}"]')).to_be_focused()
    expect(page.locator("[x-text='ansage']")).to_have_text("TOP verschoben, jetzt 2.")
    assert _titel(meeting) == ["Haushalt 2031", "Presse", "Verkehr"]

    # Maus: „Verkehr“ am Griff ganz nach oben ziehen
    verkehr = FactionAgendaItem.objects.get(meeting=meeting, title="Verkehr")
    ziel = page.locator(f"#top-{FactionAgendaItem.objects.get(meeting=meeting, title='Haushalt 2031').pk}")
    quelle = page.locator(f'[data-griff="{verkehr.pk}"]')
    quelle.hover()
    page.mouse.down()
    box = ziel.bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + 4, steps=12)
    page.mouse.up()
    expect(page.locator(".fs-teil").first.locator(".fs-top-titel").first).to_have_text("Verkehr")
    # Die Antwort des Servers bringt die neuen Nummern
    expect(page.locator(f"#top-{verkehr.pk} .nr")).to_have_text("1")
    assert _titel(meeting) == ["Verkehr", "Haushalt 2031", "Presse"]
    nummern = dict(FactionAgendaItem.objects.filter(meeting=meeting).values_list("title", "number"))
    assert nummern == {"Verkehr": "1", "Haushalt 2031": "2", "Presse": "3"}
    screenshot("fraktion_vorbereiten_ziehen_1440")


def test_titel_und_beschreibung_sofort_speichern_auch_nach_stoerung(
    page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems, screenshot: Any
) -> None:
    meeting = _sitzung(admin)
    verkehr = FactionAgendaItem.objects.get(meeting=meeting, title="Verkehr")
    _oeffnen(page, goto, login, admin, meeting, top=str(verkehr.pk))

    titel = page.locator(f"#ti-{verkehr.pk}")
    titel.fill("Verkehr und Radwege")
    titel.press("Tab")
    expect(page.get_by_test_id("fs-speicherstand").get_by_text("Gespeichert", exact=True)).to_be_visible()
    expect(page.locator(f"#top-{verkehr.pk} .fs-top-titel")).to_have_text("Verkehr und Radwege")
    verkehr.refresh_from_db()
    assert verkehr.title == "Verkehr und Radwege"

    # Server nicht erreichbar: Eingabe bleibt im Browser und geht nach der Störung raus
    ziel = f"**/item/{verkehr.pk}/felder/"
    page.route(ziel, lambda route: route.abort("connectionrefused"))
    beschreibung = page.locator(f"#b-{verkehr.pk}")
    beschreibung.fill("Antrag Tempo 30 vorbereiten")
    page.locator(f"#ti-{verkehr.pk}").focus()
    expect(page.get_by_test_id("fs-speicherstand").get_by_text("Work versucht es erneut")).to_be_visible()
    page.wait_for_function(
        f"() => ({SICHERUNGEN})().some((k) => localStorage.getItem(k).includes('Tempo 30'))", timeout=10000
    )
    screenshot("fraktion_vorbereiten_stoerung_1440")
    page.unroute(ziel)
    page.get_by_test_id("fs-speicherstand").get_by_role("button", name="Erneut versuchen").click()
    expect(page.get_by_test_id("fs-speicherstand").get_by_text("Gespeichert", exact=True)).to_be_visible(timeout=15000)
    verkehr.refresh_from_db()
    assert verkehr.get_description_decrypted() == "Antrag Tempo 30 vorbereiten"
    page.wait_for_function(f"() => ({SICHERUNGEN})().length === 0", timeout=10000)
