# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session RIS Models.

Implements the data model for the administrative RIS (Ratsinformationssystem).

Key concepts:
- SessionTenant: The administrative unit (Kommune/Verwaltung)
- All data is tenant-isolated using tenant_id foreign keys
- Sensitive data is encrypted using AES-256-GCM
- Models extend OParl entities with non-public fields

Security:
- Row-level security via tenant_id filtering
- Encrypted fields for non-public content
- Audit logging for all changes
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.common.encryption import EncryptedTextField, EncryptionMixin

# =============================================================================
# TENANT MODEL
# =============================================================================


class SessionTenant(models.Model):
    """
    Administrative unit that uses Session RIS.

    This represents a Kommune/Verwaltung that manages their own RIS.
    Can be linked to an OParlBody for public data synchronization.

    Security: All Session data is isolated by tenant_id.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Basic Info
    name = models.CharField(max_length=255, verbose_name="Name")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="URL-Slug")
    short_name = models.CharField(max_length=50, blank=True, verbose_name="Kurzname")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # OParl Connection (optional - for public data sync)
    oparl_body = models.OneToOneField(
        "insight_core.OParlBody",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_tenant",
        verbose_name="OParl-Kommune",
        help_text="Verknüpfung zur öffentlichen OParl-Schnittstelle",
    )

    # Branding
    logo = models.ImageField(
        upload_to="session/tenants/logos/",
        blank=True,
        null=True,
        verbose_name="Logo",
    )
    primary_color = models.CharField(max_length=7, default="#1e40af", verbose_name="Primärfarbe")
    secondary_color = models.CharField(max_length=7, default="#3b82f6", verbose_name="Sekundärfarbe")

    # Contact
    contact_email = models.EmailField(blank=True, verbose_name="Kontakt-E-Mail")
    contact_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon")
    website = models.URLField(blank=True, verbose_name="Website")
    address = models.TextField(blank=True, verbose_name="Adresse")

    # Encryption Key (encrypted with master key)
    encryption_key = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Verschlüsselungsschlüssel",
        help_text="AES-256 Schlüssel, verschlüsselt mit Master-Key",
    )

    # Settings
    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")

    # Bürgerportal-Veröffentlichung (Issue #36): Erst wenn der Mandant den
    # Schalter aktiviert, wird seine OParl-API als Quelle im Insight-Ingestor
    # registriert und die öffentlichen Daten fließen ins Bürgerportal.
    insight_publish = models.BooleanField(
        default=False,
        verbose_name="Im Bürgerportal veröffentlichen",
        help_text="Registriert die OParl-API dieses Mandanten als Quelle für das Insight-Bürgerportal",
    )

    # Fristen-Erinnerungen (Issue #83): Vorlaufzeiten und An/Aus je Typ.
    # Nur abweichende Werte werden gespeichert; Defaults siehe
    # REMINDER_DEFAULTS bzw. reminder_config().
    REMINDER_DEFAULTS = {
        "invitation_enabled": True,
        "invitation_days_before": 3,
        "paper_enabled": True,
        "paper_days_before": 3,
        "rsvp_enabled": True,
        "rsvp_days_before": 5,
        "resolution_enabled": True,
        "resolution_days_before": 7,
    }
    reminder_settings = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Erinnerungs-Einstellungen",
        help_text="Abweichungen von den Standard-Vorlaufzeiten für Fristen-Erinnerungen",
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_tenants"
        verbose_name = "Session-Mandant"
        verbose_name_plural = "Session-Mandanten"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def reminder_config(self) -> dict:
        """Erinnerungs-Einstellungen mit Defaults zusammenführen (Issue #83)."""
        config = dict(self.REMINDER_DEFAULTS)
        stored = self.reminder_settings if isinstance(self.reminder_settings, dict) else {}
        for key, default in self.REMINDER_DEFAULTS.items():
            value = stored.get(key, default)
            if key.endswith("_enabled"):
                config[key] = bool(value)
            else:
                try:
                    config[key] = max(0, min(60, int(value)))
                except (TypeError, ValueError):
                    config[key] = default
        return config

    def get_encryption_organization(self):
        """Required for EncryptionMixin compatibility."""
        return self


# =============================================================================
# USER & PERMISSION MODELS
# =============================================================================


class SessionRole(models.Model):
    """
    Role definition for Session users.

    Roles define what actions a user can perform within Session.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="roles",
        verbose_name="Mandant",
    )

    name = models.CharField(max_length=100, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Permission flags (explicit for clarity and security)
    # Dashboard
    can_view_dashboard = models.BooleanField(default=True, verbose_name="Dashboard anzeigen")

    # Meetings
    can_view_meetings = models.BooleanField(default=True, verbose_name="Sitzungen anzeigen")
    can_create_meetings = models.BooleanField(default=False, verbose_name="Sitzungen erstellen")
    can_edit_meetings = models.BooleanField(default=False, verbose_name="Sitzungen bearbeiten")
    can_delete_meetings = models.BooleanField(default=False, verbose_name="Sitzungen löschen")
    can_view_non_public_meetings = models.BooleanField(
        default=False, verbose_name="Nicht-öffentliche Sitzungen anzeigen"
    )

    # Papers
    can_view_papers = models.BooleanField(default=True, verbose_name="Vorlagen anzeigen")
    can_create_papers = models.BooleanField(default=False, verbose_name="Vorlagen erstellen")
    can_edit_papers = models.BooleanField(default=False, verbose_name="Vorlagen bearbeiten")
    can_delete_papers = models.BooleanField(default=False, verbose_name="Vorlagen löschen")
    can_approve_papers = models.BooleanField(default=False, verbose_name="Vorlagen freigeben")
    can_view_non_public_papers = models.BooleanField(default=False, verbose_name="Nicht-öffentliche Vorlagen anzeigen")

    # Applications (from parties)
    can_view_applications = models.BooleanField(default=True, verbose_name="Anträge anzeigen")
    can_process_applications = models.BooleanField(default=False, verbose_name="Anträge bearbeiten")

    # Protocols
    can_view_protocols = models.BooleanField(default=True, verbose_name="Protokolle anzeigen")
    can_create_protocols = models.BooleanField(default=False, verbose_name="Protokolle erstellen")
    can_edit_protocols = models.BooleanField(default=False, verbose_name="Protokolle bearbeiten")
    can_approve_protocols = models.BooleanField(default=False, verbose_name="Protokolle freigeben")

    # Attendance & Allowances
    can_manage_attendance = models.BooleanField(default=False, verbose_name="Anwesenheit verwalten")
    can_manage_allowances = models.BooleanField(default=False, verbose_name="Sitzungsgelder verwalten")

    # Administration
    can_manage_users = models.BooleanField(default=False, verbose_name="Benutzer verwalten")
    can_manage_organizations = models.BooleanField(default=False, verbose_name="Gremien verwalten")
    can_manage_settings = models.BooleanField(default=False, verbose_name="Einstellungen verwalten")
    can_view_audit_log = models.BooleanField(default=False, verbose_name="Audit-Log anzeigen")

    # API Access
    can_access_api = models.BooleanField(default=False, verbose_name="API-Zugang")
    can_access_oparl_api = models.BooleanField(default=True, verbose_name="OParl-API-Zugang")

    # Role metadata
    is_admin = models.BooleanField(
        default=False,
        verbose_name="Administrator",
        help_text="Hat alle Berechtigungen",
    )
    is_system_role = models.BooleanField(
        default=False,
        verbose_name="Systemrolle",
        help_text="Kann nicht gelöscht werden",
    )
    priority = models.PositiveIntegerField(default=50, verbose_name="Priorität")
    color = models.CharField(max_length=7, default="#6b7280", verbose_name="Farbe")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_roles"
        verbose_name = "Session-Rolle"
        verbose_name_plural = "Session-Rollen"
        unique_together = ["tenant", "name"]
        ordering = ["-priority", "name"]

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

    def has_permission(self, permission: str) -> bool:
        """Check if role has a specific permission."""
        if self.is_admin:
            return True
        return getattr(self, f"can_{permission}", False)

    @classmethod
    def create_default_roles(cls, tenant: SessionTenant) -> dict:
        """Create default roles for a new tenant."""
        roles = {}

        # Administrator
        roles["admin"] = cls.objects.create(
            tenant=tenant,
            name="Administrator",
            description="Vollzugriff auf alle Funktionen",
            is_admin=True,
            is_system_role=True,
            priority=100,
            color="#dc2626",
        )

        # Sachbearbeiter
        roles["clerk"] = cls.objects.create(
            tenant=tenant,
            name="Sachbearbeiter",
            description="Kann Sitzungen und Vorlagen verwalten",
            is_system_role=True,
            priority=70,
            color="#7c3aed",
            can_view_meetings=True,
            can_create_meetings=True,
            can_edit_meetings=True,
            can_view_non_public_meetings=True,
            can_view_papers=True,
            can_create_papers=True,
            can_edit_papers=True,
            can_view_non_public_papers=True,
            can_view_applications=True,
            can_process_applications=True,
            can_view_protocols=True,
            can_create_protocols=True,
            can_edit_protocols=True,
            can_manage_attendance=True,
        )

        # Protokollant
        roles["recorder"] = cls.objects.create(
            tenant=tenant,
            name="Protokollant",
            description="Kann Protokolle erstellen und bearbeiten",
            is_system_role=True,
            priority=60,
            color="#2563eb",
            can_view_meetings=True,
            can_view_non_public_meetings=True,
            can_view_papers=True,
            can_view_non_public_papers=True,
            can_view_protocols=True,
            can_create_protocols=True,
            can_edit_protocols=True,
            can_manage_attendance=True,
        )

        # Lesezugriff
        roles["viewer"] = cls.objects.create(
            tenant=tenant,
            name="Lesezugriff",
            description="Nur Anzeige von Informationen",
            is_system_role=True,
            priority=10,
            color="#6b7280",
            can_view_meetings=True,
            can_view_papers=True,
            can_view_applications=True,
            can_view_protocols=True,
        )

        return roles


class SessionUser(models.Model):
    """
    User membership in a Session tenant.

    Links Django users to Session tenants with role-based permissions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="session_memberships",
        verbose_name="Benutzer",
    )
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="users",
        verbose_name="Mandant",
    )

    # Roles (multiple allowed)
    roles = models.ManyToManyField(
        SessionRole,
        blank=True,
        related_name="users",
        verbose_name="Rollen",
    )

    # Optional link to OParl person
    oparl_person = models.ForeignKey(
        "insight_core.OParlPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_users",
        verbose_name="OParl-Person",
    )

    # User-specific settings
    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name="Beigetreten")
    last_access = models.DateTimeField(blank=True, null=True, verbose_name="Letzter Zugriff")

    class Meta:
        db_table = "session_users"
        verbose_name = "Session-Benutzer"
        verbose_name_plural = "Session-Benutzer"
        unique_together = ["user", "tenant"]
        ordering = ["-joined_at"]

    def __str__(self):
        return f"{self.user.email} @ {self.tenant.name}"

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission through any role."""
        for role in self.roles.all():
            if role.has_permission(permission):
                return True
        return False

    def is_admin(self) -> bool:
        """Check if user is an administrator."""
        return self.roles.filter(is_admin=True).exists()


class SessionInvitation(models.Model):
    """
    Einladung eines Benutzers in einen Session-Mandanten (Issue #27).

    Vorbild: Work-Einladungsflow (tenants.UserInvitation). Ermöglicht das
    Einladen per E-Mail mit vorbelegten Rollen — auch für Personen, die
    noch kein Konto haben.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="invitations",
        verbose_name="Mandant",
    )

    email = models.EmailField(verbose_name="E-Mail")
    token = models.CharField(max_length=64, unique=True, editable=False)

    # Vorbelegte Rollen
    roles = models.ManyToManyField(
        SessionRole,
        blank=True,
        related_name="invitations",
        verbose_name="Rollen",
    )

    invited_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
        verbose_name="Eingeladen von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(verbose_name="Gültig bis")
    accepted_at = models.DateTimeField(blank=True, null=True, verbose_name="Angenommen am")
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_session_invitations",
        verbose_name="Angenommen von",
    )

    class Meta:
        db_table = "session_invitations"
        verbose_name = "Session-Einladung"
        verbose_name_plural = "Session-Einladungen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Einladung für {self.email} zu {self.tenant.name}"

    @property
    def is_valid(self) -> bool:
        """Einladung noch offen und nicht abgelaufen?"""
        return self.accepted_at is None and timezone.now() < self.expires_at

    @classmethod
    def create_for_tenant(cls, tenant, email: str, invited_by=None, roles=None, valid_days: int = 7):
        """Neue Einladung mit kryptografisch sicherem Token anlegen."""
        import secrets
        from datetime import timedelta

        invitation = cls.objects.create(
            tenant=tenant,
            email=email.lower().strip(),
            token=secrets.token_urlsafe(48),
            invited_by=invited_by,
            expires_at=timezone.now() + timedelta(days=valid_days),
        )
        if roles:
            for role in roles:
                if role.tenant_id != tenant.id:
                    invitation.delete()
                    raise ValueError(f"Rolle '{role.name}' gehört nicht zu diesem Mandanten.")
            invitation.roles.set(roles)
        return invitation


# =============================================================================
# ORGANIZATION MODELS
# =============================================================================


class SessionOrganization(models.Model):
    """
    Organization/Committee within Session.

    Extends OParlOrganization with Session-specific fields.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="organizations",
        verbose_name="Mandant",
    )

    # OParl link (optional)
    oparl_organization = models.OneToOneField(
        "insight_core.OParlOrganization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_organization",
        verbose_name="OParl-Gremium",
    )

    # Basic info
    name = models.CharField(max_length=500, verbose_name="Name")
    short_name = models.CharField(max_length=100, blank=True, verbose_name="Kurzname")
    organization_type = models.CharField(
        max_length=100,
        choices=[
            ("committee", "Ausschuss"),
            ("council", "Rat"),
            ("faction", "Fraktion"),
            ("advisory", "Beirat"),
            ("commission", "Kommission"),
            ("other", "Sonstiges"),
        ],
        default="committee",
        verbose_name="Typ",
    )

    # Hierarchy
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordnetes Gremium",
    )

    # Members
    members = models.ManyToManyField(
        "SessionPerson",
        through="SessionOrganizationMembership",
        through_fields=("organization", "person"),
        related_name="organizations",
        verbose_name="Mitglieder",
    )

    # Settings
    default_meeting_location = models.CharField(max_length=255, blank=True, verbose_name="Standardort für Sitzungen")
    default_meeting_start_time = models.TimeField(blank=True, null=True, verbose_name="Standardzeit für Sitzungen")

    # Sitzungsdienst-Stammdaten
    meeting_frequency = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Sitzungsturnus",
        help_text="z. B. monatlich, 6-wöchentlich, nach Bedarf",
    )
    invitation_period_days = models.PositiveIntegerField(
        default=7,
        verbose_name="Ladungsfrist (Tage)",
        help_text="Frist zwischen Ladung und Sitzung gemäß Geschäftsordnung",
    )
    target_member_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Mitgliederzahl (Soll)",
    )

    # Allowance settings
    allowance_amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Sitzungsgeld",
    )
    allowance_currency = models.CharField(max_length=3, default="EUR", verbose_name="Währung")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    start_date = models.DateField(blank=True, null=True, verbose_name="Startdatum")
    end_date = models.DateField(blank=True, null=True, verbose_name="Enddatum")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_organizations"
        verbose_name = "Gremium"
        verbose_name_plural = "Gremien"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SessionPerson(EncryptionMixin, models.Model):
    """
    Person within Session (council members, etc.).

    Extends OParlPerson with Session-specific fields.

    Security: Kontaktdaten (Telefon, Adresse) und Bankdaten (Kontoinhaber,
    IBAN, BIC) werden AES-256-GCM-verschlüsselt gespeichert (Tenant-Key).
    Zugriff über die generierten Accessoren, z. B.
    person.set_bank_iban_encrypted("DE...") / person.get_bank_iban_decrypted().
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="persons",
        verbose_name="Mandant",
    )

    # OParl link (optional)
    oparl_person = models.OneToOneField(
        "insight_core.OParlPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_person",
        verbose_name="OParl-Person",
    )

    # Basic info
    title = models.CharField(max_length=50, blank=True, verbose_name="Titel")
    given_name = models.CharField(max_length=100, verbose_name="Vorname")
    family_name = models.CharField(max_length=100, verbose_name="Nachname")
    form_of_address = models.CharField(max_length=50, blank=True, verbose_name="Anrede")

    # Contact
    # E-Mail bleibt bewusst Klartext: Sie wird in der Personensuche gefiltert
    # (PersonListView, email__icontains) und über die öffentliche OParl-API
    # veröffentlicht — verschlüsselte Felder sind nicht filterbar.
    email = models.EmailField(blank=True, verbose_name="E-Mail")
    phone_encrypted = EncryptedTextField(verbose_name="Telefon (verschlüsselt)")
    address_encrypted = EncryptedTextField(verbose_name="Adresse (verschlüsselt)")

    # Bank details for allowances (AES-256-GCM, Tenant-Key)
    bank_account_holder_encrypted = EncryptedTextField(verbose_name="Kontoinhaber (verschlüsselt)")
    bank_iban_encrypted = EncryptedTextField(verbose_name="IBAN (verschlüsselt)")
    bank_bic_encrypted = EncryptedTextField(verbose_name="BIC (verschlüsselt)")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    start_date = models.DateField(blank=True, null=True, verbose_name="Mandatsbeginn")
    end_date = models.DateField(blank=True, null=True, verbose_name="Mandatsende")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_persons"
        verbose_name = "Person"
        verbose_name_plural = "Personen"
        ordering = ["family_name", "given_name"]

    def __str__(self):
        if self.title:
            return f"{self.title} {self.given_name} {self.family_name}"
        return f"{self.given_name} {self.family_name}"

    @property
    def display_name(self):
        """Full name for display."""
        parts = []
        if self.title:
            parts.append(self.title)
        parts.append(self.given_name)
        parts.append(self.family_name)
        return " ".join(parts)


class SessionOrganizationMembership(models.Model):
    """
    Membership of a person in an organization.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Gremium",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Person",
    )

    role = models.CharField(
        max_length=100,
        choices=[
            ("member", "Mitglied"),
            ("chair", "Vorsitzende/r"),
            ("deputy_chair", "Stellv. Vorsitzende/r"),
            ("expert_citizen", "Sachkundige/r Bürger/in"),
            ("advisor", "Beratendes Mitglied"),
            ("guest", "Gast"),
        ],
        default="member",
        verbose_name="Funktion",
    )

    start_date = models.DateField(blank=True, null=True, verbose_name="Von")
    end_date = models.DateField(blank=True, null=True, verbose_name="Bis")

    # Voting rights
    has_voting_rights = models.BooleanField(default=True, verbose_name="Stimmberechtigt")

    # Vertreterregelung: Diese Mitgliedschaft vertritt eine andere Person
    substitute_for = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="substituted_by_memberships",
        verbose_name="Vertretung für",
        help_text="Wird als Stellvertreter/in dieser Person geführt",
    )

    # Wahlperiode (Issue #39): Besetzungen werden je Periode geführt —
    # nach dem Periodenwechsel bleiben Alt-Besetzungen unter der alten
    # Periode auffindbar (Archiv)
    legislative_term = models.ForeignKey(
        "SessionLegislativeTerm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
        verbose_name="Wahlperiode",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_organization_memberships"
        verbose_name = "Gremienmitgliedschaft"
        verbose_name_plural = "Gremienmitgliedschaften"
        unique_together = ["organization", "person", "start_date"]

    def __str__(self):
        return f"{self.person} - {self.organization} ({self.role})"


# =============================================================================
# MEETING MODELS
# =============================================================================


class SessionMeeting(EncryptionMixin, models.Model):
    """
    Meeting/Session within Session RIS.

    Extends OParlMeeting with non-public fields and workflow support.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="meetings",
        verbose_name="Mandant",
    )

    # OParl link (optional - for public sync)
    oparl_meeting = models.OneToOneField(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_meeting",
        verbose_name="OParl-Sitzung",
    )

    # Basic info
    name = models.CharField(max_length=500, verbose_name="Name")
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="meetings",
        verbose_name="Gremium",
    )

    # Wahlperiode (Issue #39) — wird bei Anlage automatisch aus dem
    # Sitzungsdatum abgeleitet, bleibt beim Periodenwechsel erhalten
    legislative_term = models.ForeignKey(
        "SessionLegislativeTerm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meetings",
        verbose_name="Wahlperiode",
    )

    # Date/Time
    start = models.DateTimeField(verbose_name="Beginn")
    end = models.DateTimeField(blank=True, null=True, verbose_name="Ende")
    actual_start = models.DateTimeField(blank=True, null=True, verbose_name="Tatsächlicher Beginn")
    actual_end = models.DateTimeField(blank=True, null=True, verbose_name="Tatsächliches Ende")

    # Location
    location = models.CharField(max_length=500, blank=True, verbose_name="Ort")
    room = models.CharField(max_length=100, blank=True, verbose_name="Raum")
    street_address = models.CharField(max_length=255, blank=True, verbose_name="Straße")
    postal_code = models.CharField(max_length=10, blank=True, verbose_name="PLZ")
    locality = models.CharField(max_length=100, blank=True, verbose_name="Stadt")

    # Status
    meeting_state = models.CharField(
        max_length=50,
        choices=[
            ("draft", "Entwurf"),
            ("scheduled", "Geplant"),
            ("invitation_sent", "Einladung versandt"),
            ("in_progress", "Laufend"),
            ("completed", "Abgeschlossen"),
            ("cancelled", "Abgesagt"),
        ],
        default="draft",
        verbose_name="Status",
    )
    cancelled = models.BooleanField(default=False, verbose_name="Abgesagt")
    cancellation_reason = models.TextField(blank=True, verbose_name="Absagegrund")

    # Visibility
    is_public = models.BooleanField(
        default=True,
        verbose_name="Öffentlich",
        help_text="Wird über OParl-API veröffentlicht",
    )

    # Non-public internal notes (encrypted)
    internal_notes_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Interne Notizen")

    # Invitation
    invitation_sent_at = models.DateTimeField(blank=True, null=True, verbose_name="Einladung versandt am")
    invitation_text = models.TextField(blank=True, verbose_name="Einladungstext")

    # Workflow
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_meetings",
        verbose_name="Erstellt von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_meetings"
        verbose_name = "Sitzung"
        verbose_name_plural = "Sitzungen"
        ordering = ["-start"]

    def __str__(self):
        return f"{self.organization.name}: {self.name}"

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.tenant

    @property
    def invitation_deadline(self):
        """
        Spätester Ladungstermin gemäß Ladungsfrist des Gremiums (Issue #29).

        Beispiel: Sitzung am 20.08., Ladungsfrist 7 Tage -> Ladung bis 13.08.
        """
        from datetime import timedelta

        period = self.organization.invitation_period_days or 0
        return (timezone.localtime(self.start) - timedelta(days=period)).date()

    @property
    def invitation_overdue(self) -> bool:
        """Ladungsfrist verstrichen, ohne dass eine Einladung versandt wurde?"""
        if self.invitation_sent_at or self.cancelled:
            return False
        if self.meeting_state not in ("draft", "scheduled"):
            return False
        return timezone.localdate() > self.invitation_deadline


class SessionInvitationDispatch(models.Model):
    """
    Versandvorgang einer Ladung/Einladung zu einer Sitzung (Issue #29).

    Dokumentiert gerichtsfest, wer wann welche Art von Ladung (Erstladung
    oder Nachtrags-Tagesordnung) an welchen Empfängerkreis versandt hat.
    Die einzelnen Empfänger inkl. Zustellstatus hängen als
    SessionInvitationRecipient daran.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="invitation_dispatches",
        verbose_name="Sitzung",
    )

    dispatch_type = models.CharField(
        max_length=20,
        choices=[
            ("invitation", "Ladung"),
            ("supplementary", "Nachladung/Nachtrag"),
        ],
        default="invitation",
        verbose_name="Versandart",
    )

    subject = models.CharField(max_length=500, verbose_name="Betreff")
    message = models.TextField(blank=True, verbose_name="Anschreiben")

    sent_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="invitation_dispatches",
        verbose_name="Versandt von",
    )
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name="Versandt am")

    class Meta:
        db_table = "session_invitation_dispatches"
        verbose_name = "Ladungsversand"
        verbose_name_plural = "Ladungsversände"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.get_dispatch_type_display()} für {self.meeting} am {self.sent_at:%d.%m.%Y}"


class SessionInvitationRecipient(models.Model):
    """
    Einzelner Empfänger eines Ladungsversands inkl. Zustellstatus (Issue #29).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    dispatch = models.ForeignKey(
        SessionInvitationDispatch,
        on_delete=models.CASCADE,
        related_name="recipients",
        verbose_name="Versand",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitation_receipts",
        verbose_name="Person",
    )

    name = models.CharField(max_length=255, verbose_name="Name")
    email = models.EmailField(verbose_name="E-Mail")
    membership_role = models.CharField(max_length=100, blank=True, verbose_name="Funktion")

    # Ö/NÖ: hat dieser Empfänger die vollständige (inkl. NÖ-Teil) Tagesordnung erhalten?
    includes_non_public = models.BooleanField(
        default=False,
        verbose_name="Inkl. nichtöffentlicher Teil",
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ("sent", "Versandt"),
            ("failed", "Fehlgeschlagen"),
        ],
        default="sent",
        verbose_name="Zustellstatus",
    )
    error = models.TextField(blank=True, verbose_name="Fehler")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_invitation_recipients"
        verbose_name = "Ladungsempfänger"
        verbose_name_plural = "Ladungsempfänger"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.get_status_display()})"


class SessionAgendaItem(EncryptionMixin, models.Model):
    """
    Agenda item for a meeting.

    Extends OParlAgendaItem with voting results and non-public content.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="agenda_items",
        verbose_name="Sitzung",
    )

    # OParl link (optional)
    oparl_agenda_item = models.OneToOneField(
        "insight_core.OParlAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_agenda_item",
        verbose_name="OParl-TOP",
    )

    # Basic info
    number = models.CharField(max_length=20, verbose_name="TOP-Nr.")
    name = models.CharField(max_length=500, verbose_name="Betreff")
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")

    # Hierarchie: Unterpunkte (z. B. 5.1, 5.2)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sub_items",
        verbose_name="Übergeordneter TOP",
    )

    # Visibility
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")

    # Absetzung (dokumentiert statt gelöscht)
    is_withdrawn = models.BooleanField(default=False, verbose_name="Abgesetzt")
    withdrawn_reason = models.TextField(blank=True, verbose_name="Absetzungsgrund")

    # Nachtrag (nach Versand der Ladung hinzugefügt)
    is_supplementary = models.BooleanField(
        default=False,
        verbose_name="Nachtrag",
        help_text="Nach Versand der Einladung hinzugefügt (Nachtragstagesordnung)",
    )

    # Paper reference
    paper = models.ForeignKey(
        "SessionPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agenda_items",
        verbose_name="Vorlage",
    )

    # Voting
    resolution_text = models.TextField(blank=True, verbose_name="Beschlusstext")
    resolution_text_encrypted = EncryptedTextField(
        blank=True,
        null=True,
        verbose_name="Nicht-öffentlicher Beschlusstext",
    )

    # Niederschrift (Issue #31): TOP-weise Protokolltexte
    protocol_note = models.TextField(
        blank=True,
        verbose_name="Protokolltext",
        help_text="Wortbeiträge/Beratungsverlauf zu diesem TOP (öffentlicher Teil der Niederschrift)",
    )
    protocol_note_encrypted = EncryptedTextField(
        blank=True,
        null=True,
        verbose_name="Nicht-öffentlicher Protokolltext",
    )

    # Beschlussregister (Issue #32): fortlaufende Beschlussnummer je Mandant/Jahr
    resolution_number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Beschlussnummer",
        help_text="Wird bei der Beschlussausfertigung vergeben, z. B. B/2026/0012",
    )

    vote_result = models.CharField(
        max_length=50,
        choices=[
            ("pending", "Ausstehend"),
            ("approved", "Angenommen"),
            ("rejected", "Abgelehnt"),
            ("deferred", "Vertagt"),
            ("withdrawn", "Zurückgezogen"),
            ("noted", "Zur Kenntnis genommen"),
        ],
        default="pending",
        verbose_name="Abstimmungsergebnis",
    )
    votes_yes = models.PositiveIntegerField(default=0, verbose_name="Ja-Stimmen")
    votes_no = models.PositiveIntegerField(default=0, verbose_name="Nein-Stimmen")
    votes_abstain = models.PositiveIntegerField(default=0, verbose_name="Enthaltungen")

    # Beschlusskontrolle (Issue #37): Umsetzung nach der Beschlussfassung.
    # Nur für angenommene Beschlüsse relevant; die Verwaltung dokumentiert
    # hier Zuständigkeit, Frist und Erledigung.
    IMPLEMENTATION_CHOICES = [
        ("open", "Offen"),
        ("in_progress", "In Umsetzung"),
        ("done", "Erledigt"),
        ("deferred", "Zurückgestellt"),
    ]
    implementation_status = models.CharField(
        max_length=20,
        choices=IMPLEMENTATION_CHOICES,
        default="open",
        verbose_name="Umsetzungsstand",
    )
    implementation_recipient = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Zuständige Stelle/Amt",
        help_text="z. B. Bauamt, Kämmerei",
    )
    implementation_deadline = models.DateField(blank=True, null=True, verbose_name="Erledigungsfrist")
    implementation_note = models.TextField(blank=True, verbose_name="Erledigungsvermerk")
    implementation_updated_at = models.DateTimeField(blank=True, null=True, verbose_name="Umsetzung aktualisiert am")
    implementation_updated_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="resolution_tracking_updates",
        verbose_name="Umsetzung aktualisiert von",
    )

    # Timing
    start_time = models.TimeField(blank=True, null=True, verbose_name="Beginn")
    end_time = models.TimeField(blank=True, null=True, verbose_name="Ende")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_agenda_items"
        verbose_name = "Tagesordnungspunkt"
        verbose_name_plural = "Tagesordnungspunkte"
        ordering = ["order", "number"]

    def __str__(self):
        return f"TOP {self.number}: {self.name}"

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.meeting.tenant

    @property
    def implementation_overdue(self) -> bool:
        """Frist verstrichen, aber Beschluss noch nicht erledigt."""
        if not self.implementation_deadline or self.implementation_status == "done":
            return False
        return self.implementation_deadline < timezone.localdate()


class SessionResolutionForwarding(models.Model):
    """
    Versand-/Übergabevermerk eines Beschlussauszugs (Issue #32).

    Dokumentiert nachweisbar, an welche Stelle (Fachamt, extern) der
    Beschlussauszug eines TOP wann übergeben wurde.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    agenda_item = models.ForeignKey(
        SessionAgendaItem,
        on_delete=models.CASCADE,
        related_name="forwardings",
        verbose_name="Tagesordnungspunkt",
    )

    recipient = models.CharField(
        max_length=255,
        verbose_name="Zuständige Stelle/Amt",
        help_text="z. B. Bauamt, Kämmerei, externe Stelle",
    )
    method = models.CharField(
        max_length=20,
        choices=[
            ("email", "E-Mail"),
            ("internal", "Interner Geschäftsgang"),
            ("mail", "Postweg"),
            ("handover", "Persönliche Übergabe"),
        ],
        default="internal",
        verbose_name="Übergabeweg",
    )
    note = models.TextField(blank=True, verbose_name="Vermerk")

    sent_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="resolution_forwardings",
        verbose_name="Übergeben von",
    )
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name="Übergeben am")

    class Meta:
        db_table = "session_resolution_forwardings"
        verbose_name = "Beschlussauszug-Übergabe"
        verbose_name_plural = "Beschlussauszug-Übergaben"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"Auszug TOP {self.agenda_item.number} an {self.recipient} ({self.sent_at:%d.%m.%Y})"


# =============================================================================
# PAPER/APPLICATION MODELS
# =============================================================================


class SessionPaper(EncryptionMixin, models.Model):
    """
    Paper/Vorlage within Session RIS.

    Extends OParlPaper with workflow and non-public content support.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="papers",
        verbose_name="Mandant",
    )

    # OParl link (optional)
    oparl_paper = models.OneToOneField(
        "insight_core.OParlPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_paper",
        verbose_name="OParl-Vorlage",
    )

    # Reference
    reference = models.CharField(
        max_length=100,
        verbose_name="Aktenzeichen",
        help_text="z.B. V/2024/0001",
    )

    # Basic info
    name = models.CharField(max_length=500, verbose_name="Betreff")
    paper_type = models.CharField(
        max_length=100,
        choices=[
            ("proposal", "Beschlussvorlage"),
            ("report", "Mitteilungsvorlage"),
            ("motion", "Antrag"),
            ("inquiry", "Anfrage"),
            ("resolution", "Resolution"),
            ("bylaw", "Satzung"),
            ("budget", "Haushalt"),
            ("other", "Sonstiges"),
        ],
        default="proposal",
        verbose_name="Vorlagenart",
    )

    # Content
    main_text = models.TextField(blank=True, verbose_name="Sachverhalt")
    resolution_text = models.TextField(blank=True, verbose_name="Beschlussvorschlag")

    # Non-public content (encrypted)
    confidential_text_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Vertraulicher Inhalt")

    # Visibility
    is_public = models.BooleanField(
        default=True,
        verbose_name="Öffentlich",
        help_text="Wird über OParl-API veröffentlicht",
    )

    # Workflow
    status = models.CharField(
        max_length=50,
        choices=[
            ("draft", "Entwurf"),
            ("review", "In Prüfung"),
            ("approved", "Freigegeben"),
            ("scheduled", "Terminiert"),
            ("completed", "Abgeschlossen"),
            ("withdrawn", "Zurückgezogen"),
        ],
        default="draft",
        verbose_name="Status",
    )

    # Dates
    date = models.DateField(blank=True, null=True, verbose_name="Datum")
    deadline = models.DateField(blank=True, null=True, verbose_name="Frist")

    # References
    originator_organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originated_papers",
        verbose_name="Einreichendes Gremium",
    )
    originator_person = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originated_papers",
        verbose_name="Einreichende Person",
    )

    # Consultation chain
    main_organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="main_papers",
        verbose_name="Federführendes Gremium",
    )

    # Source (if from application)
    source_application = models.ForeignKey(
        "SessionApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_papers",
        verbose_name="Ursprungsantrag",
    )

    # Workflow tracking
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_papers",
        verbose_name="Erstellt von",
    )
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_papers",
        verbose_name="Freigegeben von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Freigegeben am")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_papers"
        verbose_name = "Vorlage"
        verbose_name_plural = "Vorlagen"
        ordering = ["-date", "-created_at"]
        unique_together = ["tenant", "reference"]

    def __str__(self):
        return f"{self.reference}: {self.name}"

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.tenant


class SessionApplication(EncryptionMixin, models.Model):
    """
    Application (Antrag) from political organizations.

    This enables simple form-based submission of applications from
    parties/factions without requiring document uploads.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name="Mandant",
    )

    # Reference
    reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Eingangsnummer",
        help_text="Wird automatisch vergeben",
    )

    # Basic info
    title = models.CharField(max_length=500, verbose_name="Titel")
    application_type = models.CharField(
        max_length=100,
        choices=[
            ("motion", "Antrag"),
            ("inquiry", "Anfrage"),
            ("resolution", "Resolution"),
            ("urgent", "Dringlichkeitsantrag"),
            ("amendment", "Änderungsantrag"),
            ("other", "Sonstiges"),
        ],
        default="motion",
        verbose_name="Art des Antrags",
    )

    # Content
    justification = models.TextField(
        verbose_name="Begründung",
        help_text="Warum soll dieser Antrag beschlossen werden?",
    )
    resolution_proposal = models.TextField(
        verbose_name="Beschlussvorschlag",
        help_text="Was genau soll beschlossen werden?",
    )
    financial_impact = models.TextField(
        blank=True,
        verbose_name="Finanzielle Auswirkungen",
        help_text="Welche Kosten entstehen? (optional)",
    )

    # Additional content (encrypted for non-public)
    additional_info_encrypted = EncryptedTextField(
        blank=True, null=True, verbose_name="Zusätzliche vertrauliche Informationen"
    )

    # Submitter info
    submitting_organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.SET_NULL,
        null=True,
        related_name="submitted_applications",
        verbose_name="Einreichende Organisation",
        help_text="Fraktion/Partei, die den Antrag einreicht",
    )
    submitter_name = models.CharField(max_length=255, verbose_name="Name des Einreichers")
    submitter_email = models.EmailField(verbose_name="E-Mail des Einreichers")
    submitter_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon des Einreichers")

    # Co-signers
    co_signers = models.TextField(
        blank=True,
        verbose_name="Mitunterzeichner",
        help_text="Namen der Mitunterzeichner (einer pro Zeile)",
    )

    # Target committee
    target_organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_applications",
        verbose_name="Zielgremium",
        help_text="An welches Gremium soll der Antrag gehen?",
    )

    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ("submitted", "Eingereicht"),
            ("received", "Eingegangen"),
            ("in_review", "In Prüfung"),
            ("accepted", "Angenommen"),
            ("rejected", "Abgelehnt"),
            ("converted", "In Vorlage umgewandelt"),
            ("withdrawn", "Zurückgezogen"),
        ],
        default="submitted",
        verbose_name="Status",
    )

    # Processing
    received_at = models.DateTimeField(blank=True, null=True, verbose_name="Eingegangen am")
    received_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_applications",
        verbose_name="Bearbeitet von",
    )
    processing_notes = models.TextField(blank=True, verbose_name="Bearbeitungsnotizen")

    # Urgency
    is_urgent = models.BooleanField(
        default=False,
        verbose_name="Dringend",
        help_text="Soll der Antrag bevorzugt behandelt werden?",
    )
    urgency_reason = models.TextField(blank=True, verbose_name="Begründung der Dringlichkeit")

    # Deadline
    deadline = models.DateField(
        blank=True,
        null=True,
        verbose_name="Gewünschter Beratungstermin",
        help_text="Bis wann soll der Antrag behandelt werden?",
    )

    # Timestamps
    submitted_at = models.DateTimeField(auto_now_add=True, verbose_name="Eingereicht am")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_applications"
        verbose_name = "Antrag"
        verbose_name_plural = "Anträge"
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "reference"],
                condition=~models.Q(reference=""),
                name="uniq_session_application_reference",
            ),
        ]

    def __str__(self):
        return f"{self.reference or 'NEU'}: {self.title}"

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.tenant

    def save(self, *args, **kwargs):
        # Auto-generate reference on first save.
        # Sicherheit: Die Vergabe läuft in einer Transaktion mit Zeilen-Lock
        # auf dem Tenant, damit parallele Einreichungen keine doppelten
        # Eingangsnummern erzeugen. Zusätzlich sichert der UniqueConstraint
        # (tenant, reference) auf DB-Ebene ab.
        if not self.reference:
            from django.db import transaction

            with transaction.atomic():
                # Serialisiert die Nummernvergabe pro Mandant
                SessionTenant.objects.select_for_update().get(pk=self.tenant_id)
                self.reference = self._next_reference()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def _next_reference(self) -> str:
        """Nächste freie Eingangsnummer für Tenant + Jahr (numerisch ermittelt)."""
        year = timezone.now().year
        prefix = f"A/{year}/"
        max_num = 0
        refs = SessionApplication.objects.filter(
            tenant_id=self.tenant_id,
            reference__startswith=prefix,
        ).values_list("reference", flat=True)
        for ref in refs:
            try:
                num = int(ref.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                continue
            max_num = max(max_num, num)
        return f"{prefix}{max_num + 1:04d}"


# =============================================================================
# LEGISLATIVE TERM (Wahlperioden, Issue #35)
# =============================================================================


class SessionLegislativeTerm(models.Model):
    """
    Wahlperiode eines Mandanten (Issue #35).

    Wird in der Session-OParl-API als ``legislativeTerm`` am Body
    ausgeliefert. Pflege über den Django-Admin (Systemebene) — eine
    eigene Portal-UI folgt bei Bedarf.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="legislative_terms",
        verbose_name="Mandant",
    )

    name = models.CharField(max_length=255, verbose_name="Name", help_text="z. B. Wahlperiode 2025–2030")
    start_date = models.DateField(blank=True, null=True, verbose_name="Beginn")
    end_date = models.DateField(blank=True, null=True, verbose_name="Ende")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_legislative_terms"
        verbose_name = "Wahlperiode"
        verbose_name_plural = "Wahlperioden"
        ordering = ["-start_date", "name"]

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

    def contains(self, date) -> bool:
        """Liegt das Datum im Zeitraum der Periode (offene Grenzen zählen mit)?"""
        if date is None:
            return False
        if self.start_date and date < self.start_date:
            return False
        if self.end_date and date > self.end_date:
            return False
        return bool(self.start_date or self.end_date)

    @property
    def is_current(self) -> bool:
        """Umfasst die Periode das heutige Datum?"""
        return self.contains(timezone.localdate())

    @classmethod
    def for_date(cls, tenant, date):
        """
        Passende Wahlperiode eines Mandanten zu einem Datum (Issue #39).

        Fallback: aktuelle Periode (enthält heute), sonst die jüngste.
        Gibt None zurück, wenn der Mandant keine Perioden pflegt.
        """
        terms = list(cls.objects.filter(tenant=tenant))
        if not terms:
            return None
        if date is not None:
            for term in terms:
                if term.contains(date):
                    return term
        return cls.current_for(tenant)

    @classmethod
    def current_for(cls, tenant):
        """Aktuelle Wahlperiode des Mandanten (enthält heute, sonst die jüngste)."""
        terms = list(cls.objects.filter(tenant=tenant))
        if not terms:
            return None
        today = timezone.localdate()
        for term in terms:
            if term.contains(today):
                return term
        # Jüngste Periode: höchstes Startdatum zuerst, Perioden ohne Datum zuletzt
        terms.sort(key=lambda t: (t.start_date is not None, t.start_date or timezone.localdate()), reverse=True)
        return terms[0]


# =============================================================================
# OPARL TOMBSTONES (Issue #35)
# =============================================================================


class SessionOParlTombstone(models.Model):
    """
    Tombstone für die Session-OParl-API (Issue #35, OParl 1.1 §2.8).

    Objekte, die einmal öffentlich über die OParl-API ausgeliefert wurden
    und danach gelöscht oder auf „nicht öffentlich“ gestellt werden,
    hinterlassen hier einen Grabstein. Die API liefert dafür weiterhin
    HTTP 200 mit dem gekürzten Objekt (``deleted: true``) und nimmt die
    Tombstones in ``modified_since``-Listen auf, damit inkrementelle
    Clients (z. B. der Insight-Ingestor) Löschungen zuverlässig mitbekommen.

    Sicherheit: Ein Tombstone enthält KEINE Inhalte — nur Objekttyp, ID
    und Zeitstempel.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="oparl_tombstones",
        verbose_name="Mandant",
    )

    # Objekttyp = URL-Segment der API (meeting, paper, file, agendaitem, …)
    oparl_type = models.CharField(max_length=50, verbose_name="Objekttyp")
    object_id = models.UUIDField(verbose_name="Objekt-ID")

    # created des ursprünglichen Objekts (für das Pflichtfeld created)
    object_created_at = models.DateTimeField(verbose_name="Objekt erstellt am")
    deleted_at = models.DateTimeField(default=timezone.now, verbose_name="Gelöscht am")

    class Meta:
        db_table = "session_oparl_tombstones"
        verbose_name = "OParl-Tombstone"
        verbose_name_plural = "OParl-Tombstones"
        unique_together = ["tenant", "oparl_type", "object_id"]
        indexes = [
            models.Index(fields=["tenant", "oparl_type", "deleted_at"]),
        ]

    def __str__(self):
        return f"Tombstone {self.oparl_type}/{self.object_id} ({self.tenant.slug})"


# =============================================================================
# CONSULTATION MODELS (Beratungsfolge, Issue #34)
# =============================================================================


class SessionConsultation(models.Model):
    """
    Beratungsstation einer Vorlage (Beratungsfolge, Issue #34).

    Bildet den realen Entscheidungsweg einer Vorlage über mehrere Gremien ab
    (z. B. Fachausschuss -> Hauptausschuss -> Rat). Entspricht der OParl-
    Entität ``Consultation``: Rolle (Vorberatung/Anhörung/Entscheidung),
    ``authoritative``-Flag für die entscheidende Station, Verknüpfung zum
    Tagesordnungspunkt, sobald die Station terminiert ist, und Ergebnis je
    Station (wird beim Erfassen des Abstimmungsergebnisses am TOP
    automatisch zurückgeschrieben, siehe signals.sync_consultation_result).
    """

    ROLE_CHOICES = [
        ("preliminary", "Vorberatung"),
        ("hearing", "Anhörung"),
        ("decision", "Entscheidung"),
        ("information", "Kenntnisnahme"),
    ]

    # Ergebnis-Auswahl entspricht SessionAgendaItem.vote_result, damit die
    # Rückschreibung 1:1 möglich ist.
    RESULT_CHOICES = [
        ("pending", "Ausstehend"),
        ("approved", "Angenommen"),
        ("rejected", "Abgelehnt"),
        ("deferred", "Vertagt"),
        ("withdrawn", "Zurückgezogen"),
        ("noted", "Zur Kenntnis genommen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    paper = models.ForeignKey(
        SessionPaper,
        on_delete=models.CASCADE,
        related_name="consultations",
        verbose_name="Vorlage",
    )
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="consultations",
        verbose_name="Gremium",
    )

    # Terminierung: Zielsitzung und (sobald angelegt) der zugehörige TOP
    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultations",
        verbose_name="Sitzung",
        help_text="Zielsitzung dieser Beratungsstation (sobald terminiert)",
    )
    agenda_item = models.OneToOneField(
        SessionAgendaItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultation",
        verbose_name="Tagesordnungspunkt",
    )

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default="preliminary",
        verbose_name="Rolle",
    )
    authoritative = models.BooleanField(
        default=False,
        verbose_name="Entscheidende Beratung",
        help_text="OParl: authoritative — die Station, die abschließend entscheidet",
    )
    order = models.PositiveIntegerField(default=1, verbose_name="Reihenfolge")

    result = models.CharField(
        max_length=50,
        choices=RESULT_CHOICES,
        default="pending",
        verbose_name="Ergebnis",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_consultations"
        verbose_name = "Beratungsstation"
        verbose_name_plural = "Beratungsfolge"
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"{self.paper.reference} – Station {self.order}: {self.organization.name} ({self.get_role_display()})"

    @property
    def tenant(self):
        """Tenant der Station (für Audit-Attribution und Filterung)."""
        return self.paper.tenant

    @property
    def is_scheduled(self) -> bool:
        """Station terminiert (TOP angelegt)?"""
        return self.agenda_item_id is not None

    @property
    def is_done(self) -> bool:
        """Station abgeschlossen (Ergebnis liegt vor)?"""
        return self.result != "pending"


# =============================================================================
# PROTOCOL MODELS
# =============================================================================


class SessionProtocol(EncryptionMixin, models.Model):
    """
    Meeting protocol.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.OneToOneField(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="protocol",
        verbose_name="Sitzung",
    )

    # Content
    content = models.TextField(blank=True, verbose_name="Protokollinhalt")
    content_encrypted = EncryptedTextField(
        blank=True,
        null=True,
        verbose_name="Nicht-öffentlicher Protokollinhalt",
    )

    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ("draft", "Entwurf"),
            ("review", "Zur Prüfung"),
            ("approved", "Genehmigt"),
            ("published", "Veröffentlicht"),
        ],
        default="draft",
        verbose_name="Status",
    )

    # Workflow
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_protocols",
        verbose_name="Erstellt von",
    )
    review_requested_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_protocols",
        verbose_name="Zur Prüfung gegeben von",
    )
    review_requested_at = models.DateTimeField(blank=True, null=True, verbose_name="Zur Prüfung gegeben am")
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_protocols",
        verbose_name="Genehmigt von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Genehmigt am")
    published_at = models.DateTimeField(blank=True, null=True, verbose_name="Veröffentlicht am")

    # Genehmigungsvermerk (Issue #31): Genehmigung erfolgt üblicherweise in
    # der Folgesitzung des Gremiums
    approval_meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_protocols",
        verbose_name="Genehmigt in Sitzung",
        help_text="Folgesitzung, in der die Niederschrift genehmigt wurde",
    )
    approval_note = models.CharField(max_length=500, blank=True, verbose_name="Genehmigungsvermerk")

    # Unterschriften-Block (Vorsitz + Protokollführung)
    chair_name = models.CharField(max_length=255, blank=True, verbose_name="Vorsitz (Unterschrift)")
    recorder_name = models.CharField(max_length=255, blank=True, verbose_name="Protokollführung (Unterschrift)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_protocols"
        verbose_name = "Protokoll"
        verbose_name_plural = "Protokolle"

    def __str__(self):
        return f"Protokoll: {self.meeting}"

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.meeting.tenant


# =============================================================================
# ATTENDANCE & ALLOWANCE MODELS
# =============================================================================


class SessionAttendance(models.Model):
    """
    Attendance record for a meeting.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="attendances",
        verbose_name="Sitzung",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="attendances",
        verbose_name="Person",
    )

    # Attendance status
    status = models.CharField(
        max_length=50,
        choices=[
            ("invited", "Eingeladen"),
            ("confirmed", "Zugesagt"),
            ("declined", "Abgesagt"),
            ("present", "Anwesend"),
            ("absent", "Abwesend"),
            ("excused", "Entschuldigt"),
            ("left_early", "Vorzeitig gegangen"),
            ("joined_late", "Verspätet"),
        ],
        default="invited",
        verbose_name="Status",
    )

    # Timing
    arrival_time = models.TimeField(blank=True, null=True, verbose_name="Ankunft")
    departure_time = models.TimeField(blank=True, null=True, verbose_name="Abgang")

    # Notes
    notes = models.TextField(blank=True, verbose_name="Notizen")
    excuse_reason = models.TextField(blank=True, verbose_name="Entschuldigungsgrund")

    # Role in this meeting
    role = models.CharField(
        max_length=50,
        choices=[
            ("member", "Mitglied"),
            ("chair", "Vorsitz"),
            ("deputy_chair", "Stellv. Vorsitz"),
            ("guest", "Gast"),
            ("expert", "Sachverständige/r"),
            ("recorder", "Protokollant/in"),
        ],
        default="member",
        verbose_name="Funktion",
    )

    # Voting rights (can be different from organization membership)
    has_voting_rights = models.BooleanField(default=True, verbose_name="Stimmberechtigt")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_attendances"
        verbose_name = "Anwesenheit"
        verbose_name_plural = "Anwesenheiten"
        unique_together = ["meeting", "person"]
        ordering = ["person__family_name"]

    def __str__(self):
        return f"{self.person} - {self.meeting}: {self.status}"


class SessionAllowanceRate(models.Model):
    """
    Entschädigungssatz je Gremium und Funktion (Issue #38).

    Der Abrechnungslauf zieht für jede anrechenbare Anwesenheit den Satz
    der Funktion (Vorsitz, Mitglied, …) im Gremium heran; ohne
    Funktions-Satz gilt das Standard-Sitzungsgeld des Gremiums
    (``SessionOrganization.allowance_amount``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="allowance_rates",
        verbose_name="Gremium",
    )
    role = models.CharField(
        max_length=50,
        choices=[
            ("member", "Mitglied"),
            ("chair", "Vorsitz"),
            ("deputy_chair", "Stellv. Vorsitz"),
            ("expert", "Sachverständige/r"),
            ("recorder", "Protokollant/in"),
        ],
        default="member",
        verbose_name="Funktion",
    )
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag")
    currency = models.CharField(max_length=3, default="EUR", verbose_name="Währung")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_allowance_rates"
        verbose_name = "Entschädigungssatz"
        verbose_name_plural = "Entschädigungssätze"
        unique_together = ["organization", "role"]
        ordering = ["organization__name", "role"]

    def __str__(self):
        return f"{self.organization.name} / {self.get_role_display()}: {self.amount} {self.currency}"


class SessionAllowance(models.Model):
    """
    Allowance payment for meeting attendance.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    attendance = models.OneToOneField(
        SessionAttendance,
        on_delete=models.CASCADE,
        related_name="allowance",
        verbose_name="Anwesenheit",
    )

    # Amount
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag")
    currency = models.CharField(max_length=3, default="EUR", verbose_name="Währung")

    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ("pending", "Ausstehend"),
            ("approved", "Genehmigt"),
            ("paid", "Ausgezahlt"),
            ("cancelled", "Storniert"),
        ],
        default="pending",
        verbose_name="Status",
    )

    # Vier-Augen-Prinzip (Issue #38): Wer den Abrechnungslauf erzeugt hat,
    # darf die entstandenen Positionen nicht selbst genehmigen
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_allowances",
        verbose_name="Erzeugt von",
    )

    # Payment tracking
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_allowances",
        verbose_name="Genehmigt von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Genehmigt am")
    paid_at = models.DateTimeField(blank=True, null=True, verbose_name="Ausgezahlt am")

    # Export reference (for accounting systems)
    export_reference = models.CharField(max_length=100, blank=True, verbose_name="Export-Referenz")
    export_date = models.DateTimeField(blank=True, null=True, verbose_name="Export-Datum")

    notes = models.TextField(blank=True, verbose_name="Notizen")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_allowances"
        verbose_name = "Sitzungsgeld"
        verbose_name_plural = "Sitzungsgelder"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.attendance.person}: {self.amount} {self.currency}"


# =============================================================================
# FILE MODELS
# =============================================================================


class SessionFile(models.Model):
    """
    File/document attachment.

    Files can be attached to papers, agenda items, or meetings.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="files",
        verbose_name="Mandant",
    )

    # File info
    name = models.CharField(max_length=500, verbose_name="Name")
    file = models.FileField(
        upload_to="session/files/%Y/%m/",
        verbose_name="Datei",
    )
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    size = models.PositiveBigIntegerField(default=0, verbose_name="Größe (Bytes)")

    # Extracted text (for search)
    text_content = models.TextField(blank=True, verbose_name="Textinhalt")

    # Versioning (Ersetzen erhöht die Version, Audit-Log hält Historie)
    version = models.PositiveIntegerField(default=1, verbose_name="Version")

    # Visibility
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")

    # Relationships
    paper = models.ForeignKey(
        SessionPaper,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Vorlage",
    )
    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Sitzung",
    )
    agenda_item = models.ForeignKey(
        SessionAgendaItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Tagesordnungspunkt",
    )

    # OParl link
    oparl_file = models.OneToOneField(
        "insight_core.OParlFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_file",
        verbose_name="OParl-Datei",
    )

    # Metadata
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_files",
        verbose_name="Hochgeladen von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_files"
        verbose_name = "Datei"
        verbose_name_plural = "Dateien"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def size_human(self) -> str:
        """Human-readable file size."""
        size = self.size
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# =============================================================================
# AUDIT LOG
# =============================================================================


class SessionAuditLog(models.Model):
    """
    Audit log for tracking changes.

    Records all significant actions within the system for compliance.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        verbose_name="Mandant",
    )

    # Actor
    user = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs",
        verbose_name="Benutzer",
    )
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-Adresse")
    user_agent = models.TextField(blank=True, verbose_name="User-Agent")

    # Action
    action = models.CharField(
        max_length=50,
        choices=[
            ("create", "Erstellt"),
            ("update", "Geändert"),
            ("delete", "Gelöscht"),
            ("view", "Angesehen"),
            ("download", "Heruntergeladen"),
            ("approve", "Freigegeben"),
            ("publish", "Veröffentlicht"),
            ("invitation_sent", "Einladung versandt"),
            ("withdraw", "Abgesetzt"),
            ("replace", "Ersetzt"),
            ("login", "Anmeldung"),
            ("logout", "Abmeldung"),
        ],
        verbose_name="Aktion",
    )

    # Target
    model_name = models.CharField(max_length=100, verbose_name="Modell")
    object_id = models.UUIDField(verbose_name="Objekt-ID")
    object_repr = models.CharField(max_length=500, blank=True, verbose_name="Objekt-Beschreibung")

    # Changes (JSON diff)
    changes = models.JSONField(default=dict, blank=True, verbose_name="Änderungen")

    # Timestamp
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_audit_logs"
        verbose_name = "Audit-Eintrag"
        verbose_name_plural = "Audit-Log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "model_name", "object_id"]),
            models.Index(fields=["tenant", "user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user}: {self.action} {self.model_name} {self.object_repr}"

    def save(self, *args, **kwargs):
        """Revisionssicherheit: Einträge sind nach dem Anlegen unveränderbar."""
        if not self._state.adding:
            raise ValueError("Audit-Einträge sind unveränderbar und können nicht aktualisiert werden.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Revisionssicherheit: Einträge können nicht gelöscht werden."""
        raise ValueError("Audit-Einträge sind unveränderbar und können nicht gelöscht werden.")


# =============================================================================
# FRISTEN-ERINNERUNGEN (Issue #83)
# =============================================================================


class SessionReminderLog(models.Model):
    """
    Protokoll versendeter Fristen-Erinnerungen (Issue #83).

    Der unique-Constraint auf (tenant, kind, dedup_key) macht den
    Erinnerungslauf idempotent: mehrfaches Ausführen am selben Tag
    erzeugt keine doppelten E-Mails.
    """

    KIND_CHOICES = [
        ("invitation_upcoming", "Ladungsfrist läuft ab"),
        ("invitation_overdue", "Ladungsfrist verstrichen"),
        ("paper_deadline", "Vorlagenfrist läuft ab"),
        ("attendance_rsvp", "Rückmeldung zur Sitzung fehlt"),
        ("resolution_followup", "Wiedervorlage Beschlusskontrolle"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="reminder_logs",
        verbose_name="Mandant",
    )
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, verbose_name="Erinnerungstyp")
    dedup_key = models.CharField(
        max_length=255,
        verbose_name="Deduplizierungs-Schlüssel",
        help_text="Objekt-/Frist-Bezug, verhindert doppelte Erinnerungen",
    )
    recipients = models.JSONField(default=list, blank=True, verbose_name="Empfänger")
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name="Versendet am")

    class Meta:
        db_table = "session_reminder_logs"
        verbose_name = "Erinnerungs-Protokoll"
        verbose_name_plural = "Erinnerungs-Protokolle"
        constraints = [models.UniqueConstraint(fields=["tenant", "kind", "dedup_key"], name="uniq_session_reminder")]
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.dedup_key})"


# =============================================================================
# TEXTBAUSTEINE UND STANDARD-TAGESORDNUNGSPUNKTE (Issue #85)
# =============================================================================


class SessionStandardAgendaItem(models.Model):
    """
    Standard-Tagesordnungspunkt (Issue #85).

    Wird beim Anlegen einer Sitzung automatisch in die Tagesordnung
    übernommen — z. B. „Eröffnung der Sitzung", „Genehmigung der
    Niederschrift", „Anfragen und Mitteilungen".
    """

    PLACEMENT_CHOICES = [
        ("start", "Am Anfang"),
        ("end", "Am Ende"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="standard_agenda_items",
        verbose_name="Mandant",
    )
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="standard_agenda_items",
        verbose_name="Gremium",
        help_text="Leer = gilt für alle Gremien",
    )
    name = models.CharField(max_length=500, verbose_name="Betreff")
    placement = models.CharField(max_length=10, choices=PLACEMENT_CHOICES, default="start", verbose_name="Position")
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_standard_agenda_items"
        verbose_name = "Standard-Tagesordnungspunkt"
        verbose_name_plural = "Standard-Tagesordnungspunkte"
        ordering = ["placement", "order", "name"]

    def __str__(self):
        return self.name


class SessionTextBlock(models.Model):
    """
    Textbaustein (Issue #85) für wiederkehrende Formulierungen in
    Beschlussvorschlägen und Niederschriften. Platzhalter wie {gremium},
    {datum} oder {vorlage} werden beim Einfügen ersetzt.
    """

    CATEGORY_CHOICES = [
        ("resolution", "Beschlusstext"),
        ("protocol", "Protokolltext"),
        ("general", "Allgemein"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="text_blocks",
        verbose_name="Mandant",
    )
    title = models.CharField(max_length=200, verbose_name="Titel")
    content = models.TextField(verbose_name="Text")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="general", verbose_name="Kategorie")
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_text_blocks"
        verbose_name = "Textbaustein"
        verbose_name_plural = "Textbausteine"
        ordering = ["category", "order", "title"]

    def __str__(self):
        return self.title


# =============================================================================
# API TOKEN MODEL
# =============================================================================


class SessionAPIToken(models.Model):
    """
    API Token for secure access to Session API.

    Used by external systems (e.g., Work module) to submit applications
    or access data via API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="api_tokens",
        verbose_name="Mandant",
    )

    # Token info
    name = models.CharField(
        max_length=100,
        verbose_name="Name",
        help_text="Beschreibender Name für den Token",
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        verbose_name="Token",
        help_text="SHA-256 Hash des API-Tokens",
    )
    token_prefix = models.CharField(
        max_length=8,
        editable=False,
        verbose_name="Token-Präfix",
        help_text="Die ersten 8 Zeichen des Tokens zur Identifikation",
    )

    # Permissions
    can_submit_applications = models.BooleanField(
        default=True,
        verbose_name="Anträge einreichen",
        help_text="Erlaubt das Einreichen von Anträgen",
    )
    can_read_meetings = models.BooleanField(
        default=True,
        verbose_name="Sitzungen lesen",
        help_text="Erlaubt das Lesen öffentlicher Sitzungsdaten",
    )
    can_read_papers = models.BooleanField(
        default=True,
        verbose_name="Vorlagen lesen",
        help_text="Erlaubt das Lesen öffentlicher Vorlagen",
    )

    # Rate limiting
    rate_limit_per_minute = models.PositiveIntegerField(
        default=60,
        verbose_name="Rate-Limit pro Minute",
        help_text="Maximale Anzahl von Anfragen pro Minute",
    )

    # IP restrictions (optional)
    allowed_ips = models.TextField(
        blank=True,
        verbose_name="Erlaubte IP-Adressen",
        help_text="Komma-getrennte Liste erlaubter IP-Adressen (leer = alle)",
    )

    # Metadata
    description = models.TextField(blank=True, verbose_name="Beschreibung")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tokens",
        verbose_name="Erstellt von",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Ablaufdatum",
        help_text="Token läuft zu diesem Zeitpunkt ab (leer = kein Ablauf)",
    )

    # Usage tracking
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name="Zuletzt verwendet")
    last_used_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="Letzte IP-Adresse")
    usage_count = models.PositiveIntegerField(default=0, verbose_name="Verwendungszähler")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_api_tokens"
        verbose_name = "API-Token"
        verbose_name_plural = "API-Tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.token_prefix}...)"

    @classmethod
    def generate_token(cls):
        """Generate a new random API token."""
        import secrets

        return secrets.token_hex(32)  # 64 characters

    @classmethod
    def hash_token(cls, raw_token: str) -> str:
        """Hash a raw token for storage."""
        import hashlib

        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def create_token(cls, tenant: SessionTenant, name: str, **kwargs):
        """Create a new API token. Returns (token_instance, raw_token)."""
        raw_token = cls.generate_token()
        hashed = cls.hash_token(raw_token)
        token = cls.objects.create(
            tenant=tenant,
            name=name,
            token=hashed,
            token_prefix=raw_token[:8],
            **kwargs,
        )
        return token, raw_token

    def is_valid(self) -> bool:
        """Check if token is valid (active and not expired)."""
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True

    def check_ip(self, ip_address: str) -> bool:
        """Check if IP address is allowed."""
        if not self.allowed_ips:
            return True
        allowed = [ip.strip() for ip in self.allowed_ips.split(",")]
        return ip_address in allowed

    def record_usage(self, ip_address: str = None):
        """Record token usage."""
        self.last_used_at = timezone.now()
        self.last_used_ip = ip_address
        self.usage_count += 1
        self.save(update_fields=["last_used_at", "last_used_ip", "usage_count"])
