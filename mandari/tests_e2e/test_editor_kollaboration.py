# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kollaboration im Dokumenteditor mit zwei Browsern (Issue #289, Rest von #185).

Läuft gegen den ASGI-Testserver (Daphne im Thread, In-Memory-Channel-Layer, kein Redis),
weil der WSGI-Live-Server keine WebSockets kann. Zwei Browserkontexte, zwei Nutzer:

- Änderungen erscheinen live beim anderen (Yjs über den Channels-Consumer)
- Verbindung trennen, speichern, neu verbinden: Der ohne Verbindung gespeicherte Stand bleibt
  (#184 Teil 1 – kein stilles Zurücksetzen durch den alten Yjs-Zustand)
- Konflikthinweis: Speichern ohne Verbindung trifft auf einen inzwischen geänderten Stand;
  „Trotzdem speichern“ übernimmt den eigenen Stand (#184 Teil 2)

Die Trennung läuft über Playwrights WebSocket-Routing: Bestehende Verbindungen werden auf
beiden Seiten geschlossen, Wiederverbindungsversuche sofort wieder beendet, bis die
Verbindung freigegeben wird. Der Editor bleibt dabei im Kollaborationsmodus („Offline“)
und speichert per POST mit ``base_content_hash`` – genau der Pfad aus #184.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from apps.work.motions.models import Motion
from tests_e2e.conftest import login_via_form, wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR, SAVE_BUTTON, SAVED_PATTERN

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

WS_DOCUMENTS = re.compile(r"/ws/documents/")
CONNECTED = f"() => {ALPINE_EDITOR}.collabEnabled === true && {ALPINE_EDITOR}.collabStatus === 'connected'"
NICHT_VERBUNDEN = f"() => {ALPINE_EDITOR}.collabEnabled === true && {ALPINE_EDITOR}.collabStatus !== 'connected'"
#: Nach der Trennung „Offline“, sobald der Provider neu verbindet „Verbindung wird hergestellt...“
OFFLINE_ANZEIGE = re.compile(r"Offline|Verbindung wird hergestellt")
KONFLIKT_BANNER = "div[role=alert]:has-text('Das Dokument wurde inzwischen an anderer Stelle geändert')"
#: Verbindungsaufbau wartet auf den Backoff des Providers (1 s, 2 s, 4 s, 8 s, …)
RECONNECT_TIMEOUT = 40000


@pytest.fixture
def zwei_nutzer(db: Any, org: Any, make_member: Any) -> tuple[Any, Any]:
    """
    Zwei Mitglieder derselben Organisation (Browser A und B).

    A ist Autorin; B braucht ``motions.edit_all``, weil der HTTP-Editor fremde Dokumente
    sonst nur lesend öffnet (DocumentEditorView._get_access_level).
    """
    members = []
    for email, permissions in (
        ("anna@example.org", PERMISSIONS),
        ("bernd@example.org", [*PERMISSIONS, "motions.edit_all"]),
    ):
        membership = make_member(org, permissions, email=email)
        membership.user.set_password(PASSWORD)
        membership.user.save(update_fields=["password"])
        members.append(membership)
    return members[0], members[1]


@pytest.fixture
def make_motion(org: Any) -> Callable[..., Motion]:
    """Dokument mit verschlüsseltem Inhalt und ohne gespeicherten Yjs-Zustand."""

    def _make(author: Any, title: str, content: str) -> Motion:
        motion = Motion.objects.create(
            organization=org,
            author=author,
            title=title,
            summary="Kurzfassung",
            status="draft",
            visibility="organization",
        )
        cast(Any, motion).set_content_encrypted(content)
        motion.save()
        return motion

    return _make


@pytest.fixture
def zwei_browser(new_context: Any) -> Iterator[tuple[Any, Any]]:
    """Zwei unabhängige Browserkontexte (eigene Cookies/Sitzungen), je eine Seite."""
    context_a = new_context()
    context_b = new_context()
    yield context_a.new_page(), context_b.new_page()


class Verbindung:
    """
    Schalter für die Kollaborations-WebSockets einer Seite.

    Playwrights ``page.route`` greift bei WebSockets nicht; ``route_web_socket`` reicht die
    Verbindung normalerweise durch und erlaubt es, beide Seiten gezielt zu schließen.

    Im Route-Handler selbst darf nichts Synchrones auf Playwright warten (``ws.close()``
    blockiert dort dauerhaft). Wiederverbindungsversuche während der Trennung werden deshalb
    nicht an den Server durchgereicht, sondern bleiben im Zustand „verbindet“ hängen und
    werden erst beim Freigeben aus dem Testthread geschlossen – der Provider verbindet dann
    mit Backoff neu.
    """

    def __init__(self, page: Any) -> None:
        self.page = page
        self.getrennt = False
        self._offen: list[tuple[Any, Any]] = []
        self._abgewiesen: list[Any] = []
        page.route_web_socket(WS_DOCUMENTS, self._route)

    def _route(self, ws: Any) -> None:
        if self.getrennt:
            self._abgewiesen.append(ws)
            return
        server = ws.connect_to_server()
        self._offen.append((ws, server))

    def trennen(self) -> None:
        self.getrennt = True
        for ws, server in self._offen:
            ws.close(code=4000, reason="E2E: Verbindung getrennt")
            server.close(code=4000, reason="E2E: Verbindung getrennt")
        self._offen.clear()
        self.page.wait_for_function(NICHT_VERBUNDEN, timeout=15000)

    def verbinden(self) -> None:
        self.getrennt = False
        for ws in self._abgewiesen:
            ws.close(code=4000, reason="E2E: Verbindung freigegeben")
        self._abgewiesen.clear()
        self.page.wait_for_function(CONNECTED, timeout=RECONNECT_TIMEOUT)


def editor_url(server: Any, org: Any, motion: Motion) -> str:
    return f"{server.url}/work/{org.slug}/documents/{motion.id}/"


def editor_oeffnen(page: Any, url: str, erwarteter_text: str) -> None:
    """Editor laden, auf die Kollaborationsverbindung und den sichtbaren Inhalt warten."""
    page.goto(url)
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(CONNECTED, timeout=15000)
    # Inhalt kommt aus dem Yjs-Zustand oder wird nach 400 ms aus dem HTML gesät
    expect(page.locator(PROSEMIRROR)).to_contain_text(erwarteter_text, timeout=15000)


def tippen(page: Any, text: str) -> None:
    page.locator(PROSEMIRROR).click()
    page.keyboard.press("End")
    page.keyboard.type(text)


def kollab_stand_sichern(page: Any) -> None:
    """
    Löst das ``yjs_save`` des Providers aus (sonst alle 10 s bzw. beim Verbergen des Tabs).

    Der Provider speichert beim ``visibilitychange`` auf „hidden“; der Zustand wird kurz
    vorgetäuscht. Danach hat der Server den Yjs-Zustand persistiert und per ``yjs_saved``
    den Fingerabdruck zurückgemeldet – der Editor gilt als gespeichert.
    """
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
            document.dispatchEvent(new Event('visibilitychange'))
            delete document.visibilityState
        }"""
    )
    page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=15000)


def unsaved_changes(page: Any) -> bool:
    return bool(page.evaluate(f"() => {ALPINE_EDITOR}.hasUnsavedChanges()"))


def decrypted_content(motion: Motion) -> str:
    motion.refresh_from_db()
    return str(cast(Any, motion).get_content_decrypted() or "")


def warten_bis_gespeichert(page: Any, motion: Motion, text: str) -> None:
    """Pollt die Datenbank, bis der Text gespeichert ist (statt der Anzeige, die ein Reload verdecken kann)."""
    for _ in range(100):
        if text in decrypted_content(motion):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"„{text}“ wurde nicht gespeichert: {decrypted_content(motion)!r}")


def warten_bis_yjs_gesichert(page: Any, motion: Motion) -> None:
    """Pollt die Datenbank, bis der Server den Yjs-Zustand persistiert hat (yjs_save ist asynchron)."""
    for _ in range(100):
        motion.refresh_from_db()
        if motion.get_yjs_state():
            return
        page.wait_for_timeout(100)
    raise AssertionError("Yjs-Zustand wurde nicht gesichert")


def editor_text_ohne_cursor(page: Any) -> str | None:
    """Editortext ohne die Cursor-Labels anderer Nutzer (Tiptap rendert sie in den Inhalt)."""
    return page.evaluate(
        """() => {
            const el = document.querySelector('#editor-container .ProseMirror');
            if (!el) return null;
            const kopie = el.cloneNode(true);
            kopie.querySelectorAll('[class*="collaboration-carets"], [class*="collaboration-cursor"], .ProseMirror-widget').forEach((n) => n.remove());
            return kopie.innerText.replace(/\\s+/g, ' ').trim();
        }"""
    )


def warte_auf_editor_text(page: Any, text: str, timeout: int = 15000) -> None:
    """Pollt den Editortext (ohne Cursor-Labels), bis er exakt dem erwarteten Text entspricht."""
    ist: str | None = None
    for _ in range(max(1, timeout // 200)):
        try:
            ist = editor_text_ohne_cursor(page)
        except playwright_sync.Error:
            # Seite lädt gerade neu (Reload-Aufforderung des Servers): weiter pollen
            ist = None
        if ist == text:
            return
        page.wait_for_timeout(200)
    html = page.evaluate(
        "() => { const el = document.querySelector('#editor-container .ProseMirror'); return el ? el.innerHTML.slice(0, 800) : null; }"
    )
    raise AssertionError(f"Editortext erwartet {text!r}, ist {ist!r}; HTML: {html!r}")


class TestKollaboration:
    def test_aenderung_erscheint_beim_anderen(
        self, asgi_server: Any, zwei_browser: Any, zwei_nutzer: Any, org: Any, make_motion: Any
    ) -> None:
        page_a, page_b = zwei_browser
        anna, bernd = zwei_nutzer
        motion = make_motion(anna, "Gemeinsamer Antrag", "<p>Alt</p>")
        url = editor_url(asgi_server, org, motion)

        login_via_form(page_a, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page_a, url, "Alt")
        tippen(page_a, " Alpha")

        # B öffnet später und bekommt den Stand von A (nicht das alte HTML doppelt)
        login_via_form(page_b, asgi_server.url, bernd.user.email, PASSWORD)
        editor_oeffnen(page_b, url, "Alpha")
        warte_auf_editor_text(page_b, "Alt Alpha", timeout=15000)
        expect(page_a.get_by_text("2 online")).to_be_visible(timeout=15000)
        # B kennt A erst nach einer Awareness-Meldung von A (Cursor); ohne Aktion erneuert
        # y-protocols den Zustand nur alle 15 s – darum A einmal in den Editor klicken lassen.
        page_a.locator(PROSEMIRROR).click()
        expect(page_b.get_by_text("2 online")).to_be_visible(timeout=30000)

        # Und zurück: B tippt, A sieht es
        tippen(page_b, " Beta")
        warte_auf_editor_text(page_a, "Alt Alpha Beta", timeout=15000)

        # Der Server persistiert den gemeinsamen Stand (Yjs-Zustand und HTML)
        kollab_stand_sichern(page_a)
        warten_bis_yjs_gesichert(page_a, motion)
        assert "Alt Alpha Beta" in decrypted_content(motion)

    def test_ohne_verbindung_gespeichert_bleibt_nach_neuverbindung(
        self, asgi_server: Any, zwei_browser: Any, zwei_nutzer: Any, org: Any, make_motion: Any
    ) -> None:
        page_a, page_b = zwei_browser
        anna, bernd = zwei_nutzer
        motion = make_motion(anna, "Unterbrochener Antrag", "<p>Alt</p>")
        url = editor_url(asgi_server, org, motion)
        verbindung_a = Verbindung(page_a)
        login_via_form(page_a, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page_a, url, "Alt")
        login_via_form(page_b, asgi_server.url, bernd.user.email, PASSWORD)
        editor_oeffnen(page_b, url, "Alt")
        tippen(page_a, " Alpha")
        warte_auf_editor_text(page_b, "Alt Alpha", timeout=15000)
        # Gespeicherter Yjs-Zustand ohne den späteren Offline-Text – die Falle aus #184
        kollab_stand_sichern(page_a)
        warten_bis_yjs_gesichert(page_a, motion)

        # A verliert die Verbindung, tippt weiter und speichert per POST
        verbindung_a.trennen()
        expect(page_a.locator("header")).to_contain_text(OFFLINE_ANZEIGE)
        tippen(page_a, " Offline")
        page_a.click(SAVE_BUTTON)
        expect(page_a.locator("header")).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        assert not unsaved_changes(page_a)
        assert "Alt Alpha Offline" in decrypted_content(motion)
        assert motion.get_yjs_state() is None, "POST-Speichern muss den veralteten Yjs-Zustand verwerfen (#184)"

        # B war verbunden, wird zum Neuladen aufgefordert und zeigt den gespeicherten Stand
        warte_auf_editor_text(page_b, "Alt Alpha Offline", timeout=15000)
        page_b.close()

        # A verbindet neu und öffnet erneut: der gespeicherte Stand bleibt, nichts wird zurückgesetzt
        verbindung_a.verbinden()
        warte_auf_editor_text(page_a, "Alt Alpha Offline")
        editor_oeffnen(page_a, url, "Offline")
        warte_auf_editor_text(page_a, "Alt Alpha Offline")
        assert "Alt Alpha Offline" in decrypted_content(motion)

    def test_konflikthinweis_und_trotzdem_speichern(
        self, asgi_server: Any, zwei_browser: Any, zwei_nutzer: Any, org: Any, make_motion: Any
    ) -> None:
        page_a, page_b = zwei_browser
        anna, bernd = zwei_nutzer
        motion = make_motion(anna, "Umstrittener Antrag", "<p>Alt</p>")
        url = editor_url(asgi_server, org, motion)
        verbindung_a = Verbindung(page_a)

        login_via_form(page_a, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page_a, url, "Alt")
        login_via_form(page_b, asgi_server.url, bernd.user.email, PASSWORD)
        editor_oeffnen(page_b, url, "Alt")

        # A offline mit eigener Änderung; B ändert und speichert derweil
        verbindung_a.trennen()
        tippen(page_a, " von A")
        tippen(page_b, " von B")
        # B ist verbunden: keine Konfliktprüfung, danach lädt B selbst neu (Reload-Broadcast)
        page_b.click(SAVE_BUTTON)
        warten_bis_gespeichert(page_b, motion, "Alt von B")

        # A speichert von einem veralteten Stand aus → Hinweis statt stiller Überschreibung
        page_a.click(SAVE_BUTTON)
        expect(page_a.locator(KONFLIKT_BANNER)).to_be_visible()
        expect(page_a.locator("header")).to_contain_text("Fehler beim Speichern", use_inner_text=True)
        assert unsaved_changes(page_a)
        assert "Alt von B" in decrypted_content(motion)
        assert "von A" not in decrypted_content(motion)

        # „Trotzdem speichern“ übernimmt den Stand von A
        page_a.locator(KONFLIKT_BANNER).get_by_role("button", name="Trotzdem speichern").click()
        expect(page_a.locator(KONFLIKT_BANNER)).to_be_hidden()
        expect(page_a.locator("header")).to_contain_text(SAVED_PATTERN, use_inner_text=True)
        assert not unsaved_changes(page_a)
        inhalt = decrypted_content(motion)
        assert "Alt von A" in inhalt
        assert "von B" not in inhalt

        # B war verbunden und lädt den übernommenen Stand
        warte_auf_editor_text(page_b, "Alt von A", timeout=15000)
