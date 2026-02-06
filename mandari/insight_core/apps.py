from django.apps import AppConfig


class InsightCoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "insight_core"
    verbose_name = "Mandari Insight Core"

    def ready(self):
        """Registriert Signals beim App-Start."""
        # Import signals to register them
        from . import signals  # noqa: F401
