# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management command to set up default roles for organizations.

Usage:
    # Create roles for all organizations
    python manage.py setup_roles

    # Create roles for a specific organization
    python manage.py setup_roles --org "Volt Münster"

    # List available default roles
    python manage.py setup_roles --list
"""

from django.core.management.base import BaseCommand, CommandError

from apps.common.permissions import DEFAULT_ROLES
from apps.tenants.models import Organization, Permission, Role


class Command(BaseCommand):
    help = "Set up default roles for organizations"

    def add_arguments(self, parser):
        parser.add_argument(
            "--org",
            type=str,
            help="Organization name or slug (default: all organizations)",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List available default roles",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Update existing roles even if they already exist",
        )

    def handle(self, *args, **options):
        # List mode
        if options["list"]:
            self.list_roles()
            return

        # Ensure permissions exist - sync them automatically!
        from apps.common.permissions import PERMISSIONS

        perm_count = Permission.objects.count()
        expected_count = len(PERMISSIONS)

        if perm_count < expected_count:
            self.stdout.write(self.style.WARNING(f"Syncing permissions... ({perm_count}/{expected_count} in DB)"))
            Permission.sync_permissions()
            perm_count = Permission.objects.count()
            self.stdout.write(self.style.SUCCESS(f"Permissions synced! Now {perm_count} in database."))
        else:
            self.stdout.write(f"Found {perm_count} permissions in database.")

        # Get organizations
        if options["org"]:
            orgs = Organization.objects.filter(name__icontains=options["org"]) | Organization.objects.filter(
                slug__icontains=options["org"]
            )
            if not orgs.exists():
                raise CommandError(f"No organization found matching: {options['org']}")
        else:
            orgs = Organization.objects.all()

        if not orgs.exists():
            self.stdout.write(
                self.style.WARNING(
                    "No organizations found. Create an organization first:\n"
                    "  Go to /admin/tenants/organization/ and create one."
                )
            )
            return

        # Create roles for each organization
        total_created = 0
        total_updated = 0

        for org in orgs:
            self.stdout.write(f"\nProcessing: {org.name}")

            existing_count = Role.objects.filter(organization=org).count()

            if existing_count > 0 and not options["force"]:
                self.stdout.write(self.style.WARNING(f"  {existing_count} roles already exist. Use --force to update."))
                continue

            roles = Role.create_default_roles(org)

            total_created += len(roles) if existing_count == 0 else 0
            total_updated += len(roles) if existing_count > 0 else 0

            self.stdout.write(self.style.SUCCESS(f"  Created/updated {len(roles)} roles:"))
            for role in sorted(roles, key=lambda r: -r.priority):
                perm_count = role.permissions.count()
                admin_marker = " [ADMIN]" if role.is_admin else ""
                self.stdout.write(
                    f"    - {role.name} (Priorität: {role.priority}, {perm_count} Berechtigungen){admin_marker}"
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Done! Created {total_created} new roles, updated {total_updated} existing roles.")
        )

    def list_roles(self):
        """List all available default roles."""
        self.stdout.write("\nVerfügbare Standard-Rollen:\n")
        self.stdout.write("=" * 80)

        for role_key, role_config in sorted(DEFAULT_ROLES.items(), key=lambda x: -x[1].get("priority", 0)):
            name = role_config["name"]
            priority = role_config.get("priority", 0)
            is_admin = role_config.get("is_admin", False)
            perm_count = len(role_config.get("permissions", []))
            description = role_config.get("description", "")
            color = role_config.get("color", "#6b7280")

            admin_marker = " [ADMIN]" if is_admin else ""

            self.stdout.write(f"\n{name}{admin_marker}")
            self.stdout.write(f"  Key: {role_key}")
            self.stdout.write(f"  Priorität: {priority}")
            self.stdout.write(f"  Berechtigungen: {perm_count}")
            self.stdout.write(f"  Farbe: {color}")
            if description:
                # Wrap description
                import textwrap

                wrapped = textwrap.fill(description, width=70)
                for line in wrapped.split("\n"):
                    self.stdout.write(f"  {line}")

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(f"\nGesamt: {len(DEFAULT_ROLES)} Rollen definiert")
