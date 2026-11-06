# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten (Issue #1239): Öffentliche API, öffentliche Protokolle und Kalender zeigen ab jetzt nur Sitzungen von
Arten mit der Sichtbarkeit „öffentlich“. Damit die Fraktionssitzungen dort nicht verschwinden, wird die Standardart
öffentlich, wenn die Organisation die öffentliche API (``FactionPublicApiAccess.is_enabled``) oder die öffentlichen
Protokolle (``Organization.publish_protocols``) eingeschaltet hat. Sonst bleibt sie bei „alle Mitglieder“; für die
Mitglieder selbst sehen beide gleich aus.

Geändert wird nur ``visibility`` der Standardart und nur von „alle Mitglieder“ auf „öffentlich“; „nur Teilnehmende“
bleibt. Eine ``UPDATE``-Anweisung mit Unterabfrage, ohne Schleife und ohne Änderungshistorie (Sitzungsarten sind nicht
im Fraktions-Audit registriert).

Rückwärts werden öffentliche Standardarten wieder „alle Mitglieder“. Das ist genau der Stand nach
``0084_sitzungsarten_zuordnen``: Bis zu dieser Migration konnte keine Art öffentlich sein (es gibt keine Einstellung
dafür), und der Stand davor wertet die Sichtbarkeit nicht aus. Es geht nichts verloren.
"""

from django.db import migrations
from django.db.models import Exists, OuterRef, Q


def oeffentlich_machen(apps, schema_editor):
    meeting_type_model = apps.get_model("work", "FactionMeetingType")
    access_model = apps.get_model("work", "FactionPublicApiAccess")
    api_an = access_model.objects.filter(organization=OuterRef("organization"), is_enabled=True)
    meeting_type_model.objects.filter(is_default=True, visibility="members").filter(
        Q(Exists(api_an)) | Q(organization__publish_protocols=True)
    ).update(visibility="public")


def zuruecksetzen(apps, schema_editor):
    meeting_type_model = apps.get_model("work", "FactionMeetingType")
    meeting_type_model.objects.filter(is_default=True, visibility="public").update(visibility="members")


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0084_sitzungsarten_zuordnen"),
    ]

    operations = [
        migrations.RunPython(oeffentlich_machen, zuruecksetzen),
    ]
