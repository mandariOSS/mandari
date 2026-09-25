# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eindeutige laufende Nummer je Organisation für die Hash-Kette der Fraktions-Änderungshistorie
(Issue #221). Auf PostgreSQL mit ``CREATE UNIQUE INDEX CONCURRENTLY`` (blockiert keine
Schreibzugriffe), daher ohne umschließende Transaktion.
"""

from django.db import migrations, models

INDEX_NAME = "uniq_faction_audit_seq"
CREATE_SQL = (
    'CREATE UNIQUE INDEX {c} IF NOT EXISTS "uniq_faction_audit_seq" '
    'ON "work_factionauditlog" ("organization_id", "seq") WHERE "seq" IS NOT NULL'
)


def _concurrently(schema_editor):
    return "CONCURRENTLY" if schema_editor.connection.vendor == "postgresql" else ""


def index_anlegen(apps, schema_editor):
    schema_editor.execute(CREATE_SQL.format(c=_concurrently(schema_editor)))


def index_entfernen(apps, schema_editor):
    schema_editor.execute(f'DROP INDEX {_concurrently(schema_editor)} IF EXISTS "{INDEX_NAME}"')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("work", "0053_fraktionsprotokoll_kette"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="factionauditlog",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("seq__isnull", False)),
                        fields=("organization", "seq"),
                        name=INDEX_NAME,
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(index_anlegen, index_entfernen)],
        ),
    ]
