# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Echte Seiten im neuen Editor (Satz-Kette, Issue #1584).

- Die Seitenansicht setzt den Text auf A4-Seiten mit Briefpapier, Kopf, Fußzeile und Seitenzahl.
- Editor und Druckansicht (dieselbe Kette, aus der das PDF entsteht) brechen an denselben Stellen um: Vor jeder Lücke
  der Kette steht in beiden derselbe Text.
- Schriftart und -größe der Werkzeugleiste wirken auf die Auswahl; nur Schriften des Satzes.
- Bei schmaler Breite stehen Werkzeuge im Menü „Mehr“, nichts ragt seitlich heraus.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.motions.models import Motion, OrganizationLetterhead
from tests_e2e.conftest import wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

DESKTOP = {"width": 1440, "height": 900}
HANDY = {"width": 390, "height": 844}
#: Bildschirmfotos zum Ansehen (nur lokal, z. B. MANDARI_E2E_FOTOS=C:/tmp/fotos)
FOTOS = os.environ.get("MANDARI_E2E_FOTOS", "")

ABSATZ = (
    "Der Rat der Stadt möge beschließen, dass die Verwaltung bis zum Ende des Jahres ein Konzept für die "
    "barrierefreie Gestaltung aller Haltestellen im Stadtgebiet vorlegt. Dabei sind die Belange von Menschen mit "
    "Seh- und Gehbehinderungen besonders zu berücksichtigen."
)


def _inhalt() -> str:
    teile = ["<h1>Barrierefreie Haltestellen</h1>", "<h2>Beschlussvorschlag</h2>"]
    teile += [f"<p>{ABSATZ}</p>" for _ in range(7)]
    teile.append("<ul><li><p>Erster Punkt der Liste</p></li><li><p>Zweiter Punkt der Liste</p></li></ul>")
    teile.append("<h2>Begründung</h2>")
    teile += [f'<p style="text-align: justify">{ABSATZ} {ABSATZ}</p>' for _ in range(8)]
    zeilen = "".join(
        f"<tr><td><p>Haltestelle {i}</p></td><td><p>{'ja' if i % 2 else 'nein'}</p></td></tr>" for i in range(20)
    )
    teile.append(f"<table><tbody><tr><th><p>Ort</p></th><th><p>Barrierefrei</p></th></tr>{zeilen}</tbody></table>")
    teile.append("<p>Mit freundlichen Grüßen</p>")
    return "".join(teile)


@pytest.fixture(autouse=True)
def neues_design(db: Any, org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="autorin-seiten@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


@pytest.fixture
def antrag(org: Any, autorin: Any) -> Motion:
    briefkopf = OrganizationLetterhead.objects.create(
        organization=org,
        name="Fraktion",
        kind="generated",
        address_block="Fraktion Muster\nRathausplatz 1\n12345 Musterstadt",
        sender_line="Fraktion Muster · Rathausplatz 1 · 12345 Musterstadt",
        footer_text="Fraktion Muster · fraktion@example.org",
        content_margin_top=55,
        font_family="Arial",
        font_size=11,
    )
    motion = Motion.objects.create(
        organization=org,
        author=autorin,
        title="Barrierefreie Haltestellen",
        status="draft",
        visibility="organization",
        letterhead=briefkopf,
    )
    cast(Any, motion).set_content_encrypted(_inhalt())
    motion.save()
    return motion


def _foto(page: Any, name: str) -> None:
    if FOTOS:
        Path(FOTOS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(FOTOS) / f"{name}.png"))


#: Text, der hinter jeder Lücke der Kette beginnt (erste 40 Zeichen), in Reihenfolge
TEXT_NACH_LUECKEN = """() => {
  const luecken = Array.from(document.querySelectorAll('.satz-luecke, .satz-luecke-zeile, .satz-luecke-tabelle'))
  return luecken.map((l) => {
    const r = document.createRange()
    r.setStartAfter(l)
    const ende = l.closest('.tiptap') || document.body
    r.setEndAfter(ende.lastChild)
    return r.toString().replace(/\\s+/g, ' ').trim().slice(0, 40)
  })
}"""


def _satz_fertig(page: Any) -> None:
    page.wait_for_function(
        f"() => {ALPINE_EDITOR}.pageCount > 1 && document.querySelectorAll('.satz-bogen').length > 1", timeout=20000
    )


class TestSeitenansicht:
    def test_seiten_wie_im_druck(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        url = live_server.url + reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": antrag.id})
        page.goto(url)
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        _satz_fertig(page)

        seiten = page.evaluate(f"() => {ALPINE_EDITOR}.pageCount")
        assert seiten >= 3
        assert page.locator(".satz-bogen").count() == seiten
        expect(page.locator(".satz-bogen").first.locator(".satz-kopf")).to_contain_text("Rathausplatz 1")
        expect(page.locator(".satz-bogen").nth(1).locator(".satz-seitenzahl")).to_have_text(f"Seite 2 von {seiten}")
        assert "dokument-satz" in (page.locator(PROSEMIRROR).get_attribute("class") or "")
        editor_umbrueche = page.evaluate(TEXT_NACH_LUECKEN)
        assert editor_umbrueche, "keine Lücken gesetzt"
        _foto(page, "editor-seiten-1440")

        # Druckansicht: dieselbe Kette ohne Lücke zwischen den Seiten, aus der auch das PDF entsteht
        druck = page.context.new_page()
        druck.set_viewport_size(DESKTOP)
        druck.goto(
            live_server.url
            + reverse("work:document_export", kwargs={"org_slug": org.slug, "motion_id": antrag.id})
            + "?format=druck"
        )
        druck.wait_for_function("() => window.satzFertig === true", timeout=20000)
        assert druck.evaluate("() => window.satzSeiten") == seiten
        assert druck.evaluate(TEXT_NACH_LUECKEN) == editor_umbrueche
        _foto(druck, "druck-seiten-1440")

    def test_schrift_und_groesse_der_auswahl(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        page.set_viewport_size(DESKTOP)
        login(autorin.user.email, PASSWORD)
        page.goto(
            live_server.url + reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": antrag.id})
        )
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        _satz_fertig(page)
        # Ein Wort im ersten Absatz markieren
        page.locator(f"{PROSEMIRROR} p").first.dblclick()
        knopf = page.locator("[data-menue-knopf='schrift']").first
        expect(knopf).to_contain_text("Arimo")
        knopf.click()
        page.locator("[data-menue='schrift'] button", has_text="Tinos").click()
        expect(page.locator(f"{PROSEMIRROR} p").first.locator("span[style*='Tinos']")).to_have_count(1)
        expect(knopf).to_contain_text("Tinos")
        page.locator("[data-menue-knopf='groesse']").first.click()
        page.locator("[data-menue='groesse'] button", has_text="14").first.click()
        expect(page.locator(f"{PROSEMIRROR} p").first.locator("span[style*='14pt']")).to_have_count(1)
        _foto(page, "editor-schrift-1440")

    def test_handy_werkzeuge_im_menue_mehr(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        page.set_viewport_size(HANDY)
        login(autorin.user.email, PASSWORD)
        page.goto(
            live_server.url + reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": antrag.id})
        )
        wait_for_bundle(page)
        page.wait_for_selector(PROSEMIRROR, timeout=15000)
        page.wait_for_function(f"() => {ALPINE_EDITOR}.wzVerborgen.length > 0", timeout=10000)
        breite = page.evaluate("() => document.documentElement.scrollWidth")
        assert breite <= HANDY["width"], f"seitlicher Überlauf: {breite}px"
        page.locator("[data-menue-knopf='mehr']").click()
        expect(page.locator("[data-menue='mehr']")).to_be_visible()
        _foto(page, "editor-handy-390")
