# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Basisdemo (setup_demo_environment): Termine an Werktagen, durchgehende Vorführung Work → Session.

Die öffentliche Demo baut nachts neu auf; alle Sitzungen liegen relativ zum Aufbautag. Fiel der Aufbau auf
einen Sonntag, lag die kommende Ratssitzung auf einem Sonntag – noch dazu um 19 Uhr, weil die Uhrzeit in UTC
gesetzt wurde. Außerdem stand der Antrag der Musterfraktion nicht auf der Tagesordnung der Ratssitzung in
Session, und in Work zeigte „Sitzung vorbereiten“ keine Dokumente aus dem RIS, weil die Vorlagen der
kommenden Ratssitzung keine Dateien hatten.
"""

from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db.models import Model
from django.utils import timezone

from apps.common.management.commands import setup_demo_environment as basisdemo
from apps.common.management.commands.setup_demo_environment import (
    DEMO_ANTRAG_TRINKBRUNNEN,
    DEMO_BODY_SLUG,
    DEMO_ORG_SLUG,
    DEMO_RATSSITZUNG,
    DEMO_SESSION_SLUG,
    DEMO_USERS,
    _ext,
    _ostersonntag,
    ist_sitzungstag,
    sitzungstermin,
)
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionPaper,
)
from apps.tenants.models import Membership
from apps.work.meetings.selectors import load_preparation_data
from apps.work.meetings.serializers import serialize_prepared_item
from apps.work.models import FactionMeeting
from insight_core.models import OParlFile, OParlMeeting

#: Ein Sonntag: Die kommende Ratssitzung (+14 Tage) fiele ohne Ausgleich wieder auf einen Sonntag
SONNTAG = date(2026, 10, 4)


@pytest.fixture
def aufbau_am(monkeypatch: pytest.MonkeyPatch) -> Any:
    def setzen(tag: date) -> None:
        monkeypatch.setattr(basisdemo, "aufbautag", lambda: tag)

    return setzen


def _lokal(termin: Any) -> Any:
    return timezone.localtime(termin)


class TestSitzungstermin:
    @pytest.mark.parametrize(
        ("jahr", "ostern"),
        [(2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28)), (2038, date(2038, 4, 25))],
    )
    def test_ostersonntag(self, jahr: int, ostern: date) -> None:
        assert _ostersonntag(jahr) == ostern

    def test_kommender_termin_rueckt_vom_wochenende_auf_montag(self, aufbau_am: Any) -> None:
        aufbau_am(SONNTAG)
        termin = _lokal(sitzungstermin(14))
        assert termin.date() == date(2026, 10, 19)
        assert (termin.hour, termin.minute) == (17, 0)

    def test_vergangener_termin_rueckt_auf_den_vorherigen_werktag(self, aufbau_am: Any) -> None:
        aufbau_am(SONNTAG)
        assert _lokal(sitzungstermin(-28)).date() == date(2026, 9, 4)  # Sonntag → Freitag davor

    def test_feiertage_werden_uebersprungen(self, aufbau_am: Any) -> None:
        aufbau_am(date(2026, 12, 10))
        # Do 24.12. (Heiligabend), Fr 25., Sa 26., So 27. → Montag 28.12.
        assert _lokal(sitzungstermin(14)).date() == date(2026, 12, 28)
        aufbau_am(date(2027, 1, 5))
        # Do 31.12. (Silvester) → Mittwoch 30.12.
        assert _lokal(sitzungstermin(-5)).date() == date(2026, 12, 30)
        aufbau_am(date(2026, 3, 30))
        # Fr 3.4. Karfreitag → Dienstag 7.4. (Wochenende, Ostermontag)
        assert _lokal(sitzungstermin(4)).date() == date(2026, 4, 7)

    def test_uhrzeit_ist_ortszeit(self, aufbau_am: Any) -> None:
        aufbau_am(date(2026, 1, 12))  # Winterzeit
        assert _lokal(sitzungstermin(1, stunde=19)).hour == 19
        aufbau_am(date(2026, 7, 13))  # Sommerzeit
        assert _lokal(sitzungstermin(1)).hour == 17


@pytest.mark.django_db
class TestBasisdemo:
    @pytest.fixture(autouse=True)
    def _umgebung(self, settings: Any, tmp_path: Path, aufbau_am: Any) -> None:
        settings.MEDIA_ROOT = str(tmp_path / "media")
        aufbau_am(SONNTAG)

    @staticmethod
    def aufbauen() -> None:
        call_command("setup_demo_environment", stdout=StringIO())

    def test_alle_sitzungen_an_werktagen(self) -> None:
        self.aufbauen()
        termine = [
            *OParlMeeting.objects.filter(body__slug=DEMO_BODY_SLUG).values_list("name", "start"),
            *SessionMeeting.objects.filter(tenant__slug=DEMO_SESSION_SLUG).values_list("name", "start"),
            *FactionMeeting.objects.filter(organization__slug=DEMO_ORG_SLUG).values_list("title", "start"),
        ]
        assert len(termine) >= 10
        for name, start in termine:
            assert ist_sitzungstag(_lokal(start).date()), f"{name}: {_lokal(start):%a %d.%m.%Y}"

        ratssitzung = SessionMeeting.objects.get(tenant__slug=DEMO_SESSION_SLUG, name=DEMO_RATSSITZUNG)
        assert ratssitzung.organization.name == "Rat der Stadt Musterstadt"
        beginn = _lokal(ratssitzung.start)
        assert (beginn.date(), beginn.hour) == (date(2026, 10, 19), 17)
        # Dieselbe Ratssitzung im RIS der Fraktion (Insight) liegt am selben Tag
        rat3 = OParlMeeting.objects.get(external_id=_ext("meeting", "rat-3"))
        assert _lokal(rat3.start) == beginn

    def test_antrag_steht_auf_der_tagesordnung_der_ratssitzung(self) -> None:
        self.aufbauen()
        antrag = SessionApplication.objects.get(tenant__slug=DEMO_SESSION_SLUG, title=DEMO_ANTRAG_TRINKBRUNNEN)
        assert antrag.status == "converted"
        vorlage = SessionPaper.objects.get(source_application=antrag)
        assert vorlage.status == "scheduled"
        assert vorlage.main_organization is not None and vorlage.main_organization.name == "Rat der Stadt Musterstadt"

        top = SessionAgendaItem.objects.get(paper=vorlage)
        assert top.meeting.name == DEMO_RATSSITZUNG
        beratung = SessionConsultation.objects.get(paper=vorlage)
        assert (beratung.role, beratung.meeting_id, beratung.agenda_item_id) == ("decision", top.meeting_id, top.pk)

    def test_sitzung_vorbereiten_zeigt_dokumente_aus_dem_ris(self) -> None:
        self.aufbauen()
        vorsitz = Membership.objects.get(organization__slug=DEMO_ORG_SLUG, user__email=DEMO_USERS["vorsitz"]["email"])
        rat3 = OParlMeeting.objects.get(external_id=_ext("meeting", "rat-3"))
        daten = load_preparation_data(vorsitz.organization, vorsitz, rat3)
        dateien = {
            eintrag.item.name: [f["name"] for f in serialize_prepared_item(eintrag, i, daten)["files"]]
            for i, eintrag in enumerate(daten.prepared_items)
        }
        assert dateien["Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt"] == [
            "Antrag Trinkwasserbrunnen in der Innenstadt (Demo)"
        ]
        assert dateien["Antrag: Einrichtung eines Jugendbeirats"]
        assert dateien["Feuerwehrbedarfsplan 2026-2031"]

    def test_zweiter_lauf_verdoppelt_nichts(self) -> None:
        modelle: tuple[type[Model], ...] = (
            OParlMeeting,
            OParlFile,
            SessionMeeting,
            SessionAgendaItem,
            SessionPaper,
            SessionConsultation,
            SessionApplication,
            FactionMeeting,
        )
        self.aufbauen()
        vorher = {m.__name__: m._default_manager.count() for m in modelle}
        vorlage = SessionPaper.objects.get(source_application__title=DEMO_ANTRAG_TRINKBRUNNEN)

        self.aufbauen()
        assert {m.__name__: m._default_manager.count() for m in modelle} == vorher
        antrag = SessionApplication.objects.get(tenant__slug=DEMO_SESSION_SLUG, title=DEMO_ANTRAG_TRINKBRUNNEN)
        assert antrag.status == "converted"
        assert SessionPaper.objects.get(source_application=antrag).pk == vorlage.pk
        assert SessionAgendaItem.objects.get(paper=vorlage).meeting.name == DEMO_RATSSITZUNG
