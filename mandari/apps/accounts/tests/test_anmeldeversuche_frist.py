# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufbewahrungsfrist der Anmeldeversuche (``LoginAttempt``, Issue #366).

``purge_login_attempts`` löscht Versuche nach ``LOGIN_ATTEMPT_RETENTION_DAYS``; der Zeitplan im Worker ruft
den Befehl täglich auf. Jüngere Versuche (und damit die Drosselung der letzten 15 Minuten) bleiben.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.accounts.management.commands import purge_login_attempts
from apps.accounts.models import LoginAttempt
from apps.common.schedules import registrieren
from apps.events import verwaltungsbefehle
from apps.events.schedule import Cron, ScheduleRegistry

pytestmark = pytest.mark.django_db


def attempt(days_ago: float, *, success: bool = False) -> LoginAttempt:
    entry = LoginAttempt.objects.create(email="person@example.org", ip_address="192.0.2.1", was_successful=success)
    # timestamp ist auto_now_add: Alter über update() setzen
    LoginAttempt.objects.filter(pk=entry.pk).update(timestamp=timezone.now() - timedelta(days=days_ago))
    return entry


def run(*args: str) -> str:
    out = StringIO()
    call_command("purge_login_attempts", *args, stdout=out)
    return out.getvalue()


def test_alte_anmeldeversuche_werden_nach_der_frist_geloescht() -> None:
    alt = [attempt(91), attempt(400, success=True)]
    jung = [attempt(89), attempt(0)]

    assert "[DRY-RUN] Anmeldeversuche: 2 älter als 90 Tage." in run("--dry-run")
    assert LoginAttempt.objects.count() == 4

    assert "Anmeldeversuche: 2 älter als 90 Tage gelöscht." in run()
    assert set(LoginAttempt.objects.values_list("pk", flat=True)) == {a.pk for a in jung}
    assert not LoginAttempt.objects.filter(pk__in=[a.pk for a in alt]).exists()
    # Wiederholbar: ein zweiter Lauf findet nichts mehr
    assert "Anmeldeversuche: 0 älter als 90 Tage gelöscht." in run()


@override_settings(LOGIN_ATTEMPT_RETENTION_DAYS=30)
def test_frist_aus_den_einstellungen_und_abweichend() -> None:
    attempt(31)
    attempt(10)
    run()
    assert LoginAttempt.objects.count() == 1
    run("--days", "5")
    assert LoginAttempt.objects.count() == 0


@override_settings(LOGIN_ATTEMPT_RETENTION_DAYS=0)
def test_frist_null_loescht_nichts() -> None:
    attempt(1000)
    assert "Frist deaktiviert" in run()
    assert LoginAttempt.objects.count() == 1


def test_loeschen_in_schritten() -> None:
    for _ in range(5):
        attempt(100)
    attempt(1)
    assert purge_login_attempts.purge(timezone.now() - timedelta(days=90), batch_size=2) == 5
    assert LoginAttempt.objects.count() == 1


def test_zeitplan_im_worker() -> None:
    register = ScheduleRegistry()
    registrieren(register)
    eintrag = register.get("befehl:purge_login_attempts")
    assert eintrag is not None
    assert eintrag.task is verwaltungsbefehle.befehl_ausfuehren
    assert eintrag.trigger == Cron("35 4 * * *")
    assert eintrag.args[:2] == ("purge_login_attempts", [])
