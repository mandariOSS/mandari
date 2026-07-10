# SPDX-License-Identifier: AGPL-3.0-or-later
# Organization.ai_token_limit_monthly wird optional:
# leer (NULL) = Standard aus den globalen KI-Einstellungen (common.AISettings),
# 0 = KI für diese Organisation deaktiviert.
# Bestehende Werte (z.B. 3.000.000) bleiben als explizite Overrides erhalten.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0015_antragsdatenbank_tracking"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="ai_token_limit_monthly",
            field=models.PositiveIntegerField(
                blank=True,
                default=None,
                help_text=(
                    "Leer = Standard aus den globalen KI-Einstellungen (Admin → KI-Einstellungen), "
                    "0 = KI für diese Organisation deaktiviert."
                ),
                null=True,
                verbose_name="Token-Limit pro Monat",
            ),
        ),
    ]
