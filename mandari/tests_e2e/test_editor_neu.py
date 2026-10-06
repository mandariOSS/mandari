# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuer Antragseditor im Browser (Teil von #856, Prototyp E1+E2).

- Kopfzeile mit Menü und Ablaufleiste, Kommentare am Rand neben ihrer Stelle, Antworten am Rand, Speichern
- Ablauf über die vorhandenen Funktionen: Zur Abstimmung geben (Status + Zustimmungsanfragen), Zustimmen,
  Fassung freigeben, Einreichen (Status „Eingereicht“) – der Text bleibt dabei unverändert
- Menüs per Tastatur, Weg zurück zur bisherigen Ansicht, Kommentare am Handy als Blatt, axe ohne schwere Befunde

Der Schalter „Neues Erscheinungsbild“ (Organization.work_new_design, #852) ist für die Organisation eingeschaltet. Der Live-Server spricht kein WebSocket: Der Editor läuft im Solo-Modus wie in tests_e2e/test_editor.py.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, cast

import pytest

from apps.work.motions.models import Motion, MotionApproval, MotionComment
from tests_e2e.conftest import login_via_form, wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

MARK_ID = "6a1f0d2e-3b4c-4d5e-8f60-718293a4b5c6"
INHALT = (
    "<h2>Beschlussvorschlag</h2><p>Die Verwaltung wird beauftragt, am Musterweg Tempo 30 anzuordnen.</p>"
    f'<h2>Begründung</h2><p>Der Musterweg ist der <span data-comment-id="{MARK_ID}">Schulweg</span> vieler Kinder.</p>'
)
DESKTOP = {"width": 1440, "height": 900}
FAILING = ("critical", "serious")


@pytest.fixture(autouse=True)
def neues_design(db: Any, org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


def _mitglied(make_member: Any, org: Any, email: str, permissions: list[str]) -> Any:
    membership = make_member(org, permissions, email=email)
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    return _mitglied(make_member, org, "autorin-neu@example.org", [*PERMISSIONS, "voting.participate"])


@pytest.fixture
def stimme(db: Any, org: Any, make_member: Any) -> Any:
    return _mitglied(make_member, org, "stimme-neu@example.org", [*PERMISSIONS, "voting.participate"])


@pytest.fixture
def antrag(org: Any, autorin: Any, stimme: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Tempo 30 am Musterweg", status="draft", visibility="organization"
    )
    cast(Any, motion).set_content_encrypted(INHALT)
    motion.save()
    MotionComment.objects.create(
        motion=motion,
        author=stimme,
        content="Ist das nicht zu spät?",
        selected_text="Schulweg",
        mark_id=uuid.UUID(MARK_ID),
    )
    return motion


def oeffnen(page: Any, url: str) -> None:
    page.goto(url)
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)


def oeffnen_lesend(page: Any, url: str) -> None:
    """Editor ohne Schreibrecht (nur kommentieren): kein ProseMirror, der Text steht lesbar da."""
    page.goto(url)
    wait_for_bundle(page)
    page.wait_for_selector(".content-readonly[data-ke-text]", timeout=15000)


def inhalt(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted() or "")


def axe_sauber(axe: Any, wo: str) -> None:
    result = axe()
    schwer = [v for v in result.failing if v.get("impact") in FAILING]
    assert not schwer, f"{wo}: {result.describe()}"


class TestNeuerEditor:
    def test_kopf_rand_antworten_und_speichern(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion, axe: Any
    ) -> None:
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/")

        kopf = page.locator(".ke-kopf")
        expect(kopf.get_by_role("menubar", name="Menü").get_by_role("menuitem")).to_have_count(6)
        stufen = kopf.locator(".ke-stufe-knopf")
        expect(stufen).to_have_count(4)
        expect(kopf.locator('.ke-stufe-knopf[aria-current="step"]')).to_contain_text("Entwurf")

        # Kommentar am Rand auf Höhe seiner Stelle
        karte = page.locator(f'[data-rand-karte="{MARK_ID}"]')
        expect(karte).to_be_visible()
        expect(karte).to_contain_text("Ist das nicht zu spät?")
        marke = page.locator(f'{PROSEMIRROR} [data-comment-id="{MARK_ID}"]')
        abstand = abs(karte.bounding_box()["y"] - marke.bounding_box()["y"])
        assert abstand < 40, f"Karte steht {abstand:.0f} px neben ihrer Stelle"
        # Gliederung erst ab 1.600 px, Text und Rand nebeneinander
        assert karte.bounding_box()["x"] > marke.bounding_box()["x"]
        axe_sauber(axe, "Neuer Editor")

        # Klick auf die Stelle macht die Karte aktiv; Antwort am Rand wird gespeichert
        marke.click()
        expect(karte).to_have_class(re.compile(r"\bke-karte-aktiv\b"))
        expect(karte.locator("textarea")).to_be_visible()
        karte.locator("textarea").fill("Ich frage nach.")
        karte.get_by_role("button", name="Antworten").click()
        expect(karte).to_contain_text("Ich frage nach.")
        assert MotionComment.objects.filter(motion=antrag, parent__isnull=False, content="Ich frage nach.").exists()

        # Schreiben und speichern (Strg+S)
        page.locator(f"{PROSEMIRROR} p", has_text="Tempo 30 anzuordnen").click()
        page.keyboard.press("End")
        page.keyboard.type(" Neu ergänzt.")
        page.keyboard.press("Control+s")
        page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=15000)
        assert "Neu ergänzt." in inhalt(antrag)
        assert MARK_ID in inhalt(antrag)

    def test_ablauf_abstimmung_freigabe_einreichung(
        self,
        page: Any,
        live_server: Any,
        login: Any,
        new_context: Any,
        autorin: Any,
        stimme: Any,
        org: Any,
        antrag: Motion,
    ) -> None:
        url = f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/"
        text_vorher = inhalt(antrag)
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        oeffnen(page, url)

        # Zur Abstimmung geben: Stufe „Abstimmung“ öffnet den Dialog, Stimmberechtigte sind vorausgewählt
        page.locator(".ke-stufe-knopf[data-stufe=abstimmung]").click()
        dialog = page.get_by_test_id("dialog-abstimmung")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_label(stimme.user.get_display_name())).to_be_checked()
        dialog.get_by_role("button", name="Abstimmung starten").click()
        page.wait_for_load_state("load")
        expect(page.get_by_test_id("ablauf-info")).to_contain_text("0 von 1 haben zugestimmt, 1 offen", timeout=15000)
        antrag.refresh_from_db()
        assert antrag.status == "internal_review"
        anfrage = MotionApproval.objects.get(motion=antrag, approver=stimme)
        assert anfrage.approved is None and anfrage.approval_type == "council"

        # Das angefragte Mitglied stimmt in der Infozeile zu
        seite_b = new_context().new_page()
        seite_b.set_viewport_size(DESKTOP)
        login_via_form(seite_b, live_server.url, stimme.user.email, PASSWORD)
        oeffnen_lesend(seite_b, url)
        info_b = seite_b.get_by_test_id("ablauf-info")
        expect(info_b).to_contain_text("Ihre Stimme")
        info_b.get_by_role("button", name="Zustimmen").click()
        expect(seite_b.get_by_test_id("ablauf-info")).to_contain_text("1 von 1 haben zugestimmt", timeout=15000)
        anfrage.refresh_from_db()
        assert anfrage.approved is True

        # Fassung freigeben, danach einreichen (anderer Weg: als eingereicht markieren)
        oeffnen(page, url)
        page.get_by_test_id("ablauf-info").get_by_role("button", name="Freigeben …").click()
        page.get_by_test_id("dialog-freigeben").get_by_role("button", name="Freigeben", exact=True).click()
        expect(page.get_by_test_id("ablauf-info")).to_contain_text("Freigegeben.", timeout=15000)
        antrag.refresh_from_db()
        assert antrag.status == "approved"

        page.get_by_test_id("ablauf-info").get_by_role("button", name="Einreichen …").click()
        page.get_by_test_id("dialog-einreichen").get_by_role("button", name="Als eingereicht markieren").click()
        expect(page.get_by_test_id("ablauf-info")).to_contain_text("Eingereicht", timeout=15000)
        antrag.refresh_from_db()
        assert antrag.status == "submitted"
        assert antrag.submitted_at is not None
        expect(page.locator('.ke-stufe-knopf[aria-current="step"]')).to_contain_text("Einreichung")

        # Kein Datenverlust: Text und Kommentar unverändert
        assert inhalt(antrag) == text_vorher
        assert MotionComment.objects.filter(motion=antrag, mark_id=uuid.UUID(MARK_ID), is_resolved=False).exists()

    def test_menues_tastatur_und_bisherige_ansicht(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/")

        leiste = page.get_by_role("menubar", name="Menü")
        leiste.get_by_role("menuitem", name="Datei").click()
        expect(page.get_by_role("menu", name="Datei")).to_be_visible()
        page.keyboard.press("ArrowRight")
        expect(page.get_by_role("menu", name="Bearbeiten")).to_be_visible()
        expect(page.get_by_role("menu", name="Datei")).to_be_hidden()
        page.keyboard.press("Escape")
        expect(page.get_by_role("menu", name="Bearbeiten")).to_be_hidden()
        expect(leiste.get_by_role("menuitem", name="Bearbeiten")).to_be_focused()

        # Gliederung aus den Überschriften (ab 1.600 px sichtbar)
        page.set_viewport_size({"width": 1920, "height": 1080})
        gliederung = page.get_by_role("navigation", name="Gliederung")
        expect(gliederung).to_contain_text("Beschlussvorschlag")
        expect(gliederung).to_contain_text("Begründung")

        # Details als Bereich rechts, dann zurück zur bisherigen Ansicht
        leiste.get_by_role("menuitem", name="Datei").click()
        page.get_by_role("menu", name="Datei").get_by_role("menuitem", name="Details und Zuständigkeit").click()
        expect(page.locator(".ke-panel")).to_contain_text("Federführung")
        page.locator(".ke-panel").get_by_role("button", name="Schließen").click()
        expect(page.locator(".ke-panel")).to_be_hidden()

        leiste.get_by_role("menuitem", name="Ansicht").click()
        page.get_by_role("menu", name="Ansicht").get_by_role("menuitem", name="Bisherige Ansicht").click()
        page.wait_for_url("**?ansicht=bisher")
        wait_for_bundle(page)
        expect(page.locator(".editor-sidebar")).to_have_count(1)

    def test_handy_kommentare_als_blatt(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion, axe: Any
    ) -> None:
        page.set_viewport_size({"width": 390, "height": 844})
        login(autorin.user.email, PASSWORD)
        oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/")
        expect(page.locator(".ke-rand-spalte")).to_be_hidden()
        expect(page.locator(".ke-ablauf-mobil-knopf")).to_contain_text("Entwurf")
        page.locator(".ke-mobil-leiste").get_by_role("button", name="Kommentare").click()
        panel = page.locator(".ke-panel")
        expect(panel).to_be_visible()
        expect(panel).to_contain_text("Ist das nicht zu spät?")
        axe_sauber(axe, "Neuer Editor am Handy")
        panel.get_by_role("button", name="Schließen").click()
        expect(panel).to_be_hidden()


# ---- Vorschlagen, Speichern mit Wiederholung, Abnahmebilder ------------------------------------------------------

#: Demo-Antrag für die Abnahmebilder (nur Demo-Daten): Gliederung, Kommentar und Vorschlag am Rand
DEMO_MARK = "0b7c1f4e-2a3d-4e5f-9a6b-7c8d9e0f1a2b"
DEMO_INHALT = (
    "<p>An den Rat der Musterstadt</p><h1>Antrag: Tempo 30 vor der Grundschule am Musterweg</h1>"
    "<h2>Beschlussvorschlag</h2><p>Der Rat der Musterstadt möge beschließen:</p>"
    "<ol><li><p>Die Verwaltung wird beauftragt, auf dem Musterweg im Bereich der Grundschule eine "
    "Geschwindigkeitsbegrenzung auf 30 km/h anzuordnen.</p></li>"
    "<li><p>Die Verwaltung prüft, ob die Begrenzung auf die Zeit von 7 bis 17 Uhr beschränkt werden kann.</p></li></ol>"
    f'<h2>Begründung</h2><p>Der Musterweg ist der <span data-comment-id="{MARK_ID}">Schulweg</span> für die meisten '
    "Kinder des Viertels. Eltern und Schulleitung berichten von gefährlichen Situationen beim Queren.</p>"
    "<p>Eine niedrigere Geschwindigkeit verkürzt den Anhalteweg deutlich und senkt das Unfallrisiko.</p>"
    f'<h2>Finanzielle Auswirkungen</h2><p>Beschilderung und Markierung, <span data-comment-id="{DEMO_MARK}">'
    "nach Schätzung unter 5.000 Euro brutto</span>.</p>"
)
BREITEN = (
    ("390", {"width": 390, "height": 844}),
    ("1280", {"width": 1280, "height": 800}),
    ("1920", {"width": 1920, "height": 1080}),
    ("2560", {"width": 2560, "height": 1440}),
)
#: Markiert einen Text im Editor so, wie es die Maus täte (Browser-Auswahl, ProseMirror übernimmt sie)
MARKIEREN_JS = """(text) => {
    const root = document.querySelector('#editor-container .ProseMirror');
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        const i = node.textContent.indexOf(text);
        if (i >= 0) {
            const range = document.createRange();
            range.setStart(node, i);
            range.setEnd(node, i + text.length);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            return true;
        }
    }
    return false;
}"""


@pytest.fixture
def demo_antrag(antrag: Motion, stimme: Any) -> Motion:
    cast(Any, antrag).set_content_encrypted(DEMO_INHALT)
    antrag.title = "Antrag: Tempo 30 vor der Grundschule am Musterweg"
    antrag.save()
    MotionComment.objects.create(
        motion=antrag,
        author=stimme,
        content="Änderungsvorschlag: „nach Schätzung unter 5.000 Euro brutto“ ersetzen durch „rund 4.500 Euro brutto“",
        selected_text="nach Schätzung unter 5.000 Euro brutto",
        mark_id=uuid.UUID(DEMO_MARK),
        vorschlag="rund 4.500 Euro brutto",
    )
    return antrag


def text_markieren(page: Any, text: str) -> None:
    assert page.evaluate(MARKIEREN_JS, text), f"Text nicht gefunden: {text}"
    # ProseMirror liest die Auswahl beim nächsten selectionchange
    page.wait_for_function(f"() => {ALPINE_EDITOR}.inlineSelectedText === {text!r}", timeout=5000)


class TestVorschlagen:
    def test_vorschlag_machen_und_annehmen(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/")

        # Modus „Vorschlagen“: Der Text ist nicht direkt änderbar
        page.locator(".ke-modus").click()
        page.get_by_role("menuitemradio", name=re.compile("^Vorschlagen")).click()
        expect(page.locator(".ke-modus")).to_contain_text("Vorschlagen")
        expect(page.locator(PROSEMIRROR)).to_have_attribute("contenteditable", "false")

        text_markieren(page, "am Musterweg")
        page.locator(".ke-werkzeug").get_by_role("button", name="Änderung zur markierten Stelle vorschlagen").click()
        formular = page.get_by_test_id("vorschlag-formular")
        expect(formular).to_be_visible()
        formular.get_by_label("Neuer Text").fill("vor der Grundschule am Musterweg")
        formular.get_by_role("button", name="Vorschlagen").click()
        expect(formular).to_be_hidden()

        karte = page.locator(".ke-karte[data-vorschlag]")
        expect(karte).to_contain_text("Änderungsvorschlag")
        expect(karte).to_contain_text("Ersetzen: „am Musterweg“ durch „vor der Grundschule am Musterweg“")
        vorschlag = MotionComment.objects.get(motion=antrag, vorschlag__isnull=False)
        assert vorschlag.selected_text == "am Musterweg"
        # Bis zur Entscheidung bleibt der Text, wie er ist
        expect(page.locator(PROSEMIRROR)).not_to_contain_text("vor der Grundschule")

        # Annehmen: Die Stelle wird ersetzt, der Vorschlag ist erledigt, der Text gespeichert
        karte.get_by_role("button", name="Vorschlag annehmen").click()
        expect(page.locator(PROSEMIRROR)).to_contain_text(
            "beauftragt, vor der Grundschule am Musterweg Tempo 30 anzuordnen"
        )
        expect(page.locator(".ke-karte[data-vorschlag]")).to_have_count(0)
        vorschlag.refresh_from_db()
        assert vorschlag.is_resolved is True and vorschlag.vorschlag_angenommen is True
        page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=15000)
        gespeichert = inhalt(antrag)
        assert "beauftragt, vor der Grundschule am Musterweg Tempo 30 anzuordnen" in gespeichert
        # Der übrige Text und der Kommentar an „Schulweg“ bleiben
        assert MARK_ID in gespeichert and "Der Musterweg ist der" in gespeichert

    def test_geaenderte_stelle_wird_nicht_ueberschrieben(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        # Vorschlag für „Kinder“, die Marke liegt aber (inzwischen) auf „Schulweg“
        MotionComment.objects.filter(motion=antrag).delete()
        MotionComment.objects.create(
            motion=antrag,
            author=autorin,
            content="Änderungsvorschlag",
            selected_text="Kinder",
            mark_id=uuid.UUID(MARK_ID),
            vorschlag="Schulkinder",
        )
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/")
        page.locator(".ke-karte[data-vorschlag]").get_by_role("button", name="Vorschlag annehmen").click()
        expect(page.get_by_text("Die Stelle wurde inzwischen geändert.", exact=False)).to_be_visible()
        assert MotionComment.objects.get(motion=antrag, vorschlag="Schulkinder").is_resolved is False
        expect(page.locator(PROSEMIRROR)).to_contain_text("Der Musterweg ist der Schulweg vieler Kinder.")


class TestSpeichernMitWiederholung:
    def test_gestoertes_speichern_wird_wiederholt(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        url = f"{live_server.url}/work/{org.slug}/documents/{antrag.id}/"
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        oeffnen(page, url)

        # Der erste Speicherversuch kommt nicht an (Netzstörung, Neustart beim Deploy), die folgenden schon
        versuche = {"n": 0}

        def stoeren(route: Any) -> None:
            if route.request.method == "POST":
                versuche["n"] += 1
                if versuche["n"] == 1:
                    route.abort("connectionreset")
                    return
            route.continue_()

        page.route(url, stoeren)
        page.locator(f"{PROSEMIRROR} p", has_text="Tempo 30 anzuordnen").click()
        page.keyboard.press("End")
        page.keyboard.type(" Trotz Störung gespeichert.")
        page.keyboard.press("Control+s")
        expect(page.locator(".ke-gespeichert")).to_contain_text("Keine Verbindung, wird wiederholt")
        # Ohne weiteres Zutun: Der nächste Versuch nach etwa einer Sekunde speichert den Stand
        page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=15000)
        expect(page.locator(".ke-gespeichert")).to_contain_text("Gespeichert")
        assert versuche["n"] >= 2
        assert "Trotz Störung gespeichert." in inhalt(antrag)


class TestAbnahmebilder:
    """Bilder für die Abnahme (nur Demo-Daten): Entwurf mit Kommentar und Vorschlag am Rand, Dialog, Abstimmung."""

    def test_bilder_entwurf_dialog_und_abstimmung(
        self,
        page: Any,
        live_server: Any,
        login: Any,
        autorin: Any,
        org: Any,
        demo_antrag: Motion,
        screenshot: Any,
        axe: Any,
    ) -> None:
        url = f"{live_server.url}/work/{org.slug}/documents/{demo_antrag.id}/"
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        for name, groesse in BREITEN:
            page.set_viewport_size(groesse)
            oeffnen(page, url)
            expect(page.locator(".ke-kopf")).to_be_visible()
            # Eine Kopfzeile: Die des Rahmens entfällt, die Glocke steht im Kopf des Editors
            expect(page.locator("#kopf-suche")).to_have_count(0)
            expect(
                page.locator(".ke-kopf").get_by_role("button", name=re.compile("Benachrichtigungen"))
            ).to_be_visible()
            # Hinweis „Solo-Modus“ des Testservers (kein WebSocket) nicht ins Bild
            page.add_style_tag(content="[x-data='toastManager']{display:none!important}")
            if groesse["width"] >= 1024:
                expect(page.locator(".ke-karte[data-vorschlag]")).to_contain_text("rund 4.500 Euro brutto")
            # Keine graue Fläche neben dem Text: die Fläche des Editors ist weiß
            hintergrund = page.evaluate("() => getComputedStyle(document.querySelector('.ke-editor')).backgroundColor")
            assert hintergrund == "rgb(255, 255, 255)", hintergrund
            page.wait_for_timeout(300)
            screenshot(f"editor-neu-entwurf-{name}")
        axe_sauber(axe, "Neuer Editor (2560 px)")

        # Dialog „Zur Abstimmung geben“ bei 1280 px
        page.set_viewport_size({"width": 1280, "height": 800})
        oeffnen(page, url)
        page.locator(".ke-stufe-knopf[data-stufe=abstimmung]").click()
        page.add_style_tag(content="[x-data='toastManager']{display:none!important}")
        dialog = page.get_by_test_id("dialog-abstimmung")
        expect(dialog).to_contain_text("1 stimmberechtigtes Mitglied")
        expect(dialog).to_contain_text("Noch offen: 1 Kommentar, 1 Vorschlag")
        screenshot("editor-neu-dialog-abstimmung-1280")
        axe_sauber(axe, "Dialog Zur Abstimmung geben")
        dialog.get_by_role("button", name="Abstimmung starten").click()
        page.wait_for_load_state("load")
        expect(page.get_by_test_id("ablauf-info")).to_contain_text("0 von 1 haben zugestimmt", timeout=15000)
        # Während der Abstimmung öffnet der Text im Modus „Vorschlagen“, ohne dass der Text als geändert gilt
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)
        expect(page.locator(".ke-karte[data-vorschlag]")).to_be_visible()
        assert page.evaluate(f"() => {ALPINE_EDITOR}.hasUnsavedChanges()") is False
        expect(page.locator(".ke-modus")).to_contain_text("Vorschlagen")
        page.add_style_tag(content="[x-data='toastManager']{display:none!important}")
        screenshot("editor-neu-abstimmung-1280")
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.wait_for_timeout(300)
        screenshot("editor-neu-abstimmung-1920")
