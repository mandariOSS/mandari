# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import sys

from django.apps import AppConfig


class InsightSyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "insight_sync"
    verbose_name = "Mandari Insight Sync"

    def ready(self):
        # Explizit deaktivierbar (z. B. Smoke-Tests: der Watchdog schreibt
        # kurz nach dem Start in die DB und kollidiert unter SQLite/Windows
        # mit laufenden Migrationen -> "database is locked")
        if os.environ.get("MANDARI_SYNC_WATCHDOG", "").lower() in ("0", "false", "off"):
            return

        # Nicht bei Management Commands (außer runserver)
        is_management_command = (
            len(sys.argv) > 1 and sys.argv[0].endswith("manage.py") and sys.argv[1] not in ("runserver", "runworker")
        )
        if is_management_command:
            return

        # Bei runserver mit Auto-Reload: NUR im Worker starten
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return

        from . import daemon

        daemon.start()
