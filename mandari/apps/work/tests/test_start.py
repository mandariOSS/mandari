# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Startseite im neuen Rahmen (Issue #852): nächste Sitzungen mit Vorbereitungsstand, „Für Sie“, „Neu in Ihren
Gremien“, ein Satz mit dem Stand und höchstens eine Hauptaktion, keine Zählerkacheln. Ohne Schalter bleibt der
bisherige Start; Daten anderer Organisationen erscheinen nicht.
"""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests import factories
from apps.tenants.models import Membership, Organization
from apps.work.dashboard import selectors
from apps.work.dashboard.views import DashboardView
from apps.work.faction.models import FactionMeeting
from apps.work.meetings.models import AgendaItemPosition
from apps.work.motions.models import Motion, MotionApproval
from apps.work.tasks.models import Task
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)

RIS = "https://ris.start.example/oparl"


def _kennung(art: str) -> str:
    return f"{RIS}/{art}/{uuid.uuid4()}"


@dataclass
class Welt:
    org: Organization
    vorsitz: Membership
    ausschuss: OParlOrganization
    andere_gremium: OParlOrganization
    sitzung: OParlMeeting
    top_vorlage: OParlAgendaItem
    top_vorlage2: OParlAgendaItem
    vorlage: OParlPaper
    fremde_sitzung: OParlMeeting


def _beratung(body: OParlBody, sitzung: OParlMeeting, top: OParlAgendaItem, vorlage: OParlPaper) -> None:
    OParlConsultation.objects.create(
        external_id=_kennung("consultations"),
        body=body,
        paper=vorlage,
        paper_external_id=vorlage.external_id,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=top.external_id,
    )


@pytest.fixture
def welt(db: Any, org: Organization, make_member: Any) -> Welt:
    source = OParlSource.objects.create(name="Start-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Musterstadt")
    org.body = body
    org.work_new_design = True
    org.save(update_fields=["body", "work_new_design"])
    ausschuss = OParlOrganization.objects.create(external_id=_kennung("org"), body=body, name="Hauptausschuss")
    andere = OParlOrganization.objects.create(external_id=_kennung("org"), body=body, name="Jugendhilfeausschuss")
    jetzt = timezone.now()
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=body, name="Sitzung", start=jetzt + timedelta(days=5)
    )
    sitzung.organizations.add(ausschuss)
    fremde = OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=body, name="Sitzung", start=jetzt + timedelta(days=6)
    )
    fremde.organizations.add(andere)
    eroeffnung = OParlAgendaItem.objects.create(
        external_id=_kennung("items"), meeting=sitzung, number="1", order=1, name="Eröffnung"
    )
    top_a = OParlAgendaItem.objects.create(external_id=_kennung("items"), meeting=sitzung, number="2", order=2)
    top_b = OParlAgendaItem.objects.create(external_id=_kennung("items"), meeting=sitzung, number="3", order=3)
    vorlage = OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name="Feuerwehrbedarfsplan", reference="V/1", date=date.today()
    )
    zweite = OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name="Radweg", reference="V/2", date=date.today()
    )
    _beratung(body, sitzung, top_a, vorlage)
    _beratung(body, sitzung, top_b, zweite)
    fremde_vorlage = OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name="Jugendbeirat", reference="V/3", date=date.today()
    )
    fremder_top = OParlAgendaItem.objects.create(external_id=_kennung("items"), meeting=fremde, number="1", order=1)
    _beratung(body, fremde, fremder_top, fremde_vorlage)
    del eroeffnung
    vorsitz = make_member(org, [], email="vorsitz@example.org", is_admin=True)
    vorsitz.oparl_committees.add(ausschuss)
    return Welt(org, vorsitz, ausschuss, andere, sitzung, top_a, top_b, vorlage, fremde)


def _start(client_for: Any, membership: Membership, query: str = "") -> str:
    antwort = client_for(membership.user).get(
        reverse("work:dashboard", kwargs={"org_slug": membership.organization.slug}) + query
    )
    assert antwort.status_code == 200
    return str(antwort.content.decode())


# ---- Seite -----------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_ohne_schalter_bleibt_der_bisherige_start(welt: Welt, client_for: Any) -> None:
    welt.org.work_new_design = False
    welt.org.save(update_fields=["work_new_design"])
    html = _start(client_for, welt.vorsitz)
    assert "Kommende Sitzungen" in html and "Für Sie" not in html


@pytest.mark.django_db
def test_start_zeigt_stand_fuer_sie_und_neue_vorlagen(welt: Welt, client_for: Any) -> None:
    AgendaItemPosition.objects.create(organization=welt.org, agenda_item=welt.top_vorlage, position="for")
    AgendaItemPosition.objects.create(organization=welt.org, agenda_item=welt.top_vorlage2, position="open")
    Task.objects.create(
        organization=welt.org,
        title="Redebeitrag vorbereiten",
        assigned_to=welt.vorsitz,
        created_by=welt.vorsitz,
        priority="high",
        due_date=date.today() + timedelta(days=2),
    )
    FactionMeeting.objects.create(
        organization=welt.org, title="Fraktionssitzung", start=timezone.now() + timedelta(days=2)
    )

    html = _start(client_for, welt.vorsitz)
    assert "Nächste Sitzungen" in html and "Für Sie" in html and "Neu in Ihren Gremien" in html
    # Vorbereitungsstand: nur TOPs mit Vorlage zählen, „Noch offen“ ist keine Position
    assert "Positionen: 1 von 2 Vorlagen" in html
    assert "3 TOPs" in html
    assert "Geplant, Einladung noch nicht versandt" in html or "Entwurf, Einladung noch nicht versandt" in html
    # ein Satz mit dem Stand und genau eine Hauptaktion
    assert "Hauptausschuss am" in html and "1 von 2 Vorlagen mit Position" in html
    prepare = reverse("work:meeting_prepare", kwargs={"org_slug": welt.org.slug, "meeting_id": welt.sitzung.id})
    assert html.count(f'href="{prepare}"') == 2  # Hauptaktion und „Vorbereiten“ in der Liste
    assert "Redebeitrag vorbereiten" in html and "Priorität hoch" in html
    # neue Vorlagen nur aus den eigenen Gremien
    assert "Feuerwehrbedarfsplan" in html
    assert "Jugendbeirat" not in html
    # keine Zählerkacheln, kein Gruß in Riesenschrift
    assert "Hallo," not in html and "text-4xl" not in html


@pytest.mark.django_db
def test_mitglied_ohne_vorbereitungsrecht_bekommt_keinen_vorbereiten_link(
    welt: Welt, client_for: Any, make_member: Any
) -> None:
    """Parteimitglied (Standardrolle ohne „meetings.prepare“): Start ohne Link in die Vorbereitung (dort 403)."""
    from apps.common.permissions import DEFAULT_ROLES

    rechte = cast(list[str], DEFAULT_ROLES["party_member"]["permissions"])
    assert "meetings.prepare" not in rechte and "meetings.view" in rechte
    mitglied = make_member(welt.org, rechte, email="parteimitglied@example.org")
    mitglied.oparl_committees.add(welt.ausschuss)

    html = _start(client_for, mitglied)
    kwargs = {"org_slug": welt.org.slug, "meeting_id": welt.sitzung.id}
    assert reverse("work:meeting_prepare", kwargs=kwargs) not in html
    assert re.search(r"Sitzung am [^<]*vorbereiten", html) is None
    assert re.search(r">\s*Vorbereiten<", html) is None
    detail = reverse("work:meeting_detail", kwargs=kwargs)
    # Titel, Zeilenlink „Öffnen“ und Hauptaktion „Sitzung am … öffnen“ zeigen auf die Sitzung
    assert html.count(f'href="{detail}"') == 3
    assert "0 von 2 Vorlagen mit Position" in html
    satz, aktion = selectors.satz_und_hauptaktion(
        welt.org,
        [{"type": "ris", "id": welt.sitzung.id, "title": "Hauptausschuss", "start": welt.sitzung.start}],
        {welt.sitzung.id: selectors.Stand(tops=3, vorlagen=2, positionen=0)},
        darf_vorbereiten=False,
    )
    assert aktion is not None and aktion["url"] == detail and aktion["label"].endswith("öffnen")
    assert client_for(mitglied.user).get(detail).status_code == 200


@pytest.mark.django_db
def test_alle_anzeigen_nimmt_alle_gremien(welt: Welt, client_for: Any) -> None:
    html = _start(client_for, welt.vorsitz, "?alle=1")
    assert "Neue Vorlagen" in html and "Jugendbeirat" in html
    assert "Nur Ihre Gremien" in html


@pytest.mark.django_db
def test_fuer_sie_nur_eigene_organisation_und_sichtbare_dokumente(welt: Welt, make_member: Any) -> None:
    andere = cast(Organization, cast(Any, factories.OrganizationFactory)(name="Andere Fraktion"))
    fremdes_mitglied = make_member(andere, [], email="fremd@example.org", is_admin=True)
    Task.objects.create(organization=andere, title="Fremde Aufgabe", assigned_to=fremdes_mitglied)
    eigenes = Motion.objects.create(organization=welt.org, author=welt.vorsitz, title="Eigener Entwurf", status="draft")
    zur_freigabe = Motion.objects.create(
        organization=welt.org,
        author=welt.vorsitz,
        title="Stellungnahme",
        status="internal_review",
        visibility="organization",
    )
    MotionApproval.objects.create(motion=zur_freigabe, approver=welt.vorsitz, approval_type="chair")
    Motion.objects.create(organization=welt.org, author=welt.vorsitz, title="Eingereicht", status="submitted")

    eintraege = selectors.fuer_sie(welt.org, welt.vorsitz)
    titel = [e["titel"] for e in eintraege]
    assert titel[0] == "Stellungnahme"  # Freigaben zuerst, kein doppelter Eintrag als eigenes Dokument
    assert "Eigener Entwurf" in titel
    assert "Eingereicht" not in titel and "Fremde Aufgabe" not in titel
    assert titel.count("Stellungnahme") == 1
    assert eigenes.title in titel


# ---- Hinweisband (Issue #857) ----------------------------------------------------------------------------------

#: Stellvertreter für work/partials/hinweisband.html: Die Vorlage und der Kontext „hinweis“ kommen mit #857;
#: geprüft wird hier, dass der neue Start das Band an derselben Stelle wie der bisherige einbindet.
BAND = '<section data-testid="hinweisband">{{ hinweis.titel }}</section>'


@pytest.fixture
def band_vorlage(settings: Any) -> None:
    vorlagen = copy.deepcopy(settings.TEMPLATES)
    optionen = vorlagen[0].setdefault("OPTIONS", {})
    optionen["loaders"] = [
        ("django.template.loaders.locmem.Loader", {"work/partials/hinweisband.html": BAND}),
        *optionen["loaders"],
    ]
    settings.TEMPLATES = vorlagen


def _mit_hinweis(monkeypatch: pytest.MonkeyPatch, hinweis: dict[str, str] | None) -> None:
    bisher: Any = DashboardView.get_context_data

    def kontext(self: DashboardView, **kwargs: Any) -> dict[str, Any]:
        daten: dict[str, Any] = bisher(self, **kwargs)
        daten["hinweis"] = hinweis
        return daten

    monkeypatch.setattr(DashboardView, "get_context_data", kontext)


@pytest.mark.django_db
@pytest.mark.usefixtures("band_vorlage")
def test_start_bindet_das_hinweisband_oben_ein(welt: Welt, client_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _mit_hinweis(monkeypatch, {"titel": "Neue Navigation in Work"})
    html = _start(client_for, welt.vorsitz)
    assert html.count('data-testid="hinweisband"') == 1
    assert "Neue Navigation in Work" in html
    assert html.index('data-testid="hinweisband"') < html.index('id="start-sitzungen"')


@pytest.mark.django_db
@pytest.mark.usefixtures("band_vorlage")
def test_start_ohne_hinweis_ohne_band(welt: Welt, client_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _mit_hinweis(monkeypatch, None)
    assert 'data-testid="hinweisband"' not in _start(client_for, welt.vorsitz)


# ---- Hilfsfunktionen -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sitzung", "text"),
    [
        (
            {"status": "planned", "status_display": "Geplant", "invitation_sent": False},
            "Geplant, Einladung noch nicht versandt",
        ),
        ({"status": "invited", "status_display": "Eingeladen", "invitation_sent": True}, "Eingeladen"),
        ({"status": "planned", "status_display": "Geplant", "invitation_sent": True}, "Geplant, Einladung versandt"),
        ({"status": "ongoing", "status_display": "Läuft", "invitation_sent": False}, "Läuft"),
    ],
)
def test_stand_fraktionssitzung(sitzung: dict[str, Any], text: str) -> None:
    assert selectors.stand_fraktionssitzung(sitzung) == text


def test_kurzdatum() -> None:
    assert selectors.kurzdatum(date(2026, 10, 14)) == "Mi 14.10."
    assert selectors.kurzdatum(None) == ""
    assert selectors.kurzdatum(timezone.make_aware(datetime(2026, 10, 19, 17, 0))) == "Mo 19.10."


def test_satz_ohne_sitzungen_ohne_aktion() -> None:
    class Org:
        slug = "x"

    satz, aktion = selectors.satz_und_hauptaktion(Org(), [], {})
    assert satz == "In den nächsten Wochen stehen keine Sitzungen an." and aktion is None
