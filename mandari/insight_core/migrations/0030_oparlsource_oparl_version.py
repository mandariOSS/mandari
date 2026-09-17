# SPDX-License-Identifier: AGPL-3.0-or-later

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0029_portal_indizes"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlsource",
            name="oparl_version",
            field=models.CharField(
                blank=True,
                help_text="Vom Ingestor erkannt (z. B. 1.0 bei more! rubin auf gremien.info).",
                max_length=20,
                null=True,
                verbose_name="OParl-Version",
            ),
        ),
    ]
