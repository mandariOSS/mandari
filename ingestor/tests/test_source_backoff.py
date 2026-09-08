# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quellen-Schonung im Daemon (Issue #89): wachsender Abstand nach Fehlversuchen."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.sync.orchestrator import BACKOFF_MAX_MINUTES, source_backoff_until

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def src(failures, minutes_ago=5, tz=UTC):
    when = (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=tz)
    return SimpleNamespace(consecutive_failures=failures, last_error_at=when)


def test_below_threshold_is_never_paused():
    assert source_backoff_until(src(0), NOW) is None
    assert source_backoff_until(src(2), NOW) is None


def test_three_failures_pause_ten_minutes():
    assert source_backoff_until(src(3, minutes_ago=5), NOW) == NOW + timedelta(minutes=5)
    assert source_backoff_until(src(3, minutes_ago=15), NOW) is None


def test_backoff_doubles_and_caps():
    assert source_backoff_until(src(5, minutes_ago=0), NOW) == NOW + timedelta(minutes=40)
    assert source_backoff_until(src(20, minutes_ago=0), NOW) == NOW + timedelta(
        minutes=BACKOFF_MAX_MINUTES
    )


def test_missing_error_time_or_naive_datetime():
    assert (
        source_backoff_until(SimpleNamespace(consecutive_failures=9, last_error_at=None), NOW)
        is None
    )
    assert source_backoff_until(src(3, minutes_ago=5, tz=None), NOW) == NOW + timedelta(minutes=5)
