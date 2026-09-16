# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indizes für die Startseite und die Listenseiten des Portals (#256).

Die Indizes werden als ``IF NOT EXISTS`` angelegt: Auf dem Produktivbestand
entstehen sie vorab ohne Sperre (``CREATE INDEX CONCURRENTLY``), weil das
Sortieren zehntausender Zeilen dort sofort weh tut. Diese Migration darf
darüber nicht stolpern — und muss für alle anderen Installationen trotzdem
den Index anlegen.
"""

from django.db import migrations, models

PAPER_INDEX = "oparl_paper_body_datum"
MEETING_INDEX = "oparl_meeting_body_start"


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0028_oparlbody_is_listed"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=f'CREATE INDEX IF NOT EXISTS "{MEETING_INDEX}" ON "oparl_meetings" ("body_id", "start")',
                    reverse_sql=f'DROP INDEX IF EXISTS "{MEETING_INDEX}"',
                ),
                migrations.RunSQL(
                    sql=(
                        f'CREATE INDEX IF NOT EXISTS "{PAPER_INDEX}" ON "oparl_papers" '
                        '("body_id", "date" DESC, "oparl_created" DESC)'
                    ),
                    reverse_sql=f'DROP INDEX IF EXISTS "{PAPER_INDEX}"',
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="oparlmeeting",
                    index=models.Index(fields=["body", "start"], name=MEETING_INDEX),
                ),
                migrations.AddIndex(
                    model_name="oparlpaper",
                    index=models.Index(fields=["body", "-date", "-oparl_created"], name=PAPER_INDEX),
                ),
            ],
        ),
    ]
