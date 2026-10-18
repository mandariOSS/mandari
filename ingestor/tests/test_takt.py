# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Takt je Quelle und Abruf nach Bedarf (Issue #1026): Regeln mit fester Uhr, ohne Datenbank und ohne Netz.

97 % aller Änderungen fielen in 60 Tagen werktags zwischen 7 und 19 Uhr an, am Wochenende keine. Der Takt folgt dem:
werktags in der Arbeitszeit 10 Minuten, sonst 60, am Wochenende 120, an Sitzungstagen und am Vortag 5 (07–22 Uhr),
nach Läufen ohne Änderung schrittweise länger.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.client.source_options import FullSyncWindow, SourceFetchOptions
from src.scheduler.takt import (
    MEETING_IDLE_MAX_MINUTES,
    PERIOD_MEETING,
    PERIOD_OFF_HOURS,
    PERIOD_WEEKEND,
    PERIOD_WORKDAY,
    SyncSchedule,
    SyncState,
    default_full_sync_window,
    full_sync_due,
)

BERLIN = ZoneInfo("Europe/Berlin")


def berlin(tag: date, stunde: int, minute: int = 0) -> datetime:
    """Ortszeit Berlin als UTC-Zeitpunkt."""
    return datetime.combine(tag, time(stunde, minute), tzinfo=BERLIN).astimezone(UTC)


DIENSTAG = date(2026, 10, 13)
MITTWOCH = date(2026, 10, 14)
DONNERSTAG = date(2026, 10, 15)
FREITAG = date(2026, 10, 16)
SAMSTAG = date(2026, 10, 17)
SONNTAG = date(2026, 10, 18)
MONTAG = date(2026, 10, 19)
DIENSTAG_DANACH = date(2026, 10, 20)

TAKT = SyncSchedule()


def test_grundtakt_nach_uhrzeit_und_wochentag() -> None:
    assert TAKT.base(berlin(DIENSTAG, 10)) == (10, PERIOD_WORKDAY)
    assert TAKT.base(berlin(DIENSTAG, 7)) == (10, PERIOD_WORKDAY)
    assert TAKT.base(berlin(DIENSTAG, 6, 59)) == (60, PERIOD_OFF_HOURS)
    assert TAKT.base(berlin(DIENSTAG, 19)) == (60, PERIOD_OFF_HOURS)
    assert TAKT.base(berlin(DIENSTAG, 2)) == (60, PERIOD_OFF_HOURS)
    assert TAKT.base(berlin(SAMSTAG, 10)) == (120, PERIOD_WEEKEND)
    assert TAKT.base(berlin(SONNTAG, 23, 30)) == (120, PERIOD_WEEKEND)


def test_naechster_lauf_ein_abstand_nach_dem_ende() -> None:
    assert TAKT.next_due(berlin(DIENSTAG, 10, 3)) == (berlin(DIENSTAG, 10, 13), 10)
    assert TAKT.next_due(berlin(DIENSTAG, 20, 1)) == (berlin(DIENSTAG, 21, 1), 60)
    assert TAKT.next_due(berlin(SAMSTAG, 9, 0)) == (berlin(SAMSTAG, 11, 0), 120)


def test_kuerzerer_grundtakt_beginnt_vorher_dann_zu_seinem_beginn() -> None:
    # 06:30 Uhr + 60 min wäre 07:30 Uhr; die Arbeitszeit beginnt um 07:00 Uhr mit 10 Minuten
    assert TAKT.next_due(berlin(DIENSTAG, 6, 30)) == (berlin(DIENSTAG, 7, 0), 60)
    # Sonntag 23:30 Uhr + 120 min; ab Montag 00:00 Uhr gilt der Werktag außerhalb der Arbeitszeit (60 min)
    assert TAKT.next_due(berlin(SONNTAG, 23, 30)) == (berlin(MONTAG, 0, 0), 120)
    # Längerer Grundtakt danach verschiebt nichts: 18:55 Uhr + 10 min
    assert TAKT.next_due(berlin(DIENSTAG, 18, 55)) == (berlin(DIENSTAG, 19, 5), 10)


def test_sitzungstag_und_vortag_im_kurzen_takt() -> None:
    sitzung = berlin(DONNERSTAG, 17)
    tage = TAKT.meeting_days([sitzung])
    assert tage == frozenset({MITTWOCH, DONNERSTAG})

    assert TAKT.base(berlin(MITTWOCH, 10), tage) == (5, PERIOD_MEETING)
    assert TAKT.base(berlin(MITTWOCH, 7), tage) == (5, PERIOD_MEETING)
    assert TAKT.base(berlin(MITTWOCH, 21, 59), tage) == (5, PERIOD_MEETING)
    assert TAKT.base(berlin(MITTWOCH, 22), tage) == (60, PERIOD_OFF_HOURS), "nur 07–22 Uhr"
    assert TAKT.base(berlin(DONNERSTAG, 6, 59), tage) == (60, PERIOD_OFF_HOURS)
    assert TAKT.base(berlin(DONNERSTAG, 20), tage) == (5, PERIOD_MEETING), "auch nach der Arbeitszeit"
    assert TAKT.base(berlin(DIENSTAG, 10), tage) == (10, PERIOD_WORKDAY), "zwei Tage vorher normal"
    assert TAKT.base(berlin(FREITAG, 10), tage) == (10, PERIOD_WORKDAY), "am Tag danach normal"
    assert TAKT.next_due(berlin(MITTWOCH, 6, 30), tage) == (berlin(MITTWOCH, 7, 0), 60)
    assert TAKT.next_due(berlin(MITTWOCH, 12, 0), tage) == (berlin(MITTWOCH, 12, 5), 5)


def test_sitzungstag_gilt_auch_am_wochenende_und_nach_ortszeit() -> None:
    # Sitzung Montag 00:30 Uhr Ortszeit = Sonntag 22:30 UTC: zählt als Montag
    tage = TAKT.meeting_days([datetime(2026, 10, 18, 22, 30, tzinfo=UTC)])
    assert tage == frozenset({SONNTAG, MONTAG})
    assert TAKT.base(berlin(SONNTAG, 10), tage) == (5, PERIOD_MEETING)
    assert SyncSchedule.from_sync_config({"sync_schedule": {"meeting_days_before": 0}}).meeting_days(
        [berlin(MONTAG, 18)]
    ) == frozenset({MONTAG})


def test_ruhe_verdoppelt_den_abstand_bis_zur_obergrenze() -> None:
    zehn_uhr = berlin(DIENSTAG, 10)
    assert [TAKT.interval(zehn_uhr, idle_runs=n) for n in (0, 1, 2)] == [10, 10, 10]
    assert [TAKT.interval(zehn_uhr, idle_runs=n) for n in (3, 5, 6, 9, 12, 30)] == [20, 20, 40, 80, 120, 120]
    nachts = berlin(DIENSTAG, 2)
    assert [TAKT.interval(nachts, idle_runs=n) for n in (2, 3, 6)] == [60, 120, 120]
    # Ein längerer Grundtakt als die Obergrenze bleibt
    assert TAKT.interval(berlin(SAMSTAG, 10), idle_runs=9) == 120
    weit = SyncSchedule.from_sync_config({"sync_schedule": {"idle_max_minutes": 300}})
    assert [weit.interval(nachts, idle_runs=n) for n in (3, 6, 9)] == [120, 240, 300]
    aus = SyncSchedule.from_sync_config({"sync_schedule": {"idle_runs": 0}})
    assert aus.interval(zehn_uhr, idle_runs=50) == 10


def test_ruhe_an_sitzungstagen_hoechstens_zehn_minuten() -> None:
    tage = TAKT.meeting_days([berlin(DONNERSTAG, 17)])
    mittag = berlin(DONNERSTAG, 12)
    assert MEETING_IDLE_MAX_MINUTES == 10
    assert [TAKT.interval(mittag, tage, idle_runs=n) for n in (0, 2, 3, 6, 12, 50)] == [5, 5, 10, 10, 10, 10]
    # Nach der Sitzungszeit gilt wieder die übliche Obergrenze
    assert TAKT.interval(berlin(DONNERSTAG, 23), tage, idle_runs=12) == 120
    eigen = SyncSchedule.from_sync_config({"sync_schedule": {"meeting_idle_max_minutes": 20}})
    assert [eigen.interval(mittag, tage, idle_runs=n) for n in (3, 6, 12)] == [10, 20, 20]
    # Ein längerer Sitzungstakt als die Obergrenze bleibt
    lang = SyncSchedule.from_sync_config({"sync_schedule": {"meeting_minutes": 15}})
    assert lang.interval(mittag, tage, idle_runs=12) == 15


def test_sitzungstag_ohne_aenderung_bleibt_im_kurzen_takt() -> None:
    """Eine ruhige Quelle mit Ratssitzung um 17 Uhr: den ganzen Tag höchstens 10 Minuten Abstand, auch abends."""
    tage = TAKT.meeting_days([berlin(DONNERSTAG, 17)])
    laeufe = _laeufe(berlin(DONNERSTAG, 7), berlin(DONNERSTAG, 22), tage=tage)
    abstaende = [int((b - a).total_seconds() // 60) for a, b in zip(laeufe, laeufe[1:], strict=False)]
    assert abstaende[:3] == [5, 5, 10], "nach drei Läufen ohne Änderung 10 Minuten"
    assert max(abstaende) == 10
    assert any(berlin(DONNERSTAG, 16, 50) <= lauf <= berlin(DONNERSTAG, 17) for lauf in laeufe)
    assert any(berlin(DONNERSTAG, 18, 50) <= lauf <= berlin(DONNERSTAG, 19) for lauf in laeufe)
    assert len(laeufe) == 2 + 89, "07:00 und 07:05, dann alle 10 Minuten von 07:10 bis 21:50 Uhr"


def test_zustand_nach_dem_lauf() -> None:
    zustand = SyncState()
    for _ in range(3):
        zustand = zustand.after_run(TAKT, berlin(DIENSTAG, 10), success=True, changes=0, full=False)
    assert zustand.idle_runs == 3 and zustand.interval_minutes == 20

    # Vollabgleich und gescheiterter Lauf ändern die Ruhe nicht
    assert zustand.after_run(TAKT, berlin(DIENSTAG, 10), success=True, changes=500, full=True).idle_runs == 3
    assert zustand.after_run(TAKT, berlin(DIENSTAG, 10), success=False, changes=0, full=False).idle_runs == 3

    # Eine Änderung: zurück auf den Takt der Stunde
    geaendert = zustand.after_run(TAKT, berlin(DIENSTAG, 10, 20), success=True, changes=2, full=False)
    assert geaendert.idle_runs == 0 and geaendert.interval_minutes == 10
    assert geaendert.next_due == berlin(DIENSTAG, 10, 30)
    assert geaendert.last_changes == 2 and geaendert.period == PERIOD_WORKDAY

    # Wechsel des Grundtakts (19 Uhr): Zähler beginnt neu
    abends = zustand.after_run(TAKT, berlin(DIENSTAG, 19, 0), success=True, changes=0, full=False)
    assert abends.idle_runs == 1 and abends.interval_minutes == 60


def test_zustand_als_json_und_zurueck() -> None:
    zustand = SyncState().after_run(TAKT, berlin(DIENSTAG, 10), success=True, changes=0, full=False)
    zustand = SyncState(**{**zustand.__dict__, "full_attempted": berlin(DIENSTAG, 1)})
    wieder = SyncState.from_sync_config({"sync_state": zustand.to_dict()})
    assert wieder == zustand
    assert SyncState.from_sync_config({"sync_state": {"next_due": "kaputt", "idle_runs": -3}}) == SyncState()
    assert SyncState.from_sync_config(None) == SyncState()


def test_einstellungen_je_quelle() -> None:
    eigen = SyncSchedule.from_sync_config(
        {
            "sync_schedule": {
                "workday_minutes": 15,
                "off_hours_minutes": 90,
                "weekend_minutes": 240,
                "meeting_minutes": 2,  # unter 5: gilt als 5
                "workday_hours": {"start": "08:00", "end": "17:30"},
                "meeting_hours": {"start": "22:00", "end": "07:00"},  # über Mitternacht: ungültig
                "idle_max_minutes": 9999,  # über 1440: ungültig
                "tz": "Mars/Olympus",
            }
        },
        default_workday_minutes=10,
    )
    assert eigen.workday_minutes == 15 and eigen.off_hours_minutes == 90 and eigen.weekend_minutes == 240
    assert eigen.meeting_minutes == 5
    assert eigen.workday_hours == (time(8, 0), time(17, 30))
    assert eigen.meeting_hours == (time(7, 0), time(22, 0))
    assert eigen.idle_max_minutes == 120
    assert eigen.tz == BERLIN
    assert eigen.base(berlin(DIENSTAG, 7, 30)) == (90, PERIOD_OFF_HOURS)

    # daemon --interval ist der Standard des Grundtakts, die Quelle geht vor
    assert SyncSchedule.from_sync_config({}, default_workday_minutes=15).workday_minutes == 15
    assert SyncSchedule.from_sync_config({"sync_schedule": {"workday_minutes": True}}).workday_minutes == 10
    assert SyncSchedule.from_sync_config({"sync_schedule": "alle 5 Minuten"}) == SyncSchedule()


def _laeufe(
    von: datetime, bis: datetime, *, dauer: timedelta = timedelta(0), tage: frozenset[date] = frozenset()
) -> list[datetime]:
    """Alle Läufe einer Quelle ohne jede Änderung von ``von`` bis vor ``bis``, jeder Lauf dauert ``dauer``."""
    zustand = SyncState()
    jetzt = von
    laeufe: list[datetime] = []
    while jetzt < bis:
        laeufe.append(jetzt)
        zustand = zustand.after_run(TAKT, jetzt + dauer, success=True, changes=0, full=False, meeting_days=tage)
        assert zustand.next_due is not None
        jetzt = zustand.next_due
    return laeufe


def _woche_ohne_aenderung(dauer: timedelta) -> list[datetime]:
    """Alle Läufe einer Woche (Freitag 19 Uhr bis Freitag 19 Uhr) einer Quelle ohne jede Änderung."""
    return _laeufe(berlin(FREITAG, 19), berlin(FREITAG, 19) + timedelta(days=7), dauer=dauer)


def test_ohne_aenderung_weniger_laeufe_ausserhalb_der_arbeitszeit() -> None:
    laeufe = _woche_ohne_aenderung(timedelta(0))

    def uhrzeiten(von: datetime, bis: datetime) -> list[str]:
        return [lauf.astimezone(BERLIN).strftime("%H:%M") for lauf in laeufe if von <= lauf < bis]

    # Wochenende: alle 120 Minuten, 12 Läufe je Tag (vorher 144 im 10-Minuten-Takt)
    assert len(uhrzeiten(berlin(SAMSTAG, 0), berlin(SONNTAG, 0))) == 12
    assert len(uhrzeiten(berlin(SONNTAG, 0), berlin(MONTAG, 0))) == 12
    # Werktags 19–07 Uhr: 60 Minuten, nach drei Läufen ohne Änderung 120 (vorher 72 Läufe)
    assert uhrzeiten(berlin(MONTAG, 19), berlin(DIENSTAG_DANACH, 7)) == [
        "19:00",
        "20:00",
        "21:00",
        "23:00",
        "01:00",
        "03:00",
        "05:00",
    ]
    # Arbeitszeit ohne Änderung: ab 07:00 Uhr wieder 10 Minuten, dann schrittweise länger (vorher 72 Läufe)
    tag = uhrzeiten(berlin(DIENSTAG_DANACH, 7), berlin(DIENSTAG_DANACH, 19))
    assert tag == [
        "07:00",
        "07:10",
        "07:20",
        "07:40",
        "08:00",
        "08:20",
        "09:00",
        "09:40",
        "10:20",
        "11:40",
        "13:00",
        "14:20",
        "16:20",
        "18:20",
    ]
    # Die ganze Woche ohne Änderung: 131 statt 1008 Läufe im bisherigen 10-Minuten-Takt
    assert len(laeufe) == 131


def test_lange_laeufe_lassen_einen_ganzen_abstand_pause() -> None:
    laeufe = _woche_ohne_aenderung(timedelta(minutes=7))
    abstaende = {b - a for a, b in zip(laeufe, laeufe[1:], strict=False)}
    assert min(abstaende) >= timedelta(minutes=17), "7 Minuten Lauf + 10 Minuten Abstand"


def test_vollabgleich_im_standardfenster_einmal_je_fenster() -> None:
    fenster = default_full_sync_window(1)
    assert fenster is not None and (fenster.start, fenster.end) == (time(1, 0), time(6, 0))
    optionen = SourceFetchOptions()
    eins = berlin(DIENSTAG, 1, 0)
    assert full_sync_due(optionen, fenster, eins, None, None)
    assert full_sync_due(optionen, fenster, berlin(DIENSTAG, 5, 59), None, None)
    assert not full_sync_due(optionen, fenster, berlin(DIENSTAG, 6, 0), None, None)
    assert not full_sync_due(optionen, fenster, berlin(DIENSTAG, 0, 59), None, None)
    # In diesem Fenster schon begonnen (auch wenn er scheiterte): erst im nächsten wieder
    assert not full_sync_due(optionen, fenster, berlin(DIENSTAG, 3), eins, None)
    assert full_sync_due(optionen, fenster, berlin(MITTWOCH, 1), eins, None)


def test_vollabgleich_nach_abstand_und_eigenem_fenster_der_quelle() -> None:
    fenster = default_full_sync_window(1)
    woechentlich = SourceFetchOptions(full_sync_every_days=7)
    gestern = berlin(MONTAG, 1, 30)
    assert not full_sync_due(woechentlich, fenster, berlin(DIENSTAG, 1), None, gestern)
    assert full_sync_due(woechentlich, fenster, berlin(MONTAG + timedelta(days=7), 1, 30), None, gestern)

    eigenes = SourceFetchOptions(full_sync_window=FullSyncWindow(start=time(22), end=time(5), tz=BERLIN))
    assert not full_sync_due(eigenes, fenster, berlin(DIENSTAG, 21), None, None)
    assert full_sync_due(eigenes, fenster, berlin(DIENSTAG, 22, 30), None, None), "unabhängig von --full-sync-hour"
    assert not full_sync_due(eigenes, fenster, berlin(MITTWOCH, 1), berlin(DIENSTAG, 22, 30), None)
    assert not full_sync_due(eigenes, fenster, berlin(MITTWOCH, 5), None, None)


def test_vollabgleich_abgeschaltet() -> None:
    assert default_full_sync_window(99) is None
    assert default_full_sync_window(-1) is None
    assert not full_sync_due(SourceFetchOptions(), None, berlin(DIENSTAG, 1), None, None)
    assert default_full_sync_window(22) == FullSyncWindow(start=time(22), end=time(3), tz=BERLIN)


def test_fensterbeginn_ueber_mitternacht() -> None:
    fenster = FullSyncWindow(start=time(22), end=time(5), tz=BERLIN)
    assert fenster.opened_at(berlin(MITTWOCH, 2)) == berlin(DIENSTAG, 22)
    assert fenster.opened_at(berlin(DIENSTAG, 23)) == berlin(DIENSTAG, 22)
    assert fenster.opened_at(berlin(DIENSTAG, 12)) is None


# Zeitumstellung: Der Takt rechnet in echter Zeit (UTC), Grundtakt und Grenzen in Ortszeit
HERBST_SAMSTAG = date(2026, 10, 24)
HERBST_SONNTAG = date(2026, 10, 25)  # 03:00 Uhr Sommerzeit wird 02:00 Uhr Winterzeit (01:00 UTC)
HERBST_MONTAG = date(2026, 10, 26)
FRUEHLING_SAMSTAG = date(2027, 3, 27)
FRUEHLING_SONNTAG = date(2027, 3, 28)  # 02:00 Uhr Winterzeit wird 03:00 Uhr Sommerzeit (01:00 UTC)
FRUEHLING_MONTAG = date(2027, 3, 29)


def _ortszeiten(laeufe: list[datetime], von: datetime, bis: datetime) -> list[str]:
    return [lauf.astimezone(BERLIN).strftime("%H:%M %Z") for lauf in laeufe if von <= lauf < bis]


def test_zeitumstellung_herbst_takt() -> None:
    laeufe = _laeufe(berlin(HERBST_SAMSTAG, 0), berlin(HERBST_MONTAG, 8))
    abstaende = {b - a for a, b in zip(laeufe, laeufe[1:], strict=False) if b < berlin(HERBST_MONTAG, 0)}
    assert abstaende == {timedelta(minutes=120)}, "am Wochenende 120 Minuten in echter Zeit, auch über die Umstellung"
    # Der Sonntag hat 25 Stunden: 13 Läufe
    sonntag = _ortszeiten(laeufe, berlin(HERBST_SONNTAG, 0), berlin(HERBST_MONTAG, 0))
    assert sonntag[:4] == ["00:00 CEST", "02:00 CEST", "03:00 CET", "05:00 CET"]
    assert sonntag[-1] == "23:00 CET" and len(sonntag) == 13
    # Montag 00:00 Uhr beginnt der Werktag, 07:00 Uhr die Arbeitszeit
    assert _ortszeiten(laeufe, berlin(HERBST_MONTAG, 0), berlin(HERBST_MONTAG, 8)) == [
        "00:00 CET",
        "01:00 CET",
        "02:00 CET",
        "04:00 CET",
        "06:00 CET",
        "07:00 CET",
        "07:10 CET",
        "07:20 CET",
        "07:40 CET",
    ]


def test_zeitumstellung_fruehling_takt() -> None:
    laeufe = _laeufe(berlin(FRUEHLING_SAMSTAG, 0), berlin(FRUEHLING_MONTAG, 8))
    abstaende = {b - a for a, b in zip(laeufe, laeufe[1:], strict=False) if b < berlin(FRUEHLING_MONTAG, 0)}
    assert abstaende == {timedelta(minutes=120)}
    # Der Sonntag hat 23 Stunden: 12 Läufe
    sonntag = _ortszeiten(laeufe, berlin(FRUEHLING_SONNTAG, 0), berlin(FRUEHLING_MONTAG, 0))
    assert sonntag[:3] == ["00:00 CET", "03:00 CEST", "05:00 CEST"]
    assert sonntag[-1] == "23:00 CEST" and len(sonntag) == 12
    assert _ortszeiten(laeufe, berlin(FRUEHLING_MONTAG, 0), berlin(FRUEHLING_MONTAG, 7, 30)) == [
        "00:00 CEST",
        "01:00 CEST",
        "02:00 CEST",
        "04:00 CEST",
        "06:00 CEST",
        "07:00 CEST",
        "07:10 CEST",
        "07:20 CEST",
    ]


def test_zeitumstellung_sitzung_am_montag_sonntag_ist_vortag() -> None:
    tage = TAKT.meeting_days([berlin(HERBST_MONTAG, 17)])
    assert tage == frozenset({HERBST_SONNTAG, HERBST_MONTAG})
    assert TAKT.base(berlin(HERBST_SONNTAG, 6, 59), tage) == (120, PERIOD_WEEKEND)
    assert TAKT.base(berlin(HERBST_SONNTAG, 7), tage) == (5, PERIOD_MEETING)
    # Sonntag 05:00 Uhr + 120 Minuten: um 07:00 Uhr Winterzeit beginnt die Sitzungszeit
    assert TAKT.next_due(berlin(HERBST_SONNTAG, 5), tage) == (berlin(HERBST_SONNTAG, 7), 120)
    assert berlin(HERBST_SONNTAG, 7) == datetime(2026, 10, 25, 6, 0, tzinfo=UTC)


def test_zeitumstellung_vollabgleich_einmal_je_fenster() -> None:
    optionen = SourceFetchOptions()
    ab_eins = default_full_sync_window(1)
    ab_zwei = default_full_sync_window(2)
    assert ab_eins is not None and ab_zwei is not None

    # Herbst, Fenster 01:00–06:00 Uhr: öffnet 01:00 Uhr Sommerzeit (23:00 UTC), schließt 06:00 Uhr Winterzeit
    beginn = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    assert ab_eins.opened_at(beginn) == beginn
    assert full_sync_due(optionen, ab_eins, beginn, None, None)
    assert not full_sync_due(optionen, ab_eins, beginn - timedelta(minutes=1), None, None)
    doppelt = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 Uhr Winterzeit, die Stunde gibt es zweimal
    assert ab_eins.opened_at(doppelt) == beginn
    assert not full_sync_due(optionen, ab_eins, doppelt, beginn, None), "kein zweiter in der doppelten Stunde"
    assert full_sync_due(optionen, ab_eins, datetime(2026, 10, 25, 4, 59, tzinfo=UTC), None, None)
    assert not full_sync_due(optionen, ab_eins, datetime(2026, 10, 25, 5, 0, tzinfo=UTC), None, None)

    # Herbst, Fenster ab 02:00 Uhr: beginnt beim ersten 02:00 Uhr (Sommerzeit, 00:00 UTC)
    zwei_sommer = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    zwei_winter = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    assert not full_sync_due(optionen, ab_zwei, zwei_sommer - timedelta(minutes=1), None, None)
    assert full_sync_due(optionen, ab_zwei, zwei_sommer, None, None)
    assert ab_zwei.opened_at(zwei_winter) == zwei_sommer
    assert not full_sync_due(optionen, ab_zwei, zwei_winter + timedelta(minutes=30), zwei_sommer, None)

    # Frühling, Fenster 01:00–06:00 Uhr: öffnet 01:00 Uhr Winterzeit (00:00 UTC), schließt 06:00 Uhr Sommerzeit
    beginn = datetime(2027, 3, 28, 0, 0, tzinfo=UTC)
    assert full_sync_due(optionen, ab_eins, beginn, None, None)
    assert not full_sync_due(optionen, ab_eins, datetime(2027, 3, 28, 2, 0, tzinfo=UTC), beginn, None)
    assert not full_sync_due(optionen, ab_eins, datetime(2027, 3, 28, 4, 0, tzinfo=UTC), None, None)

    # Frühling, Fenster ab 02:00 Uhr: die Stunde fehlt, es öffnet um 03:00 Uhr Sommerzeit (01:00 UTC)
    drei_sommer = datetime(2027, 3, 28, 1, 0, tzinfo=UTC)
    assert not full_sync_due(optionen, ab_zwei, drei_sommer - timedelta(minutes=1), None, None)
    assert full_sync_due(optionen, ab_zwei, drei_sommer, None, None)
    assert ab_zwei.opened_at(drei_sommer + timedelta(hours=2)) == drei_sommer
    assert not full_sync_due(optionen, ab_zwei, drei_sommer + timedelta(hours=2), drei_sommer, None)
    assert full_sync_due(optionen, ab_zwei, datetime(2027, 3, 29, 0, 0, tzinfo=UTC), drei_sommer, None), (
        "in der nächsten Nacht wieder (02:00 Uhr Sommerzeit)"
    )
