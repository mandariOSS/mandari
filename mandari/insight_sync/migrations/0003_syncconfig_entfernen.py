# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sync-Einstellung ``SyncConfig`` entfernen (Issue #554): Sie steuerte nur ``manage.py sync_daemon``, das es nicht mehr
gibt; abgeglichen wird nur im Ingestor. Die Tabelle ``insight_sync_syncconfig`` enthielt höchstens eine Zeile mit
den Standardwerten. ``SyncLog`` bleibt (der Ingestor schreibt hinein).
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("insight_sync", "0002_full_sync_hour_default"),
    ]

    operations = [
        migrations.DeleteModel(
            name="SyncConfig",
        ),
    ]
