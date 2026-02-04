# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag models for the Work module.

Provides motion management with:
- Customizable motion types per organization
- Templates linked to types
- Letterheads (PDF backgrounds)
- Status workflow
- Version history
- Granular sharing (user, role, organization, party, regional)
- Inline comments
- Document attachments
"""

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class MotionType(models.Model):
    """
    Customizable motion/document type per organization.

    Allows organizations to define their own types like:
    - Antrag (Motion)
    - Anfrage (Inquiry)
    - Stellungnahme (Statement)
    - Pressemitteilung (Press Release)
    - Protokoll (Minutes)
    - etc.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="motion_types",
        verbose_name="Organisation"
    )

    name = models.CharField(max_length=100, verbose_name="Name")
    slug = models.SlugField(max_length=100, verbose_name="Kurzname")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Icon and color for UI
    icon = models.CharField(
        max_length=50,
        default="file-text",
        verbose_name="Icon (Lucide)"
    )
    color = models.CharField(
        max_length=20,
        default="blue",
        verbose_name="Farbe"
    )

    # Workflow settings
    requires_approval = models.BooleanField(
        default=True,
        verbose_name="Freigabe erforderlich",
        help_text="Dokument muss vor Einreichung freigegeben werden"
    )
    is_submittable = models.BooleanField(
        default=True,
        verbose_name="Einreichbar",
        help_text="Kann offiziell eingereicht werden"
    )

    # Status
    is_default = models.BooleanField(
        default=False,
        verbose_name="Standard-Typ"
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    sort_order = models.IntegerField(default=0, verbose_name="Sortierung")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dokumenttyp"
        verbose_name_plural = "Dokumenttypen"
        ordering = ["sort_order", "name"]
        unique_together = [["organization", "slug"]]

    def __str__(self):
        return self.name


class OrganizationLetterhead(models.Model):
    """
    PDF letterhead for document exports.

    The PDF serves as a background/template where content is overlaid.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="letterheads",
        verbose_name="Organisation"
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # The PDF file
    pdf_file = models.FileField(
        upload_to="motions/letterheads/%Y/%m/",
        verbose_name="PDF-Datei"
    )

    # Content positioning (in mm from top-left)
    content_margin_top = models.PositiveIntegerField(
        default=60,
        verbose_name="Abstand oben (mm)",
        help_text="Abstand vom oberen Rand zum Textbereich"
    )
    content_margin_left = models.PositiveIntegerField(
        default=25,
        verbose_name="Abstand links (mm)"
    )
    content_margin_right = models.PositiveIntegerField(
        default=20,
        verbose_name="Abstand rechts (mm)"
    )
    content_margin_bottom = models.PositiveIntegerField(
        default=30,
        verbose_name="Abstand unten (mm)"
    )

    # Font settings
    font_family = models.CharField(
        max_length=100,
        default="Arial",
        verbose_name="Schriftart"
    )
    font_size = models.PositiveIntegerField(
        default=11,
        verbose_name="Schriftgröße (pt)"
    )

    # Status
    is_default = models.BooleanField(
        default=False,
        verbose_name="Standard-Briefkopf"
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Briefkopf"
        verbose_name_plural = "Briefköpfe"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return f"{self.name} ({self.organization.name})"


class MotionTemplate(models.Model):
    """
    Template for motions (e.g., official letter format).

    Templates can be linked to specific types and letterheads.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="motion_templates",
        verbose_name="Organisation"
    )

    # Link to type (optional - if set, only available for this type)
    motion_type = models.ForeignKey(
        MotionType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="templates",
        verbose_name="Dokumenttyp",
        help_text="Vorlage nur für diesen Typ verfügbar (leer = alle Typen)"
    )

    # Link to letterhead (optional)
    letterhead = models.ForeignKey(
        OrganizationLetterhead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="templates",
        verbose_name="Briefkopf"
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Initial content template (HTML/Markdown)
    content_template = models.TextField(
        blank=True,
        verbose_name="Inhaltsvorlage",
        help_text="Vorausgefüllter Inhalt für neue Dokumente"
    )

    # Structure hints for AI
    structure_hints = models.TextField(
        blank=True,
        verbose_name="Strukturhinweise",
        help_text="Hinweise für die KI-Unterstützung (z.B. Abschnitte, Formatierung)"
    )

    # Signature block
    signature_block = models.TextField(
        blank=True,
        verbose_name="Signaturblock"
    )

    # Sharing
    is_shared_party = models.BooleanField(
        default=False,
        verbose_name="Partei-weit teilen"
    )

    # Status
    is_default = models.BooleanField(
        default=False,
        verbose_name="Standard-Vorlage"
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dokumentvorlage"
        verbose_name_plural = "Dokumentvorlagen"
        ordering = ["-is_default", "name"]

    def __str__(self):
        type_suffix = f" ({self.motion_type.name})" if self.motion_type else ""
        return f"{self.name}{type_suffix}"


class Motion(EncryptionMixin, models.Model):
    """
    Motion/Antrag/Anfrage document.

    The main document created by members for submission to councils.
    Always editable - changes are tracked via revisions.
    """

    # Legacy type choices (for backwards compatibility)
    LEGACY_TYPE_CHOICES = [
        ("motion", "Antrag"),
        ("inquiry", "Anfrage"),
        ("statement", "Stellungnahme"),
        ("amendment", "Änderungsantrag"),
    ]

    VISIBILITY_CHOICES = [
        ("private", "Privat"),           # Only the author can see
        ("shared", "Geteilt"),           # Specific people via MotionShare
        ("organization", "Organisation"), # Everyone in the organization
    ]

    STATUS_CHOICES = [
        ("draft", "Entwurf"),
        ("review", "In Prüfung"),
        ("approved", "Freigegeben"),
        ("submitted", "Eingereicht"),
        ("completed", "Erledigt"),
        ("rejected", "Abgelehnt"),
        ("archived", "Archiviert"),
        ("deleted", "Gelöscht"),
        # Extended workflow statuses
        ("internal_review", "Interne Absprache"),
        ("external_review", "Externe Absprache"),  # Coalition review
        ("at_admin", "Bei Verwaltung"),
        ("on_agenda", "Auf Tagesordnung"),
    ]

    EDIT_MODE_CHOICES = [
        ("edit", "Bearbeiten"),
        ("suggest", "Vorschlagen"),
        ("comment", "Nur Kommentieren"),
        ("view", "Nur Ansehen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="motions",
        verbose_name="Organisation"
    )

    # Type - can be dynamic (MotionType) or legacy (string)
    document_type = models.ForeignKey(
        MotionType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Dokumenttyp"
    )
    # Legacy field for backwards compatibility
    motion_type = models.CharField(
        max_length=20,
        choices=LEGACY_TYPE_CHOICES,
        default="motion",
        verbose_name="Typ (legacy)",
        blank=True
    )

    title = models.CharField(max_length=500, verbose_name="Titel")
    summary = models.TextField(
        blank=True,
        verbose_name="Zusammenfassung",
        help_text="Öffentliche Kurzfassung"
    )

    # Content (encrypted, stored as HTML from WYSIWYG editor)
    content_encrypted = EncryptedTextField(
        verbose_name="Inhalt"
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
        verbose_name="Status"
    )

    # Default edit mode for collaborators
    default_edit_mode = models.CharField(
        max_length=20,
        choices=EDIT_MODE_CHOICES,
        default="edit",
        verbose_name="Standard-Bearbeitungsmodus",
        help_text="Standard-Modus für neue Mitarbeiter"
    )

    # Target meeting for agenda integration
    target_meeting = models.ForeignKey(
        "work.FactionMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_motions",
        verbose_name="Ziel-Sitzung",
        help_text="Sitzung, in der dieser Antrag behandelt werden soll"
    )

    # Template used
    template = models.ForeignKey(
        MotionTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Vorlage"
    )

    # Letterhead for export
    letterhead = models.ForeignKey(
        OrganizationLetterhead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Briefkopf"
    )

    # Linkages to OParl
    related_paper = models.ForeignKey(
        "insight_core.OParlPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_motions",
        verbose_name="Verknüpfter Vorgang",
        help_text="OParl-Vorlage wenn eingereicht"
    )
    related_meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_motions",
        verbose_name="Ziel-Sitzung"
    )

    # For amendments - link to parent motion
    parent_motion = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="amendments",
        verbose_name="Bezugsantrag"
    )

    # Real-time collaboration state (Yjs document)
    yjs_document = models.BinaryField(
        blank=True,
        null=True,
        verbose_name="Yjs-Dokument",
        help_text="Binary state for real-time collaboration"
    )

    # Visibility (simplified permission system)
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="private",
        verbose_name="Sichtbarkeit",
        help_text="Wer kann dieses Dokument sehen?"
    )

    # Metadata
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="authored_motions",
        verbose_name="Autor"
    )
    tags = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Tags"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Eingereicht am"
    )
    deleted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Gelöscht am",
        help_text="Zeitpunkt der Löschung (30 Tage Papierkorb)"
    )

    class Meta:
        verbose_name = "Dokument"
        verbose_name_plural = "Dokumente"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "document_type"]),
        ]

    def __str__(self):
        type_name = self.get_type_display()
        return f"{type_name}: {self.title}"

    def get_encryption_organization(self):
        return self.organization

    def get_type_display(self):
        """Get the display name for the document type."""
        if self.document_type:
            return self.document_type.name
        # Fallback to legacy type
        return dict(self.LEGACY_TYPE_CHOICES).get(self.motion_type, "Dokument")

    def get_type_icon(self):
        """Get the icon for the document type."""
        if self.document_type:
            return self.document_type.icon
        return "file-text"

    def get_type_color(self):
        """Get the color for the document type."""
        if self.document_type:
            return self.document_type.color
        return "blue"

    @property
    def content(self):
        """
        Get decrypted content for templates.

        This property provides easy access to the decrypted content
        without having to call get_content_decrypted() explicitly.
        """
        return self.get_content_decrypted()

    @property
    def is_editable(self):
        """
        Check if the document can be edited.
        Documents are always editable - changes are tracked via revisions.
        """
        return True

    @property
    def is_submittable(self):
        """Check if the document can be submitted."""
        if self.document_type:
            return self.document_type.is_submittable
        return True

    def can_access(self, membership) -> bool:
        """
        Check if a membership has access to this document.

        Access is granted based on visibility:
        - private: Only the author
        - shared: Author + users with MotionShare entries
        - organization: Anyone in the same organization
        """
        # Author always has access
        if self.author == membership:
            return True

        if self.visibility == "private":
            return False

        if self.visibility == "organization":
            return membership.organization == self.organization

        if self.visibility == "shared":
            # Check MotionShare entries
            return self.shares.filter(user=membership.user).exists()

        return False

    def can_edit(self, membership) -> bool:
        """
        Check if a membership can edit this document.

        With simplified permissions, anyone with access can edit
        (except in private mode, only author can edit).
        """
        if self.author == membership:
            return True

        if self.visibility == "private":
            return False

        # For shared/organization, anyone with access can edit
        return self.can_access(membership)

    def can_comment(self, membership) -> bool:
        """
        Check if a membership can comment on this document.

        With simplified permissions, anyone with access can comment.
        """
        return self.can_access(membership)

    def get_visibility_icon(self) -> str:
        """Get the Lucide icon name for the current visibility."""
        icons = {
            "private": "lock",
            "shared": "users",
            "organization": "building",
        }
        return icons.get(self.visibility, "lock")

    def get_visibility_badge_class(self) -> str:
        """Get the CSS class for the visibility badge."""
        classes = {
            "private": "bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400",
            "shared": "bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300",
            "organization": "bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300",
        }
        return classes.get(self.visibility, classes["private"])

    def get_type_icon(self) -> str:
        """Get the Lucide icon name for the document type."""
        # First check custom document type
        if self.document_type:
            return self.document_type.icon or "file-text"

        # Fall back to legacy type
        icons = {
            "motion": "file-text",
            "inquiry": "help-circle",
            "statement": "message-square",
            "amendment": "edit",
        }
        return icons.get(self.motion_type, "file-text")

    def get_type_display(self) -> str:
        """Get the display name for the document type."""
        # First check custom document type
        if self.document_type:
            return self.document_type.name

        # Fall back to legacy type display
        return self.get_motion_type_display()


class MotionShare(models.Model):
    """
    Sharing configuration for a motion.

    Supports granular sharing at multiple levels:
    - User: Share with a specific user
    - Role: Share with all members of a role
    - Organization: Share with an entire organization
    - Party: Share with all organizations in party hierarchy
    - Regional: Share with all organizations in same OParl Body
    """

    SCOPE_CHOICES = [
        ("user", "Einzelner Benutzer"),
        ("role", "Rolle"),
        ("organization", "Organisation"),
        ("party_group", "Parteigruppe"),
        ("regional", "Regional (OParl Body)"),
    ]

    LEVEL_CHOICES = [
        ("view", "Lesen"),
        ("comment", "Kommentieren"),
        ("edit", "Bearbeiten"),
        ("admin", "Verwalten"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    motion = models.ForeignKey(
        Motion,
        on_delete=models.CASCADE,
        related_name="shares",
        verbose_name="Antrag"
    )

    # Scope and level
    scope = models.CharField(
        max_length=20,
        choices=SCOPE_CHOICES,
        verbose_name="Bereich"
    )
    level = models.CharField(
        max_length=20,
        choices=LEVEL_CHOICES,
        default="view",
        verbose_name="Berechtigung"
    )

    # Target (only one should be set based on scope)
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="motion_shares",
        verbose_name="Benutzer"
    )
    role = models.ForeignKey(
        "tenants.Role",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="motion_shares",
        verbose_name="Rolle"
    )
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_motions",
        verbose_name="Organisation"
    )
    party_group = models.ForeignKey(
        "tenants.PartyGroup",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_motions",
        verbose_name="Parteigruppe"
    )
    body = models.ForeignKey(
        "insight_core.OParlBody",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_motions",
        verbose_name="OParl Body"
    )

    # Metadata
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="created_motion_shares",
        verbose_name="Erstellt von"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.TextField(
        blank=True,
        verbose_name="Nachricht",
        help_text="Optionale Nachricht an den Empfänger"
    )

    class Meta:
        verbose_name = "Antragsfreigabe"
        verbose_name_plural = "Antragsfreigaben"
        ordering = ["-created_at"]

    def __str__(self):
        target = self.user or self.role or self.organization or self.party_group or self.body
        return f"{self.motion.title} → {target} ({self.level})"

    def grants_access_to(self, membership) -> bool:
        """Check if this share grants access to a membership."""
        if self.scope == "user":
            return membership.user == self.user

        if self.scope == "role":
            return self.role in membership.roles.all()

        if self.scope == "organization":
            return membership.organization == self.organization

        if self.scope == "party_group":
            if not membership.organization.party_group:
                return False
            org_group = membership.organization.party_group
            # Check if org is in this party group or its descendants
            all_groups = [self.party_group] + self.party_group.get_descendants()
            return org_group in all_groups

        if self.scope == "regional":
            return membership.organization.body == self.body

        return False


class MotionDocument(models.Model):
    """
    File attachment for a motion.

    Supports PDF, Word, and other document formats.
    Text is extracted for search.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    motion = models.ForeignKey(
        Motion,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name="Antrag"
    )

    file = models.FileField(
        upload_to="motions/documents/%Y/%m/",
        verbose_name="Datei"
    )
    filename = models.CharField(max_length=255, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(
        default=0,
        verbose_name="Dateigröße (Bytes)"
    )

    # Extracted text for search
    text_content = models.TextField(
        blank=True,
        verbose_name="Extrahierter Text"
    )

    # Metadata
    uploaded_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="uploaded_documents",
        verbose_name="Hochgeladen von"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Antragsdokument"
        verbose_name_plural = "Antragsdokumente"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.filename


class MotionRevision(EncryptionMixin, models.Model):
    """
    Version history for motion content.

    Created automatically when content changes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    motion = models.ForeignKey(
        Motion,
        on_delete=models.CASCADE,
        related_name="revisions",
        verbose_name="Antrag"
    )

    version = models.PositiveIntegerField(verbose_name="Version")

    # Snapshot of content (encrypted)
    content_encrypted = EncryptedTextField(
        verbose_name="Inhalt"
    )

    # Metadata
    changed_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="motion_revisions",
        verbose_name="Geändert von"
    )
    change_summary = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="Änderungszusammenfassung"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Antragsversion"
        verbose_name_plural = "Antragsversionen"
        unique_together = ["motion", "version"]
        ordering = ["-version"]

    def __str__(self):
        return f"{self.motion.title} v{self.version}"

    @property
    def content(self):
        """Get decrypted content for templates."""
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.motion.organization


class MotionComment(models.Model):
    """
    Inline comment on a motion (like Google Docs).

    Comments can be threaded (replies).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    motion = models.ForeignKey(
        Motion,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name="Antrag"
    )

    # Threading
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
        verbose_name="Antwort auf"
    )

    # Position in document (for inline comments)
    selection_start = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Auswahl Start"
    )
    selection_end = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Auswahl Ende"
    )
    selected_text = models.TextField(
        blank=True,
        verbose_name="Ausgewählter Text"
    )

    # Content
    content = models.TextField(verbose_name="Kommentar")

    # Metadata
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="motion_comments",
        verbose_name="Autor"
    )
    is_resolved = models.BooleanField(
        default=False,
        verbose_name="Erledigt"
    )
    resolved_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_comments",
        verbose_name="Erledigt von"
    )
    resolved_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Erledigt am"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Antragskommentar"
        verbose_name_plural = "Antragskommentare"
        ordering = ["created_at"]

    def __str__(self):
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"{self.author.user.email}: {preview}"


class MotionApproval(models.Model):
    """
    Approval workflow for motions.

    Tracks who needs to approve a motion and their decisions.
    """

    APPROVAL_TYPE_CHOICES = [
        ("chair", "Vorsitzende:r"),
        ("vice_chair", "Stellv. Vorsitzende:r"),
        ("council", "Ratsmitglied"),
        ("party_lead", "Fraktionsführung"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    motion = models.ForeignKey(
        Motion,
        on_delete=models.CASCADE,
        related_name="approvals",
        verbose_name="Antrag"
    )
    approver = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="motion_approvals",
        verbose_name="Genehmiger"
    )
    approval_type = models.CharField(
        max_length=20,
        choices=APPROVAL_TYPE_CHOICES,
        verbose_name="Genehmigungstyp"
    )

    # Decision: None=pending, True=approved, False=rejected
    approved = models.BooleanField(
        null=True,
        default=None,
        verbose_name="Genehmigt"
    )
    comment = models.TextField(
        blank=True,
        verbose_name="Kommentar"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Entschieden am"
    )

    class Meta:
        verbose_name = "Genehmigung"
        verbose_name_plural = "Genehmigungen"
        unique_together = [["motion", "approver", "approval_type"]]
        ordering = ["-created_at"]

    def __str__(self):
        status = "ausstehend"
        if self.approved is True:
            status = "genehmigt"
        elif self.approved is False:
            status = "abgelehnt"
        return f"{self.motion.title} - {self.approver} ({status})"

    @property
    def is_pending(self) -> bool:
        """Check if this approval is still pending."""
        return self.approved is None
