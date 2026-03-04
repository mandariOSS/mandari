# Data migration: Convert TaskComment → TaskActivity(type="comment")

import uuid
from django.db import migrations


def migrate_comments_to_activities(apps, schema_editor):
    """Migriert bestehende TaskComment-Einträge zu TaskActivity(type='comment')."""
    TaskComment = apps.get_model("work", "TaskComment")
    TaskActivity = apps.get_model("work", "TaskActivity")

    activities = []
    for comment in TaskComment.objects.all().iterator():
        activities.append(
            TaskActivity(
                id=uuid.uuid4(),
                task_id=comment.task_id,
                actor_id=comment.author_id,
                activity_type="comment",
                content=comment.content,
                details={},
                created_at=comment.created_at,
            )
        )

    if activities:
        TaskActivity.objects.bulk_create(activities, batch_size=500)


def reverse_migration(apps, schema_editor):
    """Löscht migrierte Kommentar-Aktivitäten (Reverse)."""
    TaskActivity = apps.get_model("work", "TaskActivity")
    TaskActivity.objects.filter(activity_type="comment").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0026_task_labels_checklist_attachments_activity"),
    ]

    operations = [
        migrations.RunPython(migrate_comments_to_activities, reverse_migration),
    ]
