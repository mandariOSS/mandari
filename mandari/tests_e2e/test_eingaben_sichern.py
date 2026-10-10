# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eingaben gehen nicht verloren (#854, Teil 2) – im Browser geprüft.

Sitzungsvorbereitung: Ohne Netz gesetzte Position und Notiz landen nach der Rückkehr der Verbindung genau einmal in
der Datenbank; eine Eingabe, deren Speichern beim Neuladen noch offen war, wird angeboten und lässt sich übernehmen;
bei abgelaufener Anmeldung bzw. fehlendem zweiten Faktor erscheint ein Hinweis, die Eingabe bleibt im Browser und
wird nach der Anmeldung gespeichert. Die Sicherung im Browser trägt die Kontokennung im Schlüssel und verschwindet,
sobald gespeichert ist.

Automatisch speichernde htmx-Formulare (TOP einer Fraktionssitzung, Aufgaben): 403, 400 und die Pflicht zum zweiten
Faktor zeigen „Nicht gespeichert“ mit Erklärung statt dauerhaft „Speichert …“; die Seite lädt dabei nicht neu.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.meetings.models import AgendaItemPosition, AgendaPrivateNote
from apps.work.tasks.models import Task
from insight_core.models import OParlAgendaItem
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems, login_via_form, wait_for_component
from tests_e2e.test_vorbereitung_neu import _sitzung, _warte_auf_top

SICHERUNGEN = "() => Object.keys(localStorage).filter((k) => k.startsWith('mandari.eingaben.'))"
ZWEITER_FAKTOR = {"error": "two_factor_setup_required", "redirect": "/accounts/2fa/setup/"}


@pytest.fixture
def schalter_an(admin: Any) -> None:
    """Neues Erscheinungsbild der Vorbereitung für die Organisation (Schalter aus #852)."""
    admin.organization.work_new_design = True
    admin.organization.save(update_fields=["work_new_design"])


def _vorbereitung_oeffnen(page: Any, goto: Any, login: Any, admin: Any, tmp_path: Path) -> tuple[Any, Any]:
    meeting = _sitzung(admin, tmp_path)
    top = OParlAgendaItem.objects.get(meeting=meeting, number="2")
    page.set_viewport_size({"width": 1920, "height": 1080})
    login(admin.user.email, PASSWORD)
    goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/#top-{top.id}")
    wait_for_component(page, "vorbereitung")
    _warte_auf_top(page, str(top.id))
    return meeting, top


def _sicherung_mit(page: Any, text: str) -> None:
    """Wartet, bis eine Eingabe im Browser gesichert ist."""
    page.wait_for_function(
        f"(text) => ({SICHERUNGEN})().some((k) => localStorage.getItem(k).includes(text))",
        arg=text,
        timeout=10000,
    )


class TestSitzungsvorbereitung:
    def test_ohne_netz_gesetzt_wird_nach_rueckkehr_genau_einmal_gespeichert(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems, schalter_an: None, tmp_path: Path
    ) -> None:
        meeting, top = _vorbereitung_oeffnen(page, goto, login, admin, tmp_path)

        page.context.set_offline(True)
        page.locator(".vb-leiste .vb-pos", has_text="Ablehnung").click()
        page.get_by_role("tab", name="Notiz").click()
        page.locator("#notiz-eingabe").fill("Rückfrage an die Verwaltung")
        expect(page.locator("#autosave-status")).to_contain_text("Nicht gespeichert")
        _sicherung_mit(page, "Rückfrage an die Verwaltung")
        # Je Konto, Sitzung und TOP – mit der Kennung des Kontos, nicht dem Namen
        assert page.evaluate(SICHERUNGEN) == [f"mandari.eingaben.{admin.user.pk}.{meeting.id}.{top.id}"]
        assert not AgendaItemPosition.objects.filter(organization=admin.organization, agenda_item=top).exists()

        page.context.set_offline(False)
        expect(page.locator("#autosave-status")).to_contain_text("Gespeichert um", timeout=20000)
        positionen = AgendaItemPosition.objects.filter(organization=admin.organization, agenda_item=top)
        assert positionen.count() == 1
        assert positionen.get().position == "against"
        notizen = AgendaPrivateNote.objects.filter(author=admin, agenda_item=top)
        assert notizen.count() == 1
        assert notizen.get().get_content_decrypted() == "Rückfrage an die Verwaltung"
        # Gespeichert: nichts mehr im Browser
        assert page.evaluate(SICHERUNGEN) == []
        problems.assert_clean("Vorbereitung ohne Netz")

    def test_neuladen_waehrend_offenem_speichern_bietet_eingabe_an(
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
        _meeting, top = _vorbereitung_oeffnen(page, goto, login, admin, tmp_path)

        # Der Server ist gerade nicht erreichbar (Neustart beim Deploy): das Speichern bleibt offen
        ziel = f"**/private-note/{top.id}/"
        page.route(ziel, lambda route: route.abort("connectionrefused"))
        page.get_by_role("tab", name="Notiz").click()
        page.locator("#notiz-eingabe").fill("Antrag auf Vertagung vorbereiten")
        expect(page.locator("#autosave-status")).to_contain_text("Nicht gespeichert – wird wiederholt")

        # Rückfrage beim Verlassen bestätigen und neu laden
        page.once("dialog", lambda dialog: dialog.accept())
        page.reload()
        wait_for_component(page, "vorbereitung")
        _warte_auf_top(page, str(top.id))
        hinweis = page.locator("#eingaben-sicherung")
        expect(hinweis).to_contain_text("Nicht gespeicherte Eingaben vom")
        expect(hinweis).to_contain_text("TOP 2 (Notiz)")
        assert not AgendaPrivateNote.objects.filter(author=admin, agenda_item=top).exists()
        screenshot("vorbereitung_sicherung_1920")
        page.set_viewport_size({"width": 390, "height": 844})
        expect(hinweis).to_be_visible()
        screenshot("vorbereitung_sicherung_390")
        page.set_viewport_size({"width": 1920, "height": 1080})

        page.unroute(ziel)
        hinweis.get_by_role("button", name="Übernehmen").click()
        expect(hinweis).to_be_hidden()
        expect(page.locator("#notiz-eingabe")).to_have_value("Antrag auf Vertagung vorbereiten")
        expect(page.locator("#autosave-status")).to_contain_text("Gespeichert um", timeout=15000)
        notiz = AgendaPrivateNote.objects.get(author=admin, agenda_item=top)
        assert notiz.get_content_decrypted() == "Antrag auf Vertagung vorbereiten"
        assert page.evaluate(SICHERUNGEN) == []

        # Nach dem Speichern wird nichts mehr angeboten
        page.reload()
        wait_for_component(page, "vorbereitung")
        expect(page.locator("#eingaben-sicherung")).to_be_hidden()
        problems.assert_clean("Vorbereitung nach Neuladen")

    def test_abgelaufene_anmeldung_eingabe_bleibt_und_wird_nachgeholt(
        self, page: Any, goto: Any, login: Any, admin: Any, live_server: Any, schalter_an: None, tmp_path: Path
    ) -> None:
        _meeting, top = _vorbereitung_oeffnen(page, goto, login, admin, tmp_path)

        # Anmeldung läuft ab (Sitzungs-Cookie weg; das CSRF-Cookie bleibt wie im echten Ablauf)
        page.context.clear_cookies(name="sessionid")
        page.get_by_role("tab", name="Begründung").click()
        page.locator("#reasoning-input").fill("Begründung vor dem Ablauf")
        expect(page.locator("#autosave-status")).to_contain_text("Nicht gespeichert – bitte anmelden")
        hinweis = page.locator(".vb-hinweis", has_text="Anmeldung ist abgelaufen")
        expect(hinweis).to_be_visible()
        expect(hinweis.get_by_role("link", name="In neuem Tab anmelden")).to_have_attribute("href", "/accounts/login/")
        _sicherung_mit(page, "Begründung vor dem Ablauf")

        # In einem zweiten Tab anmelden, zurück in die Vorbereitung: wird ohne Zutun gespeichert
        zweiter = page.context.new_page()
        login_via_form(zweiter, live_server.url, admin.user.email, PASSWORD)
        zweiter.close()
        page.bring_to_front()
        page.evaluate("() => window.dispatchEvent(new Event('focus'))")
        expect(page.locator("#autosave-status")).to_contain_text("Gespeichert um", timeout=15000)
        position = AgendaItemPosition.objects.get(organization=admin.organization, agenda_item=top)
        assert position.get_reasoning_decrypted() == "Begründung vor dem Ablauf"
        assert page.evaluate(SICHERUNGEN) == []

    def test_zweiter_faktor_fehlt_hinweis_eingabe_bleibt(
        self, page: Any, goto: Any, login: Any, admin: Any, schalter_an: None, tmp_path: Path
    ) -> None:
        _meeting, top = _vorbereitung_oeffnen(page, goto, login, admin, tmp_path)

        ziel = f"**/position/{top.id}/"
        page.route(ziel, lambda route: route.fulfill(status=403, json=ZWEITER_FAKTOR))
        page.locator(".vb-leiste .vb-pos", has_text="Zustimmung").click()
        hinweis = page.locator(".vb-hinweis", has_text="zweiten Faktor")
        expect(hinweis).to_be_visible()
        link = hinweis.get_by_role("link", name="Zweiten Faktor einrichten")
        expect(link).to_have_attribute("href", "/accounts/2fa/setup/")
        expect(link).to_have_attribute("target", "_blank")
        _sicherung_mit(page, '"position":"for"')

        # Eingerichtet (hier: Antwort wieder normal), zurück in die Vorbereitung: gespeichert, Sicherung weg
        page.unroute(ziel)
        page.evaluate("() => window.dispatchEvent(new Event('focus'))")
        expect(page.locator("#autosave-status")).to_contain_text("Gespeichert um", timeout=15000)
        assert AgendaItemPosition.objects.get(organization=admin.organization, agenda_item=top).position == "for"
        assert page.evaluate(SICHERUNGEN) == []

    def test_abmelden_loescht_die_sicherung(
        self, page: Any, goto: Any, login: Any, admin: Any, live_server: Any, schalter_an: None, tmp_path: Path
    ) -> None:
        _meeting, top = _vorbereitung_oeffnen(page, goto, login, admin, tmp_path)
        page.route(f"**/private-note/{top.id}/", lambda route: route.abort("connectionrefused"))
        page.get_by_role("tab", name="Notiz").click()
        page.locator("#notiz-eingabe").fill("Vertraulich")
        _sicherung_mit(page, "Vertraulich")
        page.evaluate("() => localStorage.setItem('darkMode', 'false')")

        page.once("dialog", lambda dialog: dialog.accept())
        page.goto(f"{live_server.url}/accounts/logout/")
        page.get_by_role("button", name="Ja, abmelden").click()
        page.wait_for_url("**/accounts/logged-out/")
        page.wait_for_function(f"() => ({SICHERUNGEN})().length === 0")
        assert page.evaluate("() => localStorage.getItem('darkMode')") == "false"


def _panel_antwort(page: Any, muster: str, antwort: dict[str, Any] | None) -> None:
    """
    Automatisches Speichern (action=update) mit der vorgegebenen Antwort beantworten (``None``: keine Verbindung),
    alles andere durchlassen.
    """

    def bearbeiten(route: Any) -> None:
        if route.request.method != "POST" or "action=update" not in (route.request.post_data or ""):
            route.continue_()
        elif antwort is None:
            route.abort("connectionrefused")
        else:
            route.fulfill(**antwort)

    page.unroute(muster)
    page.route(muster, bearbeiten)


class TestHtmxAutosave:
    def test_top_panel_zeigt_fehler_statt_dauerhaft_speichert(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems, screenshot: Any
    ) -> None:
        meeting = FactionMeeting.objects.create(
            organization=admin.organization,
            title="Fraktionssitzung E2E",
            start=timezone.now() + timedelta(days=3),
            status="planned",
            created_by=admin,
        )
        item = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt 2027", visibility="public")
        login(admin.user.email, PASSWORD)
        goto(f"/work/{admin.organization.slug}/faction/{meeting.id}/")
        wait_for_component(page, "factionDetail")
        page.evaluate(
            "(id) => window.dispatchEvent(new CustomEvent('open-item-panel', { detail: { id } }))", str(item.id)
        )
        wait_for_component(page, "agendaItemPanel")
        seite = page.url
        muster = f"**/item/{item.id}/panel/action/"
        panel = page.locator('[x-data="agendaItemPanel"]')
        titel = panel.locator("#agenda-item-update-form input[name=title]")
        hinweis = panel.get_by_role("alert")

        # 403: keine Berechtigung
        _panel_antwort(page, muster, {"status": 403, "body": ""})
        titel.fill("Haushalt 2027 (Entwurf)")
        titel.blur()
        expect(panel.get_by_text("Nicht gespeichert", exact=True)).to_be_visible()
        expect(hinweis).to_contain_text("Keine Berechtigung")
        expect(panel.locator('#agenda-item-update-form [x-show="saving"]')).to_be_hidden()

        # 400 mit Klartext des Servers
        _panel_antwort(page, muster, {"status": 400, "body": "Titel fehlt.", "content_type": "text/plain"})
        titel.fill("Haushalt 2027 (Entwurf 2)")
        titel.blur()
        expect(hinweis).to_contain_text("Nicht gespeichert: Titel fehlt.")

        # Pflicht zum zweiten Faktor (204 mit HX-Redirect): Hinweis mit Link, die Seite bleibt
        _panel_antwort(page, muster, {"status": 204, "headers": {"HX-Redirect": "/accounts/2fa/setup/"}})
        titel.fill("Haushalt 2027 (Entwurf 3)")
        titel.blur()
        expect(hinweis).to_contain_text("zweiten Faktor")
        expect(hinweis.get_by_role("link", name="Zweiten Faktor einrichten")).to_have_attribute(
            "href", "/accounts/2fa/setup/"
        )
        page.wait_for_timeout(500)
        assert page.url == seite
        screenshot("top_panel_autosave_zweiter_faktor")
        expect(titel).to_have_value("Haushalt 2027 (Entwurf 3)")

        # Zurück aus dem anderen Tab: wird ohne Zutun gespeichert, der Hinweis verschwindet
        page.unroute(muster)
        page.evaluate("() => window.dispatchEvent(new Event('focus'))")
        expect(hinweis).to_be_hidden()
        expect(panel.get_by_text("Nicht gespeichert", exact=True)).to_be_hidden()
        item.refresh_from_db()
        assert item.title == "Haushalt 2027 (Entwurf 3)"
        problems.assert_clean("TOP-Panel mit Fehlern beim automatischen Speichern")

    def test_aufgaben_panel_zeigt_fehler_und_erneut_versuchen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        task = Task.objects.create(organization=admin.organization, title="Protokoll schreiben", created_by=admin)
        login(admin.user.email, PASSWORD)
        goto(f"/work/{admin.organization.slug}/tasks/?open={task.id}")
        wait_for_component(page, "autosaveAnzeige")
        muster = f"**/tasks/{task.id}/panel/action/"
        panel = page.locator('[x-data="autosaveAnzeige"]')
        titel = panel.get_by_placeholder("Aufgabentitel")

        # Keine Verbindung: Hinweis statt dauerhaft „Speichert …“, „Erneut versuchen“ sendet noch einmal
        _panel_antwort(page, muster, None)
        titel.fill("Protokoll schreiben und versenden")
        titel.blur()
        hinweis = panel.get_by_role("alert")
        expect(hinweis).to_contain_text("keine Verbindung")
        expect(panel.get_by_text("Nicht gespeichert", exact=True)).to_be_visible()

        page.unroute(muster)
        hinweis.get_by_role("button", name="Erneut versuchen").click()
        expect(hinweis).to_be_hidden()
        task.refresh_from_db()
        assert task.title == "Protokoll schreiben und versenden"
        problems.assert_clean("Aufgaben-Panel mit Fehler beim automatischen Speichern")
