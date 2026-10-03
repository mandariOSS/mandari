# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight- und Session-Portal, Fehlerbericht: ausgelagerte Inline-Skripte im Browser (#172, Teil 2).

Insight-Seiten nutzen Alpine-Komponenten aus frontend/alpine/insight.ts (in main.ts registriert),
Session-Formulare und der Fehlerbericht deklarative Helfer aus frontend/js/form-behaviors.ts.
Regressionstest: „Abo verwalten“ schickte die gespeicherten Koordinaten lokalisiert („51,96…“)
zurück, das Speichern endete mit einem Serverfehler.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.common.tests.factories import DEFAULT_PASSWORD, UserFactory
from apps.session.models import (
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionTextBlock,
    SessionUser,
)
from insight_core.models import (
    InsightSubscriber,
    OParlBody,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from tests_e2e.conftest import BrowserProblems, login_via_form, wait_for_component

RIS = "https://ris.e2e.example/oparl"


def _kommune(nummer: int, name: str) -> OParlBody:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-RIS"})
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/{nummer}", source=source, name=name, slug=f"e2e-{nummer}", is_listed=True
    )


def _kommune_waehlen(page: Any, goto: Any, body: OParlBody) -> None:
    goto(f"/insight/kommune/{body.id}/")


class TestInsight:
    def test_kommunenauswahl_schlaegt_vor(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        """Kommunenwechsel (#783): Vorschläge vom Server, Pfeiltasten in die Liste, keine Liste aller Kommunen."""
        _kommune(1, "Nordstadt")
        _kommune(2, "Südheim")
        goto("/insight/")
        wait_for_component(page, "kommunenWahl")
        expect(page.get_by_text("Südheim")).to_have_count(0)

        page.fill("#auswahl-eingabe", "nord")
        treffer = page.locator("#auswahl-ergebnisse a[data-kommune-ziel]", has_text="Nordstadt")
        expect(treffer).to_be_visible()
        expect(page.locator("#auswahl-ergebnisse", has_text="Südheim")).to_have_count(0)
        page.keyboard.press("ArrowDown")
        expect(treffer).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#auswahl-eingabe")).to_be_focused()
        page.fill("#auswahl-eingabe", "gibtsnicht")
        expect(page.get_by_text("Keine Kommune gefunden.", exact=True)).to_be_visible()
        problems.assert_clean("Kommunenauswahl")

    def test_kommune_wechseln_als_dialog(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        """Dialog: Fokus im Suchfeld, Escape schließt, der Fokus kehrt auf den Auslöser zurück (#783)."""
        body = _kommune(4, "Weststadt")
        _kommune(5, "Oststadt")
        _kommune_waehlen(page, goto, body)
        goto("/insight/vorgaenge/")
        ausloeser = page.locator("[data-kommune-wechseln]:visible").first
        ausloeser.click()
        dialog = page.get_by_role("dialog", name="Kommune wechseln")
        expect(dialog).to_be_visible()
        expect(page.locator("#kommune-dialog-eingabe")).to_be_focused()
        expect(dialog.get_by_text("Zuletzt besucht")).to_be_visible()
        page.keyboard.type("osts")
        expect(dialog.locator("a[data-kommune-ziel]", has_text="Oststadt")).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(ausloeser).to_be_focused()
        problems.assert_clean("Kommune wechseln")

    def test_merkliste_laedt_gemerkte_vorgaenge(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        body = _kommune(3, "Merkstadt")
        paper = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Radweg Hauptstraße")
        _kommune_waehlen(page, goto, body)
        merkliste = {"person": [], "paper": [str(paper.id)], "meeting": [], "organization": []}
        page.evaluate("(daten) => localStorage.setItem('mandari_bookmarks', daten)", json.dumps(merkliste))

        goto("/insight/gespeichert/")
        wait_for_component(page, "merklisteController")
        expect(page.get_by_text("Radweg Hauptstraße")).to_be_visible()
        problems.assert_clean("Merkliste")

    def test_buergerfrage_zaehlt_zeichen(self, page: Any, goto: Any, settings: Any, problems: BrowserProblems) -> None:
        settings.INSIGHT_QUESTIONS_ENABLED = True  # Ratsfragen sind standardmäßig pausiert (#734)
        body = _kommune(4, "Fragestadt")
        person = OParlPerson.objects.create(
            external_id=f"{RIS}/person/1", body=body, name="Anna Rat", family_name="Rat"
        )
        fraktion = OParlOrganization.objects.create(
            external_id=f"{RIS}/organization/1", body=body, name="Fraktion Mitte", organization_type="Fraktion"
        )
        OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=fraktion)
        _kommune_waehlen(page, goto, body)

        goto(f"/insight/personen/{person.id}/frage-stellen/")
        wait_for_component(page, "questionForm")
        expect(page.get_by_text("0 / 2000 Zeichen")).to_be_visible()
        page.fill("textarea[data-question-text]", "Wann kommt der Radweg?")
        expect(page.get_by_text("22 / 2000 Zeichen")).to_be_visible()
        expect(page.get_by_text("(mind. 50)")).to_be_visible()
        problems.assert_clean("Bürgerfrage")

    def test_abo_verwalten_speichert_koordinaten(
        self, page: Any, goto: Any, settings: Any, problems: BrowserProblems
    ) -> None:
        settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
        body = _kommune(5, "Abostadt")
        abo = InsightSubscriber.objects.create(
            email="leser@example.org",
            body=body,
            confirmed=True,
            neighborhood_active=True,
            neighborhood_name="Prinzipalmarkt",
            neighborhood_lat=Decimal("51.9606649"),
            neighborhood_lon=Decimal("7.6261347"),
            neighborhood_radius=1000,
        )

        goto(f"/insight/abo/verwalten/{abo.token}/")
        wait_for_component(page, "neighborhoodSubscription")
        expect(page.locator('input[name="neighborhood_lat"]')).to_have_value("51.9606649")
        expect(page.get_by_text("Standort gewählt")).to_be_visible()
        page.fill('input[name="keyword"]', "Radweg")
        page.get_by_role("button", name="Speichern").click()
        page.wait_for_load_state("networkidle")
        problems.assert_clean("Abo verwalten")

        abo.refresh_from_db()
        assert abo.keyword == "Radweg"
        assert abo.neighborhood_lat == Decimal("51.9606649")
        assert abo.neighborhood_radius == 1000

    def test_abo_anlegen_mit_vorbelegtem_ort(
        self, page: Any, goto: Any, settings: Any, problems: BrowserProblems
    ) -> None:
        settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
        body = _kommune(6, "Ortstadt")
        _kommune_waehlen(page, goto, body)
        goto("/insight/benachrichtigungen/?lat=51.96&lon=7.62&name=Domplatz")
        wait_for_component(page, "neighborhoodSubscription")
        expect(page.locator('input[name="neighborhood_name"]')).to_have_value("Domplatz")
        expect(page.get_by_text("Standort gewählt")).to_be_visible()
        page.get_by_role("button", name="Zurücksetzen").click()
        expect(page.locator('input[name="neighborhood_lat"]')).to_have_value("")
        problems.assert_clean("Abo anlegen")


class TestFehlerbericht:
    def test_technische_angaben_werden_gefuellt(self, page: Any, goto: Any, problems: BrowserProblems) -> None:
        goto("/feedback/")
        angaben = page.locator("#id_browser_info")
        page.wait_for_function("() => document.getElementById('id_browser_info').value.includes('Browser: ')")
        assert "Zeitzone: " in angaben.input_value()
        page.get_by_text("Technische Angaben (werden mitgesendet)").click()
        expect(page.locator("[data-browser-info-preview]")).to_contain_text("Cookies aktiv:")
        problems.assert_clean("Fehlerbericht")


class TestSession:
    @pytest.fixture
    def sachbearbeitung(self) -> SessionUser:
        tenant = SessionTenant.objects.create(name="E2E-Kreis", slug="e2e-kreis")
        rolle = SessionRole.objects.create(
            tenant=tenant, name="Sachbearbeitung", can_view_papers=True, can_create_papers=True, can_edit_papers=True
        )
        user = cast(Any, UserFactory)(email="sachbearbeitung@example.org")
        session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
        session_user.roles.add(rolle)
        return session_user

    def test_textbaustein_einfuegen(
        self, page: Any, live_server: Any, sachbearbeitung: SessionUser, problems: BrowserProblems
    ) -> None:
        tenant = sachbearbeitung.tenant
        SessionTextBlock.objects.create(
            tenant=tenant, title="Beschluss", content="Beschlossen am {datum}.", category="resolution"
        )
        login_via_form(page, live_server.url, sachbearbeitung.user.email, DEFAULT_PASSWORD)
        page.goto(f"{live_server.url}/session/{tenant.slug}/papers/create/")

        page.locator("#id_resolution_text").fill("Der Rat")
        page.locator("#id_resolution_text").focus()
        page.select_option("[data-textblock-picker] .tb-select", label="Beschluss (Beschlusstext)")
        page.click("[data-textblock-picker] .tb-insert")
        heute = page.evaluate("() => new Date().toLocaleDateString('de-DE')")
        expect(page.locator("#id_resolution_text")).to_have_value(f"Der Rat Beschlossen am {heute}.")
        problems.assert_clean("Textbaustein")

    def test_zielsitzung_nach_gremium_gefiltert(
        self, page: Any, live_server: Any, sachbearbeitung: SessionUser, problems: BrowserProblems
    ) -> None:
        tenant = sachbearbeitung.tenant
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        ausschuss = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss")
        start = timezone.now() + timedelta(days=7)
        ratssitzung = SessionMeeting.objects.create(
            tenant=tenant, name="Ratssitzung", organization=rat, start=start, is_public=True
        )
        ausschusssitzung = SessionMeeting.objects.create(
            tenant=tenant, name="Bauausschusssitzung", organization=ausschuss, start=start, is_public=True
        )
        paper = SessionPaper.objects.create(tenant=tenant, reference="V/2026/9", name="Neubau", is_public=True)
        login_via_form(page, live_server.url, sachbearbeitung.user.email, DEFAULT_PASSWORD)
        page.goto(f"{live_server.url}/session/{tenant.slug}/papers/{paper.id}/")

        page.select_option("#consultation-add-org", str(rat.id))
        versteckt = page.evaluate(
            "() => Object.fromEntries(Array.from(document.querySelectorAll('#consultation-add-meeting option'))"
            ".filter((o) => o.value).map((o) => [o.value, o.hidden]))"
        )
        assert versteckt == {str(ratssitzung.id): False, str(ausschusssitzung.id): True}
        problems.assert_clean("Beratungsfolge")
