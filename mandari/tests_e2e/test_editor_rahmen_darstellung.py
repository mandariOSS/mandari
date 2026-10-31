# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Darstellungs- und Meldungsfehler im neuen Antragseditor und im Rahmen (Issue #1018) im Browser.

- Dokumenttyp und Briefpapier werden am Handy nicht von der Textfläche abgeschnitten.
- Die Editorhöhe ist die sichtbare Höhe; Kopf-Menüs bleiben auch in niedrigen Fenstern vollständig sichtbar.
- Offene Kopf-Menüs liegen über dem Kommentar-Popup, sonst bleibt das Popup über dem Kopf.
- Meldungen verdecken weder den Modus-Knopf noch die Leiste unten am Handy.
- „Version wiederherstellen“ meldet im eigenen Fenster nicht „an anderer Stelle gespeichert“.
- Die Mandantenliste der bisherigen Session-Leiste rollt, statt unten abgeschnitten zu werden.
"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.session.models import SessionTenant, SessionUser
from apps.work.motions.models import Motion, MotionRevision, MotionType, OrganizationLetterhead
from tests_e2e.conftest import login_via_form, wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR
from tests_e2e.test_editor_kollaboration import editor_oeffnen, editor_url, warte_auf_editor_text

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

HANDY = {"width": 390, "height": 844}
DESKTOP = {"width": 1440, "height": 900}
#: Meldungen über ein Neuladen hinweg merken
MELDUNGEN_MERKEN = """
window.addEventListener('show-toast', (e) => {
    sessionStorage.setItem('meldungen', (sessionStorage.getItem('meldungen') || '') + e.detail.message + '\\n')
})
"""
#: Oberstes Element an einem Punkt in der Mitte der Fläche eines Elements, und ob es im genannten Element liegt
LIEGT_OBEN = """([punkt, auswahl]) => {
    const oben = document.elementFromPoint(punkt.x, punkt.y)
    return !!(oben && oben.closest(auswahl))
}"""


@pytest.fixture(autouse=True)
def neues_design(db: Any, org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="autorin-darstellung@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


@pytest.fixture
def dokument(org: Any, autorin: Any) -> Motion:
    motion = Motion.objects.create(organization=org, author=autorin, title="Antrag Radweg", visibility="organization")
    cast(Any, motion).set_content_encrypted("<p>Aktueller Text</p>")
    motion.save()
    return motion


def _oeffnen(page: Any, live_server: Any, org: Any, autorin: Any, dokument: Motion, groesse: dict[str, int]) -> None:
    page.set_viewport_size(groesse)
    login_via_form(page, live_server.url, autorin.user.email, PASSWORD)
    page.goto(f"{live_server.url}/work/{org.slug}/documents/{dokument.id}/")
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)


def _box(locator: Any) -> dict[str, float]:
    box = locator.bounding_box()
    assert box is not None
    return cast(dict[str, float], box)


def _ueberlappen(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        a["x"] < b["x"] + b["width"]
        and b["x"] < a["x"] + a["width"]
        and a["y"] < b["y"] + b["height"]
        and b["y"] < a["y"] + a["height"]
    )


def test_typ_und_briefpapier_am_handy_nicht_abgeschnitten(
    page: Any, live_server: Any, org: Any, autorin: Any, dokument: Motion
) -> None:
    MotionType.objects.create(organization=org, name="Beschlussvorlage", slug="beschlussvorlage")
    OrganizationLetterhead.objects.create(
        organization=org, name="Fraktion im Rat der Stadt mit langem Namen", kind="generated"
    )
    _oeffnen(page, live_server, org, autorin, dokument, HANDY)
    # So lang, dass der Knopf „Briefpapier“ noch in der ersten Zeile steht, sein Menü aber nicht mehr daneben passt
    page.locator(".ke-blattkopf [data-menue-knopf=typ]").click()
    page.locator(".ke-blattkopf").get_by_role("menuitemradio", name="Beschlussvorlage").click()

    for name in ("typ", "briefpapier"):
        page.locator(f".ke-blattkopf [data-menue-knopf={name}]").click()
        menue = page.locator(".ke-blattkopf .ke-menue:visible")
        expect(menue).to_have_count(1)
        box = _box(menue)
        assert box["x"] >= 0 and box["x"] + box["width"] <= HANDY["width"], f"{name}: {box}"
        ragt_heraus = page.evaluate(
            "() => { const s = document.querySelector('.ke-scroll'); return s.scrollWidth > s.clientWidth }"
        )
        assert not ragt_heraus, f"{name}: Die Textfläche rollt seitlich"
        page.locator(f".ke-blattkopf [data-menue-knopf={name}]").click()


@pytest.mark.parametrize(
    ("breite", "hoehe", "knopf"),
    [(1280, 420, "datei"), (1280, 420, "ablauf"), (1280, 420, "modus"), (390, 500, "stand-mobil"), (390, 500, "modus")],
)
def test_kopfmenues_bleiben_im_sichtbaren_bereich(
    page: Any, live_server: Any, org: Any, autorin: Any, dokument: Motion, breite: int, hoehe: int, knopf: str
) -> None:
    _oeffnen(page, live_server, org, autorin, dokument, {"width": breite, "height": hoehe})
    assert page.evaluate("() => document.querySelector('.work-main').getBoundingClientRect().height") == hoehe
    # Am Desktop sind 100vh und 100dvh gleich, der Unterschied zeigt sich nur am Handy: darum die Regel selbst prüfen
    regel = page.evaluate(
        """() => [...document.styleSheets]
            .flatMap((blatt) => { try { return [...blatt.cssRules] } catch { return [] } })
            .find((r) => r.selectorText === '.work-main' && r.style.height)?.style.height"""
    )
    assert regel == "100dvh"

    page.locator(f"[data-menue-knopf='{knopf}']").click()
    box = _box(page.locator(".ke-kopf .ke-menue:visible"))
    unten = page.locator("nav[aria-label='Hauptbereiche']:visible")
    grenze = _box(unten)["y"] if unten.count() else hoehe
    assert box["y"] + box["height"] <= grenze, f"{knopf}: {box}, Grenze {grenze}"


def test_offene_kopfmenues_liegen_ueber_dem_kommentar_popup(
    page: Any, live_server: Any, org: Any, autorin: Any, dokument: Motion
) -> None:
    _oeffnen(page, live_server, org, autorin, dokument, DESKTOP)
    page.locator(".ke-kopf [data-menue-knopf=datei]").click()
    menue = _box(page.locator(".ke-kopf .ke-menue:visible"))
    punkt = {"x": menue["x"] + 30, "y": menue["y"] + 20}
    # Das Popup genau auf das Menü legen (wie bei einer Markierung in den ersten Zeilen unter dem Kopf)
    page.evaluate(
        f"""([x, y]) => {{
            const d = {ALPINE_EDITOR}
            d.inlineSelectedText = 'Text'
            d.commentPopupTop = y - 10
            d.commentPopupLeft = x - 10
            d.commentPopupVisible = true
        }}""",
        [punkt["x"], punkt["y"]],
    )
    expect(page.locator(".comment-popup:visible")).to_have_count(1)
    assert page.evaluate(LIEGT_OBEN, [punkt, ".ke-menue"]), "Das Popup liegt über dem offenen Menü"

    # Ohne offenes Menü bleibt ein Popup, das in den Kopf ragt, vollständig sichtbar
    page.keyboard.press("Escape")
    expect(page.locator(".ke-kopf .ke-menue:visible")).to_have_count(0)
    page.evaluate(
        f"""() => {{
            const d = {ALPINE_EDITOR}
            d.commentPopupVisible = true
            d.commentPopupTop = 10
            d.commentPopupLeft = 200
        }}"""
    )
    box = _box(page.locator(".comment-popup:visible"))
    mitte = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
    assert page.evaluate(LIEGT_OBEN, [mitte, ".comment-popup"]), "Das Popup verschwindet hinter dem Kopf"


@pytest.mark.parametrize("groesse", [DESKTOP, HANDY], ids=["desktop", "handy"])
def test_meldungen_verdecken_modus_knopf_und_leiste_nicht(
    page: Any, live_server: Any, org: Any, autorin: Any, dokument: Motion, groesse: dict[str, int]
) -> None:
    _oeffnen(page, live_server, org, autorin, dokument, groesse)
    page.evaluate(
        "() => { window.showToast('Das Dokument wurde gespeichert, alle Änderungen sind übernommen.', 'info') }"
    )
    # Auch die Meldung des Solo-Modus ist dann noch zu sehen: Beide dürfen nichts verdecken
    meldungen = page.locator("[x-data='toastManager'] [x-show='toast.visible']")
    expect(meldungen.last).to_contain_text("gespeichert")
    page.wait_for_timeout(400)  # Einblenden abwarten

    modus = _box(page.locator("[data-menue-knopf=modus]"))
    leiste = page.locator("nav[aria-label='Hauptbereiche']:visible")
    for meldung in meldungen.all():
        box = _box(meldung)
        assert not _ueberlappen(box, modus), f"Meldung {box} verdeckt den Modus-Knopf {modus}"
        assert box["y"] + box["height"] <= groesse["height"]
        if leiste.count():
            assert box["y"] + box["height"] <= _box(leiste)["y"], "Die Meldung liegt auf der Leiste unten"


def test_wiederherstellen_ohne_meldung_an_anderer_stelle_gespeichert(
    page: Any, asgi_server: Any, org: Any, autorin: Any, dokument: Motion
) -> None:
    """Die Antwort kommt absichtlich nach der Aufforderung zum Neuladen über die Verbindung."""
    alt = MotionRevision(motion=dokument, version=1, changed_by=autorin, change_summary="Erste Fassung")
    cast(Any, alt).set_content_encrypted("<p>Erste Fassung</p>")
    alt.save()
    page.set_viewport_size(DESKTOP)
    page.add_init_script(MELDUNGEN_MERKEN)
    login_via_form(page, asgi_server.url, autorin.user.email, PASSWORD)
    editor_oeffnen(page, editor_url(asgi_server, org, dokument), "Aktueller Text")

    def verzoegern(route: Any) -> None:
        antwort = route.fetch()
        time.sleep(0.5)
        route.fulfill(response=antwort)

    page.route("**/restore/", verzoegern)
    page.evaluate(
        f"""(id) => {{ {ALPINE_EDITOR}.restoreRevision({{ id, version: 1 }}) }}""",
        str(alt.id),
    )
    page.get_by_role("button", name="Wiederherstellen").click()
    warte_auf_editor_text(page, "Erste Fassung")

    meldungen = page.evaluate("() => sessionStorage.getItem('meldungen') || ''")
    assert "anderer Stelle" not in meldungen, meldungen


def test_mandantenliste_der_bisherigen_session_leiste_rollt(
    page: Any, live_server: Any, session_user: tuple[Any, str]
) -> None:
    mitglied, passwort = session_user
    for nummer in range(40):
        raum = SessionTenant.objects.create(name=f"Kommune {nummer:02d}", slug=f"kommune-{nummer:02d}")
        SessionUser.objects.create(user=mitglied.user, tenant=raum, is_active=True)
    page.set_viewport_size({"width": 1280, "height": 700})
    login_via_form(page, live_server.url, mitglied.user.email, passwort)
    page.goto(live_server.url + reverse("session:dashboard", kwargs={"tenant_slug": mitglied.tenant.slug}))
    wait_for_bundle(page)

    wechsel = page.get_by_test_id("mandant-wechseln")
    wechsel.locator("summary").click()
    liste = wechsel.locator("ul")
    box = _box(liste)
    assert box["y"] + box["height"] <= 700, f"Die Liste endet unter dem Fenster: {box}"
    assert liste.evaluate("(el) => el.scrollHeight > el.clientHeight"), "Die Liste rollt nicht"
    letzter = wechsel.get_by_role("link", name="Kommune 39")
    letzter.scroll_into_view_if_needed()
    expect(letzter).to_be_in_viewport()
    # Die Navigation darunter bleibt erreichbar
    expect(page.locator(".session-sidebar nav")).to_be_visible()
