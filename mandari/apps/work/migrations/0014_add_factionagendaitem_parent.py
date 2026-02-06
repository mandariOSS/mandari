# Generated migration for FactionAgendaItem parent field

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0013_extend_motion_workflow"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionagendaitem",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                help_text="Für Unterpunkte wie TOP 1.1, 1.2",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="children",
                to="work.factionagendaitem",
                verbose_name="Übergeordneter TOP",
            ),
        ),
    ]
