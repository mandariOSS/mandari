# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Indizes für die Hash-Kette des Session-Protokolls (Issue #221).

Auf PostgreSQL entstehen sie mit ``CREATE INDEX CONCURRENTLY``: Das blockiert keine
Schreibzugriffe, auch nicht auf großen Protokolltabellen. Deshalb läuft diese Migration
ohne umschließende Transaktion (``atomic = False``). Der Modellzustand entspricht einem
gewöhnlichen ``AddIndex``/``AddConstraint`` mit denselben Namen.
"""

from django.db import migrations, models

INDEXES = [
    (
        "session_audit_tenant_created",
        'CREATE INDEX {c} IF NOT EXISTS "session_audit_tenant_created" '
        'ON "session_audit_logs" ("tenant_id", "created_at")',
    ),
    (
        "uniq_session_audit_seq",
        'CREATE UNIQUE INDEX {c} IF NOT EXISTS "uniq_session_audit_seq" '
        'ON "session_audit_logs" ("tenant_id", "seq") WHERE "seq" IS NOT NULL',
    ),
]


def _concurrently(schema_editor):
    return "CONCURRENTLY" if schema_editor.connection.vendor == "postgresql" else ""


def indizes_anlegen(apps, schema_editor):
    for _name, sql in INDEXES:
        schema_editor.execute(sql.format(c=_concurrently(schema_editor)))


def indizes_entfernen(apps, schema_editor):
    for name, _sql in reversed(INDEXES):
        schema_editor.execute(f'DROP INDEX {_concurrently(schema_editor)} IF EXISTS "{name}"')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("session", "0031_protokollierung"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="sessionauditlog",
                    index=models.Index(fields=["tenant", "created_at"], name="session_audit_tenant_created"),
                ),
                migrations.AddConstraint(
                    model_name="sessionauditlog",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("seq__isnull", False)),
                        fields=("tenant", "seq"),
                        name="uniq_session_audit_seq",
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(indizes_anlegen, indizes_entfernen)],
        ),
    ]
