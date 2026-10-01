# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungscockpit (Issue #140): Recht „Sitzungen leiten“ und Zeitpunkte der Abstimmung je TOP.

1. Neue Spalten mit DB-Default bzw. ``NULL`` – ein älteres Image schreibt weiter, ohne sie zu kennen.
2. Bestand: Rollen, die Anwesenheit verwalten und Protokolle bearbeiten dürfen (Standardrollen
   „Sachbearbeiter“ und „Protokollant“ sowie gleich geschnittene eigene Rollen), erhalten das neue Recht.
   Sie haben die Sitzung bisher schon über Anwesenheit und Abstimmungserfassung begleitet; ohne den
   Schritt könnte nach dem Update nur noch die Administration das Cockpit steuern.

Idempotent: Der Schritt setzt das Recht nur, nimmt es nie weg. Rückwärts ist nichts zu tun (die Spalte
entfällt mit dem Rückbau der Felder). Nicht ``elidable``: Beim Zusammenfassen der Migrationen muss der
Schritt erhalten bleiben, sonst fehlte Bestandsrollen das Recht.
"""

from django.db import migrations, models


def bestandsrollen_berechtigen(apps, schema_editor):
    Role = apps.get_model("session", "SessionRole")
    Role.objects.filter(can_manage_attendance=True, can_edit_protocols=True, can_conduct_meetings=False).update(
        can_conduct_meetings=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0049_oparl_lizenz"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionagendaitem",
            name="vote_closed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Abstimmung geschlossen"),
        ),
        migrations.AddField(
            model_name="sessionagendaitem",
            name="vote_opened_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Abstimmung geöffnet"),
        ),
        migrations.AddField(
            model_name="sessionrole",
            name="can_conduct_meetings",
            field=models.BooleanField(
                db_default=False,
                default=False,
                verbose_name="Sitzungen leiten (Cockpit)",
            ),
        ),
        migrations.RunPython(bestandsrollen_berechtigen, migrations.RunPython.noop),
    ]
