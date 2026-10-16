# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teil 2b des neuen Erscheinungsbilds im Sitzungsdienst im Browser (Issue #944).

Mit den Demo-Daten (setup_demo_environment), eingeschaltetem Schalter und einem Bestand für Seiten, die die Demo nicht
füllt (Niederschrift im Entwurf, Umlauf, Fassungen und Dateiversionen, Endgeräte, Leitstelle), stehen die übrigen
Seiten des Sitzungsdienstes im neuen Erscheinungsbild und laufen am Handy (390 px) und am Tablet (768 px) nicht
seitlich über, ohne Fehler im Browser:

- Abläufe: Niederschrift (Ansicht, Bearbeiten, Berichtigen), Beschlussregister, Umlaufbeschlüsse, Anträge (Eingang,
  Bearbeiten, Umwandeln), Ladung und Rückmeldungen
- Werkzeuge: Cockpit (neuer Rahmen, Stand wie bisher) und Abstimmung erfassen, Fassungen, Dateiversionen, Endgeräte und
  die Leitstelle einer Mandantengruppe
- Formulare zum Anlegen und Bearbeiten und alle Einstellungen; deren Reiter führen am Handy zu ihren Seiten

Die Live-Funktion des Cockpits (TOP aufrufen, Abstimmung, Polling) prüft tests_e2e/test_sitzungscockpit.py in beiden
Erscheinungsbildern.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect

from apps.common.demo_daten import DEMO_USERS
from tests_e2e.conftest import BrowserProblems, login_via_form, wait_for_bundle
from tests_e2e.test_session_rahmen import BREITEN, MANDANT, PASSWORT, UEBERLAUF, demo  # noqa: F401


def _adresse(name: str, **kw: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": MANDANT, **kw})


@pytest.fixture
def bestand(demo: dict[str, str]) -> dict[str, Any]:  # noqa: F811
    """Mandant, Konto der Verwaltung und die Sitzungen der Demo: vergangen (genehmigte Niederschrift) und kommend."""
    from apps.accounts.models import User
    from apps.session.models import SessionMeeting, SessionTenant, SessionUser

    tenant = SessionTenant.objects.get(slug=MANDANT)
    nutzer = User.objects.get(email=DEMO_USERS["verwaltung"]["email"])
    sitzungen = SessionMeeting.objects.filter(tenant=tenant)
    vergangen = sitzungen.filter(protocol__status="approved").first()
    kommend = sitzungen.filter(protocol__isnull=True).order_by("-start").first()
    assert vergangen is not None and kommend is not None
    return {
        "tenant": tenant,
        "nutzer": nutzer,
        "konto": SessionUser.objects.get(tenant=tenant, user=nutzer),
        "vergangen": vergangen,
        "kommend": kommend,
    }


@pytest.fixture
def ablaeufe(bestand: dict[str, Any]) -> dict[str, str]:
    """Niederschrift im Entwurf für die kommende Sitzung, ein offener Umlauf; Anträge kommen aus der Demo."""
    from apps.session.models import SessionApplication, SessionCircularResolution, SessionOrganization, SessionProtocol

    tenant, vergangen, kommend = bestand["tenant"], bestand["vergangen"], bestand["kommend"]
    SessionProtocol.objects.create(
        meeting=kommend, status="draft", content="Der Vorsitz eröffnet die Sitzung.", created_by=bestand["konto"]
    )
    gremium = SessionOrganization.objects.filter(tenant=tenant, memberships__isnull=False).distinct().first()
    eingang = SessionApplication.objects.filter(tenant=tenant, status="submitted").first()
    umwandeln = SessionApplication.objects.filter(
        tenant=tenant, status__in=("submitted", "received", "in_review", "accepted")
    ).first()
    assert gremium is not None and eingang is not None and umwandeln is not None
    umlauf = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=gremium,
        reference="U/2026/0001",
        title="Zuschuss für den Sportverein",
        resolution_text="Der Zuschuss von 2.000 Euro wird gewährt.",
        deadline=timezone.localdate() + timedelta(days=7),
    )
    return {
        "niederschrift": _adresse("meeting_protocol", meeting_id=vergangen.pk),
        "niederschrift-bearbeiten": _adresse("meeting_protocol_edit", meeting_id=kommend.pk),
        "niederschrift-berichtigen": _adresse("meeting_protocol_correction", meeting_id=vergangen.pk),
        "beschlussregister": _adresse("resolutions"),
        "umlaufbeschluesse": _adresse("circulars"),
        "umlaufbeschluss": _adresse("circular_detail", circular_id=umlauf.pk),
        "antraege": _adresse("applications"),
        "antrag-bearbeiten": _adresse("application_process", application_id=eingang.pk),
        "antrag-umwandeln": _adresse("application_convert", application_id=umwandeln.pk),
        "ladung": _adresse("meeting_invitation", meeting_id=kommend.pk),
        "rueckmeldungen": _adresse("meeting_invitation_status", meeting_id=kommend.pk),
    }


def _durchlaufen(page: Any, server: Any, seiten: dict[str, str], screenshot: Any) -> None:
    for breite in BREITEN:
        page.set_viewport_size({"width": breite, "height": 900})
        for name, pfad in seiten.items():
            antwort = page.goto(f"{server.url}{pfad}")
            assert antwort is not None and antwort.ok, f"{name}: {antwort.status if antwort else 'keine Antwort'}"
            wait_for_bundle(page)
            expect(page.locator("main#inhalt")).to_have_attribute("data-gestaltung", "neu")
            masse = page.evaluate(UEBERLAUF)
            assert masse["scroll"] <= masse["breite"] + 1, f"{name} bei {breite} px läuft seitlich über: {masse}"
            if breite == 390:
                screenshot(f"session-neu-2b-{name}-390")


def test_ablaeufe_am_handy_und_tablet(
    page: Any, live_server: Any, ablaeufe: dict[str, str], problems: BrowserProblems, screenshot: Any
) -> None:
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    _durchlaufen(page, live_server, ablaeufe, screenshot)
    problems.assert_clean("Abläufe aus Teil 2b im neuen Rahmen")

    # Niederschrift bearbeiten am Handy: Die Speichern-Leiste liegt über der festen Leiste unten (#1018)
    page.set_viewport_size({"width": 390, "height": 700})
    page.goto(f"{live_server.url}{ablaeufe['niederschrift-bearbeiten']}")
    wait_for_bundle(page)
    speichern = page.get_by_role("button", name="Speichern", exact=True)
    speichern.scroll_into_view_if_needed()
    frei = speichern.evaluate(
        """(el) => {
            const r = el.getBoundingClientRect()
            const punkt = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
            return !!punkt && el.contains(punkt)
        }"""
    )
    assert frei, "„Speichern“ liegt unter der Leiste unten"


@pytest.fixture
def werkzeuge(bestand: dict[str, Any]) -> dict[str, str]:
    """Zwei Fassungen der Vorlage, eine Anlage mit zwei Dateiversionen, zwei Endgeräte und eine Leitstelle."""
    from apps.session.models import (
        SessionAgendaItem,
        SessionDevice,
        SessionFile,
        SessionPaper,
        SessionPaperVersion,
        SessionPerson,
        SessionTenantGroup,
        SessionTenantGroupMembership,
        SessionTenantGroupTenant,
    )
    from apps.session.services import file_version_service, paper_version_service

    tenant, konto, kommend = bestand["tenant"], bestand["konto"], bestand["kommend"]
    top = SessionAgendaItem.objects.filter(meeting=kommend).order_by("order").first()
    vorlage = SessionPaper.objects.filter(tenant=tenant).order_by("-created_at").first()
    person = SessionPerson.objects.filter(tenant=tenant).first()
    assert top is not None and vorlage is not None and person is not None
    paper_version_service.snapshot(vorlage, trigger=SessionPaperVersion.TRIGGER_MANUAL, user=konto, note="Erster Stand")
    vorlage.main_text = f"{vorlage.main_text or ''}\n\nErgänzt um die Kosten der Beleuchtung."
    vorlage.save()
    paper_version_service.snapshot(vorlage, trigger=SessionPaperVersion.TRIGGER_MANUAL, user=konto)
    anlage = SessionFile(
        tenant=tenant, name="Kostenaufstellung.txt", mime_type="text/plain", is_public=True, paper=vorlage
    )
    file_version_service.attach_upload(anlage, SimpleUploadedFile(anlage.name, b"Kosten A", "text/plain"), user=None)
    neu = SimpleUploadedFile(anlage.name, b"Kosten B", "text/plain")
    file_version_service.replace_content(anlage, neu, user=None, mime_type="text/plain", text_content="")
    SessionDevice.objects.create(tenant=tenant, label="iPad 1", inventory_number="INV-1", status="in_stock")
    SessionDevice.objects.create(
        tenant=tenant,
        label="iPad 2",
        inventory_number="INV-2",
        status="issued",
        issued_to=person,
        issued_at=timezone.now(),
    )
    # Leitstelle einer Gruppe nur mit dem Demo-Mandanten: Sie folgt dessen Schalter
    gruppe = SessionTenantGroup.objects.create(name="Demo-Kreis", slug="demo-kreis")
    SessionTenantGroupTenant.objects.create(group=gruppe, tenant=tenant)
    SessionTenantGroupMembership.objects.create(
        group=gruppe, user=bestand["nutzer"], role=SessionTenantGroupMembership.ROLE_LEITSTELLE
    )
    leitstelle = {"group_slug": gruppe.slug}
    return {
        "cockpit": _adresse("meeting_cockpit", meeting_id=kommend.pk),
        "abstimmung": _adresse("voting_capture", item_id=top.pk),
        "fassungen": _adresse("paper_versions", paper_id=vorlage.pk),
        "fassung": _adresse("paper_version_detail", paper_id=vorlage.pk, number=1),
        "fassungen-vergleich": _adresse("paper_version_compare", paper_id=vorlage.pk) + "?a=1&b=2",
        "dateiversionen": _adresse("file_versions", file_id=anlage.pk),
        "endgeraete": _adresse("devices"),
        "leitstelle": reverse("session:leitstelle", kwargs=leitstelle),
        "leitstelle-suche": reverse("session:leitstelle_search", kwargs=leitstelle) + "?q=Demo",
    }


def test_werkzeuge_am_handy_und_tablet(
    page: Any, live_server: Any, werkzeuge: dict[str, str], problems: BrowserProblems, screenshot: Any
) -> None:
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    _durchlaufen(page, live_server, werkzeuge, screenshot)
    problems.assert_clean("Cockpit, Fassungen, Endgeräte und Leitstelle aus Teil 2b im neuen Rahmen")


@pytest.fixture
def formulare_und_einstellungen(bestand: dict[str, Any]) -> dict[str, str]:
    """Formulare zum Anlegen und Bearbeiten und alle Einstellungen; die Daten kommen aus der Demo."""
    from apps.session.models import SessionAgendaItem, SessionBody, SessionOrganization, SessionPaper, SessionPerson

    tenant, kommend = bestand["tenant"], bestand["kommend"]
    top = SessionAgendaItem.objects.filter(meeting=kommend).order_by("order").first()
    gremium = SessionOrganization.objects.filter(tenant=tenant).first()
    person = SessionPerson.objects.filter(tenant=tenant).first()
    vorlage = SessionPaper.objects.filter(tenant=tenant).order_by("-created_at").first()
    koerperschaft = SessionBody.objects.filter(tenant=tenant).first()
    assert top is not None and gremium is not None and person is not None and vorlage is not None
    assert koerperschaft is not None
    return {
        "sitzung-neu": _adresse("meeting_create"),
        "sitzung-bearbeiten": _adresse("meeting_edit", meeting_id=kommend.pk),
        "vorlage-neu": _adresse("paper_create"),
        "vorlage-bearbeiten": _adresse("paper_edit", paper_id=vorlage.pk),
        "gremium-neu": _adresse("organization_create"),
        "gremium-bearbeiten": _adresse("organization_edit", organization_id=gremium.pk),
        "person-neu": _adresse("person_create"),
        "person-bearbeiten": _adresse("person_edit", person_id=person.pk),
        "top-bearbeiten": _adresse("agenda_item_edit", item_id=top.pk),
        "einstellungen": _adresse("settings"),
        "veroeffentlichung-beenden": _adresse("portal_publication_end"),
        "benutzer": _adresse("users"),
        "benutzer-einladen": _adresse("user_invite"),
        "rollen": _adresse("settings_roles"),
        "vier-augen": _adresse("settings_four_eyes"),
        "sitzungsformate": _adresse("settings_meeting_formats"),
        "ortsrecht": _adresse("settings_local_rules", body_id=koerperschaft.pk),
        "koerperschaften": _adresse("settings_bodies"),
        "koerperschaft-neu": _adresse("settings_body_create"),
        "koerperschaft-bearbeiten": _adresse("settings_body_edit", body_id=koerperschaft.pk),
        "wahlperioden": _adresse("terms"),
        "textbausteine": _adresse("settings_textblocks"),
        "nummernkreise": _adresse("settings_numbering"),
        "mitzeichnung": _adresse("settings_cosign"),
        "datenschutz": _adresse("privacy_settings"),
        "zugaenge": _adresse("settings_api_tokens"),
    }


def test_formulare_und_einstellungen_am_handy_und_tablet(
    page: Any,
    live_server: Any,
    formulare_und_einstellungen: dict[str, str],
    problems: BrowserProblems,
    screenshot: Any,
) -> None:
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    _durchlaufen(page, live_server, formulare_und_einstellungen, screenshot)
    problems.assert_clean("Formulare und Einstellungen aus Teil 2b im neuen Rahmen")


def test_reiter_der_einstellungen_am_handy(
    page: Any, live_server: Any, formulare_und_einstellungen: dict[str, str], problems: BrowserProblems
) -> None:
    """Am Handy: Ein Reiter führt zu seiner ersten Seite, die Unterpunkte zu den übrigen, ohne seitlichen Überlauf."""
    seiten = formulare_und_einstellungen
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server.url}{seiten['einstellungen']}")
    wait_for_bundle(page)
    reiter = page.get_by_role("navigation", name="Einstellungen")
    expect(reiter.get_by_role("link", name="Allgemein")).to_have_attribute("aria-current", "page")
    reiter.get_by_role("link", name="Sitzungen").click()
    wait_for_bundle(page)
    assert page.url.endswith(seiten["sitzungsformate"])
    reiter = page.get_by_role("navigation", name="Einstellungen")
    expect(reiter.get_by_role("link", name="Sitzungen")).to_have_attribute("aria-current", "page")
    unterpunkte = page.get_by_test_id("einstellungen-unterpunkte")
    expect(unterpunkte.get_by_role("link", name="Sitzungsformate")).to_have_attribute("aria-current", "page")
    unterpunkte.get_by_role("link", name="Wahlperioden").click()
    wait_for_bundle(page)
    assert page.url.endswith(seiten["wahlperioden"])
    masse = page.evaluate(UEBERLAUF)
    assert masse["scroll"] <= masse["breite"] + 1, masse
    problems.assert_clean("Reiter der Einstellungen")
