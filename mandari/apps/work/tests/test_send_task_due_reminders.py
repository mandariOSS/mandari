# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fristen-Erinnerungen (``send_task_due_reminders``): Der Kalendertag ist der lokale Tag
(Europe/Berlin), nicht der UTC-Tag. Zwischen Mitternacht und 2 Uhr deutscher Zeit liegt
UTC noch am Vortag – bisher fielen dann „heute fällige“ Dokumente aus der Erinnerung und
die Deduplizierung je Tag verglich zwei verschiedene Tage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.work.motions.models import Motion
from apps.work.notifications.models import Notification, NotificationType

PERMISSIONS = ["dashboard.view", "motions.view", "motions.edit"]


@pytest.fixture
def kurz_nach_mitternacht(monkeypatch: pytest.MonkeyPatch, settings: Any) -> datetime:
    """00:30 Uhr in Berlin = 22:30 UTC am Vortag."""
    settings.TIME_ZONE = "Europe/Berlin"
    settings.USE_TZ = True
    jetzt = datetime(2026, 9, 17, 22, 30, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: jetzt)
    return jetzt


@pytest.mark.django_db
def test_heute_faellig_zaehlt_nach_lokalem_tag(org: Any, make_member: Any, kurz_nach_mitternacht: datetime) -> None:
    autorin = make_member(org, PERMISSIONS, email="autorin@example.org")
    federfuehrung = make_member(org, PERMISSIONS, email="federfuehrung@example.org")
    assert timezone.localdate() != kurz_nach_mitternacht.date(), "Testbedingung: lokaler Tag ≠ UTC-Tag"
    motion = Motion.objects.create(
        organization=org,
        author=autorin,
        title="Heute fällig",
        responsible=federfuehrung,
        due_date=timezone.localdate(),
    )

    call_command("send_task_due_reminders")
    erinnerungen = Notification.objects.filter(
        recipient=federfuehrung,
        notification_type=NotificationType.MOTION_DUE_SOON,
        metadata__motion_id=str(motion.id),
    )
    assert erinnerungen.count() == 1
    assert erinnerungen.get().metadata["days_left"] == 0

    # Zweiter Lauf am selben (lokalen) Tag: dedupliziert
    call_command("send_task_due_reminders")
    assert erinnerungen.count() == 1
