# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work-Portal: ausgelagerte Inline-Skripte funktionieren im Browser (#172, Teil 2).

Die Seiten hatten ihr JavaScript als `<script>` im Template. Es lebt jetzt als Alpine-Komponente
(`Alpine.data`, frontend/alpine/) bzw. als deklaratives Verhalten (frontend/js/), Startwerte
kommen per `json_script` oder Datenattribut. Geprüft wird je Seite: Komponente initialisiert,
keine JavaScript-Ausnahme, keine Alpine-Fehler, keine Serverfehler – und die typische
Bedienung. Drei Fälle waren vorher defekt und sind hier Regressionstests:

- Artikelvorschläge beim neuen Support-Ticket erschienen nie (Kopplung über Alpine-2-API `__x`).
- Kontaktweg im Profil: Umschalten warf eine Ausnahme (Icons sind nach dem Rendern `<svg>`, das
  Skript suchte `<i>`); die Markierung blieb beim alten Eintrag.
- Rollen-Rechte: „Alle“ einer Kategorie reagierte nicht auf einzelne Häkchen (keine reaktive
  Abhängigkeit auf die Checkboxen).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.common.permissions import get_permissions_by_category
from apps.common.tests.factories import RoleFactory
from apps.work.motions.models import MotionTemplate, MotionType
from apps.work.organization.models import DataExport
from apps.work.support.models import ArticleFeedback, KnowledgeBaseArticle, KnowledgeBaseCategory
from tests_e2e.conftest import ADMIN_PASSWORD, BrowserProblems, component_state, wait_for_component

PRIMARY_500 = "rgb(99, 102, 241)"


def _anmelden(login: Any, admin: Any) -> str:
    login(admin.user.email, ADMIN_PASSWORD)
    return str(admin.organization.slug)


class TestOrganisation:
    def test_einstellungen_farbwaehler_und_logo(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/organization/")
        wait_for_component(page, "logoPreview")
        wait_for_component(page, "colorPicker")

        farbwaehler = page.locator('[x-data="colorPicker"]')
        farbwaehler.locator('input[x-model="hex"]').fill("123abc")
        expect(farbwaehler.locator('input[name="primary_color"]')).to_have_value("#123abc")
        # Farbfeld per :style-Objekt (CSSOM); transition-colors braucht einen Moment
        expect(farbwaehler.locator("div.cursor-pointer")).to_have_css("background-color", "rgb(18, 58, 188)")

        page.set_input_files(
            'input[name="logo"]', files=[{"name": "logo.png", "mimeType": "image/png", "buffer": b"\x89PNG\r\n"}]
        )
        expect(page.locator('img[alt="Logo-Vorschau"]')).to_have_attribute("src", re.compile(r"^blob:"))
        problems.assert_clean("Organisationseinstellungen")

    def test_rolle_alle_haekchen_farbe_und_loeschen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        kategorie, info = next((c, i) for c, i in get_permissions_by_category().items() if len(i["permissions"]) >= 2)
        codes = [p["code"] for p in info["permissions"]]
        rolle = RoleFactory(organization=admin.organization, name="E2E-Rolle", color="#10b981", permissions=codes)
        slug = _anmelden(login, admin)

        goto(f"/work/{slug}/organization/roles/{rolle.id}/")
        wait_for_component(page, "permissionsManager")
        expect(page.locator('[x-data="colorPicker"] input[name="color"]')).to_have_value("#10b981")

        kopf = page.locator('[role="group"] button', has_text=info["name"]).first
        kopf.click()
        alle = kopf.locator("xpath=..").locator('input[type="checkbox"]')
        expect(alle).to_be_checked()

        # Ein Recht abwählen: „Alle“ ist nicht mehr angehakt, sondern unbestimmt
        page.locator(f'input[name="permissions"][value="{codes[0]}"]').uncheck()
        expect(alle).not_to_be_checked()
        assert alle.evaluate("(el) => el.indeterminate") is True
        # „Alle“ wählt die ganze Kategorie wieder an
        alle.check()
        for code in codes:
            expect(page.locator(f'input[name="permissions"][value="{code}"]')).to_be_checked()
        assert component_state(page, "permissionsManager", f"data.allChecked('{kategorie}')") is True

        # Löschen über den Bestätigungsdialog (data-action="confirm-submit")
        dialog = page.locator('[x-data="confirmDialog"]')
        page.click("#role-delete-btn")
        expect(dialog).to_contain_text("Möchten Sie die Rolle «E2E-Rolle» wirklich löschen?")
        dialog.get_by_role("button", name="Abbrechen").click()
        expect(dialog.get_by_role("button", name="Löschen")).to_be_hidden()
        assert type(rolle).objects.filter(pk=rolle.pk).exists()

        page.click("#role-delete-btn")
        dialog.get_by_role("button", name="Löschen").click()
        page.wait_for_url(f"**/work/{slug}/organization/roles/")
        problems.assert_clean("Rollenformular")
        assert not type(rolle).objects.filter(pk=rolle.pk).exists()

    def test_ratsfraktionen_formular_aufklappen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/organization/parties/")
        wait_for_component(page, "partyManager")
        kurzname = page.locator('input[name="short_name"][placeholder="SPD"]')
        expect(kurzname).to_have_count(0)
        page.get_by_role("button", name="Fraktion hinzufügen").click()
        expect(kurzname).to_be_visible()
        problems.assert_clean("Ratsfraktionen")


class TestDokumente:
    def test_neues_dokument_vorlage_folgt_dem_typ(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        org = admin.organization
        antrag = MotionType.objects.create(organization=org, name="Antrag", slug="antrag", is_default=True)
        anfrage = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage", sort_order=1)
        standard = MotionTemplate.objects.create(
            organization=org, motion_type=antrag, name="Antragsvorlage", description="Mit Begründung", is_default=True
        )
        fuer_anfragen = MotionTemplate.objects.create(
            organization=org, motion_type=anfrage, name="Anfragevorlage", is_default=True
        )
        slug = _anmelden(login, admin)

        goto(f"/work/{slug}/documents/create/")
        wait_for_component(page, "createMotion")
        vorlage = page.locator('select[name="template"]')
        expect(vorlage).to_have_value(str(standard.id))
        expect(page.get_by_text("Mit Begründung")).to_be_visible()

        page.select_option('select[name="document_type"]', str(anfrage.id))
        expect(vorlage).to_have_value(str(fuer_anfragen.id))
        assert component_state(page, "createMotion", "data.filteredTemplates.length") == 1
        problems.assert_clean("Neues Dokument")

    def test_dokumenttyp_kuerzel_aus_dem_namen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/organization/documents/types/create/")
        page.fill("#name", "Große Übung 2027")
        expect(page.locator("#slug")).to_have_value("grosse-uebung-2027")
        # Selbst getipptes Kürzel bleibt stehen
        page.fill("#slug", "eigenes")
        page.fill("#name", "Anderer Name")
        expect(page.locator("#slug")).to_have_value("eigenes")
        problems.assert_clean("Dokumenttyp")

    def test_briefkopf_vorbelegung_und_vorschau(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/organization/documents/letterheads/create/")
        wait_for_component(page, "letterheadForm")
        expect(page.locator("#sender_line")).to_have_value(admin.organization.name)
        expect(page.locator("#letterhead-preview")).not_to_be_empty()

        page.locator('input[name="kind"][value="pdf"]').check()
        expect(page.locator("#pdf_file")).to_be_visible()
        expect(page.locator("#sender_line")).to_be_hidden()
        problems.assert_clean("Briefkopf")


class TestProfil:
    def test_kontaktweg_umschalten(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/profile/visibility/")
        email = page.locator('label:has(input[name="preferred_contact"][value="email"])')
        telefon = page.locator('label:has(input[name="preferred_contact"][value="phone"])')

        def rahmen(label: Any) -> str:
            return str(label.evaluate("(el) => getComputedStyle(el).borderTopColor"))

        assert rahmen(email) == PRIMARY_500
        telefon.click()
        page.wait_for_function(
            "(farbe) => getComputedStyle(document.querySelector("
            "'label:has(input[value=\"phone\"])')).borderTopColor === farbe",
            arg=PRIMARY_500,
        )
        assert rahmen(email) != PRIMARY_500
        assert telefon.locator("svg").evaluate("(el) => getComputedStyle(el).color") != email.locator("svg").evaluate(
            "(el) => getComputedStyle(el).color"
        )
        problems.assert_clean("Sichtbarkeit")

    def test_aenderungsantrag_art_waehlen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/profile/requests/")
        wait_for_component(page, "changeRequestForm")
        page.locator('label:has(input[name="request_type"][value="committee_change"])').click()
        assert component_state(page, "changeRequestForm", "data.requestType") == "committee_change"
        problems.assert_clean("Änderungsanträge")

    def test_datenexport_status_wird_nachgeladen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        export = DataExport.objects.create(
            organization=admin.organization, membership=admin, status="processing", export_format="json"
        )
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/profile/data/")
        wait_for_component(page, "dataExport")
        expect(page.get_by_text("Daten werden zusammengefuehrt")).to_be_visible()
        expect(page.get_by_role("button", name="Export starten")).to_be_disabled()

        DataExport.objects.filter(pk=export.pk).update(status="completed", file_size=2048, completed_at=timezone.now())
        expect(page.get_by_role("link", name="Download")).to_be_visible(timeout=15000)
        expect(page.get_by_role("button", name="Export starten")).to_be_enabled()
        problems.assert_clean("Datenexport")


class TestSupportUndAufgaben:
    @pytest.fixture
    def artikel(self) -> KnowledgeBaseArticle:
        kategorie = KnowledgeBaseCategory.objects.create(name="Konto", slug="konto")
        return KnowledgeBaseArticle.objects.create(
            category=kategorie,
            title="Passwort zurücksetzen",
            slug="passwort-zuruecksetzen",
            excerpt="So setzen Sie Ihr Passwort zurück.",
            content="Schritt für Schritt …",
            is_published=True,
        )

    def test_ticket_artikelvorschlaege_und_anhaenge(
        self, page: Any, goto: Any, login: Any, admin: Any, artikel: KnowledgeBaseArticle, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/support/create/")
        wait_for_component(page, "ticketForm")
        wait_for_component(page, "kbSuggestions")

        page.fill("#subject", "Passwort")
        vorschlaege = page.get_by_test_id("kb-vorschlaege")
        expect(vorschlaege).to_be_visible()
        expect(vorschlaege).to_contain_text("Passwort zurücksetzen")

        page.set_input_files(
            "#attachments", files=[{"name": "notiz.txt", "mimeType": "text/plain", "buffer": b"hallo"}]
        )
        expect(page.get_by_text("notiz.txt")).to_be_visible()
        expect(page.get_by_text("5 B", exact=True)).to_be_visible()
        assert page.locator("#attachments").evaluate("(el) => el.files.length") == 1
        problems.assert_clean("Neues Ticket")

    def test_artikel_bewerten(
        self, page: Any, goto: Any, login: Any, admin: Any, artikel: KnowledgeBaseArticle, problems: BrowserProblems
    ) -> None:
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/support/kb/konto/{artikel.slug}/")
        wait_for_component(page, "feedbackWidget")
        page.get_by_role("button", name="Ja").click()
        expect(page.get_by_text("Vielen Dank für Ihr Feedback!")).to_be_visible()
        assert ArticleFeedback.objects.filter(article=artikel, is_helpful=True).count() == 1
        problems.assert_clean("Hilfe-Artikel")

    def test_aufgabe_zuweisen_mit_rueckfrage(
        self, page: Any, goto: Any, login: Any, admin: Any, make_member: Any, problems: BrowserProblems
    ) -> None:
        kollegin = make_member(admin.organization, ["tasks.view"], email="kollegin@example.org")
        kollegin.user.first_name, kollegin.user.last_name = "Karla", "Kollegin"
        kollegin.user.save(update_fields=["first_name", "last_name"])
        slug = _anmelden(login, admin)
        goto(f"/work/{slug}/tasks/create/")
        wait_for_component(page, "assignmentHandler")
        auswahl = page.locator('select[name="assigned_to"]')

        auswahl.select_option(str(kollegin.id))
        dialog = page.get_by_role("dialog", name="Zuweisung bestätigen")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("Karla Kollegin")
        dialog.get_by_role("button", name="Abbrechen").click()
        expect(auswahl).to_have_value("")

        auswahl.select_option(str(kollegin.id))
        dialog.get_by_role("button", name="Zuweisen").click()
        expect(dialog).to_be_hidden()
        expect(auswahl).to_have_value(str(kollegin.id))
        problems.assert_clean("Neue Aufgabe")
