# SPDX-License-Identifier: AGPL-3.0-or-later
# Issue #238: Zeitstempel der Ablehnung einer Registrierungsanfrage, damit abgelehnte
# Konten nach Frist automatisch gelöscht werden können.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_webauthn_credential"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="registration_rejected_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Registrierung abgelehnt am"),
        ),
    ]
