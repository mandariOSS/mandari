# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorschläge der Reihe nach prüfen und weitere Formate im neuen Editor (Issue #1584).

- „Bearbeiten → Vorschläge der Reihe nach prüfen“ zeigt „Vorschlag x von y“; Annehmen und Ablehnen gehen zum
  nächsten offenen Vorschlag weiter, bis alle entschieden sind.
- „Datei“ bietet PDF, Word, OpenDocument, Markdown und Text zum Herunterladen.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.motions.models import Motion, MotionComment
from tests_e2e.conftest import wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

ERSTE = "a1d0c2b4-1111-4c1e-9d55-2b7c1a0e9f11"
ZWEITE = "b2e1d3c5-2222-4c1e-9d55-2b7c1a0e9f12"
DESKTOP = {"width": 1440, "height": 900}


@pytest.fixture(autouse=True)
def neues_design(db: Any, org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="pruefen@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


@pytest.fixture
def antrag(org: Any, autorin: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Prüfen", status="draft", visibility="organization"
    )
    cast(Any, motion).set_content_encrypted(
        f'<p>Der <span data-comment-id="{ERSTE}">Musterweg</span> ist eng.</p>'
        f'<p>Die <span data-comment-id="{ZWEITE}">Stadt</span> handelt.</p>'
    )
    motion.save()
    for mark, alt, neu in ((ERSTE, "Musterweg", "Schulweg"), (ZWEITE, "Stadt", "Verwaltung")):
        MotionComment.objects.create(
            motion=motion,
            author=autorin,
            content=f"Ersetzen: {alt}",
            selected_text=alt,
            vorschlag=neu,
            mark_id=uuid.UUID(mark),
        )
    return motion


def _oeffnen(page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion) -> None:
    page.set_viewport_size(DESKTOP)
    login(autorin.user.email, PASSWORD)
    page.goto(live_server.url + reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": antrag.id}))
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)


class TestPruefen:
    def test_vorschlaege_der_reihe_nach(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        _oeffnen(page, live_server, login, autorin, org, antrag)
        page.get_by_role("menuitem", name="Bearbeiten").click()
        page.get_by_role("menuitem", name="Vorschläge der Reihe nach prüfen").click()
        leiste = page.get_by_test_id("pruefleiste")
        expect(leiste).to_contain_text("Vorschlag 1 von 2")
        leiste.get_by_role("button", name="Annehmen").click()
        expect(page.locator(f"{PROSEMIRROR} p").first).to_contain_text("Der Schulweg ist eng.")
        expect(leiste).to_contain_text("Vorschlag 1 von 1")
        leiste.get_by_role("button", name="Ablehnen").click()
        expect(leiste).to_be_hidden()
        expect(page.locator(f"{PROSEMIRROR} p").nth(1)).to_contain_text("Die Stadt handelt.")
        entscheidungen = dict(
            MotionComment.objects.filter(motion=antrag).values_list("vorschlag", "vorschlag_angenommen")
        )
        assert entscheidungen == {"Schulweg": True, "Verwaltung": False}

    def test_formate_im_menue_datei(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        _oeffnen(page, live_server, login, autorin, org, antrag)
        page.get_by_role("menuitem", name="Datei").click()
        for name in ("PDF", "Word (.docx)", "OpenDocument (.odt)", "Markdown (.md)", "Text (.txt)"):
            expect(page.get_by_role("menuitem", name=name, exact=True)).to_be_visible()
        expect(page.get_by_role("menuitem", name="Drucken")).to_be_visible()
        with page.expect_download() as laden:
            page.get_by_role("menuitem", name="OpenDocument (.odt)", exact=True).click()
        assert laden.value.suggested_filename == "Prüfen.odt"
