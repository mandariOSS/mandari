# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Journal für den öffentlichen Änderungsfeed (Issue #562).

- Index ``events_event_body_public``: Der Änderungsfeed einer Kommune liest ``seq > cursor`` unter
  ihren öffentlichen Ereignissen. Ohne diesen Index liefe die Abfrage über die Ereignisse aller
  Kommunen, bis genug eigene gefunden sind. Er umfasst nur nummerierte Ereignisse der Sichtbarkeit
  ``oeffentlich`` und bleibt damit klein.
- Tabelle ``events_pruning``: Wer Zeilen des Journals löscht, hält hier fest, bis zu welcher
  Folgenummer (``apps.events.pruning``). Leer, solange nichts aufgeräumt wird.

Nur ein Index und eine neue Tabelle; ein Rückfall auf ein älteres Image braucht keinen Rückbau.
"""

import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0004_idempotenz"),
    ]

    operations = [
        migrations.CreateModel(
            name="JournalPruning",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "through_seq",
                    models.BigIntegerField(
                        help_text="höchste Folgenummer, die fehlen kann",
                        verbose_name="gelöscht bis Folgenummer",
                    ),
                ),
                (
                    "recorded_before",
                    models.DateTimeField(
                        help_text="alle gelöschten Zeilen wurden vor diesem Zeitpunkt erfasst",
                        verbose_name="erfasst vor",
                    ),
                ),
                (
                    "pruned_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="aufgeräumt am",
                    ),
                ),
            ],
            options={
                "verbose_name": "Aufräumen des Journals",
                "verbose_name_plural": "Aufräumen des Journals",
                "db_table": "events_pruning",
            },
        ),
        migrations.AddIndex(
            model_name="event",
            index=models.Index(
                condition=models.Q(
                    ("seq__isnull", False), ("visibility", "oeffentlich")
                ),
                fields=["body_id", "seq"],
                name="events_event_body_public",
            ),
        ),
        migrations.AddConstraint(
            model_name="journalpruning",
            constraint=models.CheckConstraint(
                condition=models.Q(("through_seq__gte", 1)),
                name="events_pruning_seq_positive",
            ),
        ),
    ]
