# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokument-Import im Browser (Issue #620).

Nicht unterstützte Dateien (etwa .odt) fielen früher stumm weg – der Knopf blieb gesperrt, ohne
Hinweis. Ein Word-Antrag landet mit nummerierter Liste und Unterschriften-Tabelle im Editor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from apps.work.motions.models import Motion
from apps.work.tests.test_import_text import _antrag_docx
from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

PASSWORD = "E2e-Passwort-123456"
PERMISSIONS = ["dashboard.view", "motions.view", "motions.view_drafts", "motions.create", "motions.edit"]


@pytest.fixture
def importer(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="import@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


def test_nicht_unterstuetzte_datei_wird_mit_grund_gezeigt_word_antrag_landet_im_editor(
    page: Any, live_server: Any, login: Any, importer: Any, org: Any, tmp_path: Path
) -> None:
    login(importer.user.email, PASSWORD)
    page.goto(f"{live_server.url}/work/{org.slug}/documents/import/")
    wait_for_bundle(page)
    knopf = page.locator("form button[type=submit]")

    odt = tmp_path / "antrag.odt"
    odt.write_bytes(b"PK\x03\x04odt")
    page.set_input_files("#import_files", str(odt))
    hinweis = page.get_by_role("alert").filter(has_text="Nicht übernommen")
    expect(hinweis).to_be_visible()
    expect(hinweis).to_contain_text("antrag.odt")
    expect(hinweis).to_contain_text("als DOCX oder PDF speichern")
    expect(knopf).to_be_disabled()

    antrag = tmp_path / "Ratsantrag_Digitalisierung.docx"
    antrag.write_bytes(_antrag_docx())
    page.set_input_files("#import_files", str(antrag))
    expect(hinweis).to_be_hidden()
    expect(knopf).to_be_enabled()
    knopf.click()

    page.wait_for_url(re.compile(r"/documents/[0-9a-f-]{36}/$"), timeout=15000)
    wait_for_bundle(page)
    editor = page.locator("#editor-container .ProseMirror")
    expect(editor.locator("ol > li")).to_have_count(5)  # drei Punkte, zwei Unterpunkte
    expect(editor.locator("table")).to_contain_text("Albert Wenzel")
    motion = Motion.objects.get(organization=org)
    assert motion.title == "Ratsantrag Digitalisierung"
    assert motion.documents.count() == 1
