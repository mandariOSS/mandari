# Fehlerklasse „robots.txt sperrt“ für Scraper-Quellen (Issue #116)

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0031_oparlsource_user_agent_error_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="oparlsource",
            name="last_error_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ua_blocked", "User-Agent gesperrt"),
                    ("server_error_series", "5xx-Serie"),
                    ("robots_blocked", "robots.txt sperrt"),
                ],
                max_length=40,
                null=True,
                verbose_name="Fehlerklasse",
            ),
        ),
    ]
