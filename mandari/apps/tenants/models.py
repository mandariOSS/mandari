# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tenant models for multi-organization support.

Implements a dual grouping system:
1. Party Hierarchy: Organizations can belong to a party structure
   (e.g., Volt Deutschland → Volt NRW → Volt Münster)
2. Regional Grouping: Organizations can be linked to an OParl Body
   (e.g., Stadt Münster has multiple faction organizations)

An organization can belong to BOTH hierarchies simultaneously,
enabling features like:
- Party-wide motion sharing (e.g., share with all Volt organizations)
- Regional collaboration (e.g., coalition work within a municipality)
"""

import uuid

from django.conf import settings as django_settings
from django.db import models
from django.utils.text import slugify

from apps.common.permissions import DEFAULT_ROLES, PERMISSIONS


class PartyGroup(models.Model):
    """
    Party hierarchy for grouping organizations.

    Represents a political party or umbrella organization at various levels:
    - Federal level (e.g., "Volt Deutschland")
    - State level (e.g., "Volt NRW")
    - Can be nested to any depth

    This is NOT the actual workspace (that's Organization).
    PartyGroup is only for hierarchical grouping.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Basic info
    name = models.CharField(max_length=200, verbose_name="Name")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="URL-Slug")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Hierarchy (self-referencing for nested structure)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordnete Gruppe"
    )

    # Branding (inherited by child organizations unless overridden)
    logo = models.ImageField(
        upload_to="parties/logos/",
        blank=True,
        null=True,
        verbose_name="Logo"
    )
    primary_color = models.CharField(
        max_length=7,
        default="#6366f1",
        verbose_name="Primärfarbe"
    )
    website = models.URLField(blank=True, verbose_name="Website")

    # Settings (inherited by children)
    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Parteigruppe"
        verbose_name_plural = "Parteigruppen"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def full_path(self) -> str:
        """Return the full hierarchical path (e.g., 'Volt > NRW > Münster')."""
        parts = [self.name]
        current = self.parent
        while current:
            parts.insert(0, current.name)
            current = current.parent
        return " > ".join(parts)

    @property
    def level(self) -> int:
        """Return the hierarchy level (0 = root)."""
        level = 0
        current = self.parent
        while current:
            level += 1
            current = current.parent
        return level

    def get_ancestors(self):
        """Return all ancestor groups (parent, grandparent, etc.)."""
        ancestors = []
        current = self.parent
        while current:
            ancestors.append(current)
            current = current.parent
        return ancestors

    def get_descendants(self):
        """Return all descendant groups (children, grandchildren, etc.)."""
        descendants = list(self.children.all())
        for child in self.children.all():
            descendants.extend(child.get_descendants())
        return descendants

    def get_all_organizations(self):
        """Return all organizations in this group and descendants."""
        org_ids = [self.id] + [d.id for d in self.get_descendants()]
        return Organization.objects.filter(party_group_id__in=org_ids)


class Organization(models.Model):
    """
    The actual tenant/workspace for a political organization.

    This is where users work - it has members, motions, meetings, etc.

    Organizations can optionally belong to:
    1. A PartyGroup (party hierarchy)
    2. An OParlBody (regional grouping)
    3. Both (most common for local political groups)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Basic info
    name = models.CharField(max_length=200, verbose_name="Name")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="URL-Slug")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # === DUAL GROUPING ===

    # Party hierarchy (optional)
    party_group = models.ForeignKey(
        PartyGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organizations",
        verbose_name="Parteigruppe",
        help_text="Übergeordnete Parteistruktur (z.B. Volt NRW)"
    )

    # Regional grouping via OParl Body (optional)
    body = models.ForeignKey(
        "insight_core.OParlBody",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_organizations",
        verbose_name="Kommune/Region",
        help_text="OParl-Body für RIS-Daten (z.B. Stadt Münster)"
    )

    # Optional: specific OParl organizations (factions/committees)
    oparl_organizations = models.ManyToManyField(
        "insight_core.OParlOrganization",
        blank=True,
        related_name="work_organizations",
        verbose_name="OParl-Gremien",
        help_text="Verknüpfte Gremien im RIS (z.B. Fraktion, Ausschüsse)"
    )

    # === BRANDING ===

    logo = models.ImageField(
        upload_to="organizations/logos/",
        blank=True,
        null=True,
        verbose_name="Logo"
    )
    primary_color = models.CharField(
        max_length=7,
        default="#6366f1",
        verbose_name="Primärfarbe"
    )
    secondary_color = models.CharField(
        max_length=7,
        default="#8b5cf6",
        verbose_name="Sekundärfarbe"
    )

    # === CONTACT ===

    contact_email = models.EmailField(blank=True, verbose_name="Kontakt-E-Mail")
    contact_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon")
    website = models.URLField(blank=True, verbose_name="Website")
    address = models.TextField(blank=True, verbose_name="Adresse")

    # Administration contact for motion submissions
    administration_email = models.EmailField(
        blank=True,
        verbose_name="Verwaltungs-E-Mail",
        help_text="Standard-E-Mail für Anträge an die Verwaltung"
    )

    # Coalition configuration
    coalition_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Koalitionsname",
        help_text="z.B. 'Ampel', 'Rot-Grün'"
    )

    # === SMTP (for sending emails from org domain) ===

    smtp_host = models.CharField(max_length=200, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_username = models.CharField(max_length=200, blank=True)
    smtp_password_encrypted = models.BinaryField(blank=True, null=True)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_from_email = models.EmailField(blank=True)
    smtp_from_name = models.CharField(max_length=200, blank=True)

    # === ENCRYPTION ===

    encryption_key = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Verschlüsselungsschlüssel",
        help_text="Encrypted with master key, used for tenant data"
    )

    # === SETTINGS ===

    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")
    require_2fa = models.BooleanField(
        default=False,
        verbose_name="2FA erforderlich",
        help_text="Alle Mitglieder müssen 2FA aktivieren"
    )

    # === STATUS ===

    # Owner is nullable for GDPR compliance:
    # - Admin can create organization structure without personal data
    # - First member to join via Work portal becomes owner
    # - This ensures personal data stays in Work, not Django Admin
    owner = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_organizations",
        verbose_name="Eigentümer",
        help_text="Wird automatisch gesetzt wenn erste Person beitritt"
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Organisation"
        verbose_name_plural = "Organisationen"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def full_party_path(self) -> str:
        """Return full party hierarchy path if in a party group."""
        if self.party_group:
            return f"{self.party_group.full_path} > {self.name}"
        return self.name

    @property
    def effective_primary_color(self) -> str:
        """Get primary color (inherit from party if not set)."""
        if self.primary_color and self.primary_color != "#6366f1":
            return self.primary_color
        if self.party_group:
            return self.party_group.primary_color
        return self.primary_color

    @property
    def effective_logo(self):
        """Get logo (inherit from party if not set)."""
        if self.logo:
            return self.logo
        if self.party_group and self.party_group.logo:
            return self.party_group.logo
        return None

    def get_party_siblings(self):
        """Get organizations in the same party group."""
        if not self.party_group:
            return Organization.objects.none()
        return Organization.objects.filter(
            party_group=self.party_group
        ).exclude(id=self.id)

    def get_regional_siblings(self):
        """Get organizations in the same OParl Body (same municipality)."""
        if not self.body:
            return Organization.objects.none()
        return Organization.objects.filter(
            body=self.body
        ).exclude(id=self.id)

    def get_party_ancestry_organizations(self):
        """Get all organizations in parent party groups."""
        if not self.party_group:
            return Organization.objects.none()

        ancestor_ids = [g.id for g in self.party_group.get_ancestors()]
        return Organization.objects.filter(party_group_id__in=ancestor_ids)

    def get_encryption_organization(self):
        """Required for EncryptionMixin compatibility."""
        return self

    def set_smtp_password(self, password: str):
        """
        Encrypt and store SMTP password.

        Security: Uses tenant-specific AES-256-GCM encryption.
        """
        from apps.common.encryption import TenantEncryption

        if not password:
            self.smtp_password_encrypted = None
            return

        encryption = TenantEncryption(self)
        self.smtp_password_encrypted = encryption.encrypt(password)

    def get_smtp_password(self) -> str:
        """
        Decrypt and return SMTP password.

        Security: Uses tenant-specific AES-256-GCM encryption.
        """
        from apps.common.encryption import TenantEncryption

        if not self.smtp_password_encrypted:
            return ""

        encryption = TenantEncryption(self)
        return encryption.decrypt(self.smtp_password_encrypted)


class Permission(models.Model):
    """
    Permission definition for role-based access control.

    Populated from apps.common.permissions.PERMISSIONS on migration.
    """

    codename = models.CharField(
        max_length=100,
        unique=True,
        primary_key=True,
        verbose_name="Code"
    )
    name = models.CharField(max_length=200, verbose_name="Name")
    category = models.CharField(max_length=50, verbose_name="Kategorie")

    class Meta:
        verbose_name = "Berechtigung"
        verbose_name_plural = "Berechtigungen"
        ordering = ["category", "codename"]

    def __str__(self):
        return f"{self.name} ({self.codename})"

    @classmethod
    def sync_permissions(cls):
        """Synchronize permissions from PERMISSIONS dict."""
        for codename, name in PERMISSIONS.items():
            category = codename.split(".")[0]
            cls.objects.update_or_create(
                codename=codename,
                defaults={"name": name, "category": category}
            )


class Role(models.Model):
    """
    Role with permissions for an organization.

    Roles are organization-specific, but default roles are created
    for each new organization.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="roles",
        verbose_name="Organisation"
    )

    name = models.CharField(max_length=100, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Permissions
    permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="roles",
        verbose_name="Berechtigungen"
    )
    is_admin = models.BooleanField(
        default=False,
        verbose_name="Administrator",
        help_text="Hat alle Berechtigungen"
    )

    # Settings
    is_system_role = models.BooleanField(
        default=False,
        verbose_name="Systemrolle",
        help_text="Kann nicht gelöscht werden"
    )
    priority = models.PositiveIntegerField(
        default=50,
        verbose_name="Priorität",
        help_text="Höhere Priorität bei Konflikten"
    )
    require_2fa = models.BooleanField(
        default=False,
        verbose_name="2FA erforderlich"
    )
    color = models.CharField(
        max_length=7,
        default="#6b7280",
        verbose_name="Farbe"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Rolle"
        verbose_name_plural = "Rollen"
        unique_together = ["organization", "name"]
        ordering = ["-priority", "name"]

    def __str__(self):
        return f"{self.name} ({self.organization.name})"

    @classmethod
    def create_default_roles(cls, organization):
        """
        Create default roles for a new organization.

        Uses the DEFAULT_ROLES from apps.common.permissions which define
        the standard faction roles (Vorsitz, Mitglied, Sachkundige, etc.).
        """
        created_roles = []

        for role_key, role_config in DEFAULT_ROLES.items():
            # Check if role already exists
            existing = cls.objects.filter(
                organization=organization,
                name=role_config["name"]
            ).first()

            if existing:
                # Update existing role
                existing.description = role_config.get("description", "")
                existing.is_system_role = role_config.get("is_system_role", False)
                existing.is_admin = role_config.get("is_admin", False)
                existing.priority = role_config.get("priority", 50)
                existing.color = role_config.get("color", "#6b7280")
                existing.save()

                # Update permissions
                permission_codes = role_config.get("permissions", [])
                permissions = Permission.objects.filter(codename__in=permission_codes)
                existing.permissions.set(permissions)
                created_roles.append(existing)
            else:
                # Create new role
                role = cls.objects.create(
                    organization=organization,
                    name=role_config["name"],
                    description=role_config.get("description", ""),
                    is_system_role=role_config.get("is_system_role", False),
                    is_admin=role_config.get("is_admin", False),
                    priority=role_config.get("priority", 50),
                    color=role_config.get("color", "#6b7280"),
                )

                # Add permissions
                permission_codes = role_config.get("permissions", [])
                permissions = Permission.objects.filter(codename__in=permission_codes)
                role.permissions.set(permissions)
                created_roles.append(role)

        return created_roles


class Membership(models.Model):
    """
    User membership in an organization.

    Links users to organizations with role-based permissions.
    A user can be a member of multiple organizations.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Benutzer"
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Organisation"
    )

    # Roles (multiple roles possible)
    roles = models.ManyToManyField(
        Role,
        blank=True,
        related_name="memberships",
        verbose_name="Rollen"
    )

    # Individual permissions (in addition to role permissions)
    individual_permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="individual_memberships",
        verbose_name="Individuelle Berechtigungen",
        help_text="Zusätzlich zu Rollenberechtigungen"
    )

    # Denied permissions (override role permissions)
    denied_permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="denied_memberships",
        verbose_name="Verweigerte Berechtigungen",
        help_text="Explizit verweigert, auch wenn Rolle sie hat"
    )

    # Optional link to OParl person
    oparl_person = models.ForeignKey(
        "insight_core.OParlPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_memberships",
        verbose_name="OParl-Person",
        help_text="Verknüpfung zur Person im RIS"
    )

    # Assigned OParl committees/organizations (Gremien)
    # Determines which meetings appear in "Meine Sitzungen"
    oparl_committees = models.ManyToManyField(
        "insight_core.OParlOrganization",
        blank=True,
        related_name="work_memberships",
        verbose_name="Zugewiesene Gremien",
        help_text="OParl-Gremien deren Sitzungen angezeigt werden"
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    is_sworn_in = models.BooleanField(
        default=False,
        verbose_name="Vereidigt",
        help_text="Zugang zu nicht-öffentlichen Inhalten nach Verpflichtungserklärung"
    )

    # Timestamps
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name="Beigetreten")
    updated_at = models.DateTimeField(auto_now=True)

    # Invitation tracking
    invited_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_invitations",
        verbose_name="Eingeladen von"
    )
    invitation_accepted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Einladung angenommen"
    )

    class Meta:
        verbose_name = "Mitgliedschaft"
        verbose_name_plural = "Mitgliedschaften"
        unique_together = ["user", "organization"]
        ordering = ["-joined_at"]

    def __str__(self):
        return f"{self.user.email} @ {self.organization.name}"

    def clean(self):
        """
        Validate that roles belong to the same organization.

        Security: Prevents cross-organization role assignment.
        """
        from django.core.exceptions import ValidationError

        if self.pk:  # Only check for existing memberships
            for role in self.roles.all():
                if role.organization_id != self.organization_id:
                    raise ValidationError(
                        f"Role '{role.name}' belongs to a different organization. "
                        "Roles must belong to the same organization as the membership."
                    )

    def add_role(self, role):
        """
        Safely add a role to this membership.

        Security: Validates role belongs to the same organization.
        """
        if role.organization_id != self.organization_id:
            raise ValueError(
                f"Cannot add role '{role.name}' - it belongs to a different organization"
            )
        self.roles.add(role)

    def has_permission(self, permission: str) -> bool:
        """
        Check if this membership has a specific permission.

        Uses the PermissionChecker for consistent permission evaluation.
        """
        from apps.common.permissions import PermissionChecker
        return PermissionChecker(self).has_permission(permission)


class UserInvitation(models.Model):
    """
    Invitation for a user to join an organization.

    Used when inviting someone who may or may not have an account yet.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
        verbose_name="Organisation"
    )

    # Invitation details
    email = models.EmailField(verbose_name="E-Mail")
    token = models.CharField(max_length=64, unique=True)

    # Pre-assigned roles
    roles = models.ManyToManyField(
        Role,
        blank=True,
        related_name="invitations",
        verbose_name="Rollen"
    )

    # Personal message
    message = models.TextField(
        blank=True,
        verbose_name="Nachricht",
        help_text="Persönliche Nachricht in der Einladungs-E-Mail"
    )

    # Status
    invited_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_invitations",
        verbose_name="Eingeladen von"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(verbose_name="Gültig bis")
    accepted_at = models.DateTimeField(blank=True, null=True)
    accepted_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_invitations"
    )

    class Meta:
        verbose_name = "Einladung"
        verbose_name_plural = "Einladungen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Einladung für {self.email} zu {self.organization.name}"

    @property
    def is_valid(self) -> bool:
        """Check if invitation is still valid."""
        from django.utils import timezone
        return self.accepted_at is None and timezone.now() < self.expires_at

    @classmethod
    def create_for_organization(
        cls,
        organization,
        email: str,
        invited_by,
        roles=None,
        message: str = "",
        valid_days: int = 7
    ):
        """
        Create a new invitation with a secure token.

        Security: Uses cryptographically secure token generation.
        """
        import secrets
        from datetime import timedelta
        from django.utils import timezone

        token = secrets.token_urlsafe(48)
        expires_at = timezone.now() + timedelta(days=valid_days)

        invitation = cls.objects.create(
            organization=organization,
            email=email.lower().strip(),
            token=token,
            invited_by=invited_by,
            message=message,
            expires_at=expires_at,
        )

        if roles:
            # Security: Validate roles belong to the organization
            for role in roles:
                if role.organization_id != organization.id:
                    invitation.delete()
                    raise ValueError(
                        f"Role '{role.name}' does not belong to organization '{organization.name}'"
                    )
            invitation.roles.set(roles)

        return invitation


class CouncilParty(models.Model):
    """
    Council party/faction for coalition management.

    Represents a political party/faction in the local council.
    Used for sharing motions with coalition partners.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="council_parties",
        verbose_name="Organisation"
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    short_name = models.CharField(max_length=20, verbose_name="Kurzname")

    # Contact information
    email = models.EmailField(
        blank=True,
        verbose_name="E-Mail",
        help_text="E-Mail für Antragsversand"
    )
    contact_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Ansprechpartner"
    )
    contact_phone = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Telefon"
    )

    # Branding
    color = models.CharField(
        max_length=7,
        default="#6b7280",
        verbose_name="Farbe"
    )

    # Coalition membership
    is_coalition_member = models.BooleanField(
        default=False,
        verbose_name="Koalitionspartner"
    )
    coalition_order = models.IntegerField(
        default=0,
        verbose_name="Reihenfolge in Koalition"
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ratsfraktion"
        verbose_name_plural = "Ratsfraktionen"
        ordering = ["coalition_order", "name"]
        unique_together = [["organization", "short_name"]]

    def __str__(self):
        coalition = " (Koalition)" if self.is_coalition_member else ""
        return f"{self.name}{coalition}"
