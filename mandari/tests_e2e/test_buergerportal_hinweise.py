# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweise im Bürgerportal im Browser (Issue #734).

- Hinweis je Kommune (``OParlBody.portal_notice``): sichtbar auf Einstieg und Listen der Kommune,
  nicht bei einer Kommune ohne Hinweis; barrierefrei (axe-core).
- Ratsfragen pausiert (Standard): Hinweis im Portal, keine Einladung zum Fragen.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.utils import timezone

from insight_core.models import (
    OParlBody,
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    OParlSource,
    PublicQuestion,
)
from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

RIS = "https://ris.hinweise.e2e.example/oparl"
HINWEIS = (
    "Die Stadt hat die Bereitstellung ihrer Daten über die OParl-Schnittstelle zum 30.09.2026 beendet. "
    "Angezeigt wird der Stand vom 29.09.2026."
)


def _kommune(nummer: int, name: str, hinweis: str = "") -> OParlBody:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-Hinweis-RIS"})
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/{nummer}",
        source=source,
        name=name,
        slug=f"e2e-hinweis-{nummer}",
        is_listed=True,
        portal_notice=hinweis,
    )


def _assert_axe_clean(page: Any, selektor: str, wo: str) -> None:
    """axe-core nur für den Hinweis selbst; die übrigen Befunde der Seite gehören nicht zu diesem Test."""
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        "async (selektor) => await axe.run(selektor, { resultTypes: ['violations'], runOnly: { type: 'tag',"
        " values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })",
        selektor,
    )
    befunde = AxeResult(violations=json.loads(json.dumps(ergebnis.get("violations", []))))
    assert not befunde.failing, f"axe: kritische/schwere Befunde im {wo}:\n{befunde.describe()}"


class TestKommuneHinweis:
    def test_hinweis_auf_einstieg_und_listen(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        darmstadt = _kommune(1, "Hinweisstadt", HINWEIS)
        goto(f"/insight/kommune/{darmstadt.id}/")

        goto("/insight/")
        hinweis = page.get_by_test_id("kommune-hinweis")
        expect(hinweis).to_be_visible()
        expect(hinweis).to_contain_text("zum 30.09.2026 beendet")
        expect(hinweis).to_have_attribute("role", "status")
        _assert_axe_clean(page, "[data-testid=kommune-hinweis]", "Hinweis der Kommune")

        # Listen, deren Views im E2E-Pfadfilter stehen (weitere Listen: insight_core/tests/test_kommune_hinweis.py)
        for liste in ("/insight/termine/", "/insight/beschluesse/"):
            goto(liste)
            expect(page.get_by_test_id("kommune-hinweis")).to_contain_text("Angezeigt wird der Stand vom 29.09.2026.")
        problems.assert_clean("Hinweis der Kommune")

    def test_ohne_hinweis_nichts(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        andere = _kommune(2, "Ohnehinweisstadt")
        goto(f"/insight/kommune/{andere.id}/")
        goto("/insight/termine/")
        expect(page.get_by_test_id("kommune-hinweis")).to_have_count(0)
        problems.assert_clean("Kommune ohne Hinweis")


class TestRatsfragenPausiert:
    def test_portal_mit_hinweis_ohne_fragen_stellen(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        body = _kommune(3, "Fragepausestadt")
        person = OParlPerson.objects.create(
            external_id=f"{RIS}/person/1", body=body, name="Anna Rat", family_name="Rat"
        )
        fraktion = OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/1", body=body, name="Fraktion Mitte", organization_type="Fraktion"
        )
        OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=fraktion)
        frage = PublicQuestion.objects.create(
            body=body,
            recipient=person,
            questioner_name="Bert",
            questioner_email="bert@example.org",
            subject="Wann wird die Schule saniert?",
            question_text="Die Sanierung ist seit Jahren angekündigt.",
            status="published",
            published_at=timezone.now(),
        )
        goto(f"/insight/kommune/{body.id}/")

        goto("/insight/fragen/")
        expect(page.get_by_test_id("ratsfragen-pausiert")).to_be_visible()
        expect(page.get_by_text("Wann wird die Schule saniert?")).to_be_visible()
        expect(page.get_by_role("link", name="Frage stellen")).to_have_count(0)
        _assert_axe_clean(page, "[data-testid=ratsfragen-pausiert]", "Hinweis zur Pause")

        goto(f"/insight/fragen/{frage.id}/")
        expect(page.get_by_test_id("ratsfragen-pausiert")).to_be_visible()
        expect(page.get_by_text("Ohne Antwort")).to_be_visible()
        problems.assert_clean("Ratsfragen pausiert")
