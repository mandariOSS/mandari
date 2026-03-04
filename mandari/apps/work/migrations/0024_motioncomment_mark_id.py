from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("work", "0023_add_data_export"),
    ]

    operations = [
        migrations.AddField(
            model_name="motioncomment",
            name="mark_id",
            field=models.UUIDField(blank=True, null=True, verbose_name="Editor Mark ID"),
        ),
    ]
