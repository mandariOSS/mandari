# Generated manually to fix UUID foreign key type mismatch
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def clear_author_references(apps, schema_editor):
    """Clear author references before type change."""
    BlogPost = apps.get_model('insight_content', 'BlogPost')
    BlogPost.objects.all().update(author=None)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('insight_content', '0001_initial'),
    ]

    operations = [
        # First clear the data (can't convert int to uuid)
        migrations.RunPython(clear_author_references, migrations.RunPython.noop),

        # Remove the old field
        migrations.RemoveField(
            model_name='blogpost',
            name='author',
        ),

        # Add it back with the correct type
        migrations.AddField(
            model_name='blogpost',
            name='author',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
                verbose_name='Autor',
            ),
        ),
    ]
