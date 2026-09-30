# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Django-Admin (django-unfold) im Browser: Bezugsobjekte aus einem Formular heraus anlegen und löschen
(Issue #686).

Die Verweise neben einem Auswahlfeld (Hinzufügen, Ändern, Löschen) öffnen eine zweite Admin-Ansicht und
tragen deren Ergebnis ins Ausgangsformular zurück. Bis django-unfold 0.106 geschieht das in einem eigenen
Fenster, ab 0.107 in einem Dialog mit eingebettetem Rahmen. Der Rahmen lädt nur, wenn die Antwort das
Einbetten für dieselbe Herkunft erlaubt – das sehen serverseitige Tests nicht, deshalb hier im Browser.
Der Test läuft mit beiden Darstellungen, damit er vor und nach dem Versionswechsel gilt.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

import pytest
from packaging.version import Version

from tests_e2e.conftest import BrowserProblems

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORT = "E2e-Betreiber-Passwort-123456"
#: Ab dieser Version zeigt django-unfold Bezugsobjekte im Dialog statt in einem eigenen Fenster
IM_DIALOG = Version(version("django-unfold")) >= Version("0.107")


@pytest.fixture
def betreiber(db: Any) -> Any:
    from apps.common.tests.factories import UserFactory

    user = UserFactory(email="e2e-betreiber@example.org", is_staff=True, is_superuser=True)
    user.set_password(PASSWORT)
    user.save(update_fields=["password"])
    return user


@pytest.fixture
def organisationsformular(page: Any, live_server: Any, login: Any, betreiber: Any) -> Any:
    """Angemeldet im Admin auf „Organisation hinzufügen“ (Auswahlfeld „Parteigruppe“ mit Verweisen)."""
    login(betreiber.email, PASSWORT)
    page.goto(f"{live_server.url}/admin/tenants/organization/add/")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#id_party_group")).to_be_visible()
    return page


def _verweis_oeffnen(page: Any, aktion: str) -> Any:
    """
    Klickt den Verweis ``aktion`` (add, change, delete) am Feld „Parteigruppe“ und liefert die Fläche, in
    der die zweite Admin-Ansicht erscheint: den Rahmen im Dialog oder das eigene Fenster.
    """
    page.locator('[data-id="related-widget-wrapper-party_group"] [x-ref="relatedWidgetWrapperparty_group"]').click()
    verweis = page.locator(f"#{aktion}_id_party_group")
    if IM_DIALOG:
        verweis.click()
        return page.frame_locator("#modal-content iframe")
    with page.expect_popup() as fenster:
        verweis.click()
    return fenster.value


class TestBezugsobjekte:
    def test_parteigruppe_aus_dem_formular_anlegen(self, organisationsformular: Any, problems: BrowserProblems) -> None:
        from apps.tenants.models import PartyGroup

        page = organisationsformular

        ansicht = _verweis_oeffnen(page, "add")
        ansicht.locator("input[name=name]").fill("Bundesverband E2E")
        ansicht.locator("[name=_save]").click()

        # Die neue Gruppe ist im Auswahlfeld des Ausgangsformulars gewählt
        expect(page.locator("#id_party_group option:checked")).to_have_text("Bundesverband E2E")
        assert PartyGroup.objects.filter(name="Bundesverband E2E").exists()
        problems.assert_clean("Admin: Parteigruppe anlegen")

    def test_parteigruppe_aus_dem_formular_loeschen(
        self, organisationsformular: Any, problems: BrowserProblems
    ) -> None:
        from apps.tenants.models import PartyGroup

        gruppe = PartyGroup.objects.create(name="Landesverband E2E", slug="landesverband-e2e")
        page = organisationsformular
        page.reload()
        page.wait_for_load_state("networkidle")
        page.select_option("#id_party_group", str(gruppe.pk))

        ansicht = _verweis_oeffnen(page, "delete")
        # Die Bestätigungsseite erscheint (im Dialog: nicht vom Browser blockiert) und nennt das Objekt
        expect(ansicht.locator("body")).to_contain_text("Landesverband E2E")
        ansicht.locator("form [type=submit]").click()

        # Gelöscht und aus dem Auswahlfeld des Ausgangsformulars entfernt
        expect(page.locator(f'#id_party_group option[value="{gruppe.pk}"]')).to_have_count(0)
        assert not PartyGroup.objects.filter(pk=gruppe.pk).exists()
        problems.assert_clean("Admin: Parteigruppe löschen")
