# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungscockpit (Issue #140): Recht „Sitzungen leiten“, Zeitpunkte der Abstimmung je TOP, Unterbrechungen der
Anwesenheit und Verlauf in der Niederschrift.

1. Neue Spalten mit DB-Default bzw. ``NULL`` – ein älteres Image schreibt weiter, ohne sie zu kennen.
2. Bestand: Rollen, die Anwesenheit verwalten und Protokolle bearbeiten dürfen (Standardrollen
   „Sachbearbeiter“ und „Protokollant“ sowie gleich geschnittene eigene Rollen), erhalten das neue Recht.
   Sie haben die Sitzung bisher schon über Anwesenheit und Abstimmungserfassung begleitet; ohne den
   Schritt könnte nach dem Update nur noch die Administration das Cockpit steuern.
3. Bestand: Niederschriften im Entwurf oder in der Prüfung weisen Verlauf und TOP-Zeiten aus wie neue.
   Genehmigte und veröffentlichte behalten ``show_timings = False`` (DB-Default), damit sich ihr Inhalt
   nicht nachträglich ändert.

Idempotent: Die Schritte setzen nur, nehmen nie weg. Rückwärts ist nichts zu tun (die Spalten entfallen
mit dem Rückbau der Felder). Nicht ``elidable``: Beim Zusammenfassen der Migrationen müssen die Schritte
erhalten bleiben, sonst fehlte Bestandsrollen das Recht bzw. offenen Niederschriften der Verlauf.
"""

from django.db import migrations, models


def bestandsrollen_berechtigen(apps, schema_editor):
    Role = apps.get_model("session", "SessionRole")
    Role.objects.filter(can_manage_attendance=True, can_edit_protocols=True, can_conduct_meetings=False).update(
        can_conduct_meetings=True
    )


def offene_niederschriften_mit_verlauf(apps, schema_editor):
    Protocol = apps.get_model("session", "SessionProtocol")
    Protocol.objects.filter(status__in=("draft", "review"), show_timings=False).update(show_timings=True)


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
            model_name="sessionattendance",
            name="interruptions",
            field=models.JSONField(
                blank=True, default=list, null=True, verbose_name="Unterbrechungen der Anwesenheit"
            ),
        ),
        migrations.AddField(
            model_name="sessionprotocol",
            name="show_timings",
            field=models.BooleanField(db_default=False, default=True, verbose_name="Verlauf und TOP-Zeiten ausweisen"),
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
        migrations.RunPython(offene_niederschriften_mit_verlauf, migrations.RunPython.noop),
    ]
