# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechtekatalog und Standardrollen prüfen und Fehlendes ergänzen.

Aufruf:
    python manage.py fix_permissions                         # Probelauf: zeigt, was --fix ändern würde
    python manage.py fix_permissions --fix                   # Fehlendes ergänzen
    python manage.py fix_permissions --org volt-muenster --fix
    python manage.py fix_permissions --user person@example.org
    python manage.py fix_permissions --org volt-muenster --assign-role "Parteimitglied" [--fix]

``--fix`` ergänzt nur:
- fehlende Berechtigungen im Katalog und Namen/Kategorien nach dem Code (``Permission.sync_permissions``),
- fehlende Standardrollen einer Organisation (``Role.restore_missing_default_roles``).

``--fix`` ändert nie:
- vorhandene Rollen – auch angepasste Standardrollen behalten Rechte, Flags und Priorität
  (bewusst zurücksetzen: Rollenverwaltung im Work-Portal oder ``setup_roles --force``). Fehlt einer
  Standardrolle ein Recht, das der heutige Standard vorsieht (z. B. ``agenda.approve`` für die
  Geschäftsführung, Issue #872), nennt der Befehl es nur als Hinweis,
- die Rollen von Mitgliedschaften. Mitgliedschaften ohne Rolle werden nur gelistet; eine Rolle
  erhalten sie ausschließlich mit ``--assign-role`` (nur zusammen mit ``--org``, nie eine Rolle mit
  Vollzugriff, nie Gast-Zugänge, die ohne Rollen vorgesehen sind).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import Count, QuerySet

from apps.accounts.models import User
from apps.common.permissions import DEFAULT_ROLES, PERMISSIONS, PermissionChecker
from apps.tenants.models import Membership, Organization, Permission, Role

#: So viele Einträge zeigt eine Liste, der Rest wird gezählt.
LIST_LIMIT = 10

KEY_PERMISSIONS = (
    "dashboard.view",
    "motions.view",
    "motions.create",
    "motions.edit",
    "motions.edit_all",
    "meetings.view",
    "organization.admin",
)


class Command(BaseCommand):
    help = "Rechte und Standardrollen prüfen; mit --fix nur Fehlendes ergänzen (ohne --fix: Probelauf)"

    fix: bool
    changes: int

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--fix", action="store_true", help="Änderungen ausführen (ohne diese Option: Probelauf)")
        parser.add_argument("--org", type=str, help="Slug der Organisation (Standard: alle aktiven)")
        parser.add_argument("--user", type=str, help="E-Mail einer Person, deren Rollen und Rechte angezeigt werden")
        parser.add_argument(
            "--assign-role",
            dest="assign_role",
            type=str,
            help=(
                "Name einer Rolle, die aktive Mitgliedschaften ohne Rolle erhalten sollen "
                "(nur mit --org; keine Rolle mit Vollzugriff; Gast-Zugänge ausgenommen)"
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        self.fix = bool(options["fix"])
        self.changes = 0
        org_slug: str | None = options.get("org")
        user_email: str | None = options.get("user")
        assign_role_name: str | None = options.get("assign_role")

        if assign_role_name and not org_slug:
            raise CommandError("--assign-role nur zusammen mit --org.")
        orgs = self._organizations(org_slug)
        assign_role = self._role_to_assign(orgs[0], assign_role_name) if assign_role_name else None

        self.stdout.write(self.style.NOTICE("\n" + "=" * 60))
        titel = "RECHTE UND ROLLEN – " + ("ÄNDERUNGEN WERDEN AUSGEFÜHRT" if self.fix else "PROBELAUF")
        self.stdout.write(self.style.NOTICE(titel))
        self.stdout.write(self.style.NOTICE("=" * 60 + "\n"))

        with transaction.atomic():
            self._check_permissions()
            for org in orgs:
                self._check_organization(org, assign_role)
            if user_email:
                self._check_user(user_email, org_slug)

        self.stdout.write(self.style.NOTICE("\n" + "=" * 60))
        if not self.changes:
            self.stdout.write(self.style.SUCCESS("Nichts zu ergänzen."))
        elif self.fix:
            self.stdout.write(self.style.SUCCESS(f"{self.changes} Änderung(en) ausgeführt."))
        else:
            self.stdout.write(
                self.style.WARNING(f"Probelauf: {self.changes} Änderung(en) würden ausgeführt. Mit --fix ausführen.")
            )
        self.stdout.write(self.style.NOTICE("=" * 60 + "\n"))

    # ------------------------------------------------------------------
    # Auswahl
    # ------------------------------------------------------------------

    def _organizations(self, org_slug: str | None) -> list[Organization]:
        if not org_slug:
            return list(Organization.objects.filter(is_active=True).order_by("slug"))
        org = Organization.objects.filter(slug=org_slug).first()
        if org is None:
            raise CommandError(f"Organisation '{org_slug}' nicht gefunden.")
        return [org]

    def _role_to_assign(self, org: Organization, name: str) -> Role:
        """Rolle für ``--assign-role`` vor jeder Änderung prüfen."""
        role = Role.objects.filter(organization=org, name=name).first()
        if role is None:
            raise CommandError(f"Rolle '{name}' gibt es in '{org.slug}' nicht.")
        if role.is_admin:
            raise CommandError("Rollen mit Vollzugriff werden über diesen Befehl nicht vergeben.")
        return role

    # ------------------------------------------------------------------
    # Ausgabe
    # ------------------------------------------------------------------

    def _change(self, text: str, items: Sequence[str] = ()) -> None:
        """Eine Änderung melden: im Probelauf als geplant, mit --fix als ausgeführt."""
        self.changes += 1
        if self.fix:
            self.stdout.write(self.style.SUCCESS(f"  -> ergänzt: {text}"))
        else:
            self.stdout.write(self.style.WARNING(f"  -> würde ergänzen: {text}"))
        self._list(items)

    def _note(self, text: str, items: Sequence[str] = ()) -> None:
        """Befund, den der Befehl bewusst nicht ändert."""
        self.stdout.write(self.style.WARNING(f"  HINWEIS: {text}"))
        self._list(items)

    def _list(self, items: Sequence[str]) -> None:
        for item in items[:LIST_LIMIT]:
            self.stdout.write(f"       - {item}")
        if len(items) > LIST_LIMIT:
            self.stdout.write(f"       … und {len(items) - LIST_LIMIT} weitere")

    # ------------------------------------------------------------------
    # [1] Rechtekatalog
    # ------------------------------------------------------------------

    def _check_permissions(self) -> None:
        self.stdout.write(self.style.HTTP_INFO("\n[1] RECHTEKATALOG"))
        self.stdout.write("-" * 40)

        in_db = {p.codename: p for p in Permission.objects.all()}
        self.stdout.write(f"  Berechtigungen im Code:       {len(PERMISSIONS)}")
        self.stdout.write(f"  Berechtigungen im Datenbestand: {len(in_db)}")

        missing = sorted(set(PERMISSIONS) - set(in_db))
        outdated = sorted(
            code
            for code, name in PERMISSIONS.items()
            if code in in_db and (in_db[code].name != name or in_db[code].category != code.split(".")[0])
        )
        extra = sorted(set(in_db) - set(PERMISSIONS))

        if missing:
            self._change(f"{len(missing)} fehlende Berechtigung(en) anlegen", missing)
        if outdated:
            self._change(f"Name/Kategorie von {len(outdated)} Berechtigung(en) nach dem Code angleichen", outdated)
        if extra:
            self._note(f"{len(extra)} Berechtigung(en) nur im Datenbestand, nicht im Code – bleiben unverändert", extra)
        if (missing or outdated) and self.fix:
            cast(Any, Permission).sync_permissions()
        if not missing and not outdated:
            self.stdout.write(self.style.SUCCESS("  OK – Katalog entspricht dem Code"))

    # ------------------------------------------------------------------
    # [2] Organisationen
    # ------------------------------------------------------------------

    def _check_organization(self, org: Organization, assign_role: Role | None) -> None:
        self.stdout.write(self.style.HTTP_INFO(f"\n[2] ORGANISATION: {org.name} ({org.slug})"))
        self.stdout.write("-" * 40)

        roles = list(Role.objects.filter(organization=org).annotate(permission_count=Count("permissions")))
        names = {role.name for role in roles}
        default_names = [str(config["name"]) for config in DEFAULT_ROLES.values()]
        missing_roles = [name for name in default_names if name not in names]
        self.stdout.write(f"  Standardrollen: {len(DEFAULT_ROLES)}, vorhandene Rollen: {len(roles)}")

        admin_roles = [role.name for role in roles if role.is_admin]
        if admin_roles:
            self.stdout.write(f"  Rolle(n) mit Vollzugriff: {', '.join(sorted(admin_roles))}")
        else:
            self._note("Keine Rolle mit Vollzugriff – wird niemandem automatisch zugewiesen")

        if missing_roles:
            self._change(
                f"{len(missing_roles)} fehlende Standardrolle(n) anlegen (ohne Zuweisung an Mitglieder)", missing_roles
            )
            if self.fix:
                Role.restore_missing_default_roles(org)

        empty = sorted(role.name for role in roles if not role.is_admin and not getattr(role, "permission_count", 0))
        if empty:
            self._note(
                "Rolle(n) ohne Berechtigungen – bleiben unverändert (Standard bei Bedarf in der "
                "Rollenverwaltung wiederherstellen)",
                empty,
            )
        behind = self._roles_behind_default(roles)
        if behind:
            self._note(
                "Standardrolle(n) ohne Rechte, die der heutige Standard vorsieht – bleiben unverändert (bei Bedarf "
                "in der Rollenverwaltung ergänzen oder auf den Standard zurücksetzen)",
                behind,
            )
        if not missing_roles and not empty and not behind and admin_roles:
            self.stdout.write(self.style.SUCCESS("  OK – Rollen vollständig"))

        self._check_memberships(org, assign_role)

    @staticmethod
    def _roles_behind_default(roles: Sequence[Role]) -> list[str]:
        """Standardrollen (nach Namen), denen Rechte aus ``DEFAULT_ROLES`` fehlen, als „Rolle: recht, …“."""
        behind = []
        for role in sorted(roles, key=lambda r: r.name):
            definition = Role.get_default_definition(role.name)
            if definition is None or role.is_admin or not getattr(role, "permission_count", 0):
                continue
            granted = set(role.permissions.values_list("codename", flat=True))
            missing = sorted(set(cast(list[str], definition.get("permissions", []))) - granted)
            if missing:
                behind.append(f"{role.name}: {', '.join(missing)}")
        return behind

    def _check_memberships(self, org: Organization, assign_role: Role | None) -> None:
        memberships: QuerySet[Membership] = (
            Membership.objects.filter(organization=org, is_active=True)
            .select_related("user")
            .annotate(role_count=Count("roles"))
            .order_by("user__email")
        )
        active = list(memberships)
        guests = [m for m in active if m.is_guest]
        self.stdout.write(f"\n  Aktive Mitgliedschaften: {len(active)} (davon Gast-Zugänge: {len(guests)})")

        guests_with_roles = [m.user.email for m in guests if getattr(m, "role_count", 0)]
        if guests_with_roles:
            self._note("Gast-Zugänge mit Rollen (vorgesehen sind keine) – bleiben unverändert", guests_with_roles)

        without_role = [m for m in active if not m.is_guest and not getattr(m, "role_count", 0)]
        if not without_role:
            self.stdout.write(self.style.SUCCESS("  OK – alle Mitglieder (ohne Gäste) haben eine Rolle"))
            return

        emails = [m.user.email for m in without_role]
        if assign_role is None:
            self._note(
                f"{len(without_role)} Mitgliedschaft(en) ohne Rolle – es wird keine Rolle automatisch vergeben "
                "(gezielt: Mitgliederverwaltung oder --org <slug> --assign-role <Rolle>)",
                emails,
            )
            return

        self._change(
            f"Rolle '{assign_role.name}' an {len(without_role)} Mitgliedschaft(en) ohne Rolle vergeben", emails
        )
        if self.fix:
            for membership in without_role:
                membership.roles.add(assign_role)

    # ------------------------------------------------------------------
    # [3] Einzelne Person
    # ------------------------------------------------------------------

    def _check_user(self, user_email: str, org_slug: str | None) -> None:
        """Rollen und wichtige Rechte einer Person anzeigen; ändert nichts."""
        self.stdout.write(self.style.HTTP_INFO(f"\n[3] PERSON: {user_email}"))
        self.stdout.write("-" * 40)

        user = User.objects.filter(email=user_email).first()
        if user is None:
            self.stdout.write(self.style.ERROR("  Konto nicht gefunden."))
            return
        self.stdout.write(f"  Konto-ID: {user.id}")
        self.stdout.write(f"  Name: {user.get_full_name() or '–'}")

        memberships = Membership.objects.filter(user=user).select_related("organization")
        if org_slug:
            memberships = memberships.filter(organization__slug=org_slug)
        if not memberships.exists():
            self.stdout.write(self.style.ERROR("  Keine Mitgliedschaften gefunden."))
            return

        for membership in memberships:
            self.stdout.write(f"\n  Organisation: {membership.organization.name}")
            self.stdout.write(f"  Aktiv: {'ja' if membership.is_active else 'nein'}")
            if membership.is_guest:
                self.stdout.write("  Gast-Zugang: ja (ohne Rollen vorgesehen)")
            role_names = [role.name for role in membership.roles.all()]
            self.stdout.write(f"  Rollen: {', '.join(role_names) or 'keine'}")

            checker = cast(Any, PermissionChecker)(membership)
            self.stdout.write("\n  Rechte:")
            for permission in KEY_PERMISSIONS:
                has = checker.has_permission(permission)
                status = self.style.SUCCESS("ja") if has else self.style.ERROR("nein")
                self.stdout.write(f"    {permission}: {status}")
            is_admin = checker.is_admin()
            self.stdout.write(f"\n  Vollzugriff: {self.style.SUCCESS('ja') if is_admin else 'nein'}")

            if not role_names and not membership.is_guest:
                self._note(
                    "Keine Rolle – Rollen werden hier nicht vergeben, bitte in der Mitgliederverwaltung zuweisen"
                )
