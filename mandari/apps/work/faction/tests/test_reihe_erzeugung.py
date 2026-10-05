# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsreihe (Issue #871): Die Reihe legt Termine voraus an, respektiert Pausen, gelöschte Termine kommen
nicht zurück, die Reihe setzt mit dem nächsten Termin fort. Gerechnet wird in Ortszeit samt Sommerzeit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from apps.work.faction.generation import generate_meetings_for_schedule, run_faction_schedule_pass
from apps.work.faction.models import FactionMeeting, FactionMeetingException, FactionMeetingSchedule
from apps.work.organization import services as org_services

# Montag, 05.10.2026, 12:00 Uhr Ortszeit (Sommerzeit)
JETZT = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _horizont(settings: Any) -> None:
    settings.FACTION_SCHEDULE_HORIZON_DAYS = 21


@pytest.fixture
def reihe(org: Any) -> FactionMeetingSchedule:
    return FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time=time(18, 0)
    )


def _termine(reihe: FactionMeetingSchedule) -> dict[date, FactionMeeting]:
    return {m.scheduled_date: m for m in FactionMeeting.objects.filter(schedule=reihe) if m.scheduled_date}


@pytest.mark.django_db
def test_reihe_legt_termine_im_horizont_an_in_ortszeit(reihe: FactionMeetingSchedule) -> None:
    generate_meetings_for_schedule(reihe, now=JETZT)

    termine = _termine(reihe)
    assert sorted(termine) == [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)]
    # 18 Uhr Ortszeit vor und nach der Zeitumstellung am 25.10.2026
    assert termine[date(2026, 10, 19)].start == datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
    assert termine[date(2026, 10, 26)].start == datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
    reihe.refresh_from_db()
    assert reihe.generated_until == date(2026, 10, 26)


@pytest.mark.django_db
def test_geloeschter_termin_kommt_nicht_zurueck_und_reihe_setzt_fort(reihe: FactionMeetingSchedule) -> None:
    generate_meetings_for_schedule(reihe, now=JETZT)
    _termine(reihe)[date(2026, 10, 12)].delete()

    # Weitere Läufe – am selben Tag und eine Woche später
    generate_meetings_for_schedule(reihe, now=JETZT + timedelta(hours=1))
    stats = generate_meetings_for_schedule(reihe, now=JETZT + timedelta(days=7))

    termine = _termine(reihe)
    assert date(2026, 10, 12) not in termine
    assert date(2026, 11, 2) in termine, "die Reihe setzt mit dem nächsten Termin fort"
    assert stats["created"] == 1


@pytest.mark.django_db
def test_abgesagter_termin_bleibt_abgesagt(reihe: FactionMeetingSchedule) -> None:
    generate_meetings_for_schedule(reihe, now=JETZT)
    FactionMeeting.objects.filter(schedule=reihe, scheduled_date=date(2026, 10, 19)).update(status="cancelled")

    generate_meetings_for_schedule(reihe, now=JETZT + timedelta(days=1))

    assert FactionMeeting.objects.filter(schedule=reihe, scheduled_date=date(2026, 10, 19)).count() == 1
    assert _termine(reihe)[date(2026, 10, 19)].status == "cancelled"


@pytest.mark.django_db
def test_pause_vor_der_erzeugung_streicht_termine(reihe: FactionMeetingSchedule) -> None:
    FactionMeetingException.objects.create(
        schedule=reihe,
        original_date=date(2026, 10, 10),
        end_date=date(2026, 10, 20),
        exception_type="cancelled",
        reason="Herbstpause",
    )

    generate_meetings_for_schedule(reihe, now=JETZT)

    termine = _termine(reihe)
    assert termine[date(2026, 10, 12)].status == "cancelled"
    assert termine[date(2026, 10, 19)].status == "cancelled"
    assert "Herbstpause" in termine[date(2026, 10, 19)].cancellation_reason
    assert termine[date(2026, 10, 26)].status == "planned", "nach der Pause geht die Reihe weiter"


@pytest.mark.django_db
def test_nachtraegliche_pause_streicht_angelegte_termine_aber_keine_eingeladenen(
    org: Any, reihe: FactionMeetingSchedule, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate_meetings_for_schedule(reihe, now=JETZT)
    eingeladen = _termine(reihe)[date(2026, 10, 19)]
    FactionMeeting.objects.filter(pk=eingeladen.pk).update(invitation_sent=True, status="invited")
    monkeypatch.setattr("django.utils.timezone.now", lambda: JETZT)

    entfallen = org_services.add_schedule_exception(
        org, reihe.id, original_date="2026-10-10", end_date="2026-10-20", reason="Herbstpause"
    )

    termine = _termine(reihe)
    assert entfallen == 1
    assert termine[date(2026, 10, 12)].status == "cancelled"
    assert "Herbstpause" in termine[date(2026, 10, 12)].cancellation_reason
    assert termine[date(2026, 10, 19)].status == "invited", "Eingeladene sagt der Vorsitz selbst ab"
    assert termine[date(2026, 10, 26)].status == "planned"


@pytest.mark.django_db
def test_termine_uebernehmen_zu_und_absagen_der_reihe(org: Any) -> None:
    mit = FactionMeetingSchedule.objects.create(
        organization=org, name="Mit Rückmeldung", weekday=1, time=time(18, 0), rsvp_enabled=True
    )
    ohne = FactionMeetingSchedule.objects.create(organization=org, name="Ohne", weekday=2, time=time(18, 0))

    run_faction_schedule_pass(now=JETZT)

    assert set(FactionMeeting.objects.filter(schedule=mit).values_list("rsvp_enabled", flat=True)) == {True}
    assert set(FactionMeeting.objects.filter(schedule=ohne).values_list("rsvp_enabled", flat=True)) == {False}


@pytest.mark.django_db
def test_pausierte_und_wieder_aktivierte_reihe_beginnt_heute(reihe: FactionMeetingSchedule) -> None:
    generate_meetings_for_schedule(reihe, now=JETZT)
    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(is_active=False)
    later = JETZT + timedelta(days=60)

    run_faction_schedule_pass(now=later)
    assert not FactionMeeting.objects.filter(schedule=reihe, scheduled_date__gt=date(2026, 10, 26)).exists()

    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(is_active=True)
    run_faction_schedule_pass(now=later)
    neue = sorted(_termine(reihe))
    assert neue[4] == date(2026, 12, 7), "nach der Pause ab dem nächsten Termin, ohne Nachholen"
