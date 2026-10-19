# SPDX-License-Identifier: AGPL-3.0-or-later
"""
„Offene erinnern“ (Issue #1071): wann zuletzt von Hand an alle ohne Rückmeldung erinnert wurde (Sperrfrist) und das
Ergebnis der Erinnerung je Person. Rein additiv mit Datenbank-Vorgaben, rückwärts lauffähig (nur die neuen Spalten
fallen weg).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0079_einladung_je_person"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionmeeting",
            name="rsvp_reminder_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="An offene Rückmeldungen erinnert am"),
        ),
        migrations.AddField(
            model_name="factionattendance",
            name="rsvp_reminder_mailed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="An Rückmeldung erinnert am"),
        ),
        migrations.AddField(
            model_name="factionattendance",
            name="rsvp_reminder_error",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=300,
                verbose_name="Erinnerung nicht verschickt, Grund",
            ),
        ),
    ]
