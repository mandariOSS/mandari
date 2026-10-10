# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes je Mandant (Issue #944).

Geprüft: Der Schalter ist für neue und bestehende Mandanten aus und ändert beim Umschalten nur sich selbst; ohne
Schalter bleibt jede Seite im bisherigen Rahmen. Mit Schalter nutzen alle Seiten den neuen Rahmen, Start, Sitzungen,
Sitzung, Vorlagen und Vorlage ihre neuen Vorlagen – mit allen Aktionen der bisherigen Seiten, aber nur mit den Rechten
wie bisher. Die Navigation zeigt jeden Bereich nur mit dem Recht seiner Seite; die Mandantentrennung bleibt.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import Any, cast

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session import rahmen
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)

pytestmark = pytest.mark.django_db

KERNSEITEN = {
    "start": "session/neu/start.html",
    "sitzungen": "session/neu/sitzungen.html",
    "sitzung": "session/neu/sitzung.html",
    "vorlagen": "session/neu/vorlagen.html",
    "vorlage": "session/neu/vorlage.html",
}


def _mandant(slug: str, *, neu: bool) -> SessionTenant:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug.title()}", slug=slug, session_new_design=neu)
    SessionRole.create_default_roles(tenant)
    return tenant


def _client(tenant: SessionTenant, rolle: str | None) -> Client:
    user = cast(Any, UserFactory)()
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    if rolle:
        session_user.roles.add(SessionRole.objects.get(tenant=tenant, name=rolle))
    client = Client()
    client.force_login(user)
    return client


def _welt(tenant: SessionTenant) -> dict[str, Any]:
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        organization=gremium,
        name="Ratssitzung",
        start=timezone.now() + timedelta(days=10),
        meeting_state="scheduled",
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant,
        name="Neue Brücke",
        reference="V/2026/001",
        paper_type="proposal",
        status="draft",
        main_organization=gremium,
        main_text="Sachverhalt der Brücke",
        resolution_text="Der Rat beschließt die Brücke.",
    )
    SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="Eröffnung")
    SessionAgendaItem.objects.create(meeting=sitzung, number="2", order=2, name="Neue Brücke", paper=vorlage)
    slug = tenant.slug
    return {
        "start": reverse("session:dashboard", kwargs={"tenant_slug": slug}),
        "sitzungen": reverse("session:meetings", kwargs={"tenant_slug": slug}),
        "sitzung": reverse("session:meeting_detail", kwargs={"tenant_slug": slug, "meeting_id": sitzung.pk}),
        "vorlagen": reverse("session:papers", kwargs={"tenant_slug": slug}),
        "vorlage": reverse("session:paper_detail", kwargs={"tenant_slug": slug, "paper_id": vorlage.pk}),
        "sitzung_obj": sitzung,
        "vorlage_obj": vorlage,
    }


def _vorlagen(antwort: Any) -> set[str]:
    return {t.name for t in antwort.templates if t.name}


# =============================================================================
# Schalter
# =============================================================================


def test_schalter_ist_standardmaessig_aus() -> None:
    tenant = SessionTenant.objects.create(name="Stadt", slug="stadt")
    assert tenant.session_new_design is False
    assert rahmen.neues_design(tenant) is False
    assert rahmen.neues_design(None) is False


def test_setzen_aendert_nur_den_schalter() -> None:
    tenant = _mandant("nord", neu=False)
    vorher: dict[str, Any] = dict(SessionTenant.objects.filter(pk=tenant.pk).values().get())
    assert rahmen.setzen(tenant, True) is True
    assert rahmen.setzen(tenant, True) is False
    nachher: dict[str, Any] = dict(SessionTenant.objects.filter(pk=tenant.pk).values().get())
    assert nachher.pop(rahmen.FELD) is True and vorher.pop(rahmen.FELD) is False
    assert nachher == vorher, "Umschalten darf nur den Schalter ändern (auch nicht updated_at)"


def test_verwaltungsbefehl() -> None:
    _mandant("nord", neu=False)
    _mandant("sued", neu=True)
    ausgabe = StringIO()
    call_command("session_neues_design", "status", stdout=ausgabe)
    assert "nord: neues Design aus" in ausgabe.getvalue() and "sued: neues Design an" in ausgabe.getvalue()

    call_command("session_neues_design", "an", "--mandant", "nord", stdout=StringIO())
    assert SessionTenant.objects.get(slug="nord").session_new_design is True
    call_command("session_neues_design", "aus", "--alle", stdout=StringIO())
    assert not SessionTenant.objects.filter(session_new_design=True).exists()

    with pytest.raises(CommandError):
        call_command("session_neues_design", "an", "--alle", stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("session_neues_design", "an", "--mandant", "gibt-es-nicht", stdout=StringIO())


# =============================================================================
# Rahmen und Seiten
# =============================================================================


def test_ohne_schalter_bleibt_der_bisherige_rahmen() -> None:
    tenant = _mandant("alt", neu=False)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, vorlage in KERNSEITEN.items():
        antwort = client.get(welt[name])
        assert antwort.status_code == 200, name
        html = antwort.content.decode()
        assert "session/base_session_alt.html" in _vorlagen(antwort), name
        assert vorlage not in _vorlagen(antwort), name
        assert 'data-rahmen="neu"' not in html and "session-sidebar" in html, name


def test_mit_schalter_neuer_rahmen_und_neue_kernseiten() -> None:
    tenant = _mandant("neu", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, vorlage in KERNSEITEN.items():
        antwort = client.get(welt[name])
        assert antwort.status_code == 200, name
        html = antwort.content.decode()
        assert {"session/base_session_neu.html", vorlage} <= _vorlagen(antwort), name
        assert 'data-rahmen="neu"' in html and 'data-gestaltung="neu"' in html, name
        assert "session-sidebar" not in html, name
    # Seiten ohne neue Gestaltung stehen im neuen Rahmen, ihr Inhalt wie bisher
    einstellungen = client.get(reverse("session:settings", kwargs={"tenant_slug": tenant.slug}))
    assert einstellungen.status_code == 200
    assert 'data-gestaltung="bisher"' in einstellungen.content.decode()


def test_alle_aktionen_der_sitzung_bleiben_erreichbar() -> None:
    tenant = _mandant("aktionen", neu=True)
    welt = _welt(tenant)
    sitzung = welt["sitzung_obj"]
    html = _client(tenant, "Administrator").get(welt["sitzung"]).content.decode()
    kw = {"tenant_slug": tenant.slug, "meeting_id": sitzung.pk}
    for adresse in (
        reverse("session:meeting_invitation", kwargs=kw),
        reverse("session:meeting_edit", kwargs=kw),
        reverse("session:meeting_cockpit", kwargs=kw),
        reverse("session:meeting_agenda_pdf", kwargs=kw),
        reverse("session:meeting_ics", kwargs=kw),
        reverse("session:resolutions_meeting_pdf", kwargs=kw),
        reverse("session:resolutions_generate", kwargs=kw),
        reverse("session:meeting_protocol", kwargs=kw),
        reverse("session:agenda_item_create", kwargs=kw),
        reverse("session:agenda_reorder", kwargs=kw),
        reverse("session:attendance_generate", kwargs=kw),
    ):
        assert adresse in html, adresse
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="1")
    for name in ("agenda_item_move", "agenda_item_edit", "agenda_item_withdraw", "agenda_item_delete"):
        assert reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, "item_id": top.pk}) in html, name
    assert 'data-tagesordnung-reihenfolge="' in html and html.count('data-testid="top"') == 2


def test_lesezugriff_sieht_keine_bearbeitung() -> None:
    tenant = _mandant("lesen", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Lesezugriff")
    sitzung = client.get(welt["sitzung"]).content.decode()
    kw = {"tenant_slug": tenant.slug, "meeting_id": welt["sitzung_obj"].pk}
    assert reverse("session:meeting_edit", kwargs=kw) not in sitzung
    assert reverse("session:agenda_item_create", kwargs=kw) not in sitzung
    assert "data-tagesordnung-reihenfolge" not in sitzung
    assert "Live-Ansicht" in sitzung
    vorlage = client.get(welt["vorlage"]).content.decode()
    assert "Zur Freigabe vorlegen" not in vorlage and "Bearbeiten" not in vorlage
    assert "Neue Sitzung" not in client.get(welt["sitzungen"]).content.decode()


def test_freigabelauf_der_vorlage() -> None:
    tenant = _mandant("freigabe", neu=True)
    welt = _welt(tenant)
    vorlage = welt["vorlage_obj"]
    client = _client(tenant, "Administrator")
    kw = {"tenant_slug": tenant.slug, "paper_id": vorlage.pk}
    html = client.get(welt["vorlage"]).content.decode()
    assert reverse("session:paper_workflow", kwargs={**kw, "action": "submit"}) in html
    assert reverse("session:paper_edit", kwargs=kw) in html
    assert "Sachverhalt der Brücke" in html and "Der Rat beschließt die Brücke." in html
    SessionPaper.objects.filter(pk=vorlage.pk).update(status="review")
    html = client.get(welt["vorlage"]).content.decode()
    assert reverse("session:paper_workflow", kwargs={**kw, "action": "reject"}) in html


def test_mandantentrennung_bleibt() -> None:
    eigen = _mandant("eigen", neu=True)
    fremd = _mandant("fremd", neu=True)
    welt = _welt(fremd)
    client = _client(eigen, "Administrator")
    for name in KERNSEITEN:
        assert client.get(welt[name]).status_code in (403, 404), name


def test_navigation_nach_rechten() -> None:
    tenant = _mandant("nav", neu=True)
    voll = rahmen.navigation(tenant, {"view_meetings", "view_papers", "view_applications"}, "meetings")
    assert [b["key"] for b in voll["bereiche"]] == [
        "start", "sitzungen", "vorlagen", "antraege", "beschluesse", "gremien", "personen",
    ]  # fmt: skip
    assert [b["key"] for b in voll["leiste_unten"]] == ["start", "sitzungen", "vorlagen", "beschluesse"]
    assert [r["key"] for r in voll["reiter"]] == ["liste", "kalender", "jahresplanung", "archiv"]
    assert [k["label"] for k in voll["brotkrumen"]] == ["Stadt Nav", "Sitzungen"]
    assert voll["brotkrumen"][-1]["aktuell"] is True

    # Kontrollrolle: nur Start und das Audit-Log unter „Verwaltung“
    kontrolle = rahmen.navigation(tenant, {"view_dashboard", "view_audit_log"}, "audit_log")
    assert [b["key"] for b in kontrolle["bereiche"]] == ["start"]
    verwaltung = next(e for e in kontrolle["unten"] if e["key"] == "verwaltung")
    assert [u["key"] for u in verwaltung["unterpunkte"]] == ["audit"] and verwaltung["offen"] is True
    assert [k["label"] for k in kontrolle["brotkrumen"]] == ["Stadt Nav", "Verwaltung", "Audit-Log"]

    # Nur das Prüfrecht: „Vorlagen“ führt auf „Zu prüfen“, mit der Zahl der offenen Arbeit
    pruefen = rahmen.navigation(tenant, {"approve_papers"}, "dashboard", zaehler={"papers_review_count": 3})
    vorlagen = next(b for b in pruefen["bereiche"] if b["key"] == "vorlagen")
    assert vorlagen["url"].endswith("/papers/review/") and vorlagen["zahl"] == 3
    assert not any(e["key"] == "einstellungen" for e in pruefen["unten"])


def test_detailseiten_ohne_reiter_mit_seitentitel_als_krume() -> None:
    tenant = _mandant("krumen", neu=True)
    nav = rahmen.navigation(tenant, {"view_meetings"}, "meeting_detail")
    assert nav["reiter"] == []
    assert [k["label"] for k in nav["brotkrumen"]] == ["Stadt Krumen", "Sitzungen"]
    assert nav["brotkrumen"][-1]["aktuell"] is False
    welt = _welt(tenant)
    html = _client(tenant, "Administrator").get(welt["sitzung"]).content.decode()
    assert 'aria-current="page"' in html and "Ratssitzung, " in html


def test_seitenleiste_nur_mit_erreichbaren_links() -> None:
    tenant = _mandant("links", neu=True)
    _welt(tenant)
    for rolle in ("Administrator", "Sachbearbeiter", "Lesezugriff", "Revision", None):
        client = _client(tenant, rolle)
        start = client.get(reverse("session:dashboard", kwargs={"tenant_slug": tenant.slug}))
        if start.status_code != 200:
            continue
        html = start.content.decode()
        leiste = html[html.index('id="session-navigation"') : html.index("</aside>")]
        basis = f"/session/{tenant.slug}/"
        links = {teil.split('"', 1)[0] for teil in leiste.split('href="')[1:]}
        tot = [h for h in sorted(links) if h.startswith(basis) and client.get(h).status_code != 200]
        assert not tot, f"{rolle}: {tot}"


def test_eintrag_der_seitenleiste_mit_zahl() -> None:
    """c-rahmen.nav-link: ``zahl`` zeigt offene Arbeit; ohne Angabe bleibt das Markup wie in Work und Insight."""
    from django.template import engines
    from django_cotton.compiler_regex import CottonCompiler

    def render(source: str) -> str:
        return engines["django"].from_string(CottonCompiler().process(source)).render({})

    mit = render('<c-rahmen.nav-link href="/v/" icon="file-text" zahl="3" dicht>Vorlagen</c-rahmen.nav-link>')
    assert '3<span class="sr-only"> offen</span>' in mit and "rahmen-label shrink-0" in mit
    ohne = render('<c-rahmen.nav-link href="/v/" icon="file-text" dicht>Vorlagen</c-rahmen.nav-link>')
    assert " offen</span>" not in ohne and "tabular-nums" not in ohne
