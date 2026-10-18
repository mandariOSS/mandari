# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS-Seiten von Work im neuen Erscheinungsbild (Issues #852, #853) im Browser: Übersicht, Sitzungen der Gremien und
Sitzung, Vorgänge und Vorgang, Gremien und Gremium, Personen und Person.

Je Breite: kein seitliches Überlaufen, ab 1.280 px rechts höchstens ein Viertel der Fensterbreite frei (keine
Leerflächen auf breiten Bildschirmen), axe ohne schwere Befunde (1.440 px, hell und dunkel), keine Fehler im Browser.
Der Vorgang zeigt Stand-Satz, Dokumentzeile mit aufklappbarem Text und „Für die Fraktion“ mit der Position; die Listen
sind die Listen von Insight (ganze Zeile führt in Work weiter), die Sitzung zeigt Niederschrift, Übertragung (dichte
Fassung mit Textlinks) und Sitzungsdateien wie Insight und keine zurückgenommenen Punkte. Screenshots bei 390, 1.280,
1.920 und 2.560 px als CI-Artefakt.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.work.meetings.models import AgendaItemPosition
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    PersonFraktion,
)
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems

RIS = "https://ris.recherche-e2e.example/oparl"

#: rechter Rand des Inhalts: die äußersten Abschnitte, Listen und Spalten der Seite
RECHTS = """() => {
  const inhalt = document.querySelector('.work-page-content');
  const teile = [...inhalt.querySelectorAll('section, aside, ul, ol, form, nav, table')].filter((el) => el.offsetParent);
  return {breite: window.innerWidth, rechts: Math.max(...teile.map((el) => el.getBoundingClientRect().right)),
          scroll: document.documentElement.scrollWidth};
}"""


@pytest.fixture
def recherche(admin: Any) -> dict[str, Any]:
    organisation = admin.organization
    source = OParlSource.objects.create(name="Recherche-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Musterstadt")
    organisation.body = body
    organisation.work_new_design = True
    organisation.save(update_fields=["body", "work_new_design"])
    ausschuss = OParlOrganization.objects.create(external_id=f"{RIS}/org/1", body=body, name="Hauptausschuss")
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/org/2", body=body, name="Rat der Stadt")
    jetzt = timezone.now()
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1", body=body, start=jetzt - timedelta(days=7), location_name="Rathaus, Saal 1"
    )
    sitzung.organizations.add(ausschuss)
    # Niederschrift, Übertragung und Sitzungsdateien wie in Insight (Issue #853)
    niederschrift = OParlFile.objects.create(
        external_id=f"{RIS}/file/niederschrift",
        body=body,
        meeting=sitzung,
        name="Niederschrift öffentlicher Teil",
        access_url="https://ris.recherche-e2e.example/niederschrift.pdf",
        mime_type="application/pdf",
        size=48000,
    )
    OParlFile.objects.create(
        external_id=f"{RIS}/file/einladung",
        body=body,
        meeting=sitzung,
        name="Einladung",
        access_url="https://ris.recherche-e2e.example/einladung.pdf",
        mime_type="application/pdf",
        size=12000,
        text_content="Einladung zur Sitzung des Hauptausschusses.",
    )
    sitzung.raw_json = {
        "resultsProtocol": niederschrift.external_id,
        "mandari:meetingFormat": "hybrid",
        "mandari:publicAccess": {"url": "https://stream.recherche-e2e.example/live", "hint": "Ohne Anmeldung."},
    }
    sitzung.save(update_fields=["raw_json"])
    # Von mandari Session zurückgenommener Punkt: erscheint wie in Insight nicht auf der Sitzungsseite
    OParlAgendaItem.objects.create(
        external_id="https://mandari.example/session/stadt/api/oparl/agendaitem/zurueck",
        meeting=sitzung,
        number="5",
        name="Zurückgenommener Punkt",
        deleted=True,
    )
    ratssitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/2", body=body, start=jetzt + timedelta(days=6)
    )
    ratssitzung.organizations.add(rat)
    vorlage = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/1",
        body=body,
        name="Öffentliche Trinkwasserbrunnen in der Innenstadt",
        reference="A/2026/001",
        paper_type="Antrag",
        date=date.today() - timedelta(days=20),
    )
    for nummer in range(2, 14):
        OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{nummer}",
            body=body,
            name=f"Beschlussvorlage Nummer {nummer} zur Sanierung städtischer Gebäude",
            reference=f"V/2026/{nummer:03d}",
            paper_type="Beschlussvorlage",
            date=date.today() - timedelta(days=nummer),
        )
    for nummer, (meeting, punkt_nr, gremium_name) in enumerate(
        ((sitzung, "3", "Vorberatung"), (ratssitzung, "4", "Entscheidung"))
    ):
        punkt = OParlAgendaItem.objects.create(
            external_id=f"{RIS}/item/{nummer}",
            meeting=meeting,
            number=punkt_nr,
            name=vorlage.name,
            result="Mit Änderungen empfohlen" if meeting is sitzung else None,
        )
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/{nummer}",
            body=body,
            paper=vorlage,
            paper_external_id=vorlage.external_id,
            meeting_external_id=meeting.external_id,
            agenda_item_external_id=punkt.external_id,
            role=gremium_name,
            authoritative=meeting is ratssitzung,
        )
        if meeting is sitzung:
            AgendaItemPosition.objects.create(
                organization=organisation, agenda_item=punkt, position="amended", is_final=True, outcome="accepted"
            )
    OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=body,
        paper=vorlage,
        name="Antrag Trinkwasserbrunnen",
        access_url="https://ris.recherche-e2e.example/datei.pdf",
        mime_type="application/pdf",
        size=2200,
        text_content="Der Rat möge beschließen: Die Verwaltung errichtet drei öffentliche Trinkwasserbrunnen.",
    )
    for nummer, name in enumerate(("Erika Beispiel", "Martin Muster", "Vera Beispiel")):
        person = OParlPerson.objects.create(
            external_id=f"{RIS}/person/{nummer}", body=body, name=name, email=f"p{nummer}@example.org"
        )
        OParlMembership.objects.create(
            external_id=f"{RIS}/membership/{nummer}", person=person, organization=ausschuss, role="Mitglied"
        )
        if nummer < 2:
            # Fraktion aus der bestätigten Zuordnung in Insight (Issue #916), mit Quellenhinweis
            PersonFraktion.objects.create(
                person=person, body=body, bezeichnung="Fraktion Mitte", quelle=PersonFraktion.QUELLE_EINBLENDUNG
            )
    admin.followed_organizations.add(ausschuss)
    return {
        "admin": admin,
        "seiten": [
            ("uebersicht", "ris/", "Recherche"),
            ("sitzungen", "ris/meetings/?view=all", "Sitzungen der Gremien"),
            ("sitzung", f"ris/meetings/{sitzung.pk}/", "Hauptausschuss"),
            ("vorgaenge", "ris/papers/", "Vorgänge"),
            ("vorgang", f"ris/papers/{vorlage.pk}/", vorlage.name),
            ("gremien", "ris/organizations/?tab=all", "Gremien"),
            ("gremium", f"ris/organizations/{ausschuss.pk}/", "Hauptausschuss"),
            ("personen", "ris/persons/", "Personen"),
            ("person", f"ris/persons/{OParlPerson.objects.get(name='Erika Beispiel').pk}/", "Erika Beispiel"),
        ],
    }


@pytest.mark.parametrize("breite", [390, 1280, 1440, 1920, 2560])
def test_recherche_seiten(
    page: Any,
    goto: Any,
    login: Any,
    recherche: dict[str, Any],
    axe: Any,
    screenshot: Any,
    dark_mode: Any,
    problems: BrowserProblems,
    breite: int,
) -> None:
    admin = recherche["admin"]
    page.set_viewport_size({"width": breite, "height": 900 if breite > 500 else 844})
    login(admin.user.email, PASSWORD)
    for name, pfad, titel in recherche["seiten"]:
        goto(f"/work/{admin.organization.slug}/{pfad}")
        expect(page.get_by_role("heading", level=1, name=titel)).to_be_visible()
        messung = page.evaluate(RECHTS)
        assert messung["scroll"] <= breite, (name, messung)
        if breite >= 1280:
            assert (breite - messung["rechts"]) / breite <= 0.25, (name, messung)
        if breite == 1440:
            ergebnis = axe()
            assert not ergebnis.failing, (name, ergebnis.describe())
        if breite != 1440:
            screenshot(f"work-recherche-{name}-{breite}")
    if breite == 1440:
        dark_mode(True)
        goto(f"/work/{admin.organization.slug}/{recherche['seiten'][4][1]}")
        ergebnis = axe()
        assert not ergebnis.failing, ergebnis.describe()
        screenshot("work-recherche-vorgang-1440-dunkel")
    problems.assert_clean(f"Recherche bei {breite} px")


def test_vorgang_stand_dokument_und_fraktion(
    page: Any, goto: Any, login: Any, recherche: dict[str, Any], problems: BrowserProblems
) -> None:
    admin = recherche["admin"]
    login(admin.user.email, PASSWORD)
    goto(f"/work/{admin.organization.slug}/{recherche['seiten'][4][1]}")

    expect(page.get_by_test_id("vorgang-stand")).to_contain_text("Nächste Beratung am")
    fraktion = page.get_by_test_id("fuer-die-fraktion")
    expect(fraktion).to_contain_text("Mit Änderungsantrag")
    expect(fraktion).to_contain_text("Ergebnis: Angenommen")
    # Dokumentzeile von Insight: Text klappt auf (Alpine-Komponente documentText im Work-Bundle)
    dokumente = page.get_by_test_id("vorgang-dokumente")
    dokumente.get_by_role("button", name="Text").click()
    expect(page.get_by_text("Die Verwaltung errichtet drei öffentliche Trinkwasserbrunnen.")).to_be_visible()
    # Zeitstrahl führt zur Sitzung in Work
    page.get_by_test_id("vorgang-verlauf").get_by_role("link", name="Hauptausschuss").click()
    page.wait_for_url("**/ris/meetings/**")
    expect(page.get_by_test_id("tagesordnung")).to_contain_text("Mit Änderungsantrag")
    problems.assert_clean("Vorgang und Sitzung")


def test_listen_und_sitzung_mit_den_bausteinen_von_insight(
    page: Any, goto: Any, login: Any, recherche: dict[str, Any], problems: BrowserProblems
) -> None:
    admin = recherche["admin"]
    page.set_viewport_size({"width": 1440, "height": 900})
    login(admin.user.email, PASSWORD)
    goto(f"/work/{admin.organization.slug}/ris/papers/")

    # Liste von Insight: Klick in die Zeile öffnet den Vorgang in Work
    liste = page.get_by_test_id("vorgangsliste")
    liste.locator("tr", has_text="Trinkwasserbrunnen").locator("td").nth(2).click()
    page.wait_for_url("**/ris/papers/*-*/")
    expect(page.get_by_test_id("vorgang-stand")).to_be_visible()

    goto(f"/work/{admin.organization.slug}/ris/persons/")
    expect(page.get_by_test_id("personenliste")).to_contain_text("Fraktion Mitte")

    goto(f"/work/{admin.organization.slug}/{recherche['seiten'][2][1]}")
    expect(page.get_by_test_id("tagesordnung")).not_to_contain_text("Zurückgenommener Punkt")
    # Niederschrift und Übertragung in der dichten Fassung: Textlinks neben der einen Hauptaktion im Kopf
    niederschrift = page.get_by_test_id("oeffentliche-niederschrift")
    expect(niederschrift.get_by_role("link", name="Herunterladen")).to_be_visible()
    expect(page.get_by_test_id("sitzungsformat")).to_contain_text("Hybride Sitzung")
    expect(page.get_by_test_id("sitzungsformat").get_by_role("link", name="Zur Übertragung")).to_be_visible()
    dateien = page.get_by_test_id("sitzungsdateien")
    expect(dateien).to_contain_text("Einladung")
    dateien.get_by_role("button", name="Text").click()
    expect(page.get_by_text("Einladung zur Sitzung des Hauptausschusses.")).to_be_visible()
    problems.assert_clean("Listen und Sitzung")
