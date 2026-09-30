# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anhänge im Dokument-Editor (Issue #584): hinzufügen, umbenennen, entfernen im Browser.

Die Seitenleiste „Details“ zeigte Anhänge früher nur zum Herunterladen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from apps.work.motions.models import Motion, MotionDocument
from tests_e2e.conftest import wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

PASSWORD = "E2e-Passwort-123456"
PERMISSIONS = ["dashboard.view", "motions.view", "motions.view_drafts", "motions.edit"]


def test_anhang_hinzufuegen_umbenennen_entfernen(
    page: Any, live_server: Any, login: Any, org: Any, make_member: Any, tmp_path: Path, settings: Any
) -> None:
    settings.MEDIA_ROOT = tmp_path / "media"
    autorin = make_member(org, PERMISSIONS, email="anhaenge@example.org")
    autorin.user.set_password(PASSWORD)
    autorin.user.save(update_fields=["password"])
    motion = Motion.objects.create(organization=org, author=autorin, title="Radweg", visibility="organization")
    cast(Any, motion).set_content_encrypted("<p>Antragstext</p>")
    motion.save()

    login(autorin.user.email, PASSWORD)
    page.goto(f"{live_server.url}/work/{org.slug}/documents/{motion.id}/")
    wait_for_bundle(page)
    page.get_by_role("button", name="Details").click()
    bereich = page.locator("#motion-attachments")
    expect(bereich).to_contain_text("Keine Anhänge.")

    datei = tmp_path / "Lageplan Hauptstraße.pdf"
    datei.write_bytes(b"%PDF-1.4 Lageplan")
    bereich.locator("input[type=file]").set_input_files(str(datei))
    expect(bereich).to_contain_text("Anhang hinzugefügt.")
    expect(bereich.get_by_role("link", name="Lageplan Hauptstraße.pdf")).to_be_visible()

    page.locator("#motion-attachments summary", has_text="Umbenennen").click()
    feld = page.locator("#motion-attachments input[name=filename]")
    feld.fill("Lageplan Nord")
    page.locator("#motion-attachments form button", has_text="Speichern").click()
    expect(page.locator("#motion-attachments")).to_contain_text("Lageplan Nord.pdf")
    assert MotionDocument.objects.get(motion=motion).filename == "Lageplan Nord.pdf"

    page.get_by_role("button", name="Anhang Lageplan Nord.pdf entfernen").click()
    page.get_by_role("button", name="Bestätigen").click()  # eigener Bestätigungsdialog statt confirm()
    expect(page.locator("#motion-attachments")).to_contain_text("Keine Anhänge.")
    assert not MotionDocument.objects.filter(motion=motion).exists()
