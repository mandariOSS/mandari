# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitgliedschaft „Nur Lesen, Löschung nach 30 Tagen“ (Issue #1237): geplantes Löschdatum und versandte Erinnerung.

Nur neue, nullbare Spalten ohne Datenänderung. Ein älteres Image kennt die Spalten nicht und legt Mitgliedschaften
weiter an (NULL = kein Auslauf). Rückwärts entfallen nur die beiden Spalten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0029_provisioning_v2"),
    ]

    operations = [
        migrations.AddField(
            model_name="membership",
            name="deletion_scheduled_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Löschung geplant am"),
        ),
        migrations.AddField(
            model_name="membership",
            name="deletion_reminder_days",
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name="Erinnerung an die Löschung verschickt (Tage vorher)"
            ),
        ),
    ]
