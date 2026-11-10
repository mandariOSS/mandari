# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichern vor jedem Schritt, der die Seite verlässt oder neu lädt (Issue #1018), im neuen Antragseditor.

Mit Live-Zusammenarbeit (ASGI-Testserver mit In-Memory-Channel-Layer, wie tests_e2e/test_editor_kollaboration.py):

- Titel, Dokumenttyp und Briefpapier laufen nicht über Yjs. Sie werden per POST gesichert, ohne den Text zu schicken,
  und der Editor gilt erst danach als gespeichert – eine Bestätigung der Verbindung (yjs_saved) allein reicht nicht.
- „Freigeben“ sichert vorher den gemeinsamen Stand. Das auslösende Fenster bekommt keine Aufforderung zum Neuladen,
  das andere schon; die letzten Eingaben stehen in der gesperrten Fassung.
- „Herunterladen als Word“ sichert vorher und lädt die Datei, ohne die Seite zu verlassen.
- „Vorschau (PDF)“, Links auf andere Seiten und der Teilen-Dialog sichern vorher und gehen ohne Rückfrage.

Ohne Verbindung (WSGI-Live-Server, Solo-Modus): Federführung und Zustimmen aus der Seitenleiste bzw. Infozeile sichern
vorher den Text und laden ohne Rückfrage „Website verlassen?“ neu.

Folgepunkte (Issue #1199): Ein abgebrochener POST mit dem Titel, den der Server erst nach dem Wiederholversuch
verarbeitet, überschreibt den neueren Titel nicht; nach dem Wechsel in den Solo-Modus läuft das automatische Speichern
weiter.

Das regelmäßige Sichern der Verbindung (alle 10 s) ist in diesen Tests abgeschaltet: Sonst hinge es vom Zufall ab, ob
die letzten Eingaben schon gespeichert sind. Gesichert wird nur, was die geprüften Schritte selbst sichern.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from apps.work.motions.models import Motion, MotionApproval, MotionType, OrganizationLetterhead
from tests_e2e.conftest import login_via_form, wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR
from tests_e2e.test_editor_kollaboration import (
    CONNECTED,
    WS_DOCUMENTS,
    decrypted_content,
    editor_oeffnen,
    editor_url,
    kollab_stand_sichern,
    tippen,
    unsaved_changes,
    warte_auf_editor_text,
    warten_bis_yjs_gesichert,
)

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

DESKTOP = {"width": 1440, "height": 900}
#: Das regelmäßige Sichern des Kollaborationsanbieters (setInterval mit 10 s) auf eine Stunde strecken
OHNE_REGELMAESSIGES_SICHERN = """
(() => {
    const original = window.setInterval
    window.setInterval = (fn, ms, ...rest) => original(fn, ms === 10000 ? 3600000 : ms, ...rest)
})()
"""
#: Das automatische Speichern des Editors (setInterval mit 60 s) läuft alle 500 ms
AUTOMATISCH_SPEICHERN_RASCH = """
(() => {
    const original = window.setInterval
    window.setInterval = (fn, ms, ...rest) => original(fn, ms === 60000 ? 500 : ms, ...rest)
})()
"""
GESPEICHERT_ANZEIGE = ".ke-gespeichert"
#: Schreibt alle 10 ms mit, ob der Editor ungesicherte Änderungen meldet (Millisekunden seit dem Laden). Daran hängt die
#: Rückfrage „Website verlassen?“; ein echtes beforeunload ließe den Provider sichern und verdeckte den Befund.
UNGESICHERT_MITSCHREIBEN = """
(() => {
    window.__ungesichert = []
    const start = performance.now()
    window.setInterval(() => {
        const el = document.querySelector('.editor-wrapper')
        if (!el || !window.Alpine) return
        try {
            if (window.Alpine.$data(el).hasUnsavedChanges()) {
                window.__ungesichert.push(Math.round(performance.now() - start))
            }
        } catch {
            // Komponente noch nicht bereit
        }
    }, 10)
})()
"""
#: Gespeichert und keine Störung (auch kein wartender Versuch)
GESICHERT_OHNE_STOERUNG = (
    f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false && {ALPINE_EDITOR}.speicherStoerung === ''"
    f" && {ALPINE_EDITOR}.saveError === false"
)
#: Der Provider sichert beim Verbergen des Tabs (yjs_save); der Zustand wird kurz vorgetäuscht
TAB_KURZ_VERBORGEN = """() => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    delete document.visibilityState
}"""
#: Der Text lässt sich bearbeiten (contenteditable)
EDITOR_BEARBEITBAR = f"() => document.querySelector('{PROSEMIRROR}')?.isContentEditable === true"
#: Die Kollaborationsverbindung kommt nie zustande: Der Provider bleibt bei „Verbindung wird hergestellt“
WEBSOCKET_OHNE_VERBINDUNG = """
(() => {
    const Original = window.WebSocket
    function Haengend(url, protocols) {
        if (!String(url).includes('/ws/documents/')) return new Original(url, protocols)
        this.url = String(url)
        this.readyState = 0
        this.binaryType = 'blob'
        this.send = () => {}
        this.close = () => {}
    }
    Haengend.CONNECTING = 0
    Haengend.OPEN = 1
    Haengend.CLOSING = 2
    Haengend.CLOSED = 3
    window.WebSocket = Haengend
})()
"""


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
def anna(db: Any, org: Any, make_member: Any) -> Any:
    """Autorin: bearbeitet, teilt und steuert den Ablauf ihres Dokuments."""
    return _mitglied(make_member, org, "anna-sichern@example.org", [*PERMISSIONS, "motions.share"])


@pytest.fixture
def bernd(db: Any, org: Any, make_member: Any) -> Any:
    """Zweites Mitglied, darf alle Dokumente bearbeiten (Kollaboration in fremden Dokumenten)."""
    return _mitglied(make_member, org, "bernd-sichern@example.org", [*PERMISSIONS, "motions.edit_all"])


@pytest.fixture
def dokument(org: Any, anna: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=anna, title="Antrag Radweg", summary="Kurzfassung", visibility="organization"
    )
    cast(Any, motion).set_content_encrypted("<p>Alt</p>")
    motion.save()
    return motion


class WsNachrichten:
    """JSON-Nachrichten der Kollaborationsverbindung einer Seite (auch über ein Neuladen hinweg)."""

    def __init__(self, page: Any) -> None:
        self.empfangen: list[dict[str, Any]] = []
        self.gesendet: list[dict[str, Any]] = []
        #: Gemeinsame Liste mit anderen Ereignissen der Seite: Typen empfangener Nachrichten in der Reihenfolge
        self.protokoll: list[str] | None = None
        page.on("websocket", self._verbindung)

    def _verbindung(self, ws: Any) -> None:
        if "/ws/documents/" not in ws.url:
            return
        ws.on("framereceived", lambda payload: self._merken(self.empfangen, payload, self.protokoll))
        ws.on("framesent", lambda payload: self._merken(self.gesendet, payload, None))

    @staticmethod
    def _merken(liste: list[dict[str, Any]], payload: Any, protokoll: list[str] | None) -> None:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        try:
            nachricht = json.loads(payload)
        except ValueError:
            return
        if isinstance(nachricht, dict):
            liste.append(nachricht)
            if protokoll is not None:
                protokoll.append(str(nachricht.get("type")))

    def empfangene_typen(self) -> list[str]:
        return [str(n.get("type")) for n in self.empfangen]


def dialoge_merken(page: Any) -> list[str]:
    """Rückfragen des Browsers (beforeunload, confirm) merken und ablehnen: Ein Schritt mit Rückfrage gilt als Fehler."""
    dialoge: list[str] = []

    def ablehnen(dialog: Any) -> None:
        dialoge.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", ablehnen)
    return dialoge


def vorbereiten(page: Any) -> None:
    page.set_viewport_size(DESKTOP)
    page.add_init_script(OHNE_REGELMAESSIGES_SICHERN)


def kein_neuladen_markieren(page: Any) -> None:
    page.evaluate("() => { window.__ohneNeuladen = true }")


def nicht_neu_geladen(page: Any) -> bool:
    return bool(page.evaluate("() => window.__ohneNeuladen === true"))


def warten_bis(page: Any, bedingung: Callable[[], bool], beschreibung: str, sekunden: float = 15) -> None:
    for _ in range(int(sekunden * 10)):
        if bedingung():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"Nicht eingetreten: {beschreibung}")


def datei_menue(page: Any, punkt: str) -> Any:
    page.locator(".ke-kopf [data-menue-knopf=datei]").click()
    return page.locator(".ke-kopf").get_by_role("menuitem", name=punkt)


class PostAbbruch:
    """
    Der erste POST an den Editor kommt beim Server an, die Antwort aber nicht beim Browser (wie bei einer
    Zeitüberschreitung). Bis zum Aufruf von ``annehmen_und_abbrechen`` bleibt er zurückgehalten; alle weiteren POSTs
    gehen durch und werden mitgeschrieben.
    """

    def __init__(self, page: Any, dokument: Motion) -> None:
        self.page = page
        self.gehalten: list[Any] = []
        self.weitere: list[str] = []
        #: Adresse, Content-Type und Inhalt des abgebrochenen POST für ``nachliefern``
        self.abgebrochen: tuple[str, str, bytes] | None = None
        page.route(f"**/documents/{dokument.id}/", self._route)

    def _route(self, route: Any) -> None:
        if route.request.method != "POST":
            route.continue_()
        elif not self.gehalten:
            self.gehalten.append(route)
        else:
            self.weitere.append(route.request.post_data or "")
            route.continue_()

    def annehmen_und_abbrechen(self) -> None:
        route = self.gehalten[0]
        antwort = route.fetch()
        assert antwort.ok, f"Der Server hat den zurückgehaltenen POST nicht angenommen: {antwort.status}"
        route.abort("timedout")

    def abbrechen(self) -> None:
        """Der Browser sieht eine Zeitüberschreitung, der Server hat den POST noch nicht verarbeitet."""
        route = self.gehalten[0]
        request = route.request
        self.abgebrochen = (request.url, request.headers.get("content-type", ""), request.post_data_buffer or b"")
        route.abort("timedout")

    def nachliefern(self) -> Any:
        """Der Server verarbeitet den abgebrochenen POST erst jetzt, unverändert und mit derselben Anmeldung."""
        assert self.abgebrochen is not None
        url, content_type, daten = self.abgebrochen
        return self.page.context.request.post(
            url, headers={"Content-Type": content_type, "X-Requested-With": "XMLHttpRequest"}, data=daten
        )


class ServerNachrichtenHalten:
    """
    Hält JSON-Nachrichten der Kollaborationsverbindung vom Server an die Seite zurück, deren Typ in ``typen`` steht,
    bis ``freigeben``. Alles andere geht in beide Richtungen durch. Im Handler wird nur gemerkt oder weitergereicht
    (``send`` wartet nicht auf Playwright).
    """

    def __init__(self, page: Any, typen: set[str]) -> None:
        self.typen = set(typen)
        self.gehalten: list[tuple[Any, str]] = []
        page.route_web_socket(WS_DOCUMENTS, self._route)

    def _route(self, ws: Any) -> None:
        server = ws.connect_to_server()
        server.on_message(lambda nachricht: self._vom_server(ws, nachricht))

    def _vom_server(self, ws: Any, nachricht: Any) -> None:
        if isinstance(nachricht, str) and self.typen:
            try:
                typ = json.loads(nachricht).get("type")
            except (ValueError, AttributeError):
                typ = None
            if typ in self.typen:
                self.gehalten.append((ws, nachricht))
                return
        ws.send(nachricht)

    def freigeben(self) -> None:
        self.typen = set()
        gehalten, self.gehalten = self.gehalten, []
        for ws, nachricht in gehalten:
            ws.send(nachricht)


def solo_oeffnen(page: Any, url: str) -> None:
    """Editor ohne Verbindung (WSGI-Live-Server) laden und auf den Solo-Modus warten."""
    page.goto(url)
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)


def docx_text(pfad: Path) -> str:
    from docx import Document

    dokument = Document(str(pfad))
    teile = [absatz.text for absatz in dokument.paragraphs]
    for tabelle in dokument.tables:
        teile.extend(zelle.text for zeile in tabelle.rows for zelle in zeile.cells)
    return "\n".join(teile)


class TestMitVerbindung:
    def test_titel_typ_und_briefpapier_gesichert(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
        briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
        vorbereiten(page)
        ws = WsNachrichten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        url = editor_url(asgi_server, org, dokument)
        editor_oeffnen(page, url, "Alt")
        kein_neuladen_markieren(page)

        # POST-Anfragen an den Editor zurückhalten, um den Zwischenstand zu prüfen
        gehalten: list[Any] = []
        freigegeben: list[bool] = []

        def halten(route: Any) -> None:
            if route.request.method == "POST" and not freigegeben:
                gehalten.append(route)
            else:
                route.continue_()

        page.route(f"**/documents/{dokument.id}/", halten)

        page.fill("#ke-titel", "Antrag Radweg Hauptstraße")
        page.locator("[data-menue-knopf=typ]").click()
        page.get_by_role("menuitemradio", name="Anfrage").click()
        page.locator("[data-menue-knopf=briefpapier]").click()
        page.get_by_role("menuitemradio", name="Fraktion").click()
        tippen(page, " Neu")

        # Die Verbindung sichert den Text (Tab kurz verborgen); das allein gilt nicht als gespeichert
        bestaetigt = ws.empfangene_typen().count("yjs_saved")
        page.evaluate(
            """() => {
                Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
                document.dispatchEvent(new Event('visibilitychange'))
                delete document.visibilityState
            }"""
        )
        warten_bis(page, lambda: ws.empfangene_typen().count("yjs_saved") > bestaetigt, "yjs_saved nach dem Sichern")
        assert page.evaluate(f"() => {ALPINE_EDITOR}.hasUnsavedChanges()") is True
        expect(page.locator(GESPEICHERT_ANZEIGE)).to_contain_text("Ungespeicherte Änderungen", use_inner_text=True)

        # Kurz danach gehen Titel, Typ und Briefpapier per POST – ohne den Text
        warten_bis(page, lambda: bool(gehalten), "POST mit Titel, Dokumenttyp und Briefpapier")
        anfrage = gehalten[0].request.post_data or ""
        assert 'name="title"' in anfrage and "Antrag Radweg Hauptstraße" in anfrage
        assert 'name="content"' not in anfrage, "mit Verbindung darf der POST den Text nicht schicken"
        freigegeben.append(True)
        for route in gehalten:
            route.continue_()

        page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=15000)
        expect(page.locator(GESPEICHERT_ANZEIGE)).to_contain_text("Gespeichert", use_inner_text=True)
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg Hauptstraße"
        assert dokument.document_type_id == typ.id
        assert dokument.letterhead_id == briefkopf.id
        assert "Alt Neu" in decrypted_content(dokument)
        assert nicht_neu_geladen(page), "das eigene Fenster darf nicht neu laden"
        assert "reload" not in ws.empfangene_typen()

        # Danach nur der Titel: Der POST schickt nur ihn. Dokumenttyp und Briefpapier, die ein anderes Fenster
        # inzwischen geändert haben kann, setzt er nicht auf den Stand dieses Fensters zurück.
        posts: list[str] = []
        page.on(
            "request",
            lambda r: posts.append(r.post_data or "")
            if r.method == "POST" and r.url.endswith(f"/documents/{dokument.id}/")
            else None,
        )
        Motion.objects.filter(pk=dokument.pk).update(letterhead=None)  # „anderes Fenster“ entfernt das Briefpapier
        page.fill("#ke-titel", "Antrag Radweg Nebenstraße")
        warten_bis(page, lambda: bool(posts), "POST nur mit dem Titel")
        page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=15000)
        assert 'name="title"' in posts[-1] and "Antrag Radweg Nebenstraße" in posts[-1]
        assert 'name="letterhead_id"' not in posts[-1] and 'name="document_type_id"' not in posts[-1]
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg Nebenstraße"
        assert dokument.letterhead_id is None, "das Briefpapier des anderen Fensters wurde zurückgesetzt"
        assert dokument.document_type_id == typ.id

    def test_freigeben_sichert_vorher_und_laedt_nur_die_anderen(
        self, new_context: Any, asgi_server: Any, org: Any, anna: Any, bernd: Any, dokument: Motion
    ) -> None:
        dokument.status = "internal_review"
        dokument.save(update_fields=["status"])
        page_a, page_b = new_context().new_page(), new_context().new_page()
        for page in (page_a, page_b):
            vorbereiten(page)
        ws_a, ws_b = WsNachrichten(page_a), WsNachrichten(page_b)
        dialoge_a = dialoge_merken(page_a)
        url = editor_url(asgi_server, org, dokument)
        login_via_form(page_a, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page_a, url, "Alt")
        login_via_form(page_b, asgi_server.url, bernd.user.email, PASSWORD)
        editor_oeffnen(page_b, url, "Alt")

        # Während der Abstimmung öffnet der Editor im Modus „Vorschlagen“: zum Tippen auf „Bearbeiten“
        page_a.evaluate(f"() => {ALPINE_EDITOR}.modusSetzen('bearbeiten')")
        tippen(page_a, " Letzter Satz.")
        warte_auf_editor_text(page_b, "Alt Letzter Satz.")
        assert "Letzter Satz." not in decrypted_content(dokument), "Vorbedingung: noch nicht gesichert"

        page_a.get_by_test_id("ablauf-info").get_by_role("button", name="Freigeben …").click()
        with page_a.expect_navigation(timeout=20000):
            page_a.get_by_test_id("dialog-freigeben").get_by_role("button", name="Freigeben", exact=True).click()
        wait_for_bundle(page_a)

        dokument.refresh_from_db()
        assert dokument.status == "approved"
        assert "Alt Letzter Satz." in decrypted_content(dokument), "die letzten Eingaben fehlen in der Fassung"
        # Keine Aufforderung „an anderer Stelle gespeichert“ an das auslösende Fenster (die Herabstufung des eigenen
        # Zugriffs durch die Sperre meldet der Server weiterhin, mit Grund)
        neuladen_a = [n for n in ws_a.empfangen if n.get("type") == "reload" and not n.get("reason")]
        assert not neuladen_a, f"das auslösende Fenster lädt selbst neu: {neuladen_a}"
        assert dialoge_a == []
        # Das andere Fenster wird zum Neuladen aufgefordert (gesperrter Status)
        warten_bis(
            page_b,
            lambda: any(n.get("type") == "reload" and not n.get("reason") for n in ws_b.empfangen),
            "Aufforderung zum Neuladen im anderen Fenster",
        )
        # Das andere Fenster lädt nach 0,8 s selbst neu; erst danach weiter
        page_b.wait_for_timeout(1500)

        # „Als eingereicht markieren“ sichert ebenfalls vorher (Bernd darf trotz Sperre bearbeiten)
        editor_oeffnen(page_b, url, "Letzter Satz.")
        dialoge_b = dialoge_merken(page_b)
        tippen(page_b, " Eingereicht.")
        page_b.get_by_test_id("ablauf-info").get_by_role("button", name="Einreichen …").click()
        with page_b.expect_navigation(timeout=20000):
            page_b.get_by_test_id("dialog-einreichen").get_by_role("button", name="Als eingereicht markieren").click()
        dokument.refresh_from_db()
        assert dokument.status == "submitted"
        assert "Alt Letzter Satz. Eingereicht." in decrypted_content(dokument)
        assert dialoge_b == []

    def test_word_herunterladen_sichert_ohne_seitenwechsel(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        vorbereiten(page)
        dialoge = dialoge_merken(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        url = editor_url(asgi_server, org, dokument)
        editor_oeffnen(page, url, "Alt")
        kein_neuladen_markieren(page)
        # Verlässt der Klick die Seite (Navigation zur Datei), feuert beforeunload – auch wenn daraus ein Download wird
        page.evaluate(
            "() => { window.__verlassen = 0; addEventListener('beforeunload', () => { window.__verlassen += 1 }) }"
        )
        tippen(page, " Für die Word-Datei.")

        with page.expect_download(timeout=20000) as download:
            datei_menue(page, "Herunterladen als Word").click()

        assert "Für die Word-Datei." in docx_text(Path(download.value.path()))
        assert "Für die Word-Datei." in decrypted_content(dokument)
        assert page.evaluate("() => window.__verlassen") == 0, "Herunterladen darf die Seite nicht verlassen"
        assert dialoge == [], "Herunterladen darf nicht nach dem Verlassen der Seite fragen"
        assert nicht_neu_geladen(page) and page.url == url

    def test_vorschau_links_und_teilen_sichern_vorher(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        vorbereiten(page)
        dialoge = dialoge_merken(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        url = editor_url(asgi_server, org, dokument)
        editor_oeffnen(page, url, "Alt")

        # „Vorschau (PDF)“: neue Registerkarte mit dem gesicherten Stand, im Browser statt als Download
        tippen(page, " Für die Vorschau.")
        with page.context.expect_event("response", lambda r: "vorschau=1" in r.url, timeout=30000) as antwort:
            datei_menue(page, "Vorschau (PDF)").click()
        assert antwort.value.headers.get("content-disposition", "").startswith("inline")
        assert "Für die Vorschau." in decrypted_content(dokument)
        for andere in page.context.pages:
            if andere is not page:
                andere.close()

        # Link im Kopf: sichern, dann ohne Rückfrage gehen
        tippen(page, " Vor dem Gehen.")
        with page.expect_navigation(timeout=20000):
            page.locator(".ke-kopf a[aria-label='Zu den Dokumenten']").click()
        assert "Vor dem Gehen." in decrypted_content(dokument)

        # Teilen-Dialog: sichert vor dem Absenden, danach lädt die Seite ohne Rückfrage neu (HX-Refresh)
        editor_oeffnen(page, url, "Vor dem Gehen.")
        tippen(page, " Vor dem Teilen.")
        datei_menue(page, "Teilen …").click()
        teilen = page.get_by_role("dialog", name="Dokument teilen")
        expect(teilen).to_be_visible()
        with page.expect_navigation(timeout=20000):
            teilen.get_by_role("button", name="Speichern").click()
        assert "Vor dem Teilen." in decrypted_content(dokument)
        assert dialoge == []

    @pytest.mark.parametrize("mit_yjs_stand", [False, True], ids=["erstes-oeffnen", "gespeicherter-yjs-stand"])
    def test_anfangsstand_gilt_nicht_als_aenderung(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion, mit_yjs_stand: bool
    ) -> None:
        """
        Wer den Editor gleich nach dem Aufbau ohne Eingabe verlässt, bekommt keine Rückfrage. Der Anfangsstand (Zustand
        vom Server bzw. das Auffüllen aus dem gespeicherten Text) zählte rund 0,2 bis 0,6 s als Änderung (#1018).
        """
        vorbereiten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        url = editor_url(asgi_server, org, dokument)
        if mit_yjs_stand:
            editor_oeffnen(page, url, "Alt")
            kollab_stand_sichern(page)
            warten_bis_yjs_gesichert(page, dokument)
        page.add_init_script(UNGESICHERT_MITSCHREIBEN)

        editor_oeffnen(page, url, "Alt")
        # Auffüllen nach 0,4 s und den früheren Ausgleich nach 0,6 s sicher abwarten
        page.wait_for_timeout(1500)
        ungesichert = page.evaluate("() => window.__ungesichert")
        assert ungesichert == [], f"Ohne Eingabe galt der Stand als ungesichert (ms nach dem Laden): {ungesichert}"

        # Eine Eingabe zählt weiter vom ersten Zeichen an, bis der Server sie bestätigt
        tippen(page, " Neu")
        assert unsaved_changes(page) is True
        page.wait_for_timeout(1000)
        assert unsaved_changes(page) is True, "die Eingabe darf nicht von selbst als gesichert gelten"
        kollab_stand_sichern(page)
        assert "Alt Neu" in decrypted_content(dokument)

    def test_abgebrochenes_sichern_des_titels_wird_wiederholt(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Der Server hat den neuen Titel angenommen, die Antwort kam aber nicht an. Setzt die Person den Titel vor dem
        nächsten Versuch zurück, muss der Editor ihn trotzdem schicken; sonst behielte der Server den Zwischenwert
        (#1018).
        """
        vorbereiten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page, editor_url(asgi_server, org, dokument), "Alt")
        abbruch = PostAbbruch(page, dokument)

        page.fill("#ke-titel", "Zwischentitel")
        warten_bis(page, lambda: bool(abbruch.gehalten), "POST mit dem Titel")
        page.fill("#ke-titel", "Antrag Radweg")
        abbruch.annehmen_und_abbrechen()
        dokument.refresh_from_db()
        assert dokument.title == "Zwischentitel", "Vorbedingung: Der Server hat den Zwischenwert"

        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        assert any('name="title"' in post and "Antrag Radweg" in post for post in abbruch.weitere), abbruch.weitere
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg", "der Server behielt den Zwischenwert"

    def test_verspaetet_verarbeiteter_post_des_titels_gewinnt_nicht(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Der Browser bricht den POST mit dem Titel nach dem Zeitlimit ab, der Server verarbeitet ihn aber erst nach dem
        Wiederholversuch mit dem neueren Titel. Der Editor schickt den Stand mit, von dem er ausgeht; der ältere POST
        passt dann nicht mehr und schreibt nichts (#1199).
        """
        vorbereiten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        editor_oeffnen(page, editor_url(asgi_server, org, dokument), "Alt")
        abbruch = PostAbbruch(page, dokument)

        page.fill("#ke-titel", "Zwischentitel")
        warten_bis(page, lambda: bool(abbruch.gehalten), "POST mit dem Titel")
        page.fill("#ke-titel", "Antrag Radweg Hauptstraße")
        abbruch.abbrechen()
        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg Hauptstraße", "Vorbedingung: Der Wiederholversuch ist gespeichert"

        assert abbruch.abgebrochen is not None and b"Zwischentitel" in abbruch.abgebrochen[2]
        antwort = abbruch.nachliefern()
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg Hauptstraße", "der ältere Titel hat den neueren überschrieben"
        assert antwort.status == 409, antwort.text()
        assert antwort.json()["error"] == "meta_ueberholt"

        # Der Editor arbeitet mit dem Stand des Servers weiter
        page.fill("#ke-titel", "Antrag Radweg Nebenstraße")
        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg Nebenstraße"

    def test_eingabe_gleich_nach_dem_anfangsstand_bleibt_ungesichert(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Eine Eingabe direkt nach dem Anfangsstand gilt als ungesichert, bis der Server sie bestätigt. Der frühere
        Ausgleich 600 ms nach dem Anfangsstand zählte sie als gesichert (#1018).
        """
        vorbereiten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        url = editor_url(asgi_server, org, dokument)
        # Mit gespeichertem Yjs-Stand: Der Text steht mit dem Anfangsstand sofort da
        editor_oeffnen(page, url, "Alt")
        kollab_stand_sichern(page)
        warten_bis_yjs_gesichert(page, dokument)

        halten = ServerNachrichtenHalten(page, {"yjs_state"})
        page.goto(url)
        wait_for_bundle(page)
        page.wait_for_function(CONNECTED, timeout=15000)
        warten_bis(page, lambda: bool(halten.gehalten), "Anfangsstand zurückgehalten")
        halten.freigeben()
        page.wait_for_function(
            f"() => {{ const el = document.querySelector('{PROSEMIRROR}');"
            " return !!el && el.isContentEditable && el.textContent.includes('Alt') }",
            timeout=15000,
        )
        tippen(page, " Sofort")
        page.wait_for_timeout(1200)
        assert unsaved_changes(page) is True, "die Eingabe gleich nach dem Anfangsstand galt als gesichert"
        kollab_stand_sichern(page)
        assert "Alt Sofort" in decrypted_content(dokument)

    def test_vor_dem_anfangsstand_keine_eingabe_und_keine_sicherung(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Ohne gespeicherten Yjs-Stand war der Editor bis zum Anfangsstand leer und bearbeitbar. Eine Eingabe verhinderte
        das Auffüllen aus dem gespeicherten Text, und die nächste Sicherung ersetzte ihn. Eine Sicherung in dieser
        Zeit (Tab verborgen, Strg+S) ersetzte ihn ebenso, auch ohne Eingabe (#1018).
        """
        vorbereiten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        halten = ServerNachrichtenHalten(page, {"yjs_state"})
        page.goto(editor_url(asgi_server, org, dokument))
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(CONNECTED, timeout=15000)
        warten_bis(page, lambda: bool(halten.gehalten), "Anfangsstand zurückgehalten")

        # Eingabe vor dem Anfangsstand: wird nicht angenommen
        page.locator(PROSEMIRROR).click()
        page.keyboard.type("Zu früh")
        assert "Zu früh" not in page.locator(PROSEMIRROR).inner_text()
        assert unsaved_changes(page) is False
        # Sichern vor dem Anfangsstand: ersetzt den gespeicherten Text nicht
        page.evaluate(TAB_KURZ_VERBORGEN)
        page.keyboard.press("Control+s")
        page.wait_for_timeout(1000)
        assert decrypted_content(dokument) == "<p>Alt</p>"
        dokument.refresh_from_db()
        assert cast(Any, dokument).get_yjs_state() is None, "ein leerer Stand wurde als gemeinsamer Stand gespeichert"

        halten.freigeben()
        expect(page.locator(PROSEMIRROR)).to_contain_text("Alt", timeout=15000)
        page.wait_for_function(EDITOR_BEARBEITBAR, timeout=15000)
        expect(page.locator(PROSEMIRROR)).not_to_contain_text("Zu früh")
        tippen(page, " Neu")
        kollab_stand_sichern(page)
        inhalt = decrypted_content(dokument)
        assert "Alt Neu" in inhalt and "Zu früh" not in inhalt

    def test_eingabe_waehrend_des_sicherns_bleibt_ungesichert(
        self, page: Any, asgi_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Die Bestätigung einer Sicherung gilt für den Stand beim Senden. Eingaben zwischen Senden und Bestätigung zählten
        als gesichert (#1018).
        """
        vorbereiten(page)
        login_via_form(page, asgi_server.url, anna.user.email, PASSWORD)
        halten = ServerNachrichtenHalten(page, set())
        editor_oeffnen(page, editor_url(asgi_server, org, dokument), "Alt")
        tippen(page, " Erster")
        stand_vorher = page.evaluate(f"() => {ALPINE_EDITOR}.contentHash")

        halten.typen = {"yjs_saved"}
        page.evaluate(TAB_KURZ_VERBORGEN)
        warten_bis(page, lambda: bool(halten.gehalten), "Bestätigung der Sicherung zurückgehalten")
        tippen(page, " Zweiter")
        halten.freigeben()
        page.wait_for_function(f"() => {ALPINE_EDITOR}.contentHash !== {json.dumps(stand_vorher)}", timeout=15000)
        assert unsaved_changes(page) is True, "die Eingabe nach dem Senden galt mit der Bestätigung als gesichert"
        assert "Zweiter" not in decrypted_content(dokument)

        kollab_stand_sichern(page)
        assert "Alt Erster Zweiter" in decrypted_content(dokument)


class TestOhneVerbindung:
    """Seitenleiste und Infozeile im Solo-Modus: Ohne Sichern gingen die Eingaben beim Neuladen verloren."""

    def test_federfuehrung_und_zustimmen_sichern_vorher(
        self, page: Any, live_server: Any, org: Any, anna: Any, bernd: Any, dokument: Motion
    ) -> None:
        page.set_viewport_size(DESKTOP)
        dialoge = dialoge_merken(page)
        login_via_form(page, live_server.url, anna.user.email, PASSWORD)
        url = f"{live_server.url}/work/{org.slug}/documents/{dokument.id}/"
        page.goto(url)
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)

        # Federführung (Details): sichert den Text, lädt ohne Rückfrage neu
        tippen(page, " Vor der Federführung.")
        datei_menue(page, "Details und Zuständigkeit").click()
        with page.expect_navigation(timeout=20000):
            page.locator(".ke-panel select[name=responsible]").select_option(str(bernd.id))
        dokument.refresh_from_db()
        assert dokument.responsible_id == bernd.id
        assert "Alt Vor der Federführung." in decrypted_content(dokument)

        # Zustimmen in der Infozeile (Abstimmung läuft, Anna ist angefragt)
        dokument.status = "internal_review"
        dokument.save(update_fields=["status"])
        anfrage = MotionApproval.objects.create(motion=dokument, approver=anna, approval_type="council")
        page.goto(url)
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)
        page.evaluate(f"() => {ALPINE_EDITOR}.modusSetzen('bearbeiten')")
        tippen(page, " Vor der Zustimmung.")
        with page.expect_navigation(timeout=20000):
            page.get_by_test_id("ablauf-info").get_by_role("button", name="Zustimmen").click()
        anfrage.refresh_from_db()
        assert anfrage.approved is True
        assert "Vor der Zustimmung." in decrypted_content(dokument)
        assert dialoge == [], "nach dem Sichern darf der Browser nicht nach dem Verlassen fragen"

    def test_nur_geaenderte_felder_gehen_mit(
        self, page: Any, live_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Ohne Verbindung gingen Titel, Dokumenttyp und Briefpapier mit jedem Sichern mit, mit dem Stand dieses Fensters.
        Ein anderes Fenster hatte sie inzwischen geändert; das Sichern des Textes setzte sie zurück (#1018).
        """
        typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
        briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
        page.set_viewport_size(DESKTOP)
        login_via_form(page, live_server.url, anna.user.email, PASSWORD)
        solo_oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{dokument.id}/")
        posts: list[str] = []
        page.on(
            "request",
            lambda r: posts.append(r.post_data or "")
            if r.method == "POST" and r.url.endswith(f"/documents/{dokument.id}/")
            else None,
        )

        # Ein anderes Fenster ändert Titel, Dokumenttyp und Briefpapier
        Motion.objects.filter(pk=dokument.pk).update(
            title="Titel aus dem anderen Fenster", document_type=typ, letterhead=briefkopf
        )
        tippen(page, " Ohne Verbindung.")
        page.keyboard.press("Control+s")
        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        assert 'name="content"' in posts[-1]
        for feld in ("title", "document_type_id", "letterhead_id"):
            assert f'name="{feld}"' not in posts[-1], f"{feld} hat sich in diesem Fenster nicht geändert"
        dokument.refresh_from_db()
        assert dokument.title == "Titel aus dem anderen Fenster"
        assert dokument.document_type_id == typ.id
        assert dokument.letterhead_id == briefkopf.id
        assert "Alt Ohne Verbindung." in decrypted_content(dokument)

        # Was dieses Fenster ändert, geht mit
        page.fill("#ke-titel", "Eigener Titel")
        page.keyboard.press("Control+s")
        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        assert 'name="title"' in posts[-1] and "Eigener Titel" in posts[-1]
        assert 'name="document_type_id"' not in posts[-1] and 'name="letterhead_id"' not in posts[-1]
        dokument.refresh_from_db()
        assert dokument.title == "Eigener Titel"
        assert dokument.document_type_id == typ.id
        assert dokument.letterhead_id == briefkopf.id

    def test_abgebrochenes_sichern_des_titels_wird_wiederholt(
        self, page: Any, live_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """Wie mit Verbindung: Felder eines unbeantworteten Versuchs gehen beim nächsten Sichern mit (#1018)."""
        page.set_viewport_size(DESKTOP)
        login_via_form(page, live_server.url, anna.user.email, PASSWORD)
        solo_oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{dokument.id}/")
        abbruch = PostAbbruch(page, dokument)

        page.fill("#ke-titel", "Zwischentitel")
        page.keyboard.press("Control+s")
        warten_bis(page, lambda: bool(abbruch.gehalten), "POST mit dem Titel")
        page.fill("#ke-titel", "Antrag Radweg")
        abbruch.annehmen_und_abbrechen()
        dokument.refresh_from_db()
        assert dokument.title == "Zwischentitel", "Vorbedingung: Der Server hat den Zwischenwert"

        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        assert any('name="title"' in post and "Antrag Radweg" in post for post in abbruch.weitere), abbruch.weitere
        dokument.refresh_from_db()
        assert dokument.title == "Antrag Radweg", "der Server behielt den Zwischenwert"

    def test_ohne_anfangsstand_nach_zeitlimit_solo_mit_gespeichertem_text(
        self, page: Any, live_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Kommt die Verbindung nicht zustande, bleibt der Editor nicht dauerhaft gesperrt: Nach dem Zeitlimit wechselt er
        mit dem gespeicherten Text in den Solo-Modus. Strg+S vorher schickt nicht den leeren Text (#1018).
        """
        page.set_viewport_size(DESKTOP)
        page.add_init_script(WEBSOCKET_OHNE_VERBINDUNG)
        login_via_form(page, live_server.url, anna.user.email, PASSWORD)
        posts: list[str] = []
        page.on(
            "request",
            lambda r: posts.append(r.post_data or "")
            if r.method == "POST" and r.url.endswith(f"/documents/{dokument.id}/")
            else None,
        )
        page.goto(f"{live_server.url}/work/{org.slug}/documents/{dokument.id}/")
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(
            f"() => {ALPINE_EDITOR}.collabEnabled === true && {ALPINE_EDITOR}.collabStatus === 'connecting'",
            timeout=15000,
        )
        page.locator(PROSEMIRROR).click()
        page.keyboard.type("Zu früh")
        page.keyboard.press("Control+s")
        page.wait_for_timeout(1000)
        assert not any('name="content"' in post for post in posts), "der leere Text ging an den Server"
        assert decrypted_content(dokument) == "<p>Alt</p>"

        page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=20000)
        expect(page.locator(PROSEMIRROR)).to_contain_text("Alt", timeout=15000)
        expect(page.locator(PROSEMIRROR)).not_to_contain_text("Zu früh")
        page.wait_for_function(EDITOR_BEARBEITBAR, timeout=15000)
        tippen(page, " Solo.")
        page.keyboard.press("Control+s")
        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        assert "Alt Solo." in decrypted_content(dokument)

    def test_automatisches_speichern_laeuft_im_solo_modus_weiter(
        self, page: Any, live_server: Any, org: Any, anna: Any, dokument: Motion
    ) -> None:
        """
        Der Wechsel in den Solo-Modus baut den Editor ab und neu auf. Dabei endete das automatische Speichern und
        startete nicht wieder: Eingaben sicherte nur noch Strg+S, ein Absturz des Browsers verlor sie (#1199).
        """
        page.set_viewport_size(DESKTOP)
        page.add_init_script(AUTOMATISCH_SPEICHERN_RASCH)
        login_via_form(page, live_server.url, anna.user.email, PASSWORD)
        solo_oeffnen(page, f"{live_server.url}/work/{org.slug}/documents/{dokument.id}/")
        assert page.evaluate(f"() => {ALPINE_EDITOR}._collabFailed") is True, "Vorbedingung: Wechsel in den Solo-Modus"

        tippen(page, " Ohne Strg+S.")
        page.wait_for_function(GESICHERT_OHNE_STOERUNG, timeout=15000)
        assert "Alt Ohne Strg+S." in decrypted_content(dokument)
