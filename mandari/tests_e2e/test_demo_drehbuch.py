# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Drehbuch im Browser: eine Drucksache von der Fraktion bis ins Bürgerportal.

Spielt die Präsentationsumgebung (``setup_demo_praesentation --profil hamburg``) so durch, wie sie
vorgeführt wird – jede Rolle in einem eigenen Browser-Kontext:

1. Der Fraktionsvorsitz reicht den Work-Antrag bei der Verwaltung ein → Eingangsnummer.
2. Die Verwaltung wandelt den Antrag in Session in eine Vorlage um → Drucksache 22-….
3. Die Verwaltung stellt den Jugendzentrum-TOP auf nicht-öffentlich → im Bürgerportal (anonymer
   Kontext, Einstieg über den Kommune-Link) ist er nach dem Neuladen verschwunden.
4. Die OParl-Schnittstelle liefert JSON; der TOP ist dort nur noch als gelöschtes Objekt abrufbar.
   Dazu Beschlusskontrolle (überfällige Frist intern, Umsetzungsstand öffentlich) und Sitzungsgeld:
   Die Verwaltung darf ihre eigenen Positionen nicht genehmigen.
5. Die Leitstelle genehmigt das Sitzungsgeld und wechselt über „Mandant wechseln“ in Mandant B.
6. Die Leitstelle öffnet die Übersicht beider Bezirke und die gemeinsame Sitzung zweier Ausschüsse.

Auf keiner besuchten Seite darf eine unbehandelte JavaScript-Ausnahme oder ein Serverfehler
auftreten. WebSocket-Verbindungen (Editor-Kollaboration) scheitern gegen den WSGI-Testserver
zwangsläufig; das zählt bewusst nicht.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.common.management.commands import setup_demo_praesentation as drehbuch
from apps.common.management.commands.setup_demo_environment import DEMO_ORG_SLUG, DEMO_SESSION_SLUG, DEMO_USERS
from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionApplication,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
)
from apps.work.motions.models import Motion
from insight_core.models import OParlMeeting
from tests_e2e.conftest import SCREENSHOT_DIR, login_via_form, wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORT = "E2e-Drehbuch-Passwort-1"
A = DEMO_SESSION_SLUG
B = drehbuch.MANDANT_B_SLUG


@dataclass
class Beobachter:
    """Sammelt Probleme aller Browser-Kontexte: unbehandelte JS-Ausnahmen und Serverfehler."""

    basis: str
    probleme: list[str] = field(default_factory=list)

    def beobachte(self, page: Any, rolle: str) -> Any:
        page.on("pageerror", lambda fehler: self.probleme.append(f"{rolle}: Ausnahme „{fehler}“ auf {page.url}"))
        page.on("response", lambda antwort: self._antwort(rolle, antwort))
        return page

    def _antwort(self, rolle: str, antwort: Any) -> None:
        if antwort.status >= 500 and antwort.url.startswith(self.basis):
            self.probleme.append(f"{rolle}: HTTP {antwort.status} {antwort.url}")


@pytest.fixture
def praesentation(settings: Any, tmp_path: Path) -> None:
    """Präsentationsumgebung in der Testdatenbank, Passwörter der vorführenden Nutzer bekannt."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.OPARL_API_RATE_LIMIT = 0
    call_command("setup_demo_praesentation", "--profil", "hamburg", stdout=StringIO())
    for email in (DEMO_USERS["vorsitz"]["email"], DEMO_USERS["verwaltung"]["email"], drehbuch.LEITSTELLE["email"]):
        nutzer = User.objects.get(email=email)
        nutzer.set_password(PASSWORT)
        nutzer.save(update_fields=["password"])


@pytest.fixture
def neue_seite(
    browser: Any, browser_context_args: dict[str, Any], live_server: Any
) -> Iterator[tuple[Callable[[str], Any], Beobachter]]:
    """Fabrik: neue_seite(rolle) → Seite in einem eigenen Browser-Kontext (eigene Anmeldung)."""
    beobachter = Beobachter(basis=live_server.url)
    kontexte: list[Any] = []

    def oeffnen(rolle: str) -> Any:
        kontext = browser.new_context(**browser_context_args)
        kontexte.append(kontext)
        return beobachter.beobachte(kontext.new_page(), rolle)

    yield oeffnen, beobachter
    for kontext in kontexte:
        kontext.close()


def bildschirmfoto(page: Any, name: str) -> None:
    page.screenshot(path=str(SCREENSHOT_DIR / f"drehbuch-{name}.png"), full_page=True)


def test_drehbuch(live_server: Any, praesentation: None, neue_seite: Any) -> None:
    oeffnen, beobachter = neue_seite
    basis = live_server.url

    # --- 1. Work: Der Vorsitz reicht den Antrag bei der Verwaltung ein ------------------------
    vorsitz = oeffnen("Vorsitz")
    login_via_form(vorsitz, basis, DEMO_USERS["vorsitz"]["email"], PASSWORT)
    antrag = Motion.objects.get(organization__slug=DEMO_ORG_SLUG, title=drehbuch.ANTRAG_TITEL)
    vorsitz.goto(f"{basis}/work/{DEMO_ORG_SLUG}/documents/{antrag.id}/")
    wait_for_bundle(vorsitz)
    vorsitz.locator("a[title='Bei Verwaltung einreichen']").first.click()
    vorsitz.wait_for_url(re.compile(r"/submit-ris/$"))
    wait_for_bundle(vorsitz)
    # Beschlussvorschlag und Begründung sind aus dem Dokument vorbelegt
    expect(vorsitz.locator("#resolution_proposal")).to_have_value(re.compile(r"^Die Verwaltung wird beauftragt"))
    expect(vorsitz.locator("#justification")).to_have_value(re.compile(r"^Der Musterweg ist der Hauptzugang"))
    vorsitz.select_option("#target_organization", label=drehbuch.GREMIUM_HA)
    vorsitz.check("input[name=confirm]")
    vorsitz.click("button:has-text('Jetzt einreichen')")
    vorsitz.wait_for_url(re.compile(rf"/documents/{antrag.id}/$"))
    wait_for_bundle(vorsitz)
    eingang = SessionApplication.objects.get(tenant__slug=A, title=drehbuch.ANTRAG_TITEL)
    assert re.fullmatch(r"A/\d{4}/\d{4}", eingang.reference)
    expect(vorsitz.locator("a[title='Status bei der Verwaltung']")).to_contain_text(eingang.reference)
    bildschirmfoto(vorsitz, "1-eingangsnummer")

    # --- 2. Session: Die Verwaltung wandelt den Antrag in eine Vorlage um ---------------------
    verwaltung = oeffnen("Verwaltung")
    login_via_form(verwaltung, basis, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    verwaltung.goto(f"{basis}/session/{A}/applications/")
    wait_for_bundle(verwaltung)
    expect(verwaltung.get_by_text(drehbuch.ANTRAG_TITEL)).to_be_visible()
    verwaltung.locator(f"a[href$='/applications/{eingang.id}/']").first.click()
    verwaltung.wait_for_url(re.compile(rf"/applications/{eingang.id}/$"))
    wait_for_bundle(verwaltung)
    verwaltung.click("a:has-text('In Vorlage umwandeln')")
    verwaltung.wait_for_url(re.compile(r"/convert/$"))
    wait_for_bundle(verwaltung)
    hauptausschuss = SessionOrganization.objects.get(tenant__slug=A, name=drehbuch.GREMIUM_HA)
    # Das Zielgremium aus der Einreichung ist als federführendes Gremium vorausgewählt
    expect(verwaltung.locator("#id_main_organization")).to_have_value(str(hauptausschuss.id))
    # Die Vorlagenart folgt der Antragsart und bestimmt den Nummernkreis
    expect(verwaltung.locator("#id_paper_type")).to_have_value("motion")
    verwaltung.click("button:has-text('In Vorlage umwandeln')")
    verwaltung.wait_for_url(re.compile(r"/papers/[0-9a-f-]{36}/$"))
    wait_for_bundle(verwaltung)
    vorlage = SessionPaper.objects.get(source_application=eingang)
    assert re.fullmatch(r"22-\d{4}", vorlage.reference), vorlage.reference
    expect(verwaltung.get_by_test_id("vorlagennummer")).to_have_text(f"Drucksache {vorlage.reference}")
    bildschirmfoto(verwaltung, "2-drucksache")

    # --- 3. Ö → NÖ: Der TOP verschwindet sofort aus dem Bürgerportal --------------------------
    top = SessionAgendaItem.objects.get(
        meeting__tenant__slug=A, meeting__name=drehbuch.SITZUNG_KOMMEND, name=drehbuch.TOP_JUGENDZENTRUM
    )
    portal_sitzung = OParlMeeting.objects.get(external_id__endswith=f"/session/{A}/api/oparl/meeting/{top.meeting_id}/")
    buerger = oeffnen("Bürgerportal")
    # Einstieg wie in der Vorführung: direkter Link auf die (nicht gelistete) Kommune
    buerger.goto(f"{basis}/insight/kommune/{portal_sitzung.body_id}/")
    buerger.wait_for_url(re.compile(r"/insight/$"))
    buerger.wait_for_load_state("networkidle")
    buerger.goto(f"{basis}/insight/termine/{portal_sitzung.id}/")
    buerger.wait_for_load_state("networkidle")
    expect(buerger.locator("main")).to_contain_text(drehbuch.TOP_JUGENDZENTRUM)
    expect(buerger.locator("main")).to_contain_text(drehbuch.TOP_SPIELPLATZ)
    expect(buerger.locator("main")).not_to_contain_text(drehbuch.TOP_GRUNDSTUECK)
    bildschirmfoto(buerger, "3-buergerportal-vorher")

    verwaltung.goto(f"{basis}/session/{A}/meetings/{top.meeting_id}/")
    wait_for_bundle(verwaltung)
    zeile = verwaltung.locator(f"[data-agenda-item='{top.id}']")
    # Beratungsfolge am TOP: Vorberatung im Bauausschuss, Entscheidung im Hauptausschuss
    expect(zeile).to_contain_text("Vorberatung")
    expect(zeile).to_contain_text("Entscheidung")
    bildschirmfoto(verwaltung, "3-sitzung")
    zeile.locator("a[title='Bearbeiten']").click()
    verwaltung.wait_for_url(re.compile(rf"/agenda/{top.id}/edit/$"))
    wait_for_bundle(verwaltung)
    verwaltung.uncheck("#id_is_public")
    verwaltung.locator("form[method=post] button[type=submit]", has_text="Speichern").click()
    verwaltung.wait_for_url(re.compile(rf"/meetings/{top.meeting_id}/$"))
    wait_for_bundle(verwaltung)
    top.refresh_from_db()
    assert not top.is_public and top.number.startswith("N"), top.number

    buerger.reload()
    buerger.wait_for_load_state("networkidle")
    expect(buerger.locator("main")).not_to_contain_text(drehbuch.TOP_JUGENDZENTRUM)
    expect(buerger.locator("main")).to_contain_text(drehbuch.TOP_SPIELPLATZ)
    bildschirmfoto(buerger, "3-buergerportal-nachher")

    # --- 4. OParl: JSON, der TOP nur noch als gelöschtes Objekt ------------------------------
    system = buerger.request.get(f"{basis}/session/{A}/api/oparl/")
    assert system.ok, system.status
    assert system.json()["type"] == "https://schema.oparl.org/1.1/System"
    grabstein = buerger.request.get(f"{basis}/session/{A}/api/oparl/agendaitem/{top.id}/")
    assert grabstein.ok and grabstein.json().get("deleted") is True, grabstein.text()

    # --- Beschlusskontrolle: überfällige Frist intern, Umsetzungsstand öffentlich -------------
    verwaltung.goto(f"{basis}/session/{A}/resolutions/?overdue=1")
    wait_for_bundle(verwaltung)
    expect(verwaltung.locator("main")).to_contain_text(drehbuch.BESCHLUSS_HALTESTELLE)
    expect(verwaltung.locator("main")).not_to_contain_text(drehbuch.BESCHLUSS_LASTENRAD)
    beschluss = SessionAgendaItem.objects.get(meeting__tenant__slug=A, name=drehbuch.BESCHLUSS_LASTENRAD)
    verwaltung.goto(f"{basis}/session/{A}/meetings/{beschluss.meeting_id}/protocol/")
    wait_for_bundle(verwaltung)
    # Niederschrift: öffentlicher Teil und – für die Verwaltung – der verschlüsselte NÖ-Teil
    expect(verwaltung.locator("main")).to_contain_text(drehbuch.BESCHLUSS_LASTENRAD)
    expect(verwaltung.locator("main")).to_contain_text(drehbuch.TOP_REINIGUNG)
    buerger.goto(f"{basis}/insight/beschluesse/{beschluss.id}/")
    buerger.wait_for_load_state("networkidle")
    expect(buerger.locator("main")).to_contain_text("In Umsetzung")
    expect(buerger.locator("main")).not_to_contain_text("Angebotsfrist")  # interner Vermerk bleibt intern

    # --- Sitzungsgeld: Vier-Augen-Prinzip ----------------------------------------------------
    tag = timezone.localtime(beschluss.meeting.start).date().isoformat()
    sitzungsgeld = f"{basis}/session/{A}/allowances/?from={tag}&to={tag}"
    verwaltung.goto(sitzungsgeld)
    wait_for_bundle(verwaltung)
    verwaltung.click("button:has-text('Genehmigen (Vier-Augen)')")
    wait_for_bundle(verwaltung)
    expect(verwaltung.locator("body")).to_contain_text("Vier-Augen-Prinzip")
    assert not SessionAllowance.objects.filter(attendance__meeting=beschluss.meeting, status="approved").exists()

    # --- 5. Leitstelle: Freigabe und Mandantenwechsel in der Seitenleiste ------------------------
    leitstelle = oeffnen("Leitstelle")
    login_via_form(leitstelle, basis, drehbuch.LEITSTELLE["email"], PASSWORT)
    leitstelle.goto(sitzungsgeld)
    wait_for_bundle(leitstelle)
    leitstelle.click("button:has-text('Genehmigen (Vier-Augen)')")
    wait_for_bundle(leitstelle)
    expect(leitstelle.locator("body")).to_contain_text("5 Position(en) genehmigt")

    leitstelle.goto(f"{basis}/session/{A}/")
    wait_for_bundle(leitstelle)
    wechsel = leitstelle.get_by_test_id("mandant-wechseln")
    wechsel.locator("summary").click()
    wechsel.get_by_role("link", name=drehbuch.MANDANT_B_NAME).click()
    leitstelle.wait_for_url(re.compile(rf"/session/{B}/$"))
    wait_for_bundle(leitstelle)
    expect(leitstelle.locator(".session-org-name")).to_have_text(drehbuch.MANDANT_B_NAME)
    bildschirmfoto(leitstelle, "5-mandant-b")

    # --- 6. Leitstellen-Übersicht beider Bezirke und gemeinsame Sitzung (Issue #317) ----------
    leitstelle.get_by_role("link", name=f"Leitstelle {drehbuch.GRUPPE_NAME}").click()
    leitstelle.wait_for_url(re.compile(rf"/session/leitstelle/{drehbuch.GRUPPE_SLUG}/$"))
    wait_for_bundle(leitstelle)
    expect(leitstelle.get_by_test_id("leitstelle-vorlagen")).to_contain_text(drehbuch.VORLAGE_LEITSTELLE.name)
    expect(leitstelle.get_by_test_id("leitstelle-tabelle")).to_contain_text(drehbuch.MANDANT_B_NAME)
    bildschirmfoto(leitstelle, "6-leitstelle")
    gemeinsam = SessionMeeting.objects.get(tenant__slug=A, name=drehbuch.SITZUNG_GEMEINSAM)
    leitstelle.goto(f"{basis}/session/{A}/meetings/{gemeinsam.id}/")
    wait_for_bundle(leitstelle)
    expect(leitstelle.locator("main")).to_contain_text("Gemeinsame Sitzung")
    expect(leitstelle.locator("main")).to_contain_text(drehbuch.GREMIUM_BAU)

    # --- 7. Keine JS-Ausnahmen und keine Serverfehler auf den besuchten Seiten ---------------
    assert not beobachter.probleme, "\n".join(beobachter.probleme)
