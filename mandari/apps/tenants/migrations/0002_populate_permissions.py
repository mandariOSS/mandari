# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Data migration to populate permissions and create default system roles.

Based on the faction role structure from Volt Münster Handreichung.
"""

from django.db import migrations


def populate_permissions(apps, schema_editor):
    """Create all permissions from the PERMISSIONS dict."""
    Permission = apps.get_model("tenants", "Permission")

    # Import permissions from the permissions module
    from apps.common.permissions import PERMISSIONS, PERMISSION_CATEGORIES

    # Create permissions
    for codename, name in PERMISSIONS.items():
        # Determine category
        category = "other"
        for cat_code, cat_info in PERMISSION_CATEGORIES.items():
            if codename in cat_info["permissions"]:
                category = cat_code
                break

        Permission.objects.update_or_create(
            codename=codename,
            defaults={
                "name": name,
                "category": category,
            },
        )

    print(f"Created/updated {len(PERMISSIONS)} permissions")


def remove_permissions(apps, schema_editor):
    """Remove all permissions (reverse migration)."""
    Permission = apps.get_model("tenants", "Permission")
    from apps.common.permissions import PERMISSIONS

    Permission.objects.filter(codename__in=PERMISSIONS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(populate_permissions, remove_permissions),
    ]
