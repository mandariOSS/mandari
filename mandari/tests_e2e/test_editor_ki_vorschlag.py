# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI im neuen Editor als Änderungsvorschlag (Issue #1584).

- Das KI-Menü an der Markierung („KI“ → „Kürzen“ …) legt das Ergebnis als „Änderungsvorschlag der KI“ an den Rand;
  der Text selbst bleibt, bis ein Mensch annimmt.
- Im KI-Bereich steht an einer Antwort „Als Vorschlag“ (Rand) neben „Direkt übernehmen“ (Vorschau wie bisher).

Die KI selbst antwortet nicht: Playwright fängt die Anfrage an ``documents/ai/`` ab (KiAttrappe aus test_editor_ki).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.urls import reverse

from apps.work.motions.models import Motion, MotionComment
from apps.work.motions.services import MotionAIService
from tests_e2e.conftest import wait_for_bundle
from tests_e2e.test_editor import ALPINE_EDITOR, PASSWORD, PERMISSIONS, PROSEMIRROR
from tests_e2e.test_editor_ki import KiAttrappe

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

ERSTER = "Die Verwaltung wird beauftragt, am Musterweg Tempo 30 anzuordnen."
ZWEITER = "Der Musterweg ist der Schulweg vieler Kinder aus dem ganzen Viertel."
KURZ = "Der Musterweg ist ein wichtiger Schulweg."
DESKTOP = {"width": 1440, "height": 900}


@pytest.fixture(autouse=True)
def neues_design_mit_ki(db: Any, org: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    monkeypatch.setattr(MotionAIService, "is_available", lambda self: True)


@pytest.fixture
def autorin(db: Any, org: Any, make_member: Any) -> Any:
    membership = make_member(org, PERMISSIONS, email="ki-vorschlag@example.org")
    membership.user.set_password(PASSWORD)
    membership.user.save(update_fields=["password"])
    return membership


@pytest.fixture
def antrag(org: Any, autorin: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Tempo 30", status="draft", visibility="organization"
    )
    cast(Any, motion).set_content_encrypted(f"<p>{ERSTER}</p><p>{ZWEITER}</p>")
    motion.save()
    return motion


def _oeffnen(page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion) -> None:
    page.set_viewport_size(DESKTOP)
    login(autorin.user.email, PASSWORD)
    page.goto(live_server.url + reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": antrag.id}))
    wait_for_bundle(page)
    page.wait_for_selector(PROSEMIRROR, timeout=15000)
    page.wait_for_function(f"() => {ALPINE_EDITOR}.collabEnabled === false", timeout=15000)


def _zweiten_absatz_markieren(page: Any) -> None:
    page.locator(f"{PROSEMIRROR} p", has_text=ZWEITER).click()
    page.keyboard.press("Home")
    page.keyboard.press("Shift+End")
    page.wait_for_function(f"(t) => {ALPINE_EDITOR}.aiSelectionText.includes(t)", arg=ZWEITER, timeout=5000)


def _vorschlag_der_ki(antrag: Motion) -> MotionComment:
    return MotionComment.objects.get(motion=antrag, vorschlag__isnull=False)


class TestKiAlsVorschlag:
    def test_ki_menue_an_der_markierung(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        ki = KiAttrappe(page, vorschlag=KURZ)
        _oeffnen(page, live_server, login, autorin, org, antrag)
        _zweiten_absatz_markieren(page)
        popup = page.locator(".comment-popup")
        popup.get_by_role("button", name="KI").click()
        popup.get_by_role("menuitem", name="Kürzen").click()

        karte = page.locator("[data-rand-karte]", has_text=KURZ)
        expect(karte).to_be_visible()
        expect(karte).to_contain_text("Änderungsvorschlag der KI")
        # Nur die Stelle ging an die KI, mit der Anweisung zum Kürzen; der Text selbst bleibt unverändert
        assert ki.anfragen[0]["text"] == [ZWEITER]
        assert "Kürze" in ki.anfragen[0]["instruction"][0]
        expect(page.locator(f"{PROSEMIRROR} p", has_text=ZWEITER)).to_be_visible()
        vorschlag = _vorschlag_der_ki(antrag)
        assert vorschlag.ki_erzeugt is True
        assert vorschlag.vorschlag == KURZ
        assert vorschlag.selected_text == ZWEITER

    def test_antwort_im_ki_bereich_als_vorschlag(
        self, page: Any, live_server: Any, login: Any, autorin: Any, org: Any, antrag: Motion
    ) -> None:
        KiAttrappe(page, vorschlag=KURZ)
        _oeffnen(page, live_server, login, autorin, org, antrag)
        _zweiten_absatz_markieren(page)
        page.get_by_role("button", name="KI-Assistent öffnen").click()
        page.locator(".quick-action-btn", has_text="Verbessern").click()
        nachricht = page.locator(".chat-message.ai", has_text=KURZ)
        expect(nachricht.get_by_role("button", name="Direkt übernehmen")).to_be_visible()
        nachricht.get_by_role("button", name="Als Vorschlag").click()
        expect(page.locator(".chat-message.ai", has_text="wartet auf Ihre Entscheidung")).to_be_visible()
        assert _vorschlag_der_ki(antrag).ki_erzeugt is True
