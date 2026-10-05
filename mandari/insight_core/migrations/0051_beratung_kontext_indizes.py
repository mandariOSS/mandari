# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indizes für die Abhängigkeiten des Suchindex-Abonnements (Issue #821).

Das Abonnement ``suchindex`` sucht zu einer geänderten Sitzung bzw. einem geänderten Tagesordnungspunkt
die Beratungen, die auf sie verweisen (``meeting_external_id``, ``agenda_item_external_id``), um die
Dateien der dort beratenen Vorgänge neu zu bauen. Ohne Index liest jede solche Abfrage die ganze
Tabelle der Beratungen.

Wie ``0029_portal_indizes``: angelegt mit ``IF NOT EXISTS``, damit der Betrieb sie auf großen Beständen
vorab ohne Sperre anlegen kann (``CREATE INDEX CONCURRENTLY``) und diese Migration darüber nicht
stolpert. Rein additiv; ein älteres Image läuft mit den Indizes unverändert.
"""

from django.db import migrations, models

MEETING_INDEX = "oparl_cons_meeting_ext"
AGENDA_INDEX = "oparl_cons_agenda_ext"


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0050_loeschgrund"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=f'CREATE INDEX IF NOT EXISTS "{MEETING_INDEX}" ON "oparl_consultations" ("meeting_external_id")',
                    reverse_sql=f'DROP INDEX IF EXISTS "{MEETING_INDEX}"',
                ),
                migrations.RunSQL(
                    sql=(
                        f'CREATE INDEX IF NOT EXISTS "{AGENDA_INDEX}" ON "oparl_consultations" '
                        '("agenda_item_external_id")'
                    ),
                    reverse_sql=f'DROP INDEX IF EXISTS "{AGENDA_INDEX}"',
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="oparlconsultation",
                    index=models.Index(fields=["meeting_external_id"], name=MEETING_INDEX),
                ),
                migrations.AddIndex(
                    model_name="oparlconsultation",
                    index=models.Index(fields=["agenda_item_external_id"], name=AGENDA_INDEX),
                ),
            ],
        ),
    ]
