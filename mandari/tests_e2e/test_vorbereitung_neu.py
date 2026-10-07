# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsvorbereitung im neuen Design (#856) im Browser.

Kernpfade bei eingeschaltetem Schalter der Organisation: Tagesordnung links, Vorlage groß in der Blattansicht
(pdf.js), Position in der Leiste unten, Begründung und eigene Notiz rechts – gespeichert in der Datenbank,
TOP-Wechsel per Leiste und Pfeiltaste mit Sprungmarke in der Adresse, eine lange Tagesordnung (81 TOPs),
das Handy mit Blättern und Aufgaben aus dem TOP (anlegen, abhaken). Dazu axe-core ohne kritische/schwere Befunde und Bildschirmfotos
(hell, dunkel, Handy).
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from apps.common.management.commands.setup_demo_environment import _minimal_pdf
from apps.work.meetings.models import AgendaItemPosition, AgendaPrivateNote
from apps.work.tasks.models import Task
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems, component_state, wait_for_component

RIS = "https://ris.example.org"

# Lokal unter Windows liefert die Registry für .mjs „text/plain“; der Worker von pdf.js braucht JavaScript
mimetypes.add_type("text/javascript", ".mjs")


@pytest.fixture
def schalter_an(admin: Any) -> None:
    """Neues Erscheinungsbild für die Organisation einschalten (Schalter aus #852)."""
    admin.organization.work_new_design = True
    admin.organization.save(update_fields=["work_new_design"])


def _sitzung(admin: Any, tmp_path: Path, anzahl: int = 3) -> OParlMeeting:
    source = OParlSource.objects.create(name="E2E-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
    admin.organization.body = body
    admin.organization.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=body, name="Rat der Musterstadt")
    pdf = tmp_path / "vorlage.pdf"
    pdf.write_bytes(
        _minimal_pdf(
            "Beschlussvorlage V/2026/1",
            [
                "Beschlussvorschlag",
                "Der Rat beschließt den Radweg im Norden.",
                "",
                "Sachverhalt",
                "Es fehlt ein Radweg.",
            ],
        )
    )
    for nr in range(1, anzahl + 1):
        top = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/agenda/{nr}", meeting=meeting, number=str(nr), name=f"Tagesordnungspunkt {nr}", order=nr
        )
        if nr == 1:
            continue  # Eröffnung ohne Vorlage
        vorlage = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{nr}", body=body, name=f"Vorlage {nr}", reference=f"V/2026/{nr}"
        )
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/{nr}", body=body, paper=vorlage, agenda_item_external_id=top.external_id
        )
        OParlFile.objects.create(
            external_id=f"{RIS}/file/{nr}",
            body=body,
            paper=vorlage,
            name=f"Vorlage V/2026/{nr}",
            file_name="vorlage.pdf",
            mime_type="application/pdf",
            access_url=f"{RIS}/file/{nr}.pdf",
            local_path=str(pdf),
        )
    return meeting


def _ausgewaehlt(page: Any) -> str:
    return str(component_state(page, "vorbereitung", "data.selectedItemId"))


def _warte_auf_top(page: Any, item_id: str) -> None:
    page.wait_for_function(
        '(id) => window.Alpine.$data(document.querySelector(`[x-data^="vorbereitung"]`)).selectedItemId === id',
        arg=item_id,
    )


class TestVorbereitungNeu:
    def test_kernpfad_desktop(
        self,
        page: Any,
        goto: Any,
        login: Any,
        admin: Any,
        problems: BrowserProblems,
        axe: Any,
        screenshot: Any,
        dark_mode: Any,
        schalter_an: None,
        tmp_path: Path,
    ) -> None:
        meeting = _sitzung(admin, tmp_path)
        tops = list(OParlAgendaItem.objects.filter(meeting=meeting).order_by("order"))
        page.set_viewport_size({"width": 1920, "height": 1080})
        login(admin.user.email, PASSWORD)
        schreibend: list[str] = []
        page.on(
            "request",
            lambda r: schreibend.append(r.url) if r.method in ("POST", "DELETE") and "/meetings/" in r.url else None,
        )
        goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/")
        wait_for_component(page, "vorbereitung")

        # Tagesordnung links, erster TOP gewählt; Wechsel über die Leiste unten setzt die Sprungmarke
        for top in tops:
            expect(page.locator(f"#nav-item-{top.id}")).to_be_visible()
        assert _ausgewaehlt(page) == str(tops[0].id)
        page.get_by_role("button", name="Nächster TOP").click()
        _warte_auf_top(page, str(tops[1].id))
        assert page.evaluate("() => location.hash") == f"#top-{tops[1].id}"
        # Öffnen und Wechseln speichert nichts (#887: kein leerer Redebeitrag beim Ansehen)
        page.wait_for_timeout(1500)
        assert schreibend == [], schreibend

        # Vorlage groß: pdf.js zeichnet die Seite, A+ vergrößert sie
        blatt = page.locator(".vb-seite canvas").first
        expect(blatt).to_be_visible(timeout=15000)
        breite = page.locator(".vb-seite").first.bounding_box()["width"]
        page.get_by_role("button", name="Schrift größer").click()
        page.wait_for_function(
            "(b) => document.querySelector('.vb-seite').getBoundingClientRect().width > b + 10", arg=breite
        )

        # Position in der Leiste, Begründung und eigene Notiz rechts → in der Datenbank
        page.locator(".vb-leiste .vb-pos", has_text="Ablehnung").click()
        page.get_by_role("tab", name="Begründung").click()
        page.locator("#reasoning-input").fill("Der Radweg fehlt im Plan.")
        page.get_by_role("tab", name="Notiz").click()
        page.locator("#notiz-eingabe").fill("Rückfrage an die Verwaltung")
        page.wait_for_function(
            "() => { const d = window.Alpine.$data(document.querySelector('[x-data^=\"vorbereitung\"]'));"
            " return d.pendingSaves === 0 && !!d.lastSavedAt && Object.keys(d._timers).length === 0; }",
            timeout=10000,
        )
        position = AgendaItemPosition.objects.get(organization=admin.organization, agenda_item=tops[1])
        assert position.position == "against"
        assert position.get_reasoning_decrypted() == "Der Radweg fehlt im Plan."
        notiz = AgendaPrivateNote.objects.get(author=admin, agenda_item=tops[1])
        assert notiz.get_content_decrypted() == "Rückfrage an die Verwaltung"

        # Pfeiltaste wechselt den TOP (Fokus außerhalb von Eingaben und Blatt)
        page.locator("#top-titel").focus()
        page.keyboard.press("ArrowDown")
        _warte_auf_top(page, str(tops[2].id))

        # Neuladen bleibt beim TOP, die gespeicherte Position steht in der Leiste
        page.goto(f"{page.url.split('#')[0]}#top-{tops[1].id}")
        page.reload()
        wait_for_component(page, "vorbereitung")
        _warte_auf_top(page, str(tops[1].id))
        expect(page.locator(".vb-leiste input[value='against']")).to_be_checked()

        problems.assert_clean("Vorbereitung neu")
        befunde = axe()
        assert not befunde.failing, befunde.describe()
        screenshot("vorbereitung_neu_1920")
        dark_mode(True)
        wait_for_component(page, "vorbereitung")
        screenshot("vorbereitung_neu_1920_dunkel")
        dark_mode(False)

    def test_lange_tagesordnung_81_tops(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems, schalter_an: None, tmp_path: Path
    ) -> None:
        meeting = _sitzung(admin, tmp_path, anzahl=81)
        letzter = OParlAgendaItem.objects.get(meeting=meeting, number="81")
        page.set_viewport_size({"width": 1280, "height": 800})
        login(admin.user.email, PASSWORD)
        goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/")
        wait_for_component(page, "vorbereitung")
        assert page.locator(".vb-tl-zeile").count() == 81

        # Zehn Wechsel hintereinander bleiben flüssig (Speichern, Unterlage und Diskussion je TOP)
        page.locator("#top-titel").focus()
        start = time.monotonic()
        for _ in range(10):
            page.keyboard.press("ArrowDown")
        page.wait_for_function(
            '() => window.Alpine.$data(document.querySelector(`[x-data^="vorbereitung"]`)).topIndex === 10'
        )
        assert time.monotonic() - start < 5

        # Sprungmarke auf den letzten TOP: Liste scrollt ihn in den Blick
        page.goto(f"{page.url.split('#')[0]}#top-{letzter.id}")
        page.reload()
        wait_for_component(page, "vorbereitung")
        _warte_auf_top(page, str(letzter.id))
        page.get_by_role("button", name="Tagesordnung").first.click()
        expect(page.locator(f"#nav-item-{letzter.id}")).to_be_in_viewport()
        problems.assert_clean("Vorbereitung mit 81 TOPs")

    def test_handy_ein_top_je_bildschirm(
        self,
        page: Any,
        goto: Any,
        login: Any,
        admin: Any,
        problems: BrowserProblems,
        screenshot: Any,
        schalter_an: None,
        tmp_path: Path,
    ) -> None:
        meeting = _sitzung(admin, tmp_path)
        tops = list(OParlAgendaItem.objects.filter(meeting=meeting).order_by("order"))
        page.set_viewport_size({"width": 390, "height": 844})
        login(admin.user.email, PASSWORD)
        goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/#top-{tops[1].id}")
        wait_for_component(page, "vorbereitung")
        _warte_auf_top(page, str(tops[1].id))

        # Tagesordnung, Unterlagen und Position als Blatt; Leiste der Breite ist ausgeblendet
        expect(page.locator(".vb-leiste-position")).to_be_hidden()
        expect(page.locator(".vb-liste")).to_be_hidden()
        page.locator(".vb-mobil-unterlagen .vb-knopf").first.click()
        expect(page.locator(".vb-unterlagen.blatt-offen")).to_be_visible()
        expect(page.locator(".vb-seite canvas").first).to_be_visible(timeout=15000)
        screenshot("vorbereitung_neu_390_unterlage")
        page.get_by_role("button", name="Unterlagen schließen").click()

        page.locator(".vb-pos-mobil").click()
        blatt = page.locator(".vb-pos-blatt")
        expect(blatt).to_be_visible()
        blatt.locator(".vb-pos", has_text="Zustimmung").click()
        page.wait_for_function(
            '() => window.Alpine.$data(document.querySelector(`[x-data^="vorbereitung"]`)).pendingSaves === 0'
        )
        page.keyboard.press("Escape")
        expect(blatt).to_be_hidden()
        expect(page.locator(".vb-pos-mobil")).to_contain_text("Zustimmung")
        screenshot("vorbereitung_neu_390")

        page.get_by_role("button", name="Nächster TOP").click()
        _warte_auf_top(page, str(tops[2].id))
        problems.assert_clean("Vorbereitung am Handy")

    def test_aufgabe_aus_dem_top(
        self,
        page: Any,
        goto: Any,
        login: Any,
        admin: Any,
        problems: BrowserProblems,
        axe: Any,
        screenshot: Any,
        schalter_an: None,
        tmp_path: Path,
    ) -> None:
        meeting = _sitzung(admin, tmp_path)
        top = OParlAgendaItem.objects.get(meeting=meeting, number="2")
        page.set_viewport_size({"width": 1280, "height": 800})
        login(admin.user.email, PASSWORD)
        goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/#top-{top.id}")
        wait_for_component(page, "vorbereitung")
        _warte_auf_top(page, str(top.id))

        # Reiter „Aufgaben“: leer, Zuständig steht auf der eigenen Person; Anlegen zeigt die Aufgabe sofort
        page.get_by_role("tab", name="Aufgaben").click()
        expect(page.locator("#feld-aufgaben")).to_contain_text("Noch keine Aufgaben aus diesem TOP.")
        expect(page.get_by_label("Zuständig")).to_have_value(str(admin.id))
        page.get_by_label("Neue Aufgabe").fill("Rückfrage an die Verwaltung stellen")
        page.get_by_label("Fällig am").fill("2026-10-19")
        page.get_by_role("button", name="Anlegen").click()
        eintrag = page.locator(".vb-aufgaben li", has_text="Rückfrage an die Verwaltung stellen")
        expect(eintrag).to_contain_text("fällig 19.10.2026")
        expect(page.get_by_role("tab", name="Aufgaben")).to_contain_text("1")

        task = Task.objects.get(title="Rückfrage an die Verwaltung stellen")
        assert task.related_agenda_item_id == top.id
        assert task.related_meeting_id == meeting.id
        assert task.assigned_to_id == admin.id

        # Abhaken wie auf der Karte im Aufgabenboard (Endpunkt des Boards)
        haken = page.get_by_role("checkbox", name="Erledigt: Rückfrage an die Verwaltung stellen")
        with page.expect_response(lambda r: "/tasks/api/" in r.url) as antwort:
            haken.check()
        assert antwort.value.ok
        expect(haken).to_be_checked()
        task.refresh_from_db()
        assert task.is_completed

        # Der Reiter bleibt beim TOP-Wechsel, die Liste folgt dem TOP
        page.get_by_role("button", name="Nächster TOP").click()
        expect(page.locator("#feld-aufgaben")).to_contain_text("Noch keine Aufgaben aus diesem TOP.")
        page.get_by_role("button", name="Voriger TOP").click()
        expect(eintrag).to_be_visible()

        problems.assert_clean("Vorbereitung, Aufgaben")
        befunde = axe()
        assert not befunde.failing, befunde.describe()
        screenshot("vorbereitung_neu_1280_aufgaben")
        # Breiter Bildschirm: Blatt zeichnet in der neuen Breite neu, Zuständig, Fällig und Anlegen in einer Zeile
        breite = page.locator(".vb-seite").first.bounding_box()["width"]
        page.set_viewport_size({"width": 2560, "height": 1300})
        page.wait_for_function(
            "(b) => document.querySelector('.vb-seite').getBoundingClientRect().width > b + 50", arg=breite
        )
        expect(eintrag).to_be_visible()
        screenshot("vorbereitung_neu_2560_aufgaben")

        # Handy: alle fünf Reiter erreichbar (kurze Beschriftung), Aufgabe und Formular ohne seitliches Überlaufen
        page.set_viewport_size({"width": 390, "height": 844})
        reiter = page.get_by_role("tab", name="Aufgaben")
        expect(reiter).to_have_attribute("aria-selected", "true")
        expect(page.get_by_role("tab", name="Rede")).to_be_visible()
        leiste = page.evaluate(
            "() => { const l = document.querySelector('.vb-reiter');"
            " return {voll: l.scrollWidth, sichtbar: l.clientWidth,"
            " reiter: [...l.querySelectorAll('[role=tab]')].map((t) => Math.round(t.getBoundingClientRect().width))} }"
        )
        assert leiste["voll"] <= leiste["sichtbar"], leiste
        eintrag.scroll_into_view_if_needed()
        expect(eintrag).to_be_in_viewport()
        expect(page.get_by_label("Neue Aufgabe")).to_be_visible()
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        screenshot("vorbereitung_neu_390_aufgaben")

    def test_speichern_ueberbrueckt_neustart(
        self, page: Any, goto: Any, login: Any, admin: Any, schalter_an: None, tmp_path: Path
    ) -> None:
        """Vorfall 07.10.2026 (#854): Position während eines Neustarts gesetzt (502) – wird automatisch nachgeholt."""
        meeting = _sitzung(admin, tmp_path)
        top = OParlAgendaItem.objects.get(meeting=meeting, number="2")
        page.set_viewport_size({"width": 1920, "height": 1080})
        login(admin.user.email, PASSWORD)
        goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/#top-{top.id}")
        wait_for_component(page, "vorbereitung")
        _warte_auf_top(page, str(top.id))

        # Die ersten beiden Versuche scheitern wie beim Deploy (Proxy erreicht die Anwendung nicht)
        versuche: list[int] = []

        def neustart(route: Any) -> None:
            versuche.append(1)
            if len(versuche) <= 2:
                route.fulfill(status=502, body="Bad Gateway")
            else:
                route.continue_()

        page.route(f"**/position/{top.id}/", neustart)
        page.locator(".vb-leiste .vb-pos", has_text="Zustimmung").click()
        expect(page.locator("#autosave-status")).to_contain_text("Nicht gespeichert – wird wiederholt")
        assert not AgendaItemPosition.objects.filter(organization=admin.organization, agenda_item=top).exists()

        # Ohne weiteres Zutun: dritter Versuch geht durch, Anzeige wieder „Gespeichert“
        expect(page.locator("#autosave-status")).to_contain_text("Gespeichert", timeout=15000)
        assert len(versuche) == 3, versuche
        position = AgendaItemPosition.objects.get(organization=admin.organization, agenda_item=top)
        assert position.position == "for"
