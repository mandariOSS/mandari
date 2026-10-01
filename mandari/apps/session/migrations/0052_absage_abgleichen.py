# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Absage abgleichen: Häkchen ``cancelled`` und Status ``cancelled`` beschreiben denselben Sachverhalt.

Bisher ließ das Formular beide unabhängig setzen. Kalender, Abo-Feed, Dashboard und Erinnerungen prüfen das
Häkchen, Statusfilter und Rückmeldelink den Status – eine nur per Status abgesagte Sitzung stand weiter im
öffentlichen Abo-Feed. Seitdem gleicht ``SessionMeeting.save`` beide ab (abgesagt gewinnt); diese Migration
zieht den Bestand nach. Reine Datenänderung, wiederholbar und ohne Rückbau (der alte Stand war widersprüchlich).
``updated_at`` wird mitgesetzt, damit OParl-Clients und der Bürgerportal-Abgleich die Absage erhalten.
"""

from typing import Any

from django.db import migrations
from django.db.models.functions import Now


def absage_abgleichen(apps: Any, schema_editor: Any) -> None:
    Meeting = apps.get_model("session", "SessionMeeting")
    Meeting.objects.filter(meeting_state="cancelled", cancelled=False).update(cancelled=True, updated_at=Now())
    Meeting.objects.filter(cancelled=True).exclude(meeting_state="cancelled").update(
        meeting_state="cancelled", updated_at=Now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0050_sitzungscockpit"),
    ]

    operations = [
        migrations.RunPython(absage_abgleichen, migrations.RunPython.noop, elidable=True),
    ]
