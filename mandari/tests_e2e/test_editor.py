# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kernpfade des Dokumenteditors im Browser (Issue #185).

Anlegen, Eingeben, Speichern und Neuladen; Speicheranzeige inkl. Fehlerzustand;
Status-Sperre; Inline-Kommentar anlegen und erledigen; Sichtbarkeit fremder privater
Dokumente; Rückfrage beim Verlassen mit ungespeicherten Änderungen; Autosave nur bei
Änderung.

Der Live-Server von pytest-django spricht nur WSGI: Die WebSocket-Verbindung der
Kollaboration schlägt fehl und der Editor fällt sofort in den Solo-Modus. Die Tests
warten darauf, bevor sie tippen – der Solo-Modus ist zugleich der Pfad, in dem
POST-Speichern, Autosave und Konfliktprüfung überhaupt greifen.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from apps.work.motions.models import Motion, MotionComment
from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "E2e-Passwort-123456"
PERMISSIONS = ["dashboard.view", "motions.view", "motions.create", "motions.edit", "motions.comment"]
PROSEMIRROR = "#editor-container .ProseMirror"
STATUSBAR = "#editor-statusbar"
SAVE_BUTTON = "header button:has-text('Speichern')"
SAVED_PATTERN = re.compile(r"Gespeichert \d{2}:\d{2}")
ALPINE_EDITOR = "Alpine.$data(document.querySelector('.editor-wrapper'))"


@pytest.fixture
def editor_user(db: Any, org: Any, make_member: Any) -> Iterator[tuple[Any, str]]:
    """Mitglied, das Dokumente anlegen, bearbeiten und kommentieren darf."""
    membership = make_member(org, PERMISSIONS, email="redaktion@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    yield membership, PASSWORD


@pytest.fixture
def other_user(db: Any, org: Any, make_member: Any) -> Any:
    """Zweites Mitglied derselben Organisation (für Sichtbarkeitsprüfungen)."""
    return make_member(org, PERMISSIONS, email="kollegin@example.org")


@pytest.fixture
def make_motion(org: Any) -> Callable[..., Motion]:
    """Dokument mit verschlüsseltem Inhalt anlegen (Standard: Entwurf, für die Organisation sichtbar)."""

    def _make(author: Any, title: str, content: str, status: str = "draft", visibility: str = "organization") -> Motion:
        motion = Motion.objects.create(
            organization=org, author=author, title=title, summary="Kurzfassung", status=status, visibility=visibility
        )
        cast(Any, motion).set_content_encrypted(content)
        motion.save()
        return motion

    return _make


def editor_path(org: Any, motion: Motion) -> str:
    return f"/work/{org.slug}/documents/{motion.id}/"


def open_editor(page: Any, url: str) -> None:
    """Editor öffnen und warten, bis TipTap steht und der Solo-Fallback durch ist."""
    page.goto(url)
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)


def type_text(page: Any, text: str) -> None:
    page.locator(PROSEMIRROR).click()
    page.keyboard.press("End")
    page.keyboard.type(text)


def unsaved_changes(page: Any) -> bool:
    return bool(page.evaluate(f"() => {ALPINE_EDITOR}.hasUnsavedChanges()"))


def decrypted_content(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted() or "")


class TestEditorSpeichern:
    def test_anlegen_eingeben_speichern_neu_laden(
        self, page: Any, live_server: Any, login: Any, editor_user: Any, org: Any, screenshot: Any
    ) -> None:
        membership, password = editor_user
        login(membership.user.email, password)

        # Anlegen über das Formular – Weiterleitung in den Editor
        page.goto(f"{live_server.url}/work/{org.slug}/documents/create/")
        wait_for_bundle(page)
        page.fill("#title", "Antrag Radweg Hauptstraße")
        page.fill("#summary", "Kurzfassung des Antrags")
        page.click("form button[type=submit]")
        page.wait_for_url(re.compile(r"/documents/[0-9a-f-]{36}/$"), timeout=15000)
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false")
        motion = Motion.objects.get(title="Antrag Radweg Hauptstraße")
        assert motion.summary == "Kurzfassung des Antrags"

        expect(page.locator(STATUSBAR)).to_contain_text("Nicht gespeichert", use_inner_text=True)
        type_text(page, "Wir beantragen einen Radweg.")
        assert unsaved_changes(page)

        # Antwort zurückhalten, damit der Zwischenzustand „Speichert…“ sichtbar wird
        held: list[Any] = []

        def hold(route: Any) -> None:
            if route.request.method == "POST":
                held.append(route)
            else:
                route.continue_()

        page.route(f"**/documents/{motion.id}/", hold)
        page.click(SAVE_BUTTON)
        expect(page.locator(STATUSBAR)).to_contain_text("Speichert…", use_inner_text=True)
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(50)
        assert held, "Speicher-POST wurde nicht abgefangen"
        held[0].continue_()
        page.unroute(f"**/documents/{motion.id}/")
        expect(page.locator(STATUSBAR)).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        expect(page.locator("header")).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        assert not unsaved_changes(page)
        screenshot("editor-dokument")

        # Neu laden: Inhalt und Metadaten unverändert
        page.reload()
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR)
        expect(page.locator(PROSEMIRROR)).to_contain_text("Wir beantragen einen Radweg.")
        expect(page.locator("header input[x-ref=titleInput]")).to_have_value("Antrag Radweg Hauptstraße")
        expect(page.locator("header")).to_contain_text("Entwurf")
        assert "Wir beantragen einen Radweg." in decrypted_content(motion)
        assert motion.title == "Antrag Radweg Hauptstraße"
        assert motion.summary == "Kurzfassung des Antrags"
        assert motion.status == "draft"

    def test_fehlerzustand_und_erholung(
        self, page: Any, live_server: Any, login: Any, editor_user: Any, org: Any, make_motion: Any
    ) -> None:
        membership, password = editor_user
        motion = make_motion(membership, "Fehlerfall", "<p>Alt</p>")
        login(membership.user.email, password)
        open_editor(page, f"{live_server.url}{editor_path(org, motion)}")
        type_text(page, " Neu")

        # Server nicht erreichbar → Fehlerzustand, Änderungen gelten weiter als ungespeichert
        page.route(
            f"**/documents/{motion.id}/",
            lambda route: route.abort() if route.request.method == "POST" else route.continue_(),
        )
        page.click(SAVE_BUTTON)
        expect(page.locator(STATUSBAR)).to_contain_text("Fehler beim Speichern", use_inner_text=True)
        expect(page.locator("header")).to_contain_text("Fehler beim Speichern", use_inner_text=True)
        assert unsaved_changes(page)
        assert decrypted_content(motion) == "<p>Alt</p>"

        # Wieder erreichbar → gespeichert
        page.unroute(f"**/documents/{motion.id}/")
        page.click(SAVE_BUTTON)
        expect(page.locator(STATUSBAR)).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        expect(page.locator(STATUSBAR)).not_to_contain_text("Fehler beim Speichern", use_inner_text=True)
        assert not unsaved_changes(page)
        assert "Neu" in decrypted_content(motion)


class TestStatusSperre:
    def test_gesperrter_status_nicht_bearbeitbar(
        self, page: Any, live_server: Any, login: Any, editor_user: Any, org: Any, make_motion: Any
    ) -> None:
        membership, password = editor_user
        motion = make_motion(membership, "Freigegebener Antrag", "<p>Eingefroren</p>", status="approved")
        login(membership.user.email, password)
        url = f"{live_server.url}{editor_path(org, motion)}"
        page.goto(url)
        wait_for_bundle(page)

        # Nur Lesen: kein Editor, kein Speichern-Knopf, Inhalt als Text
        expect(page.locator("#editor-container")).to_have_count(0)
        expect(page.locator(".content-readonly")).to_contain_text("Eingefroren")
        expect(page.locator(SAVE_BUTTON)).to_have_count(0)
        expect(page.locator("header")).to_contain_text("Freigegeben")

        # Auch ein direkter POST wird abgelehnt
        csrf = next(c["value"] for c in page.context.cookies() if c["name"] == "csrftoken")
        response = page.request.post(
            url,
            form={"action": "save", "title": "Umgangen", "content": "<p>Umgangen</p>", "csrfmiddlewaretoken": csrf},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": url},
        )
        assert response.status == 403
        assert decrypted_content(motion) == "<p>Eingefroren</p>"
        assert motion.title == "Freigegebener Antrag"


class TestKommentare:
    def test_inline_kommentar_anlegen_und_erledigen(
        self, page: Any, live_server: Any, login: Any, editor_user: Any, org: Any, make_motion: Any
    ) -> None:
        membership, password = editor_user
        motion = make_motion(membership, "Kommentierter Antrag", "<p>Wir beantragen einen Radweg.</p>")
        login(membership.user.email, password)
        open_editor(page, f"{live_server.url}{editor_path(org, motion)}")

        # Absatz per Tastatur markieren → Popup „Kommentieren“ → Kommentar senden
        page.locator(PROSEMIRROR).click()
        page.keyboard.press("End")
        page.keyboard.press("Shift+Home")
        popup = page.locator(".comment-popup", has_text="Kommentieren")
        expect(popup).to_be_visible()
        popup.get_by_role("button", name="Kommentieren").click()
        page.fill(".comment-popup textarea", "Bitte Kosten prüfen")
        page.locator(".comment-popup button", has_text="Senden").click()

        mark = page.locator(f"{PROSEMIRROR} [data-comment-id]")
        expect(mark).to_have_count(1)
        expect(page.locator(".editor-sidebar")).to_contain_text("Bitte Kosten prüfen")
        comment = MotionComment.objects.get(motion=motion, content="Bitte Kosten prüfen")
        assert comment.selected_text.strip()
        assert str(comment.mark_id) == mark.get_attribute("data-comment-id")
        assert comment.is_resolved is False

        # Markierung anklicken → Popup zum Kommentar → „Erledigen“ entfernt die Markierung
        mark.click()
        mark_popup = page.locator(".comment-popup", has_text="Bitte Kosten prüfen")
        expect(mark_popup).to_be_visible()
        mark_popup.get_by_role("button", name="Erledigen").click()
        expect(page.locator(f"{PROSEMIRROR} [data-comment-id]")).to_have_count(0)
        expect(mark_popup).to_be_hidden()
        comment.refresh_from_db()
        assert comment.is_resolved is True
        assert comment.resolved_by == membership

        # Gespeichert und neu geladen: keine Markierung mehr, Kommentar in der Seitenleiste erledigt
        page.click(SAVE_BUTTON)
        expect(page.locator(STATUSBAR)).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        assert "data-comment-id" not in decrypted_content(motion)
        page.reload()
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR)
        expect(page.locator(f"{PROSEMIRROR} [data-comment-id]")).to_have_count(0)
        expect(page.locator(f"#comment-sidebar-{comment.id}")).to_be_hidden()


class TestSichtbarkeit:
    def test_fremdes_privates_dokument_unerreichbar(
        self, page: Any, live_server: Any, login: Any, editor_user: Any, other_user: Any, org: Any, make_motion: Any
    ) -> None:
        membership, password = editor_user
        sichtbar = make_motion(other_user, "Gemeinsamer Antrag", "<p>Sichtbar</p>")
        privat = make_motion(other_user, "Geheimer Entwurf", "<p>Privat</p>", visibility="private")
        login(membership.user.email, password)

        page.goto(f"{live_server.url}/work/{org.slug}/documents/")
        wait_for_bundle(page)
        expect(page.locator("body")).to_contain_text("Gemeinsamer Antrag")
        expect(page.locator("body")).not_to_contain_text("Geheimer Entwurf")

        response = page.goto(f"{live_server.url}{editor_path(org, privat)}")
        assert response is not None and response.status == 403
        assert page.locator(PROSEMIRROR).count() == 0

        # Gegenprobe: das sichtbare Dokument öffnet sich
        response = page.goto(f"{live_server.url}{editor_path(org, sichtbar)}")
        assert response is not None and response.status == 200


class TestUngespeicherteAenderungen:
    def test_rueckfrage_beim_verlassen(
        self, page: Any, context: Any, live_server: Any, login: Any, editor_user: Any, org: Any, make_motion: Any
    ) -> None:
        membership, password = editor_user
        motion = make_motion(membership, "Ungespeichert", "<p>Alt</p>")
        login(membership.user.email, password)
        url = f"{live_server.url}{editor_path(org, motion)}"

        # Ohne Änderung keine Rückfrage
        open_editor(page, url)
        assert not unsaved_changes(page)
        assert not page.evaluate("() => !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")

        # Mit Änderung: beforeunload wird abgebrochen und der Browser fragt beim Schließen nach
        type_text(page, " Neu")
        assert unsaved_changes(page)
        assert page.evaluate("() => !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")
        dialogs: list[str] = []

        def on_dialog(dialog: Any) -> None:
            dialogs.append(dialog.type)
            dialog.accept()

        page.on("dialog", on_dialog)
        page.close(run_before_unload=True)
        page.wait_for_event("close", timeout=10000)
        assert dialogs == ["beforeunload"]

        # Nach erfolgreichem Speichern keine Rückfrage mehr
        second = context.new_page()
        open_editor(second, url)
        type_text(second, " Noch neuer")
        second.click(SAVE_BUTTON)
        expect(second.locator(STATUSBAR)).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        assert not unsaved_changes(second)
        assert not second.evaluate("() => !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))")
        second_dialogs: list[str] = []
        second.on("dialog", lambda dialog: (second_dialogs.append(dialog.type), dialog.accept()))
        second.close(run_before_unload=True)
        second.wait_for_event("close", timeout=10000)
        assert second_dialogs == []

    def test_autosave_nur_bei_aenderung(
        self, page: Any, live_server: Any, login: Any, editor_user: Any, org: Any, make_motion: Any
    ) -> None:
        membership, password = editor_user
        motion = make_motion(membership, "Autosave", "<p>Alt</p>")
        login(membership.user.email, password)
        url = f"{live_server.url}{editor_path(org, motion)}"
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        open_editor(page, url)
        posts.clear()

        # Autosave ohne Änderung: keine Anfrage
        page.evaluate(f"() => {ALPINE_EDITOR}.autoSave()")
        page.wait_for_timeout(500)
        assert posts == []

        # Mit Änderung: genau eine Anfrage, danach wieder Ruhe
        type_text(page, " Neu")
        page.evaluate(f"() => {ALPINE_EDITOR}.autoSave()")
        expect(page.locator(STATUSBAR)).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        assert posts == [url]
        page.evaluate(f"() => {ALPINE_EDITOR}.autoSave()")
        page.wait_for_timeout(500)
        assert posts == [url]
        assert "Neu" in decrypted_content(motion)
