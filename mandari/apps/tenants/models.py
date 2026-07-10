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
    abbreviation = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="Kürzel",
        help_text="z.B. SPD, B90/Grüne, Volt",
    )
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Hierarchy (self-referencing for nested structure)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordnete Gruppe",
    )

    # Branding (inherited by child organizations unless overridden)
    logo = models.ImageField(upload_to="parties/logos/", blank=True, null=True, verbose_name="Logo")
    primary_color = models.CharField(max_length=7, default="#6366f1", verbose_name="Primärfarbe")
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

    # Partei (FK, Pflicht bei aktiven Organisationen).
    # Jede Organisation gehört genau EINER Partei an; das M2M `parties`
    # dient nur der Vernetzung (vertikaler/horizontaler Austausch).
    party_group = models.ForeignKey(
        PartyGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organizations",
        verbose_name="Partei",
        help_text="Partei, der die Organisation angehört (Pflicht bei aktiven Organisationen)",
    )

    # Heimat-Kommune (FK, Pflicht bei aktiven Organisationen).
    # Jede Organisation gehört genau EINER Stadt/Kommune an; das M2M
    # `bodies` gewährt zusätzlichen RIS-Zugriff auf weitere Kommunen.
    body = models.ForeignKey(
        "insight_core.OParlBody",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_organizations",
        verbose_name="Stadt/Kommune",
        help_text="Heimat-Kommune der Organisation (Pflicht bei aktiven Organisationen)",
    )

    # Weitere Kommunen (M2M) — nur für RIS-Zugriff: eine Organisation kann
    # zusätzlich zur Heimat-Kommune weitere Ratsinformationssysteme einsehen.
    bodies = models.ManyToManyField(
        "insight_core.OParlBody",
        blank=True,
        related_name="linked_work_organizations",
        verbose_name="Weitere Kommunen (RIS-Zugriff)",
        help_text="Weitere OParl-Bodies, deren Ratsinformationen die Organisation einsehen kann",
    )

    # Partei-Zugehörigkeit (M2M) — eine Organisation kann einer oder
    # mehreren Parteien/Parteigruppen angehören. Mehrere Organisationen
    # können dieselbe Partei teilen (Basis für vertikalen Austausch:
    # gleiche Partei, verschiedene Kommunen — und horizontalen Austausch:
    # gleiche Kommune, verschiedene Parteien).
    parties = models.ManyToManyField(
        PartyGroup,
        blank=True,
        related_name="member_organizations",
        verbose_name="Parteien",
        help_text="Parteien/Parteigruppen, denen die Organisation angehört",
    )

    # Optional: specific OParl organizations (factions/committees)
    oparl_organizations = models.ManyToManyField(
        "insight_core.OParlOrganization",
        blank=True,
        related_name="work_organizations",
        verbose_name="OParl-Gremien",
        help_text="Verknüpfte Gremien im RIS (z.B. Fraktion, Ausschüsse)",
    )

    # === BRANDING ===

    logo = models.ImageField(upload_to="organizations/logos/", blank=True, null=True, verbose_name="Logo")
    primary_color = models.CharField(max_length=7, default="#6366f1", verbose_name="Primärfarbe")
    secondary_color = models.CharField(max_length=7, default="#8b5cf6", verbose_name="Sekundärfarbe")

    # === CONTACT ===

    contact_email = models.EmailField(blank=True, verbose_name="Kontakt-E-Mail")
    contact_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon")
    website = models.URLField(blank=True, verbose_name="Website")
    address = models.TextField(blank=True, verbose_name="Adresse")

    # Administration contact for motion submissions
    administration_email = models.EmailField(
        blank=True,
        verbose_name="Verwaltungs-E-Mail",
        help_text="Standard-E-Mail für Anträge an die Verwaltung",
    )

    # Coalition configuration
    coalition_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Koalitionsname",
        help_text="z.B. 'Ampel', 'Rot-Grün'",
    )

    # === SMTP (for sending emails from org domain) ===

    smtp_host = models.CharField(max_length=200, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_username = models.CharField(max_length=200, blank=True)
    smtp_password_encrypted = models.BinaryField(blank=True, null=True)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_from_email = models.EmailField(blank=True)
    smtp_from_name = models.CharField(max_length=200, blank=True)

    # === AI Provider Settings (Work DMS) ===
    AI_PROVIDER_NEBIUS = "nebius"
    AI_PROVIDER_OVH = "ovh"
    AI_PROVIDER_IONOS = "ionos"
    AI_PROVIDER_CHOICES = [
        (AI_PROVIDER_NEBIUS, "Nebius TokenFactory (Standard)"),
        (AI_PROVIDER_OVH, "OVHcloud AI Endpoints"),
        (AI_PROVIDER_IONOS, "IONOS AI Model Hub"),
    ]

    ai_enabled = models.BooleanField(
        default=True,
        verbose_name="KI im DMS aktiviert",
        help_text="Erlaubt KI-Chat und KI-Aktionen im Dokumenten-Editor.",
    )
    ai_provider = models.CharField(
        max_length=20,
        choices=AI_PROVIDER_CHOICES,
        default=AI_PROVIDER_NEBIUS,
        verbose_name="KI-Anbieter",
    )
    ai_base_url = models.URLField(
        blank=True,
        verbose_name="KI API Base URL",
        help_text=(
            "Optional fuer OpenAI-kompatible Endpunkte. "
            "Bei Nebius wird standardmaessig https://api.tokenfactory.nebius.com/v1/ genutzt."
        ),
    )
    ai_model = models.CharField(
        max_length=100,
        default="openai/gpt-oss-120b",
        verbose_name="KI-Modell",
        help_text="Standard fuer Work: openai/gpt-oss-120b (Nebius).",
    )
    ai_api_key_encrypted = models.BinaryField(
        blank=True,
        null=True,
        verbose_name="KI API Key (verschluesselt)",
        help_text="Wird tenant-spezifisch AES-256-GCM verschluesselt gespeichert.",
    )
    ai_token_limit_daily = models.PositiveIntegerField(
        default=250000,
        verbose_name="Token-Limit pro Tag",
    )
    ai_token_limit_weekly = models.PositiveIntegerField(
        default=1000000,
        verbose_name="Token-Limit pro Woche",
    )
    ai_token_limit_monthly = models.PositiveIntegerField(
        blank=True,
        null=True,
        default=None,
        verbose_name="Token-Limit pro Monat",
        help_text=(
            "Leer = Standard aus den globalen KI-Einstellungen (Admin → KI-Einstellungen), "
            "0 = KI für diese Organisation deaktiviert."
        ),
    )

    # === ENCRYPTION ===

    encryption_key = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Verschlüsselungsschlüssel",
        help_text="Encrypted with master key, used for tenant data",
    )

    # === SETTINGS ===

    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")
    require_2fa = models.BooleanField(
        default=False,
        verbose_name="2FA erforderlich",
        help_text="Alle Mitglieder müssen 2FA aktivieren",
    )

    # === SELF-REGISTRATION ===

    registration_enabled = models.BooleanField(
        default=False,
        verbose_name="Selbstregistrierung aktiviert",
        help_text="Nutzer können sich selbst für diese Organisation registrieren",
    )
    registration_email_domains = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Erlaubte E-Mail-Domains",
        help_text='Liste von Domains, z.B. ["volt-muenster.de"]. Leer = alle erlaubt.',
    )
    registration_auto_approve = models.BooleanField(
        default=False,
        verbose_name="Automatische Freischaltung",
        help_text="Neue Mitglieder werden sofort freigeschaltet (ohne Admin-Bestätigung)",
    )
    registration_default_role = models.ForeignKey(
        "Role",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_for_registration",
        verbose_name="Standardrolle für Registrierungen",
        help_text="Rolle, die Selbstregistrierten automatisch zugewiesen wird",
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
        help_text="Wird automatisch gesetzt wenn erste Person beitritt",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    # SaaS/Hosting: gesetzt vom Billing-Portal über die Provisioning-API
    plan = models.CharField(
        max_length=50,
        default="community",
        verbose_name="Plan",
        help_text="Abo-Plan (community = selbst gehostet/kostenlos)",
    )
    billing_reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Billing-Referenz",
        help_text="Subscription-ID im Billing-Portal",
    )
    member_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Mitglieder-Limit",
        help_text="Maximale aktive Mitglieder laut Plan (leer = unbegrenzt). Gäste zählen nicht mit.",
    )
    guest_limit = models.PositiveIntegerField(
        default=25,
        verbose_name="Gast-Limit",
        help_text="Maximale aktive Gast-Zugänge (Standard 25, per Addon erweiterbar)",
    )

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

    def clean(self):
        """
        Validierung der Pflicht-Zuordnungen.

        Jede aktive Organisation gehört genau EINER Stadt/Kommune (body)
        und EINER Partei (party_group) an. Die M2M-Felder (bodies/parties)
        sind nur Zusatz (RIS-Zugriff bzw. Vernetzung).
        """
        from django.core.exceptions import ValidationError

        super().clean()

        if self.is_active:
            errors = {}
            if not self.body_id:
                errors["body"] = "Aktive Organisationen benötigen eine Stadt/Kommune (Heimat-Kommune)."
            if not self.party_group_id:
                errors["party_group"] = "Aktive Organisationen benötigen eine Partei."
            if errors:
                raise ValidationError(errors)

    # === MITGLIEDER-/GAST-ZÄHLUNG ===

    def get_active_member_count(self) -> int:
        """Aktive reguläre Mitglieder (ohne Gäste) — zählen gegen member_limit."""
        return self.memberships.filter(is_active=True, is_guest=False).count()

    def get_active_guest_count(self) -> int:
        """Aktive Gast-Zugänge — zählen gegen guest_limit (nicht gegen member_limit)."""
        return self.memberships.filter(is_active=True, is_guest=True).count()

    def has_free_guest_slot(self) -> bool:
        """True, wenn noch ein Gast-Platz frei ist."""
        return self.get_active_guest_count() < self.guest_limit

    # === MULTI-BODY / MULTI-PARTY HELPERS ===

    def get_all_bodies(self):
        """
        Alle verknüpften Kommunen (M2M `bodies` + primärer FK `body`).

        Dedupliziertes QuerySet — nutzbar für `body__in=...`-Filter.
        """
        from insight_core.models import OParlBody

        q = models.Q(pk__in=self.bodies.values("pk"))
        if self.body_id:
            q |= models.Q(pk=self.body_id)
        return OParlBody.objects.filter(q)

    @property
    def all_bodies(self):
        """Alias für get_all_bodies() (Template-freundlich)."""
        return self.get_all_bodies()

    @property
    def all_body_ids(self) -> list:
        """Liste aller verknüpften Body-UUIDs (für ES-terms-Queries)."""
        return list(self.get_all_bodies().values_list("id", flat=True))

    def get_primary_body(self):
        """Primäre Kommune (FK), Fallback: erste verknüpfte Kommune."""
        if self.body_id:
            return self.body
        return self.get_all_bodies().order_by("name").first()

    @property
    def has_multiple_bodies(self) -> bool:
        return self.get_all_bodies().count() > 1

    def get_all_parties(self):
        """
        Alle Parteien (M2M `parties` + primärer FK `party_group`).

        Dedupliziertes QuerySet.
        """
        q = models.Q(pk__in=self.parties.values("pk"))
        if self.party_group_id:
            q |= models.Q(pk=self.party_group_id)
        return PartyGroup.objects.filter(q)

    @property
    def all_parties(self):
        """Alias für get_all_parties() (Template-freundlich)."""
        return self.get_all_parties()

    @property
    def full_party_path(self) -> str:
        """Return full party hierarchy path if in a party group."""
        if self.party_group:
            return f"{self.party_group.full_path} > {self.name}"
        return self.name

    def is_email_allowed_for_registration(self, email: str) -> bool:
        """Prüft ob eine E-Mail-Adresse für Selbstregistrierung zugelassen ist."""
        if not self.registration_enabled:
            return False
        domains = self.registration_email_domains
        if not domains:
            return True  # Keine Einschränkung
        email_domain = email.rsplit("@", 1)[-1].lower()
        return email_domain in [d.lower().strip() for d in domains]

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
        """Get organizations sharing at least one party (group)."""
        party_ids = list(self.get_all_parties().values_list("id", flat=True))
        if not party_ids:
            return Organization.objects.none()
        return (
            Organization.objects.filter(models.Q(party_group_id__in=party_ids) | models.Q(parties__id__in=party_ids))
            .exclude(id=self.id)
            .distinct()
        )

    def get_regional_siblings(self):
        """Get organizations sharing at least one OParl Body (same municipality)."""
        body_ids = self.all_body_ids
        if not body_ids:
            return Organization.objects.none()
        return (
            Organization.objects.filter(models.Q(body_id__in=body_ids) | models.Q(bodies__id__in=body_ids))
            .exclude(id=self.id)
            .distinct()
        )

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

    def set_ai_api_key(self, api_key: str):
        """Encrypt and store organization-specific AI API key."""
        from apps.common.encryption import TenantEncryption

        if not api_key:
            self.ai_api_key_encrypted = None
            return

        encryption = TenantEncryption(self)
        self.ai_api_key_encrypted = encryption.encrypt(api_key)

    def get_ai_api_key(self) -> str:
        """Get decrypted organization-specific AI API key."""
        from apps.common.encryption import TenantEncryption

        if not self.ai_api_key_encrypted:
            return ""

        encryption = TenantEncryption(self)
        return encryption.decrypt(self.ai_api_key_encrypted)

    def get_effective_ai_base_url(self) -> str:
        """Resolve provider endpoint with sensible defaults."""
        if self.ai_base_url:
            return self.ai_base_url

        if self.ai_provider == self.AI_PROVIDER_NEBIUS:
            return "https://api.tokenfactory.nebius.com/v1/"

        # For other providers, admins should set a concrete endpoint.
        return ""


class Permission(models.Model):
    """
    Permission definition for role-based access control.

    Populated from apps.common.permissions.PERMISSIONS on migration.
    """

    codename = models.CharField(max_length=100, unique=True, primary_key=True, verbose_name="Code")
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
            cls.objects.update_or_create(codename=codename, defaults={"name": name, "category": category})


class Role(models.Model):
    """
    Role with permissions for an organization.

    Roles are organization-specific, but default roles are created
    for each new organization.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="roles", verbose_name="Organisation"
    )

    name = models.CharField(max_length=100, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Permissions
    permissions = models.ManyToManyField(Permission, blank=True, related_name="roles", verbose_name="Berechtigungen")
    is_admin = models.BooleanField(default=False, verbose_name="Administrator", help_text="Hat alle Berechtigungen")

    # Settings
    is_system_role = models.BooleanField(
        default=False, verbose_name="Systemrolle", help_text="Kann nicht gelöscht werden"
    )
    priority = models.PositiveIntegerField(
        default=50, verbose_name="Priorität", help_text="Höhere Priorität bei Konflikten"
    )
    require_2fa = models.BooleanField(default=False, verbose_name="2FA erforderlich")
    color = models.CharField(max_length=7, default="#6b7280", verbose_name="Farbe")

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
            existing = cls.objects.filter(organization=organization, name=role_config["name"]).first()

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

    @classmethod
    def get_default_definition(cls, name: str) -> dict | None:
        """Standard-Rollen-Definition (aus DEFAULT_ROLES) zu einem Rollennamen."""
        for role_config in DEFAULT_ROLES.values():
            if role_config["name"] == name:
                return role_config
        return None

    @property
    def has_default_definition(self) -> bool:
        """True, wenn der Rollenname einer Standard-Rolle entspricht (Template-freundlich)."""
        return Role.get_default_definition(self.name) is not None

    def reset_to_default(self) -> bool:
        """
        Setzt diese Rolle auf ihre Standard-Definition (setup_roles) zurück.

        Nur möglich für Rollen, deren Name einer Standard-Rolle entspricht.
        Returns False, wenn keine Standard-Definition existiert.
        """
        role_config = Role.get_default_definition(self.name)
        if role_config is None:
            return False

        self.description = role_config.get("description", "")
        self.is_system_role = role_config.get("is_system_role", False)
        self.is_admin = role_config.get("is_admin", False)
        self.priority = role_config.get("priority", 50)
        self.color = role_config.get("color", "#6b7280")
        self.save()

        permission_codes = role_config.get("permissions", [])
        permissions = Permission.objects.filter(codename__in=permission_codes)
        self.permissions.set(permissions)
        return True

    @classmethod
    def restore_missing_default_roles(cls, organization) -> list:
        """
        Legt fehlende Standard-Rollen für eine Organisation an.

        Bestehende Rollen (auch angepasste) bleiben unverändert —
        anders als create_default_roles(), das Bestand überschreibt.
        """
        created_roles = []
        for role_config in DEFAULT_ROLES.values():
            if cls.objects.filter(organization=organization, name=role_config["name"]).exists():
                continue
            role = cls.objects.create(
                organization=organization,
                name=role_config["name"],
                description=role_config.get("description", ""),
                is_system_role=role_config.get("is_system_role", False),
                is_admin=role_config.get("is_admin", False),
                priority=role_config.get("priority", 50),
                color=role_config.get("color", "#6b7280"),
            )
            permission_codes = role_config.get("permissions", [])
            permissions = Permission.objects.filter(codename__in=permission_codes)
            role.permissions.set(permissions)
            created_roles.append(role)
        return created_roles


class Topic(models.Model):
    """
    Themenkatalog einer Organisation.

    Themen werden Dokumenten (work.Motion.topics) zugeordnet und von
    Mitgliedern als Fachgebiete (Membership.expertise_topics) gewählt.
    Lebt in tenants, da sowohl Membership als auch die work-App darauf
    zugreifen und work bereits von tenants abhängt (keine neue
    App-Abhängigkeit in Gegenrichtung).
    """

    COLOR_CHOICES = [
        ("red", "Rot"),
        ("orange", "Orange"),
        ("amber", "Gelb"),
        ("green", "Grün"),
        ("teal", "Türkis"),
        ("blue", "Blau"),
        ("indigo", "Indigo"),
        ("purple", "Lila"),
        ("pink", "Rosa"),
        ("gray", "Grau"),
    ]

    # Rotations-Palette für automatisch angelegte Themen (Datenmigration, get_or_create)
    COLOR_PALETTE = ["blue", "green", "amber", "purple", "teal", "pink", "orange", "indigo"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="topics",
        verbose_name="Organisation",
    )
    name = models.CharField(max_length=100, verbose_name="Name")
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default="blue", verbose_name="Farbe")
    sort_order = models.IntegerField(default=0, verbose_name="Sortierung")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Thema"
        verbose_name_plural = "Themen"
        unique_together = [["organization", "name"]]
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


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
        verbose_name="Benutzer",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Organisation",
    )

    # Roles (multiple roles possible)
    roles = models.ManyToManyField(Role, blank=True, related_name="memberships", verbose_name="Rollen")

    # Individual permissions (in addition to role permissions)
    individual_permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="individual_memberships",
        verbose_name="Individuelle Berechtigungen",
        help_text="Zusätzlich zu Rollenberechtigungen",
    )

    # Denied permissions (override role permissions)
    denied_permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="denied_memberships",
        verbose_name="Verweigerte Berechtigungen",
        help_text="Explizit verweigert, auch wenn Rolle sie hat",
    )

    # Optional link to OParl person
    oparl_person = models.ForeignKey(
        "insight_core.OParlPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_memberships",
        verbose_name="OParl-Person",
        help_text="Verknüpfung zur Person im RIS",
    )

    # Assigned OParl committees/organizations (Gremien)
    # Determines which meetings appear in "Meine Sitzungen"
    oparl_committees = models.ManyToManyField(
        "insight_core.OParlOrganization",
        blank=True,
        related_name="work_memberships",
        verbose_name="Zugewiesene Gremien",
        help_text="OParl-Gremien deren Sitzungen angezeigt werden",
    )

    # Personally followed OParl committees ("Meine Gremien")
    # Freely editable by the member (unlike oparl_committees, which is
    # admin-assigned). Used to personalize dashboard meeting/document lists.
    followed_organizations = models.ManyToManyField(
        "insight_core.OParlOrganization",
        blank=True,
        related_name="following_memberships",
        verbose_name="Meine Gremien",
        help_text="Vom Mitglied selbst gewählte Gremien für personalisierte Ansichten",
    )

    # Fachgebiete: Themen, in denen das Mitglied Kompetenz hat.
    # Vom Mitglied selbst im Profil wählbar, zusätzlich von Admins in der
    # Mitgliederverwaltung pflegbar. Grundlage der "Kompetenz im Thema"-Kachel.
    expertise_topics = models.ManyToManyField(
        Topic,
        blank=True,
        related_name="experts",
        verbose_name="Fachgebiete",
        help_text="Themen, in denen dieses Mitglied Fachwissen hat",
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    is_guest = models.BooleanField(
        default=False,
        verbose_name="Gast",
        help_text=(
            "Gast-Zugang: sieht ausschließlich explizit freigegebene Dokumente, "
            "hat keine Rollen und keine Berechtigungen. Zählt gegen das Gast-Limit."
        ),
    )
    is_sworn_in = models.BooleanField(
        default=False,
        verbose_name="Vereidigt",
        help_text="Zugang zu nicht-öffentlichen Inhalten nach Verpflichtungserklärung",
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
        verbose_name="Eingeladen von",
    )
    invitation_accepted_at = models.DateTimeField(blank=True, null=True, verbose_name="Einladung angenommen")

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
            if self.is_guest and self.roles.exists():
                raise ValidationError("Gast-Zugänge dürfen keine Rollen haben.")
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
            raise ValueError(f"Cannot add role '{role.name}' - it belongs to a different organization")
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
        verbose_name="Organisation",
    )

    # Invitation details
    email = models.EmailField(verbose_name="E-Mail")
    token = models.CharField(max_length=64, unique=True)

    # Pre-assigned roles
    roles = models.ManyToManyField(Role, blank=True, related_name="invitations", verbose_name="Rollen")

    # Personal message
    message = models.TextField(
        blank=True,
        verbose_name="Nachricht",
        help_text="Persönliche Nachricht in der Einladungs-E-Mail",
    )

    # Status
    invited_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_invitations",
        verbose_name="Eingeladen von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(verbose_name="Gültig bis")
    accepted_at = models.DateTimeField(blank=True, null=True)
    accepted_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_invitations",
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
        valid_days: int = 7,
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
                    raise ValueError(f"Role '{role.name}' does not belong to organization '{organization.name}'")
            invitation.roles.set(roles)

        return invitation


class AdministrationContact(models.Model):
    """
    Verwaltungs-E-Mail-Empfänger für Anträge/Änderungsanträge.

    Mehrere Empfänger pro Organisation möglich (z.B. Ratsbüro, OB-Büro).
    Anträge werden immer an alle hinterlegten Kontakte versendet.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "Organization",
        on_delete=models.CASCADE,
        related_name="administration_contacts",
        verbose_name="Organisation",
    )

    label = models.CharField(max_length=200, verbose_name="Bezeichnung", help_text="z.B. Ratsbüro, OB-Büro")
    email = models.EmailField(verbose_name="E-Mail")
    order = models.IntegerField(default=0, verbose_name="Reihenfolge")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Verwaltungskontakt"
        verbose_name_plural = "Verwaltungskontakte"
        ordering = ["order", "label"]

    def __str__(self):
        return f"{self.label} <{self.email}>"


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
        verbose_name="Organisation",
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    short_name = models.CharField(max_length=20, verbose_name="Kurzname")

    # Contact information
    email = models.EmailField(blank=True, verbose_name="E-Mail", help_text="E-Mail für Antragsversand")
    contact_name = models.CharField(max_length=200, blank=True, verbose_name="Ansprechpartner")
    contact_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon")

    # Branding
    color = models.CharField(max_length=7, default="#6b7280", verbose_name="Farbe")

    # Coalition membership
    is_coalition_member = models.BooleanField(default=False, verbose_name="Koalitionspartner")
    coalition_order = models.IntegerField(default=0, verbose_name="Reihenfolge in Koalition")

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
