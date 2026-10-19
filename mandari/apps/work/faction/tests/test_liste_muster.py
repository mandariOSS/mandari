# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Liste der Fraktionssitzungen nach dem Muster (Issue #1071, Schritt 1): Nebenspalte mit Sitzungsreihe, Automatik und
Pausen, Stand je Zeile, „Vergangene“ unter den kommenden Sitzungen, Verwaltungslinks nur für den Vorsitz.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Organization
from apps.work.faction import liste
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def welt(org: Organization, make_member: Any) -> dict[str, Any]:
    Organization.objects.filter(pk=org.pk).update(work_new_design=True)
    settings = dict(org.settings or {})
    settings["faction"] = {
        "agenda_reminder_enabled": True,
        "agenda_reminder_hours": 54,
        "protocol_dispatch": "after_approval",
        "protocol_dispatch_delay_hours": 0,
    }
    Organization.objects.filter(pk=org.pk).update(settings=settings)
    org.refresh_from_db()
    vorsitz = make_member(org, [], email="vera@example.org", is_admin=True)
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    mitglied = make_member(org, ["faction.view_public"], email="martin@example.org")
    reihe = FactionMeetingSchedule.objects.create(
        organization=org,
        name="Wöchentliche Fraktionssitzung",
        recurrence="weekly",
        weekday=0,
        time=time(18, 0),
        duration_minutes=120,
        default_location="Fraktionsbüro",
        default_video_link="https://video.example.org/fraktion",
        auto_invite=True,
        auto_invite_weekday=4,
        auto_invite_time=time(18, 0),
    )
    heute = timezone.localdate()
    FactionMeetingException.objects.create(
        schedule=reihe,
        original_date=heute + timedelta(days=60),
        end_date=heute + timedelta(days=74),
        exception_type="cancelled",
        reason="Weihnachten",
    )
    jetzt = timezone.now()
    kommend = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 24",
        start=jetzt + timedelta(days=3),
        status="invited",
        invitation_sent=True,
        invitation_sent_at=jetzt - timedelta(hours=1),
        location="Fraktionsbüro",
        created_by=vorsitz,
    )
    FactionAgendaItem.objects.create(meeting=kommend, title="Haushalt", visibility="public", order=1)
    FactionAgendaItem.objects.create(meeting=kommend, title="Personal", visibility="internal", order=2)
    FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=jetzt + timedelta(days=10),
        status="cancelled",
        cancellation_reason="Ratssitzung am selben Abend",
        location="Fraktionsbüro",
        created_by=vorsitz,
    )
    FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 23",
        start=jetzt - timedelta(days=4),
        status="completed",
        protocol_status="pending",
        location="Fraktionsbüro",
        created_by=vorsitz,
    )
    return {"org": org, "vorsitz": vorsitz, "mitglied": mitglied}


def _seite(welt: dict[str, Any], client_for: Any, rolle: str, **anfrage: str) -> str:
    antwort = client_for(welt[rolle].user).get(reverse("work:faction", kwargs={"org_slug": welt["org"].slug}), anfrage)
    assert antwort.status_code == 200
    assert "work/faction/neu/sitzungen.html" in [t.name for t in antwort.templates]
    return str(antwort.content.decode())


def test_vorsitz_sieht_reihe_automatik_und_verwaltung(welt: dict[str, Any], client_for: Any) -> None:
    html = _seite(welt, client_for, "vorsitz")
    slug = welt["org"].slug
    assert 'class="fs-neben fs-liste-neben"' in html
    assert "Wöchentlich montags von 18:00 bis 20:00, Fraktionsbüro, auch per Video." in html
    assert "Work legt die Termine 90 Tage im Voraus an." in html
    assert "Pause: Weihnachten, " in html
    assert "Gelöschte Termine legt Work nicht wieder an" in html
    # Automatik: TOP-Erinnerung 54 Stunden vor Freitag 18:00 = Mittwoch 12:00, fester Einladungszeitpunkt, Protokoll
    assert "<b>Mittwoch 12:00</b><span>Erinnerung zum Eintragen von TOPs</span>" in html
    assert "<b>Freitag 18:00</b>" in html and "fester Zeitpunkt, für diese Reihe eingeschaltet" in html
    assert "<b>Nach der Genehmigung</b><span>Protokoll per E-Mail</span>" in html
    einstellungen = reverse("work:organization_faction_settings", kwargs={"org_slug": slug})
    assert f'href="{einstellungen}#reihen-titel">Sitzungsreihe bearbeiten' in html
    assert "Fraktionssitzung planen" in html
    assert reverse("work:faction_audit", kwargs={"org_slug": slug}) in html


def test_mitglied_ohne_verwaltungsaktionen(welt: dict[str, Any], client_for: Any) -> None:
    html = _seite(welt, client_for, "mitglied")
    assert "Wöchentlich montags" in html and "Erinnerung zum Eintragen von TOPs" in html
    assert "Sitzungsreihe bearbeiten" not in html
    assert "Einstellungen für Fraktionssitzungen" not in html
    assert "Fraktionssitzung planen" not in html and 'x-show="showCreateModal"' not in html
    assert "Teilnahmenachweis" in html


def test_stand_je_zeile_und_vergangene(welt: dict[str, Any], client_for: Any) -> None:
    html = _seite(welt, client_for, "vorsitz")
    assert html.index('data-testid="fraktionssitzungen"') < html.index('data-testid="fraktion-vergangene"')
    assert "<span>Eingeladen</span>" in html
    assert '<li class="entfaellt hervorheben">' in html and "<small>Ratssitzung am selben Abend</small>" in html
    assert "<small>Protokoll zur Genehmigung</small>" in html
    assert "Ältere Sitzungen anzeigen" in html
    # Vereidigter Vorsitz sieht die Zahl der nichtöffentlichen TOPs, das Mitglied nur die öffentlichen
    assert "2 TOPs, 1 nichtöffentlich" in html
    mitglied = _seite(welt, client_for, "mitglied")
    assert "1 TOP<" in mitglied and "nichtöffentlich" not in mitglied
    # Mit Zeitraum „Vergangene“ keine zweite Gruppe
    assert 'data-testid="fraktion-vergangene"' not in _seite(welt, client_for, "vorsitz", time="past")


def test_satz_im_kopfband(welt: dict[str, Any], client_for: Any) -> None:
    html = _seite(welt, client_for, "vorsitz")
    assert "Die nächste Fraktionssitzung ist am " in html and " eingeladen." in html
    assert "Offene Protokolle: 1." in html


def test_ohne_reihe_nur_vorsitz_sieht_den_weg_zur_reihe(welt: dict[str, Any], client_for: Any) -> None:
    FactionMeetingSchedule.objects.all().delete()
    assert "Sitzungsreihe anlegen" in _seite(welt, client_for, "vorsitz")
    mitglied = _seite(welt, client_for, "mitglied")
    assert 'data-testid="fraktion-reihe"' not in mitglied
    # Ohne feste Reihe: Einladung nach dem Vorlauf der Organisation
    assert "<b>3 Tage vor Beginn</b>" in mitglied


def test_wochenzeit_rechnet_ueber_die_woche() -> None:
    assert liste._wochenzeit_minus(0, time(6, 0), 12) == "Sonntag 18:00"
    assert liste._wochenzeit_minus(4, time(18, 0), 54) == "Mittwoch 12:00"


def test_einladung_ueberfaellig(welt: dict[str, Any], client_for: Any) -> None:
    """Versandzeit vorbei, nichts verschickt: nicht mehr „Einladung automatisch am …“."""
    FactionMeeting.objects.create(
        organization=welt["org"],
        title="Kurzfristig",
        start=timezone.now() + timedelta(hours=5),
        status="planned",
        created_by=welt["vorsitz"],
    )
    html = _seite(welt, client_for, "vorsitz")
    assert "<small>Einladung überfällig</small>" in html
