# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lasttest-Datengenerator (Issue #228): Profil ``klein`` liefert die Mengen aus dem
Mengengerüst, ein zweiter Lauf verdoppelt nichts, ``--reset`` räumt vollständig auf,
und außerhalb von DEBUG bricht das Kommando ab.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.accounts.models import User
from apps.session.management.commands.generate_load_data import DOMAENE, PASSWORT, PROFILE
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionTenant,
    SessionUser,
    SessionVote,
)
from apps.tenants.models import Membership, Organization
from apps.work.models import Motion
from insight_core.models import OParlBody, OParlMeeting, OParlPaper

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def medien_im_tmp(settings: Any, tmp_path: Path) -> None:
    """Die 600 Anlagen-PDFs landen im Testverzeichnis, nicht im Media-Ordner des Repos."""
    settings.MEDIA_ROOT = tmp_path / "media"


def _erzeugen(*argv: str) -> str:
    out = StringIO()
    with override_settings(DEBUG=True):
        call_command("generate_load_data", "--profile", "klein", *argv, stdout=out)
    return out.getvalue()


def test_profil_klein_erzeugt_mengengeruest() -> None:
    profil = PROFILE["klein"]
    ausgabe = _erzeugen()

    assert SessionTenant.objects.filter(slug="last-klein-stadt").count() == profil.mandanten
    assert SessionOrganization.objects.filter(tenant__slug="last-klein-stadt").count() == profil.gremien
    assert SessionPerson.objects.count() == profil.personen
    assert SessionPaper.objects.count() == profil.vorlagen_pro_jahr * 2
    assert SessionFile.objects.count() == profil.dokumente
    # Zwei Jahre Historie plus die laufende Ratssitzung für das Abstimmungs-Szenario
    assert SessionMeeting.objects.count() == profil.sitzungen_pro_jahr * 2 + 1
    live = SessionMeeting.objects.get(meeting_state="in_progress")
    assert SessionAttendance.objects.filter(meeting=live).count() == profil.abstimmende
    assert SessionAgendaItem.objects.filter(meeting=live, voting_method="roll_call").exists()
    assert SessionVote.objects.exists(), "vergangene namentliche Abstimmungen fehlen"

    # Konten: ein Verwaltungs- plus die Sachbearbeitungskonten, alle mit bekanntem Passwort
    assert SessionUser.objects.count() == profil.nutzer + 1
    sachbearbeitung = User.objects.get(email=f"last-klein-stadt-sachbearbeitung-1@{DOMAENE}")
    assert sachbearbeitung.check_password(PASSWORT)

    org = Organization.objects.get(slug="last-klein-stadt-fraktion")
    assert Membership.objects.filter(organization=org).count() == profil.fraktionsmitglieder
    assert Motion.objects.filter(organization=org).count() == profil.antraege

    body = OParlBody.objects.get(slug="last-klein-stadt")
    assert OParlPaper.objects.filter(body=body).count() == profil.vorlagen_pro_jahr * 2
    assert OParlMeeting.objects.filter(body=body).count() == profil.sitzungen_pro_jahr * 2
    assert "last-klein-stadt" in ausgabe


def test_zweiter_lauf_verdoppelt_nichts_und_reset_raeumt_auf() -> None:
    _erzeugen()
    vorher = (SessionMeeting.objects.count(), User.objects.count(), OParlPaper.objects.count())

    _erzeugen()
    assert (SessionMeeting.objects.count(), User.objects.count(), OParlPaper.objects.count()) == vorher

    _erzeugen("--reset")
    assert not SessionTenant.objects.exists()
    assert not Organization.objects.exists()
    assert not User.objects.filter(email__endswith=DOMAENE).exists()
    assert not OParlBody.objects.exists()
    assert not SessionFile.objects.exists()


def test_bricht_ausserhalb_von_debug_ab() -> None:
    with override_settings(DEBUG=False), pytest.raises(CommandError, match="ich-weiss-was-ich-tue"):
        call_command("generate_load_data", "--profile", "klein")
    assert not SessionTenant.objects.exists()


def test_seed_liefert_dieselben_daten() -> None:
    _erzeugen("--seed", "7")
    erste = list(SessionAgendaItem.objects.order_by("meeting__start", "order").values_list("votes_yes", flat=True))
    _erzeugen("--seed", "7")
    zweite = list(SessionAgendaItem.objects.order_by("meeting__start", "order").values_list("votes_yes", flat=True))
    assert erste == zweite
