# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Versandergebnis der Einladung je Person (Issue #1071): wann die Einladung an den Mailversand ging, ob ohne
nichtöffentlichen Teil und warum nicht. Rein additiv mit Datenbank-Vorgaben, rückwärts ohne Datenverlust an
bestehenden Daten (nur die neuen Spalten fallen weg).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0078_freigaben_herunterladen"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionattendance",
            name="invitation_error",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                max_length=300,
                verbose_name="Einladung nicht verschickt, Grund",
            ),
        ),
        migrations.AddField(
            model_name="factionattendance",
            name="invitation_mailed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Einladung verschickt am"),
        ),
        migrations.AddField(
            model_name="factionattendance",
            name="invitation_without_internal",
            field=models.BooleanField(
                db_default=False,
                default=False,
                verbose_name="Einladung ohne nichtöffentlichen Teil",
            ),
        ),
    ]
