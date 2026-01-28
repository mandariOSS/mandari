# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management command to diagnose and fix permission issues.

Usage:
    python manage.py fix_permissions                    # Diagnose only
    python manage.py fix_permissions --fix              # Apply fixes
    python manage.py fix_permissions --org volt-muenster --fix  # Fix specific org
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.common.permissions import PERMISSIONS, DEFAULT_ROLES
from apps.tenants.models import Permission, Role, Organization, Membership


class Command(BaseCommand):
    help = "Diagnose and fix permission system issues"

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Apply fixes (without this flag, only diagnoses)"
        )
        parser.add_argument(
            "--org",
            type=str,
            help="Organization slug to check/fix (default: all)"
        )
        parser.add_argument(
            "--user",
            type=str,
            help="User email to check"
        )

    def handle(self, *args, **options):
        fix_mode = options["fix"]
        org_slug = options.get("org")
        user_email = options.get("user")

        self.stdout.write(self.style.NOTICE("\n" + "=" * 60))
        self.stdout.write(self.style.NOTICE("MANDARI PERMISSION SYSTEM DIAGNOSTICS"))
        self.stdout.write(self.style.NOTICE("=" * 60 + "\n"))

        # 1. Check Permission table
        self._check_permissions(fix_mode)

        # 2. Check Organizations and Roles
        if org_slug:
            orgs = Organization.objects.filter(slug=org_slug)
            if not orgs.exists():
                self.stdout.write(self.style.ERROR(f"Organization '{org_slug}' not found!"))
                return
        else:
            orgs = Organization.objects.filter(is_active=True)

        for org in orgs:
            self._check_organization(org, fix_mode)

        # 3. Check specific user if provided
        if user_email:
            self._check_user(user_email, org_slug, fix_mode)

        self.stdout.write(self.style.NOTICE("\n" + "=" * 60))
        if fix_mode:
            self.stdout.write(self.style.SUCCESS("Fixes applied!"))
        else:
            self.stdout.write(self.style.WARNING(
                "Dry run complete. Use --fix to apply changes."
            ))
        self.stdout.write(self.style.NOTICE("=" * 60 + "\n"))

    def _check_permissions(self, fix_mode):
        """Check and optionally sync the Permission table."""
        self.stdout.write(self.style.HTTP_INFO("\n[1] PERMISSION TABLE"))
        self.stdout.write("-" * 40)

        db_count = Permission.objects.count()
        expected_count = len(PERMISSIONS)

        self.stdout.write(f"  Permissions in code: {expected_count}")
        self.stdout.write(f"  Permissions in DB:   {db_count}")

        # Find missing permissions
        db_perms = set(Permission.objects.values_list("codename", flat=True))
        code_perms = set(PERMISSIONS.keys())
        missing = code_perms - db_perms
        extra = db_perms - code_perms

        if missing:
            self.stdout.write(self.style.WARNING(f"  MISSING: {len(missing)} permissions"))
            for p in sorted(missing)[:10]:
                self.stdout.write(f"    - {p}")
            if len(missing) > 10:
                self.stdout.write(f"    ... and {len(missing) - 10} more")

        if extra:
            self.stdout.write(self.style.WARNING(f"  EXTRA (in DB but not in code): {len(extra)}"))

        if missing and fix_mode:
            self.stdout.write(self.style.SUCCESS("  -> Syncing permissions..."))
            Permission.sync_permissions()
            self.stdout.write(self.style.SUCCESS(f"  -> Done! Now {Permission.objects.count()} permissions."))
        elif not missing:
            self.stdout.write(self.style.SUCCESS("  OK - All permissions synced"))

    def _check_organization(self, org, fix_mode):
        """Check and optionally fix organization roles."""
        self.stdout.write(self.style.HTTP_INFO(f"\n[2] ORGANIZATION: {org.name} ({org.slug})"))
        self.stdout.write("-" * 40)

        # Check roles
        roles = Role.objects.filter(organization=org)
        expected_roles = set(r["name"] for r in DEFAULT_ROLES.values())
        existing_roles = set(roles.values_list("name", flat=True))
        missing_roles = expected_roles - existing_roles

        self.stdout.write(f"  Expected default roles: {len(expected_roles)}")
        self.stdout.write(f"  Existing roles:         {roles.count()}")

        # Check admin role specifically
        admin_role = roles.filter(is_admin=True).first()
        if admin_role:
            self.stdout.write(self.style.SUCCESS(f"  Admin role: '{admin_role.name}'"))
        else:
            self.stdout.write(self.style.ERROR("  Admin role: MISSING!"))

        if missing_roles:
            self.stdout.write(self.style.WARNING(f"  MISSING ROLES: {len(missing_roles)}"))
            for r in sorted(missing_roles):
                self.stdout.write(f"    - {r}")

        # Check role permissions
        problem_roles = []
        for role in roles:
            perm_count = role.permissions.count()
            if role.is_admin:
                # Admin doesn't need explicit permissions
                continue
            if perm_count == 0:
                problem_roles.append(role)

        if problem_roles:
            self.stdout.write(self.style.ERROR(f"  ROLES WITHOUT PERMISSIONS: {len(problem_roles)}"))
            for r in problem_roles:
                self.stdout.write(f"    - {r.name}")

        # Apply fixes
        if fix_mode and (missing_roles or problem_roles or not admin_role):
            self.stdout.write(self.style.SUCCESS("  -> Creating/updating default roles..."))
            Role.create_default_roles(org)
            self.stdout.write(self.style.SUCCESS("  -> Done!"))
        elif not missing_roles and not problem_roles and admin_role:
            self.stdout.write(self.style.SUCCESS("  OK - Roles configured correctly"))

        # Check memberships
        memberships = Membership.objects.filter(organization=org, is_active=True)
        self.stdout.write(f"\n  Active memberships: {memberships.count()}")

        no_role_members = []
        for m in memberships:
            if m.roles.count() == 0:
                no_role_members.append(m)

        if no_role_members:
            self.stdout.write(self.style.ERROR(f"  MEMBERS WITHOUT ROLES: {len(no_role_members)}"))
            for m in no_role_members[:5]:
                self.stdout.write(f"    - {m.user.email}")
            if len(no_role_members) > 5:
                self.stdout.write(f"    ... and {len(no_role_members) - 5} more")

            if fix_mode:
                # Assign default "faction_member" role to members without roles
                default_role = roles.filter(name="Fraktionsmitglied").first()
                if not default_role:
                    default_role = roles.filter(is_admin=False).order_by("-priority").first()

                if default_role:
                    self.stdout.write(self.style.SUCCESS(
                        f"  -> Assigning '{default_role.name}' to members without roles..."
                    ))
                    for m in no_role_members:
                        m.roles.add(default_role)
                    self.stdout.write(self.style.SUCCESS("  -> Done!"))
        else:
            self.stdout.write(self.style.SUCCESS("  OK - All members have roles"))

    def _check_user(self, user_email, org_slug, fix_mode):
        """Check specific user's permissions."""
        self.stdout.write(self.style.HTTP_INFO(f"\n[3] USER: {user_email}"))
        self.stdout.write("-" * 40)

        from apps.accounts.models import User
        from apps.common.permissions import PermissionChecker

        try:
            user = User.objects.get(email=user_email)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"  User not found!"))
            return

        self.stdout.write(f"  User ID: {user.id}")
        self.stdout.write(f"  Name: {user.get_full_name() or 'N/A'}")

        # Get memberships
        if org_slug:
            memberships = Membership.objects.filter(
                user=user,
                organization__slug=org_slug
            )
        else:
            memberships = Membership.objects.filter(user=user)

        if not memberships.exists():
            self.stdout.write(self.style.ERROR("  No memberships found!"))
            return

        for membership in memberships:
            self.stdout.write(f"\n  Organization: {membership.organization.name}")
            self.stdout.write(f"  Active: {membership.is_active}")

            roles = membership.roles.all()
            self.stdout.write(f"  Roles: {', '.join(r.name for r in roles) or 'NONE'}")

            # Check specific permissions
            checker = PermissionChecker(membership)
            key_permissions = [
                "dashboard.view",
                "motions.view",
                "motions.create",
                "motions.edit",
                "motions.edit_all",
                "meetings.view",
                "organization.admin",
            ]

            self.stdout.write("\n  Permission check:")
            for perm in key_permissions:
                has = checker.has_permission(perm)
                status = self.style.SUCCESS("YES") if has else self.style.ERROR("NO")
                self.stdout.write(f"    {perm}: {status}")

            is_admin = checker.is_admin()
            self.stdout.write(f"\n  Is Admin: {self.style.SUCCESS('YES') if is_admin else self.style.ERROR('NO')}")

            # If user has no roles and fix mode, offer to make admin
            if fix_mode and roles.count() == 0:
                admin_role = Role.objects.filter(
                    organization=membership.organization,
                    is_admin=True
                ).first()
                if admin_role:
                    self.stdout.write(self.style.SUCCESS(
                        f"  -> Assigning admin role '{admin_role.name}'..."
                    ))
                    membership.roles.add(admin_role)
                    self.stdout.write(self.style.SUCCESS("  -> Done!"))
