# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Vorschlag im Dokumenteditor übernehmen (Teil von #856, Sofortmaßnahme aus dem Konzept zur Editor-KI).

Bisher ersetzte „Übernehmen“ das ganze Dokument mit dem Text der KI. Jetzt gilt nur die markierte Stelle:

- Nur sie geht an die KI, nur sie wird ersetzt; Text davor und danach und Kommentarmarken bleiben.
- Hat jemand die Stelle zwischen Anfrage und Übernehmen geändert, wird nichts ersetzt.
- Im gemeinsamen Dokument bleiben Änderungen anderer erhalten (zwei Browser über den ASGI-Testserver).

Die KI selbst antwortet nicht: Playwright fängt die Anfrage an ``documents/ai/`` ab und liefert einen festen
Vorschlag. ``MotionAIService.is_available`` wird für den Seitenaufbau eingeschaltet (Live-Server im selben
Prozess).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, cast
from urllib.parse import parse_qs

import pytest

from apps.work.motions.models import Motion, MotionComment
from apps.work.motions.services import MotionAIService
from tests_e2e.conftest import login_via_form, wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR, SAVE_BUTTON, STATUSBAR
from tests_e2e.test_editor_kollaboration import CONNECTED, editor_oeffnen

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

AI_URL = re.compile(r"/documents/ai/$")
MARK_ID = "3f6c8f0e-6a7b-4c1e-9d55-2b7c1a0e9f11"
ERSTER = "Erster Absatz bleibt stehen."
ZWEITER = "Zweiter Absatz soll besser werden."
DRITTER_VOR = "Dritter Absatz mit "
KOMMENTARSTELLE = "Kommentarstelle"
DRITTER_NACH = " bleibt."
VORSCHLAG = "Zweiter Absatz, klarer formuliert."
INHALT = (
    f"<p>{ERSTER}</p><p>{ZWEITER}</p>"
    f'<p>{DRITTER_VOR}<span data-comment-id="{MARK_ID}">{KOMMENTARSTELLE}</span>{DRITTER_NACH}</p>'
)
#: Desktop-Breite mit Seitenleiste (unter 1.281 px blendet der bisherige Editor sie aus)
VIEWPORT = {"width": 1440, "height": 900}


@pytest.fixture(autouse=True)
def ki_verfuegbar(monkeypatch: pytest.MonkeyPatch) -> None:
    """KI-Bereich im Editor anzeigen, ohne einen Anbieter einzurichten."""
    monkeypatch.setattr(MotionAIService, "is_available", lambda self: True)


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="ki-autorin@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


def dokument_mit_kommentar(org: Any, author: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org,
        author=author,
        title="Antrag mit KI-Vorschlag",
        summary="Kurzfassung",
        status="draft",
        visibility="organization",
    )
    cast(Any, motion).set_content_encrypted(INHALT)
    motion.save()
    MotionComment.objects.create(
        motion=motion,
        author=author,
        content="Bitte die Stelle prüfen",
        selected_text=KOMMENTARSTELLE,
        mark_id=uuid.UUID(MARK_ID),
    )
    return motion


class KiAttrappe:
    """Fängt die KI-Anfragen einer Seite ab und antwortet mit einem festen Vorschlag."""

    def __init__(self, page: Any, vorschlag: str = VORSCHLAG) -> None:
        self.vorschlag = vorschlag
        self.anfragen: list[dict[str, list[str]]] = []
        page.route(AI_URL, self._antworten)

    def _antworten(self, route: Any) -> None:
        self.anfragen.append(parse_qs(route.request.post_data or ""))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"success": True, "content": self.vorschlag, "suggestions": [], "quota": {}}),
        )


def absatz_markieren(page: Any, text: str) -> None:
    """Einen Absatz von Anfang bis Ende per Tastatur markieren."""
    page.locator(f"{PROSEMIRROR} p", has_text=text).click()
    page.keyboard.press("Home")
    page.keyboard.press("Shift+End")


def am_absatzende_tippen(page: Any, absatz: str, text: str) -> None:
    page.locator(f"{PROSEMIRROR} p", has_text=absatz).click()
    page.keyboard.press("End")
    page.keyboard.type(text)


def vorschlag_anfragen(page: Any, erwartete_markierung: str) -> None:
    """KI-Reiter öffnen, die Markierung prüfen und „Verbessern“ wählen."""
    page.locator(".editor-sidebar .sidebar-tab", has_text="KI").click()
    expect(page.get_by_test_id("ki-markierung")).to_contain_text(erwartete_markierung)
    page.locator(".quick-action-btn", has_text="Verbessern").click()
    expect(page.locator(".chat-message.ai", has_text=VORSCHLAG)).to_be_visible()


def vorschlag_uebernehmen(page: Any) -> None:
    page.locator(".chat-message.ai", has_text=VORSCHLAG).get_by_role("button", name="Übernehmen").click()
    dialog = page.get_by_role("dialog", name="KI-Vorschau")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Übernehmen").click()
    expect(dialog).to_be_hidden()


def editor_text(page: Any) -> str:
    """Absätze des Editors, durch Leerzeichen getrennt, ohne die Cursor-Namen anderer Personen."""
    return str(
        page.evaluate(
            """() => [...document.querySelectorAll('#editor-container .ProseMirror p')].map((p) => {
                const kopie = p.cloneNode(true);
                kopie.querySelectorAll('[class*="collaboration-carets"], .ProseMirror-widget').forEach((n) => n.remove());
                return kopie.textContent;
            }).join(' ')"""
        )
    )


def warte_auf_text(page: Any, erwartet: str, timeout: int = 15000) -> None:
    """Pollt den Editortext, bis er dem erwarteten Text entspricht."""
    ist = ""
    for _ in range(max(1, timeout // 200)):
        ist = editor_text(page)
        if ist == erwartet:
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"Editortext erwartet {erwartet!r}, ist {ist!r}")


def decrypted_content(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted() or "")


class TestKiVorschlagNurMarkierteStelle:
    def test_ersetzt_nur_die_markierung(self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any) -> None:
        motion = dokument_mit_kommentar(org, autorin)
        page.set_viewport_size(VIEWPORT)
        ki = KiAttrappe(page)
        login(autorin.user.email, PASSWORD)
        page.goto(f"{live_server.url}/work/{org.slug}/documents/{motion.id}/")
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)

        absatz_markieren(page, ZWEITER)
        vorschlag_anfragen(page, ZWEITER)
        # An die KI ging nur die markierte Stelle, nicht das ganze Dokument
        assert ki.anfragen[-1]["text"] == [ZWEITER]
        assert ki.anfragen[-1]["selected_text"] == [ZWEITER]

        vorschlag_uebernehmen(page)
        expect(page.locator(PROSEMIRROR)).to_contain_text(VORSCHLAG)
        assert editor_text(page) == f"{ERSTER} {VORSCHLAG} {DRITTER_VOR}{KOMMENTARSTELLE}{DRITTER_NACH}"
        # Kommentarmarke außerhalb der Stelle unverändert
        expect(page.locator(f'{PROSEMIRROR} [data-comment-id="{MARK_ID}"]')).to_have_text(KOMMENTARSTELLE)

        # Gespeichert bleibt alles erhalten
        page.click(SAVE_BUTTON)
        expect(page.locator(STATUSBAR)).to_contain_text(re.compile(r"Gespeichert \d{2}:\d{2}"), use_inner_text=True)
        inhalt = decrypted_content(motion)
        for teil in (ERSTER, VORSCHLAG, KOMMENTARSTELLE, MARK_ID):
            assert teil in inhalt
        assert ZWEITER not in inhalt

    def test_kommentarmarke_in_der_markierung_bleibt(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any
    ) -> None:
        motion = dokument_mit_kommentar(org, autorin)
        page.set_viewport_size(VIEWPORT)
        KiAttrappe(page, vorschlag="Dritter Absatz, gekürzt.")
        login(autorin.user.email, PASSWORD)
        page.goto(f"{live_server.url}/work/{org.slug}/documents/{motion.id}/")
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)

        absatz_markieren(page, DRITTER_VOR)
        page.locator(".editor-sidebar .sidebar-tab", has_text="KI").click()
        page.locator(".quick-action-btn", has_text="Verbessern").click()
        nachricht = page.locator(".chat-message.ai", has_text="Dritter Absatz, gekürzt.")
        nachricht.get_by_role("button", name="Übernehmen").click()
        dialog = page.get_by_role("dialog", name="KI-Vorschau")
        dialog.get_by_role("button", name="Übernehmen").click()

        expect(page.locator(PROSEMIRROR)).to_contain_text("Dritter Absatz, gekürzt.")
        assert editor_text(page) == f"{ERSTER} {ZWEITER} Dritter Absatz, gekürzt."
        # Der kommentierte Text kommt im Vorschlag nicht mehr vor: Die Marke liegt auf dem neuen Text
        expect(page.locator(f'{PROSEMIRROR} [data-comment-id="{MARK_ID}"]')).to_have_text("Dritter Absatz, gekürzt.")

    def test_geaenderte_stelle_wird_nicht_ueberschrieben(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any
    ) -> None:
        motion = dokument_mit_kommentar(org, autorin)
        page.set_viewport_size(VIEWPORT)
        KiAttrappe(page)
        login(autorin.user.email, PASSWORD)
        page.goto(f"{live_server.url}/work/{org.slug}/documents/{motion.id}/")
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)

        absatz_markieren(page, ZWEITER)
        vorschlag_anfragen(page, ZWEITER)
        # Zwischen Anfrage und Übernehmen ändert sich die Stelle
        am_absatzende_tippen(page, ZWEITER, " Ergänzt.")
        vorschlag_uebernehmen(page)

        expect(page.locator(".chat-message.ai", has_text="inzwischen geändert")).to_be_visible()
        assert editor_text(page) == (f"{ERSTER} {ZWEITER} Ergänzt. {DRITTER_VOR}{KOMMENTARSTELLE}{DRITTER_NACH}")
        assert VORSCHLAG not in editor_text(page)

    def test_ohne_markierung_kein_uebernehmen(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any
    ) -> None:
        motion = dokument_mit_kommentar(org, autorin)
        page.set_viewport_size(VIEWPORT)
        ki = KiAttrappe(page)
        login(autorin.user.email, PASSWORD)
        page.goto(f"{live_server.url}/work/{org.slug}/documents/{motion.id}/")
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)

        page.locator(".editor-sidebar .sidebar-tab", has_text="KI").click()
        expect(page.get_by_test_id("ki-markierung")).to_contain_text("Markieren Sie eine Stelle")
        page.locator(".quick-action-btn", has_text="Verbessern").click()
        nachricht = page.locator(".chat-message.ai", has_text=VORSCHLAG)
        expect(nachricht).to_contain_text("Zum Übernehmen markieren Sie die Stelle")
        expect(nachricht.get_by_role("button", name="Übernehmen")).to_have_count(0)
        # Ohne Markierung liest die KI wie bisher den ganzen Text
        assert ERSTER in ki.anfragen[-1]["text"][0]
        assert editor_text(page) == f"{ERSTER} {ZWEITER} {DRITTER_VOR}{KOMMENTARSTELLE}{DRITTER_NACH}"


class TestKiVorschlagGemeinsamesDokument:
    def test_aenderungen_anderer_bleiben_erhalten(
        self, asgi_server: Any, new_context: Any, org: Any, make_member: Any
    ) -> None:
        anna = make_member(org, PERMISSIONS, email="anna-ki@example.org")
        bernd = make_member(org, [*PERMISSIONS, "motions.edit_all"], email="bernd-ki@example.org")
        for membership in (anna, bernd):
            membership.user.set_password(PASSWORD)
            membership.user.save(update_fields=["password"])
        motion = dokument_mit_kommentar(org, anna)
        url = f"{asgi_server.url}/work/{org.slug}/documents/{motion.id}/"
        page_a = new_context().new_page()
        page_b = new_context().new_page()
        for page in (page_a, page_b):
            page.set_viewport_size(VIEWPORT)
        KiAttrappe(page_a)

        login_via_form(page_a, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page_a, url, ERSTER)
        login_via_form(page_b, asgi_server.url, bernd.user.email, PASSWORD)
        editor_oeffnen(page_b, url, ERSTER)
        page_a.wait_for_function(CONNECTED, timeout=15000)

        absatz_markieren(page_a, ZWEITER)
        vorschlag_anfragen(page_a, ZWEITER)

        # Während A den Vorschlag prüft, schreibt B im ersten Absatz weiter
        am_absatzende_tippen(page_b, ERSTER, " Von B.")
        mit_b = f"{ERSTER} Von B. {ZWEITER} {DRITTER_VOR}{KOMMENTARSTELLE}{DRITTER_NACH}"
        warte_auf_text(page_a, mit_b)

        vorschlag_uebernehmen(page_a)
        erwartet = f"{ERSTER} Von B. {VORSCHLAG} {DRITTER_VOR}{KOMMENTARSTELLE}{DRITTER_NACH}"
        warte_auf_text(page_a, erwartet)
        warte_auf_text(page_b, erwartet)
        expect(page_b.locator(f'{PROSEMIRROR} [data-comment-id="{MARK_ID}"]')).to_have_text(KOMMENTARSTELLE)
