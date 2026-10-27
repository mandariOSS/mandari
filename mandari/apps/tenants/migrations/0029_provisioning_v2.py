# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Provisioning-API v2 (Issue #1132): externe Referenz und Status an der Organisation.

Nur neue, nullbare Spalten ohne Datenänderung. Ein älteres Image kennt die Spalten nicht und legt
Organisationen weiter an (NULL). Rückwärts entfallen nur die drei Spalten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0028_ki_anbieter_auswahl_als_aufruf"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="provisioning_ref",
            field=models.CharField(
                blank=True,
                help_text="Externe Kennung der Bereitstellung (Provisioning-API v2); leer = nicht verknüpft",
                max_length=64,
                null=True,
                unique=True,
                verbose_name="Bereitstellungs-Referenz",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="provisioning_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("aktiv", "Aktiv"),
                    ("gesperrt", "Gesperrt"),
                    ("beendet", "Beendet"),
                ],
                help_text="Zuletzt über die Provisioning-API v2 gesetzter Status (leer = nie gesetzt)",
                max_length=20,
                null=True,
                verbose_name="Bereitstellungs-Status",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="provisioning_status_changed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Bereitstellungs-Status geändert am"),
        ),
    ]
