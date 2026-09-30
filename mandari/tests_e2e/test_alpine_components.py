# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenbezogene Alpine-Komponenten funktionieren im Browser.

Hintergrund (10.09.2026): `work.ts` und das Editor-Bundle registrieren ihre Komponenten mit
`Alpine.data()`, wenn ihr Modul läuft. Startete `main.ts` Alpine vorher, blieb jedes
`x-data="…"` dieser Seiten leer – Dokumentliste, Aufgaben, Fraktionssitzung, Editor und
Sitzungsvorbereitung waren nicht bedienbar. `window.Alpine` existierte trotzdem, deshalb
fielen die bisherigen E2E-Tests nicht auf.

Geprüft wird je Seite:
- die Komponente ist wirklich initialisiert (Datenstapel am Element)
- keine unbehandelte JavaScript-Ausnahme und keine Alpine-Fehler in der Konsole
- keine Serverfehler (5xx) bei Anfragen, die die Seite selbst auslöst
Dazu je eine typische Interaktion dort, wo Zustand erst durch Bedienen entsteht.

WebSocket-Verbindungen scheitern gegen den Testserver zwangsläufig (kein ASGI); das zählt
bewusst nicht als Fehler.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone
from playwright.sync_api import expect

from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.motions.models import Motion
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems, component_state, wait_for_component


class TestSeitenkomponenten:
    def test_dokumentliste_und_editor(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        motion = Motion.objects.create(
            organization=admin.organization, author=admin, title="E2E-Dokument", visibility="organization"
        )
        login(admin.user.email, PASSWORD)
        slug = admin.organization.slug

        goto(f"/work/{slug}/documents/")
        wait_for_component(page, "documentManager")
        # Liste und Kachelansicht rendern beide; nur die sichtbare zählt
        expect(page.get_by_text("E2E-Dokument").filter(visible=True).first).to_be_visible()
        problems.assert_clean("Dokumentliste")

        goto(f"/work/{slug}/documents/{motion.id}/")
        wait_for_component(page, "documentEditor")
        problems.assert_clean("Dokumenteneditor")

    def test_aufgaben_und_sicherheitseinstellungen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        login(admin.user.email, PASSWORD)
        slug = admin.organization.slug

        goto(f"/work/{slug}/tasks/")
        wait_for_component(page, "kanbanBoard")
        problems.assert_clean("Aufgaben")

        goto(f"/work/{slug}/profile/security/")
        wait_for_component(page, "securitySettings")
        problems.assert_clean("Sicherheitseinstellungen")

    def test_fraktionseinstellungen(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        login(admin.user.email, PASSWORD)

        goto(f"/work/{admin.organization.slug}/faction/settings/")
        wait_for_component(page, "factionTitlePreview")
        problems.assert_clean("Fraktionseinstellungen")

    def test_fraktionssitzung_mit_top_panel(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        meeting = FactionMeeting.objects.create(
            organization=admin.organization,
            title="Fraktionssitzung E2E",
            start=timezone.now() + timedelta(days=3),
            status="planned",
            created_by=admin,
        )
        item = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt 2027", visibility="public")
        login(admin.user.email, PASSWORD)

        goto(f"/work/{admin.organization.slug}/faction/{meeting.id}/")
        wait_for_component(page, "factionDetail")
        expect(page.get_by_text("Haushalt 2027").first).to_be_visible()

        # TOP-Panel öffnen: lädt per HTMX nach und startet dort agendaItemPanel
        page.evaluate(
            "(id) => window.dispatchEvent(new CustomEvent('open-item-panel', { detail: { id } }))", str(item.id)
        )
        wait_for_component(page, "agendaItemPanel")
        assert component_state(page, "factionDetail", "data.openPanel") is True
        problems.assert_clean("Fraktionssitzung mit TOP-Panel")

    def test_sitzungsvorbereitung_mit_top_auswahl(
        self, page: Any, goto: Any, login: Any, admin: Any, problems: BrowserProblems
    ) -> None:
        source = OParlSource.objects.create(name="E2E-RIS", url="https://ris.example.org/system")
        body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt E2E")
        admin.organization.body = body
        admin.organization.save(update_fields=["body"])
        meeting = OParlMeeting.objects.create(
            external_id="https://ris.example.org/meeting/1", body=body, name="Ausschuss E2E"
        )
        for order, name in enumerate(("Haushalt", "Radverkehr"), start=1):
            OParlAgendaItem.objects.create(
                external_id=f"https://ris.example.org/agenda/{order}",
                meeting=meeting,
                number=str(order),
                name=name,
                order=order,
            )
        login(admin.user.email, PASSWORD)

        goto(f"/work/{admin.organization.slug}/meetings/{meeting.id}/prepare/")
        wait_for_component(page, "preparationApp")

        # Genau das war defekt: TOP-Liste leer, nichts auswählbar
        item_ids = component_state(page, "preparationApp", "data.items.map((item) => item.id)")
        assert len(item_ids) == 2
        for item_id in item_ids:
            expect(page.locator(f"#nav-item-{item_id}")).to_be_visible()
        page.wait_for_function(
            '(id) => window.Alpine.$data(document.querySelector(`[x-data^="preparationApp"]`)).selectedItemId === id',
            arg=item_ids[0],
        )

        # Pfeiltaste wechselt zum nächsten TOP
        page.keyboard.press("ArrowDown")
        page.wait_for_function(
            '(id) => window.Alpine.$data(document.querySelector(`[x-data^="preparationApp"]`)).selectedItemId === id',
            arg=item_ids[1],
        )
        problems.assert_clean("Sitzungsvorbereitung")
