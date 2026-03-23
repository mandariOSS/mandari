from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0019_insight_subscriptions"),
        ("work", "0028_factionagendaitemattachment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="factionagendaitem",
            name="related_papers",
            field=models.ManyToManyField(
                blank=True,
                related_name="linked_faction_items",
                to="insight_core.oparlpaper",
                verbose_name="Verknüpfte RIS-Vorlagen",
            ),
        ),
    ]
