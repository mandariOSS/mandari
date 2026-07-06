# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0010_administration_contact"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="plan",
            field=models.CharField(
                default="community",
                help_text="Abo-Plan (community = selbst gehostet/kostenlos)",
                max_length=50,
                verbose_name="Plan",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="billing_reference",
            field=models.CharField(
                blank=True,
                help_text="Subscription-ID im Billing-Portal",
                max_length=100,
                verbose_name="Billing-Referenz",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="member_limit",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Maximale aktive Mitglieder laut Plan (leer = unbegrenzt)",
                null=True,
                verbose_name="Mitglieder-Limit",
            ),
        ),
    ]
