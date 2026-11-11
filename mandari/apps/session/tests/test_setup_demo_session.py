# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand der Musterstadt in Session (``setup_demo_session``, Issue #99).

Die öffentliche Demo soll wie ein echtes Verwaltungs-RIS über ein Jahr aussehen: dieselben Personen, Gremien, Termine
und Drucksachennummern wie in Insight (``apps.common.demo_stadt.stadtplan``), mit Beratungsfolgen, Abstimmungen und
Niederschriften – ohne die Geschichten der Basisdemo zu stören. Die Tests laufen im Umfang ``klein``.
"""

from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Model
from django.utils import timezone

from apps.common.demo_daten import DEMO_ANTRAG_TRINKBRUNNEN, DEMO_ORG_SLUG, DEMO_SESSION_SLUG, ist_sitzungstag
from apps.common.demo_stadt import BASIS_SITZUNG_SESSION, BUERGERMEISTERIN, GREMIEN, PERSON, PERSONEN, stadtplan
from apps.common.management.commands import setup_demo_environment as basisdemo
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionCosignature,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionVote,
)
from apps.tenants.models import Organization

AUFBAUTAG = date(2026, 11, 11)
BESTAND_SITZUNG = r"^[0-9]+\. Sitzung "


@pytest.fixture
def demo(settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings.MEDIA_ROOT = str(tmp_path / "media")
    monkeypatch.setattr(basisdemo, "aufbautag", lambda: AUFBAUTAG)
    call_command("setup_demo_environment", stdout=StringIO())
    return stadtplan(AUFBAUTAG, "klein")


def _bestand_sitzungen() -> Any:
    return SessionMeeting.objects.filter(tenant__slug=DEMO_SESSION_SLUG, name__regex=BESTAND_SITZUNG)


@pytest.mark.django_db
class TestSessionBestand:
    def test_ohne_basisdemo_bricht_ab(self) -> None:
        with pytest.raises(CommandError, match="setup_demo_environment"):
            call_command("setup_demo_session", stdout=StringIO())

    def test_vorlagen_und_sitzungen_wie_im_plan(self, demo: Any) -> None:
        vorlagen = SessionPaper.objects.filter(
            tenant__slug=DEMO_SESSION_SLUG, reference__in=[v.nummer for v in demo.vorgaenge]
        )
        assert vorlagen.count() == len(demo.vorgaenge) > 0
        eigene = [s for s in demo.sitzungen if s.key not in BASIS_SITZUNG_SESSION]
        assert _bestand_sitzungen().count() == len(eigene)
        for name, start in _bestand_sitzungen().values_list("name", "start"):
            assert ist_sitzungstag(timezone.localtime(start).date()), name
        # Jede Vorlage des Bestands ist mindestens einmal beraten oder terminiert, die Stationen folgen dem Weg
        for vorgang in demo.vorgaenge:
            vorlage = vorlagen.get(reference=vorgang.nummer)
            stationen = list(vorlage.consultations.order_by("order").values_list("organization__name", flat=True))
            erwartet = [g.name for key in vorgang.weg for g in GREMIEN if g.key == key]
            assert [s for s in stationen if s in erwartet] and set(stationen) == set(erwartet), vorgang.titel

    def test_vergangene_sitzungen_mit_anwesenheit_ergebnis_und_niederschrift(self, demo: Any) -> None:
        vergangen = _bestand_sitzungen().filter(start__lt=timezone.now())
        assert vergangen.exists()
        for sitzung in vergangen:
            assert sitzung.meeting_state == "completed"
            niederschrift = SessionProtocol.objects.get(meeting=sitzung)
            assert niederschrift.status in ("approved", "review")
            if niederschrift.status == "approved":
                assert niederschrift.approval_meeting is not None
                assert niederschrift.approval_meeting.start > sitzung.start
            anwesend = SessionAttendance.objects.filter(meeting=sitzung, status="present").count()
            assert anwesend > 0
            for top in SessionAgendaItem.objects.filter(meeting=sitzung, paper__isnull=False):
                assert top.vote_result != "pending", top.name
                assert top.votes_yes + top.votes_no + top.votes_abstain in (0, anwesend), top.name
                if top.voting_method == "roll_call":
                    assert SessionVote.objects.filter(agenda_item=top).count() == anwesend
        # Die jüngste Niederschrift je Gremium liegt noch zur Prüfung
        assert SessionProtocol.objects.filter(meeting__in=vergangen, status="review").exists()

    def test_kommende_sitzungen_sind_offen(self, demo: Any) -> None:
        kommend = _bestand_sitzungen().filter(start__gt=timezone.now())
        assert kommend.exists()
        assert set(kommend.values_list("meeting_state", flat=True)) <= {"scheduled", "invitation_sent"}
        assert not SessionAgendaItem.objects.filter(meeting__in=kommend).exclude(vote_result="pending").exists()
        assert not SessionProtocol.objects.filter(meeting__in=kommend).exists()

    def test_besetzung_rat_ausschuesse_und_fraktionen(self, demo: Any) -> None:
        rat = SessionOrganization.objects.get(tenant__slug=DEMO_SESSION_SLUG, name="Rat der Stadt Musterstadt")
        ratsmitglieder = [p for p in PERSONEN if p.rat]
        assert rat.memberships.count() == len(ratsmitglieder)
        assert rat.memberships.get(role="chair").person.email == PERSON[BUERGERMEISTERIN].email
        for gremium in GREMIEN:
            if gremium.key == "rat":
                continue
            org = SessionOrganization.objects.get(tenant__slug=DEMO_SESSION_SLUG, name=gremium.name)
            assert org.memberships.count() == gremium.sitze, gremium.name
        assert (
            SessionOrganization.objects.filter(tenant__slug=DEMO_SESSION_SLUG, organization_type="faction").count() == 5
        )
        sachkundig = SessionPerson.objects.get(tenant__slug=DEMO_SESSION_SLUG, email=PERSON["sofia-sachkundig"].email)
        assert set(
            sachkundig.memberships.exclude(organization__organization_type="faction").values_list("role", flat=True)
        ) == {"expert_citizen"}
        # Verschlüsselte Kontaktdaten über die Accessoren
        assert cast(Any, sachkundig).get_phone_decrypted().startswith("+49")

    def test_antraege_der_fraktionen_sind_umgewandelt(self, demo: Any) -> None:
        fraktionsvorgaenge = [v for v in demo.vorgaenge if v.von_fraktion]
        assert fraktionsvorgaenge
        work = Organization.objects.get(slug=DEMO_ORG_SLUG)
        for vorgang in fraktionsvorgaenge:
            antrag = SessionApplication.objects.get(tenant__slug=DEMO_SESSION_SLUG, reference=vorgang.nummer)
            assert antrag.status == "converted"
            assert SessionPaper.objects.get(source_application=antrag).reference == vorgang.nummer
            erwartet = work if vorgang.urheber == "fraktion-bunte-liste" else None
            assert antrag.submitting_organization == erwartet
            assert antrag.submitted_at.date() <= timezone.localdate()

    def test_kaemmerei_zeichnet_vorlagen_mit_kosten_mit(self, demo: Any) -> None:
        mit_kosten = [v for v in demo.vorgaenge if v.kosten and not v.von_fraktion]
        assert SessionCosignature.objects.filter(
            paper__reference__in=[v.nummer for v in mit_kosten], department__name="Kämmerei (Demo)"
        ).count() == len(mit_kosten)

    def test_basisdemo_bleibt_unberuehrt(self, demo: Any) -> None:
        ratssitzung = SessionMeeting.objects.get(tenant__slug=DEMO_SESSION_SLUG, name=BASIS_SITZUNG_SESSION["rat-3"])
        tops = list(ratssitzung.agenda_items.order_by("order").values_list("number", "name"))
        assert tops[3] == ("4", "Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt")
        nummern = [int(n) for n, _name in tops]
        assert nummern == sorted(nummern) and len(set(nummern)) == len(nummern)
        antrag = SessionApplication.objects.get(tenant__slug=DEMO_SESSION_SLUG, title=DEMO_ANTRAG_TRINKBRUNNEN)
        assert SessionAgendaItem.objects.get(paper__source_application=antrag).meeting == ratssitzung

    def test_zweiter_lauf_verdoppelt_nichts(self, demo: Any) -> None:
        modelle: tuple[type[Model], ...] = (
            SessionMeeting,
            SessionAgendaItem,
            SessionPaper,
            SessionConsultation,
            SessionApplication,
            SessionAttendance,
            SessionProtocol,
            SessionVote,
            SessionCosignature,
            SessionOrganization,
            SessionOrganizationMembership,
            SessionPerson,
        )
        vorher = {m.__name__: m._default_manager.count() for m in modelle}
        # Die genehmigten Niederschriften sperren ihre Sitzungen – der Neuaufbau räumt sie trotzdem ab
        call_command("setup_demo_session", aufbautag=AUFBAUTAG, umfang="klein", stdout=StringIO())
        assert {m.__name__: m._default_manager.count() for m in modelle} == vorher
