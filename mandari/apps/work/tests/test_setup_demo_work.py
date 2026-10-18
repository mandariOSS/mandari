# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Inhalte für das neue Work-Design (``setup_demo_work``, Issue #884).

Die öffentliche Demo baut jede Nacht mit ``setup_demo_environment`` neu auf; der Work-Teil ruft ``setup_demo_work``
auf. Danach muss die Demo jeden Punkt des Work-Updates zeigen können: Fraktionssitzung mit vereidigtem Vorsitz,
öffentlichen und nichtöffentlichen TOPs, Standard-Tagesordnung, Sitzungsreihe, Positionen mit Beratungsverlauf,
Antrag mit Kommentaren und Änderungsanträgen – und das neue Design ist für die Demo-Organisation an. Die Seiten
dazu antworten für jede Rolle ohne Serverfehler; wer nicht vereidigt ist, sieht keine nichtöffentlichen TOPs.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import CommandError, call_command
from django.db.models import Model
from django.test import Client
from django.utils import timezone

from apps.common.demo_daten import DEMO_ORG_SLUG, DEMO_USERS
from apps.common.demo_daten import demo_ext as _ext
from apps.common.tests.factories import MembershipFactory, OrganizationFactory
from apps.tenants.models import Membership, Organization
from apps.work.faction.models import FactionProtocolEntry
from apps.work.management.commands import setup_demo_work as demo
from apps.work.models import (
    AgendaItemPosition,
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
    FactionStandardAgendaItem,
    Motion,
    MotionComment,
)
from apps.work.rahmen import neues_design
from insight_core.models import OParlAgendaItem, OParlMeeting

pytestmark = pytest.mark.django_db

PASSWORT = "Demo-Pruefung-Passwort-1"
#: Work-Konten der Demo in der Reihenfolge Vorsitz, Mitglied, Sachkundige, nicht vereidigt, Gast
ROLLEN = ("vorsitz", "mitglied", "sachkundig", "unvereidigt", "gast")


@pytest.fixture
def demo_aufgebaut(settings: Any, tmp_path: Path) -> Organization:
    settings.MEDIA_ROOT = str(tmp_path / "media")
    call_command("setup_demo_environment", stdout=StringIO())
    return Organization.objects.get(slug=DEMO_ORG_SLUG)


def _mitglied(org: Organization, rolle: str) -> Membership:
    return Membership.objects.get(organization=org, user__email=DEMO_USERS[rolle]["email"])


def test_ohne_basisdemo_bricht_der_befehl_ab() -> None:
    with pytest.raises(CommandError, match="setup_demo_environment"):
        call_command("setup_demo_work", stdout=StringIO())


class TestDemoZeigtAllePunkte:
    def test_rollen_und_vereidigung(self, demo_aufgebaut: Organization) -> None:
        org = demo_aufgebaut
        stand = {rolle: _mitglied(org, rolle) for rolle in ROLLEN}
        assert stand["vorsitz"].is_sworn_in and stand["mitglied"].is_sworn_in and stand["sachkundig"].is_sworn_in
        assert not stand["unvereidigt"].is_sworn_in
        assert stand["gast"].is_guest and not stand["gast"].roles.exists()
        assert [r.name for r in stand["sachkundig"].roles.all()] == ["Sachkundige/r Bürger/in"]
        assert [r.name for r in stand["unvereidigt"].roles.all()] == ["Fraktionsmitglied"]

    def test_fraktionssitzungen(self, demo_aufgebaut: Organization) -> None:
        org = demo_aufgebaut
        standard = list(FactionStandardAgendaItem.objects.filter(organization=org).values_list("title", "visibility"))
        assert standard == demo.STANDARD_TAGESORDNUNG

        vorige = FactionMeeting.objects.get(organization=org, title=demo.VERGANGENE_SITZUNG)
        assert (vorige.status, vorige.protocol_status) == ("completed", "pending")
        anwesenheit = dict(
            FactionAttendance.objects.filter(meeting=vorige).values_list(
                "membership__user__email", "participation_type"
            )
        )
        assert anwesenheit[DEMO_USERS["mitglied"]["email"]] == "online"
        assert anwesenheit[DEMO_USERS["vorsitz"]["email"]] == "onsite"
        # Keine Wortbeiträge (Entscheidung zum Work-Update), dafür Beschluss, Notiz und Aufgabe
        arten = set(FactionProtocolEntry.objects.filter(meeting=vorige).values_list("entry_type", flat=True))
        assert arten == {"decision", "note", "action"}

        sitzung = FactionMeeting.objects.get(organization=org, title=demo.KOMMENDE_SITZUNG)
        assert sitzung.previous_meeting_id == vorige.pk
        assert sitzung.location and sitzung.video_link
        assert sitzung.rsvp_enabled is False
        tops = list(sitzung.agenda_items.filter(proposal_status="active").order_by("order", "number"))
        nummern = {top.title: top.number for top in tops}
        assert nummern["Tagesordnung festlegen und letztes Protokoll genehmigen"] == "1"
        assert nummern["Politische Arbeit"] == "3"
        assert nummern["Ratssitzung: Antrag Trinkwasserbrunnen"] == "3.1"
        assert nummern["Ratssitzung: Feuerwehrbedarfsplan 2026–2031"] == "3.2"
        assert nummern["Personal- und Finanzangelegenheiten der Fraktion"] == "NÖ 1"
        assert nummern["Grundstücksangelegenheit Am Stadtpark"] == "NÖ 2"
        assert sum(1 for top in tops if top.standard_item_id) == len(demo.STANDARD_TAGESORDNUNG)
        feuerwehr = next(top for top in tops if top.number == "3.2")
        assert feuerwehr.related_papers.exists() and feuerwehr.related_motions.exists()
        assert feuerwehr.related_agenda_item is not None
        vorschlag = sitzung.agenda_items.get(proposal_status="proposed")
        assert vorschlag.proposed_by == _mitglied(org, "sachkundig")

    def test_sitzungsreihe(self, demo_aufgebaut: Organization) -> None:
        org = demo_aufgebaut
        reihe = FactionMeetingSchedule.objects.get(organization=org, name=demo.REIHE)
        assert (reihe.weekday, reihe.time.hour, reihe.rsvp_enabled, reihe.auto_invite) == (0, 18, False, False)
        termine = FactionMeeting.objects.filter(schedule=reihe)
        geplant = termine.exclude(status="cancelled")
        assert geplant.count() >= 8
        assert all(t.agenda_items.filter(standard_item__isnull=False).count() == 6 for t in geplant)
        pause = FactionMeetingException.objects.get(schedule=reihe, reason=demo.PAUSE)
        assert termine.filter(status="cancelled", scheduled_date=pause.original_date).exists()

    def test_positionen_mit_beratungsverlauf(self, demo_aufgebaut: Organization) -> None:
        org = demo_aufgebaut
        rat_feuerwehr = OParlAgendaItem.objects.get(external_id=_ext("agendaitem", "rat-3-4"))
        verlauf = cast(Any, AgendaItemPosition).get_cross_positions_for_items(org, [rat_feuerwehr])[rat_feuerwehr.id]
        assert [(e["gremium"], e["position"], e["is_final"]) for e in verlauf] == [("Hauptausschuss", "amended", True)]
        assert "Änderungsantrag" in verlauf[0]["reasoning"]
        ergebnisse = set(AgendaItemPosition.objects.filter(organization=org).values_list("outcome", flat=True))
        assert {"referred", "accepted"} <= ergebnisse

    def test_antrag_mit_kommentaren_und_aenderungsantraegen(self, demo_aufgebaut: Organization) -> None:
        org = demo_aufgebaut
        entwurf = Motion.objects.get(organization=org, title=demo.ANTRAG_ENTWURF)
        marken = set(re.findall(r'data-comment-id="([0-9a-f-]{36})"', entwurf.content))
        kommentare = MotionComment.objects.filter(motion=entwurf)
        assert marken == {str(k.mark_id) for k in kommentare.filter(mark_id__isnull=False)}
        assert kommentare.filter(is_resolved=True).count() == 1
        assert kommentare.filter(parent__isnull=False).count() == 1
        assert kommentare.filter(mark_id__isnull=True, parent__isnull=True).count() == 1

        zum_antrag = Motion.objects.get(organization=org, title=demo.AENDERUNG_ANTRAG)
        assert (zum_antrag.motion_type, zum_antrag.parent_motion) == (
            "amendment",
            Motion.objects.get(organization=org, title=demo.ANTRAG_EINGEREICHT),
        )
        zur_vorlage = Motion.objects.get(organization=org, title=demo.AENDERUNG_VORLAGE)
        assert zur_vorlage.parent_paper is not None and zur_vorlage.parent_motion is None

    def test_neues_design_ist_fuer_die_demo_an(self, demo_aufgebaut: Organization) -> None:
        assert neues_design(demo_aufgebaut) is True

    def test_zweiter_lauf_verdoppelt_nichts(self, demo_aufgebaut: Organization) -> None:
        modelle: tuple[type[Model], ...] = (
            FactionMeeting,
            FactionAgendaItem,
            FactionAttendance,
            FactionProtocolEntry,
            FactionStandardAgendaItem,
            FactionMeetingSchedule,
            FactionMeetingException,
            AgendaItemPosition,
            Motion,
            MotionComment,
            Membership,
        )
        vorher = {m.__name__: m._default_manager.count() for m in modelle}
        call_command("setup_demo_environment", stdout=StringIO())
        assert {m.__name__: m._default_manager.count() for m in modelle} == vorher

    def test_andere_organisationen_bleiben_unberuehrt(self, demo_aufgebaut: Organization) -> None:
        """Gleiche Titel wie in der Demo: Die natürlichen Schlüssel gelten nur in der Demo-Organisation."""
        andere = cast(Any, OrganizationFactory)(slug="andere-fraktion")
        mitglied = cast(Any, MembershipFactory)(organization=andere)
        sitzung = FactionMeeting.objects.create(organization=andere, title=demo.KOMMENDE_SITZUNG, start=timezone.now())
        FactionAgendaItem.objects.create(meeting=sitzung, title="Politische Arbeit", number="1")
        FactionStandardAgendaItem.objects.create(organization=andere, title="Beschlüsse", order=9)
        FactionMeetingSchedule.objects.create(organization=andere, name=demo.REIHE, weekday=2, time="19:00")
        Motion.objects.create(organization=andere, title=demo.ANTRAG_ENTWURF, author=mitglied)
        bezug: dict[type[Model], str] = {
            FactionMeeting: "organization",
            FactionAgendaItem: "meeting__organization",
            FactionStandardAgendaItem: "organization",
            FactionMeetingSchedule: "organization",
            Motion: "organization",
            MotionComment: "motion__organization",
            Membership: "organization",
            Organization: "pk",
        }

        def bestand() -> dict[str, list[dict[str, Any]]]:
            return {
                m.__name__: list(m._default_manager.filter(**{pfad: andere.pk}).order_by("pk").values())
                for m, pfad in bezug.items()
            }

        vorher = bestand()
        call_command("setup_demo_work", stdout=StringIO())
        assert bestand() == vorher


class TestSeitenJeRolle:
    @pytest.fixture
    def anmelden(self, demo_aufgebaut: Organization) -> Any:
        def client(rolle: str) -> Client:
            nutzer = _mitglied(demo_aufgebaut, rolle).user
            nutzer.set_password(PASSWORT)
            nutzer.save(update_fields=["password"])
            browser = Client()
            assert browser.login(email=nutzer.email, password=PASSWORT)
            return browser

        return client

    @pytest.mark.parametrize("rolle", ROLLEN)
    def test_kein_serverfehler(self, demo_aufgebaut: Organization, anmelden: Any, rolle: str) -> None:
        org = demo_aufgebaut
        sitzung = FactionMeeting.objects.get(organization=org, title=demo.KOMMENDE_SITZUNG)
        entwurf = Motion.objects.get(organization=org, title=demo.ANTRAG_ENTWURF)
        rat3 = OParlMeeting.objects.get(external_id=_ext("meeting", "rat-3"))
        browser = anmelden(rolle)
        pfade = [
            f"/work/{org.slug}/",
            f"/work/{org.slug}/faction/",
            f"/work/{org.slug}/faction/{sitzung.pk}/",
            f"/work/{org.slug}/documents/",
            f"/work/{org.slug}/documents/{entwurf.pk}/",
            f"/work/{org.slug}/meetings/{rat3.pk}/prepare/",
            f"/work/{org.slug}/freigaben/" if rolle == "gast" else f"/work/{org.slug}/tasks/",
        ]
        antworten = {pfad: browser.get(pfad).status_code for pfad in pfade}
        assert not {pfad: code for pfad, code in antworten.items() if code >= 500}, antworten
        if rolle != "gast":
            assert antworten[f"/work/{org.slug}/faction/{sitzung.pk}/"] == 200

    def test_nur_vereidigte_sehen_nichtoeffentliche_tops(self, demo_aufgebaut: Organization, anmelden: Any) -> None:
        sitzung = FactionMeeting.objects.get(organization=demo_aufgebaut, title=demo.KOMMENDE_SITZUNG)
        pfad = f"/work/{demo_aufgebaut.slug}/faction/{sitzung.pk}/"
        vereidigt = anmelden("vorsitz").get(pfad).content.decode()
        nicht_vereidigt = anmelden("unvereidigt").get(pfad).content.decode()
        assert "Grundstücksangelegenheit Am Stadtpark" in vereidigt
        assert "Grundstücksangelegenheit Am Stadtpark" not in nicht_vereidigt
        assert "Ratssitzung: Feuerwehrbedarfsplan" in nicht_vereidigt

    def test_vorbereitung_zeigt_den_beratungsverlauf(self, demo_aufgebaut: Organization, anmelden: Any) -> None:
        rat3 = OParlMeeting.objects.get(external_id=_ext("meeting", "rat-3"))
        seite = anmelden("vorsitz").get(f"/work/{demo_aufgebaut.slug}/meetings/{rat3.pk}/prepare/")
        assert seite.status_code == 200
        assert "Mit Änderungsantrag" in seite.content.decode()
