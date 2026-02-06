# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migration to add visibility field to Motion model.

This migration adds a simplified visibility system:
- private: Only the author can see
- shared: Specific people via MotionShare
- organization: Everyone in the organization
"""

from django.db import migrations, models


def migrate_visibility_forward(apps, schema_editor):
    """
    Migrate existing motions to the new visibility system.

    Logic:
    - If motion has organization-scope shares → organization
    - If motion has any other shares → shared
    - Otherwise → private
    """
    Motion = apps.get_model("work", "Motion")
    MotionShare = apps.get_model("work", "MotionShare")

    for motion in Motion.objects.all():
        # Check for organization-scope shares
        has_org_share = MotionShare.objects.filter(motion=motion, scope="organization").exists()

        if has_org_share:
            motion.visibility = "organization"
        elif MotionShare.objects.filter(motion=motion).exists():
            motion.visibility = "shared"
        else:
            motion.visibility = "private"

        motion.save(update_fields=["visibility"])


def migrate_visibility_backward(apps, schema_editor):
    """Reverse migration - no action needed as shares remain."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0017_add_proposal_and_protocol_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="motion",
            name="visibility",
            field=models.CharField(
                choices=[
                    ("private", "Privat"),
                    ("shared", "Geteilt"),
                    ("organization", "Organisation"),
                ],
                default="private",
                help_text="Wer kann dieses Dokument sehen?",
                max_length=20,
                verbose_name="Sichtbarkeit",
            ),
        ),
        migrations.RunPython(migrate_visibility_forward, migrate_visibility_backward),
    ]
