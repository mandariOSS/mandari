import os
import sys

from django.apps import AppConfig


class InsightSyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "insight_sync"
    verbose_name = "Mandari Insight Sync"

    def ready(self):
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
