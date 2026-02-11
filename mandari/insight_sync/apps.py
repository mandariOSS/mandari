import os
import sys

from django.apps import AppConfig


class InsightSyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "insight_sync"
    verbose_name = "Mandari Insight Sync"

    def ready(self):
        from django.conf import settings

        if not getattr(settings, "SYNC_DAEMON_AUTOSTART", False):
            return

        # Nicht bei Management Commands
        if len(sys.argv) > 1 and sys.argv[1] != "runserver":
            return

        # NUR im Reloader-Worker starten (RUN_MAIN=true).
        # Der Eltern-Prozess (kein RUN_MAIN) darf NICHT starten,
        # weil er den Worker jederzeit neu spawnen kann.
        if os.environ.get("RUN_MAIN") != "true":
            return

        from . import daemon

        daemon.start()
