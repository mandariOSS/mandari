# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Organisation mit Laufzeit (Issue #1406): Laufzeit in Tagen und Ende der Demo.

Nur neue, nullbare Spalten ohne Datenänderung. Ein älteres Image kennt die Spalten nicht und legt Organisationen
weiter an (NULL = keine Demo). Rückwärts entfallen nur die beiden Spalten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0030_mitgliedschaft_auslauf"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="demo_laufzeit_tage",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                help_text="Demo-Organisation: Zugang endet so viele Tage nach der ersten angenommenen Einladung "
                "(leer = keine Demo)",
                verbose_name="Demo-Laufzeit (Tage)",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="demo_endet_am",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Wird bei der ersten angenommenen Einladung gesetzt. Danach ist die Organisation gesperrt, "
                "die Anmeldung zeigt „Demozeit abgelaufen“. Verlängern: Datum verschieben und Organisation aktivieren.",
                verbose_name="Demo endet am",
            ),
        ),
    ]
