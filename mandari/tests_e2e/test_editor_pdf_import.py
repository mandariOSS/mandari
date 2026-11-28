# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Treu übernommenes PDF im Editor (Issue #1600).

- Der Editor behält die Satzmaße des Originals (Abstand, Zeilenabstand, Einzug, Tabulator) und setzt ohne
  Unterschneidung wie Word.
- Hinter jeder Seite liegt ihr Hintergrund aus dem Original (Logo, Kopf), die Seitenzahl steht an ihrer Stelle.
- Nach einer Änderung und dem Sichern sind die Maße im gespeicherten Text noch da (Weg über TipTap und Yjs).
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.work.motions.import_service import MotionImportService
from apps.work.motions.models import Motion
from apps.work.motions.tests.test_pdf_treu import _antrag
from tests_e2e.conftest import login_via_form, wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

DESKTOP = {"width": 1440, "height": 900}
# pdf.js lädt seinen Worker als Modul (.mjs); unter Windows meldet die Registry dafür mitunter text/plain
mimetypes.add_type("text/javascript", ".mjs")
FOTOS = os.environ.get("MANDARI_E2E_FOTOS", "")


@pytest.fixture(autouse=True)
def neues_design(db: Any, org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="autorin-import@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


@pytest.fixture
def importiert(org: Any, autorin: Any) -> Motion:
    datei = SimpleUploadedFile("antrag.pdf", _antrag(), content_type="application/pdf")
    ergebnis = MotionImportService.import_pdf(datei, org, autorin)
    assert ergebnis.success and ergebnis.motion is not None
    return Motion.objects.get(pk=ergebnis.motion.pk)


def test_importiertes_pdf_im_editor(page: Any, asgi_server: Any, autorin: Any, org: Any, importiert: Motion) -> None:
    # ASGI-Testserver mit Kanal-Ebene: Der Editor sichert über die Live-Zusammenarbeit (Yjs) wie in Produktion
    page.set_viewport_size(DESKTOP)
    login_via_form(page, asgi_server.url, autorin.user.email, PASSWORD)
    page.goto(
        asgi_server.url + reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": importiert.id})
    )
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(
        f"() => {ALPINE_EDITOR}.pageCount >= 2 && document.querySelectorAll('.satz-bogen').length >= 2", timeout=20000
    )

    text = page.locator(PROSEMIRROR)
    klassen = text.get_attribute("class") or ""
    assert "dokument-satz" in klassen and "satz-treu" in klassen
    # Satzmaße und Tabulator überstehen das Laden in TipTap
    liste = text.locator("p[style*='tab-size']").first
    stil = liste.get_attribute("style") or ""
    assert "padding-left" in stil and "text-indent" in stil and "line-height" in stil
    assert "\t" in (liste.text_content() or "")
    assert text.locator("p[style*='margin-top']").count() > 3
    # Hintergrund je Seite (pdf.js) und Seitenzahl an der Stelle des Originals
    page.wait_for_selector(".satz-bogen img.satz-briefpapier", timeout=20000)
    expect(page.locator(".satz-bogen").nth(1).locator(".satz-seitenzahl-treu text")).to_have_text("- 2 -")
    if FOTOS:
        Path(FOTOS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(FOTOS) / "editor-pdf-import.png"))

    # Ändern und sichern: Die Maße bleiben im gespeicherten Text
    text.locator("p").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" geändert")
    page.wait_for_function(f"() => {ALPINE_EDITOR}.hasUnsavedChanges() === false", timeout=20000)
    importiert.refresh_from_db()
    gespeichert = importiert.content or ""
    assert "geändert" in gespeichert
    assert "tab-size" in gespeichert and "margin-top" in gespeichert and "line-height" in gespeichert
