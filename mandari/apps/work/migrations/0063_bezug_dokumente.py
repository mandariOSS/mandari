# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bezug eines Dokuments (Issue #586): Bezugsantrag auch als RIS-Vorlage, „Zielsitzung“ entfällt.

1. ``parent_paper``: neue, leere Spalte für eine RIS-Vorlage als Bezugsantrag (neben ``parent_motion``).
2. ``related_meeting`` heißt in der Oberfläche „Bezugssitzung“ (nur Beschriftung, keine Datenbankänderung).
3. ``target_meeting`` (Fraktionssitzung als „Zielsitzung“) wurde nie gesetzt oder angezeigt. Das Feld
   verschwindet aus dem Modell; die Spalte bleibt, damit eine ältere Version nach einem Rückfall
   weiterläuft. Etwaige Werte werden vorher geleert, weil das neue Modell die Spalte beim Löschen
   einer Fraktionssitzung nicht mehr leert. Die Spalte kann eine spätere Migration entfernen.

Abwärtskompatibel und wiederholbar; rückwärts ändert sich nichts an den Daten.
"""

import django.db.models.deletion
from django.db import migrations, models


def zielsitzung_leeren(apps, schema_editor):
    Motion = apps.get_model("work", "Motion")
    Motion.objects.filter(target_meeting__isnull=False).update(target_meeting=None)


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0037_zugangstoken_als_hash"),
        ("work", "0062_aufgaben_herkunft_protokolleintrag"),
    ]

    operations = [
        # Erst die Spalten ändern: SQLite baut die Tabelle dabei aus dem Modellzustand neu auf und
        # behält target_meeting nur, solange das Feld noch im Zustand steht.
        migrations.AddField(
            model_name="motion",
            name="parent_paper",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="work_amendments",
                to="insight_core.oparlpaper",
                verbose_name="Bezugsvorlage (RIS)",
            ),
        ),
        migrations.AlterField(
            model_name="motion",
            name="related_meeting",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="work_motions",
                to="insight_core.oparlmeeting",
                verbose_name="Bezugssitzung",
            ),
        ),
        migrations.RunPython(zielsitzung_leeren, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="motion",
                    name="target_meeting",
                ),
            ],
            database_operations=[],
        ),
    ]
