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

import hashlib
import uuid
from datetime import timedelta

from django.db import models
from django.db.models import F
from django.utils import timezone

from apps.common.encryption import EncryptedTextField, EncryptionMixin
from apps.work.files import letterhead_path


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
        verbose_name="Organisation",
    )

    name = models.CharField(max_length=100, verbose_name="Name")
    slug = models.SlugField(max_length=100, verbose_name="Kurzname")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Icon and color for UI
    icon = models.CharField(max_length=50, default="file-text", verbose_name="Icon (Lucide)")
    color = models.CharField(max_length=20, default="blue", verbose_name="Farbe")

    # Workflow settings
    requires_approval = models.BooleanField(
        default=True,
        verbose_name="Freigabe erforderlich",
        help_text="Dokument muss vor Einreichung freigegeben werden",
    )
    is_submittable = models.BooleanField(
        default=True, verbose_name="Einreichbar", help_text="Kann offiziell eingereicht werden"
    )

    # Standard-Checkliste: Liste von Strings, wird beim Anlegen eines
    # Dokuments dieses Typs automatisch als Checklisten-Punkte erzeugt.
    default_checklist = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Standard-Checkliste",
        help_text="Ein Punkt je Zeile; wird bei neuen Dokumenten dieses Typs angelegt",
    )

    # Status
    is_default = models.BooleanField(default=False, verbose_name="Standard-Typ")
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
    Letterhead for document exports.

    Two kinds:
    - pdf: An uploaded PDF serves as background, content is overlaid.
    - generated: The letterhead is rendered from the organization's
      corporate design (logo, colors) plus the fields below.
    """

    KIND_CHOICES = [
        ("pdf", "PDF-Datei"),
        ("generated", "Generiert (Corporate Design)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="letterheads",
        verbose_name="Organisation",
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    kind = models.CharField(
        max_length=20,
        choices=KIND_CHOICES,
        default="pdf",
        verbose_name="Art",
        help_text="PDF-Hintergrund oder aus dem Corporate Design generierter Briefkopf",
    )

    # The PDF file (only for kind=pdf)
    pdf_file = models.FileField(upload_to=letterhead_path, blank=True, null=True, verbose_name="PDF-Datei")

    # === Generated letterhead (kind=generated) ===
    header_logo_enabled = models.BooleanField(
        default=True,
        verbose_name="Logo anzeigen",
        help_text="Nutzt das Logo der Organisation (bzw. der Parteigruppe)",
    )
    sender_line = models.CharField(
        max_length=300,
        blank=True,
        verbose_name="Absenderzeile",
        help_text="Einzeilige Absenderzeile über dem Adressfeld, z. B. „Fraktion XYZ · Rathausplatz 1 · 12345 Stadt“",
    )
    address_block = models.TextField(
        blank=True,
        verbose_name="Absenderblock",
        help_text="Absenderblock rechts oben (mehrzeilig)",
    )
    footer_text = models.TextField(
        blank=True,
        verbose_name="Fußzeile",
        help_text="Fußzeile auf jeder Seite, z. B. Kontakt/Bank/Web; Angaben je Zeile mit · trennbar",
    )
    accent_color_enabled = models.BooleanField(
        default=True,
        verbose_name="Akzentfarbe verwenden",
        help_text="Akzentlinie in der Primärfarbe der Organisation",
    )

    # Content positioning (in mm from top-left)
    content_margin_top = models.PositiveIntegerField(
        default=60,
        verbose_name="Abstand oben (mm)",
        help_text="Abstand vom oberen Rand zum Textbereich",
    )
    content_margin_left = models.PositiveIntegerField(default=25, verbose_name="Abstand links (mm)")
    content_margin_right = models.PositiveIntegerField(default=20, verbose_name="Abstand rechts (mm)")
    content_margin_bottom = models.PositiveIntegerField(default=30, verbose_name="Abstand unten (mm)")

    # Font settings
    font_family = models.CharField(max_length=100, default="Arial", verbose_name="Schriftart")
    font_size = models.PositiveIntegerField(default=11, verbose_name="Schriftgröße (pt)")

    # Status
    is_default = models.BooleanField(default=False, verbose_name="Standard-Briefkopf")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Briefkopf"
        verbose_name_plural = "Briefköpfe"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return f"{self.name} ({self.organization.name})"

    @property
    def is_generated(self) -> bool:
        """True, wenn der Briefkopf aus dem Corporate Design generiert wird."""
        return self.kind == "generated"


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
        verbose_name="Organisation",
    )

    # Link to type (optional - if set, only available for this type)
    motion_type = models.ForeignKey(
        MotionType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="templates",
        verbose_name="Dokumenttyp",
        help_text="Vorlage nur für diesen Typ verfügbar (leer = alle Typen)",
    )

    # Link to letterhead (optional)
    letterhead = models.ForeignKey(
        OrganizationLetterhead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="templates",
        verbose_name="Briefkopf",
    )

    name = models.CharField(max_length=200, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Initial content template (HTML/Markdown)
    content_template = models.TextField(
        blank=True,
        verbose_name="Inhaltsvorlage",
        help_text="Vorausgefüllter Inhalt für neue Dokumente",
    )

    # Structure hints for AI
    structure_hints = models.TextField(
        blank=True,
        verbose_name="Strukturhinweise",
        help_text="Hinweise für die KI-Unterstützung (z.B. Abschnitte, Formatierung)",
    )

    # Signature block
    signature_block = models.TextField(blank=True, verbose_name="Signaturblock")

    # Sharing
    is_shared_party = models.BooleanField(default=False, verbose_name="Partei-weit teilen")

    # Status
    is_default = models.BooleanField(default=False, verbose_name="Standard-Vorlage")
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


class DocumentFolder(models.Model):
    """
    Ordner für die Dokument-Ablage (Baumstruktur, max. Tiefe 4).

    Bewusst NATIV implementiert statt über ein Fremdsystem (z. B. Nextcloud):
    Die Dokumente sind tenant-spezifisch verschlüsselt und ihre Sichtbarkeit
    hängt am eigenen RBAC-/Share-System — beides würde bei einer externen
    Datei-Ablage brechen. Ordner sind reine Organisations-Struktur: Sie
    steuern für Mitglieder KEINE Sichtbarkeit (die regeln weiterhin
    ausschließlich Motion.visibility und MotionShare). Einzige Ausnahme:
    Gäste können per FolderGuestShare für einen Ordner (rekursiv inkl.
    aller Unterordner und enthaltenen Dokumente) freigegeben werden.
    """

    MAX_DEPTH = 4

    # Kleine Palette, identisch zu tenants.Topic.COLOR_CHOICES
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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="document_folders",
        verbose_name="Organisation",
    )

    name = models.CharField(max_length=100, verbose_name="Name")

    # Baum: parent=None ist die Wurzelebene ("Alle Dokumente").
    # on_delete=SET_NULL nur als Fallback bei Queryset-Löschungen —
    # DocumentFolder.delete() hängt Unterordner explizit an den Parent um.
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordneter Ordner",
    )

    color = models.CharField(max_length=20, choices=COLOR_CHOICES, blank=True, default="", verbose_name="Farbe")
    position = models.IntegerField(default=0, verbose_name="Sortierung")

    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_document_folders",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Dokumentordner"
        verbose_name_plural = "Dokumentordner"
        ordering = ["position", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "parent", "name"],
                name="uniq_document_folder_name_per_parent",
            ),
            # unique_together greift bei parent=NULL nicht — Wurzelebene separat absichern
            models.UniqueConstraint(
                fields=["organization", "name"],
                condition=models.Q(parent__isnull=True),
                name="uniq_document_folder_name_root",
            ),
        ]

    def __str__(self):
        return self.name

    def get_depth(self) -> int:
        """Tiefe im Baum (Ordner auf Wurzelebene = 1)."""
        depth = 1
        node = self.parent
        while node is not None:
            depth += 1
            node = node.parent
        return depth

    def get_ancestors(self) -> list["DocumentFolder"]:
        """Vorfahren von der Wurzel bis zum direkten Parent (für Breadcrumbs)."""
        ancestors = []
        node = self.parent
        while node is not None:
            ancestors.append(node)
            node = node.parent
        ancestors.reverse()
        return ancestors

    def is_shared_with_guests(self) -> bool:
        """Ist dieser Ordner (direkt oder über einen übergeordneten Ordner) für Gäste freigegeben?"""
        folder_ids = [self.id, *(ancestor.id for ancestor in self.get_ancestors())]
        return FolderGuestShare.objects.filter(folder_id__in=folder_ids).exists()

    def get_descendants(self) -> list["DocumentFolder"]:
        """Alle Unterordner (rekursiv)."""
        result = []
        stack = list(self.children.all())
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(node.children.all())
        return result

    def get_subtree_height(self) -> int:
        """Höhe des eigenen Teilbaums (nur dieser Ordner = 1)."""
        children = list(self.children.all())
        if not children:
            return 1
        return 1 + max(child.get_subtree_height() for child in children)

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.parent is not None:
            if self.parent_id == self.id:
                raise ValidationError({"parent": "Ein Ordner kann nicht sein eigener Unterordner sein."})
            if self.parent.organization_id != self.organization_id:
                raise ValidationError({"parent": "Der übergeordnete Ordner gehört zu einer anderen Organisation."})
            # Zyklen verhindern (Ordner in eigenen Unterordner verschieben)
            node = self.parent
            while node is not None:
                if node.pk == self.pk:
                    raise ValidationError(
                        {"parent": "Ein Ordner kann nicht in einen eigenen Unterordner verschoben werden."}
                    )
                node = node.parent

        # Maximale Verschachtelungstiefe (inkl. eigenem Teilbaum beim Verschieben)
        parent_depth = self.parent.get_depth() if self.parent is not None else 0
        subtree_height = self.get_subtree_height() if self.pk else 1
        if parent_depth + subtree_height > self.MAX_DEPTH:
            raise ValidationError(
                {"parent": f"Maximale Verschachtelungstiefe von {self.MAX_DEPTH} Ebenen überschritten."}
            )

    def delete(self, *args, **kwargs):
        """
        Löschen ohne Datenverlust: Dokumente und Unterordner wandern zum
        Parent (bzw. zur Wurzel "Alle Dokumente"). Bewusst KEIN Cascade
        auf Motions.
        """
        self.motions.update(folder=self.parent)

        # Unterordner einzeln umhängen; Namenskollisionen auf der
        # Zielebene per Suffix auflösen (unique je Ebene)
        sibling_names = set(
            DocumentFolder.objects.filter(organization=self.organization, parent=self.parent)
            .exclude(pk=self.pk)
            .values_list("name", flat=True)
        )
        for child in self.children.all():
            new_name = child.name
            counter = 2
            while new_name in sibling_names:
                new_name = f"{child.name} ({counter})"
                counter += 1
            sibling_names.add(new_name)
            child.name = new_name
            child.parent = self.parent
            child.save(update_fields=["name", "parent"])

        return super().delete(*args, **kwargs)


class FolderGuestShare(models.Model):
    """
    Ordner-Freigabe an einzelne Nutzer (insbesondere Gäste).

    Die Freigabe gilt für den Ordner UND rekursiv für alle Unterordner
    sowie alle enthaltenen Dokumente — auch künftig hinzukommende.
    Zugriffs-Checks laufen zentral über Motion.get_guest_share_level()
    bzw. shared_folder_levels(); Views/Consumer prüfen NICHT einzeln.
    """

    LEVEL_CHOICES = [
        ("view", "Lesen"),
        ("comment", "Kommentieren"),
        ("edit", "Bearbeiten"),
    ]

    # Rangfolge für "höchstes Level gewinnt" (direkt vs. geerbt)
    LEVEL_RANK = {"view": 0, "comment": 1, "edit": 2}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    folder = models.ForeignKey(
        DocumentFolder,
        on_delete=models.CASCADE,
        related_name="guest_shares",
        verbose_name="Ordner",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="folder_shares",
        verbose_name="Benutzer",
    )
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default="view", verbose_name="Berechtigung")

    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_folder_shares",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ordner-Freigabe"
        verbose_name_plural = "Ordner-Freigaben"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["folder", "user"], name="uniq_folder_guest_share_per_user"),
        ]

    def __str__(self):
        return f"{self.folder.name} → {self.user} ({self.level})"

    @classmethod
    def shared_folder_levels(cls, user, organization) -> dict:
        """
        Effektive Ordner-Freigaben eines Nutzers in einer Organisation.

        Returns {folder_id: level} für alle direkt freigegebenen Ordner
        UND (rekursiv geerbt) alle deren Unterordner. Bei mehreren
        Freigaben auf einem Pfad gewinnt das höchste Level.
        """
        direct: dict = {}
        qs = cls.objects.filter(user=user, folder__organization=organization)
        for folder_id, level in qs.values_list("folder_id", "level"):
            current = direct.get(folder_id)
            if current is None or cls.LEVEL_RANK.get(level, 0) > cls.LEVEL_RANK.get(current, 0):
                direct[folder_id] = level
        if not direct:
            return {}

        # Baum einmal laden und Level top-down vererben
        children: dict = {}
        for folder_id, parent_id in DocumentFolder.objects.filter(organization=organization).values_list(
            "id", "parent_id"
        ):
            children.setdefault(parent_id, []).append(folder_id)

        levels: dict = {}

        def walk(folder_id, inherited):
            own = direct.get(folder_id)
            effective = inherited
            if own is not None and (effective is None or cls.LEVEL_RANK.get(own, 0) > cls.LEVEL_RANK.get(effective, 0)):
                effective = own
            if effective is not None:
                levels[folder_id] = effective
            for child_id in children.get(folder_id, []):
                walk(child_id, effective)

        for root_id in children.get(None, []):
            walk(root_id, None)
        return levels

    @classmethod
    def shared_folder_scopes(cls, user, organization) -> list[tuple[set, int | None]]:
        """
        Je Ordner-Freigabe eines Nutzers: (IDs des Ordners und aller Unterordner, User-ID der freigebenden Person).

        Grundlage dafür, welche Dokumente eine Ordner-Freigabe umfasst (Motion._folder_share_applies).
        """
        shares = list(
            cls.objects.filter(user=user, folder__organization=organization).values_list("folder_id", "created_by_id")
        )
        if not shares:
            return []
        children: dict = {}
        for folder_id, parent_id in DocumentFolder.objects.filter(organization=organization).values_list(
            "id", "parent_id"
        ):
            children.setdefault(parent_id, []).append(folder_id)

        scopes = []
        for root_id, created_by_id in shares:
            subtree = set()
            stack = [root_id]
            while stack:
                folder_id = stack.pop()
                if folder_id in subtree:
                    continue
                subtree.add(folder_id)
                stack.extend(children.get(folder_id, []))
            scopes.append((subtree, created_by_id))
        return scopes


class StatusTransitionError(ValueError):
    """Statuswechsel, der in ``Motion.VALID_TRANSITIONS`` nicht vorgesehen ist."""


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
        ("private", "Privat"),  # Only the author can see
        ("shared", "Geteilt"),  # Specific people via MotionShare
        ("organization", "Organisation"),  # Everyone in the organization
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
        # Ergebnis der Beratung (Rückmeldung der Verwaltung, Issue #316)
        ("adopted", "Beschlossen"),
        ("withdrawn", "Zurückgezogen"),
    ]

    # Geführte Pipeline (Reihenfolge für den Status-Tracker in der Übersicht).
    # external_review ist optional und kann übersprungen werden.
    PIPELINE_ORDER = [
        "draft",
        "internal_review",
        "external_review",
        "approved",
        "submitted",
        "at_admin",
        "on_agenda",
        "completed",
    ]

    # Zentrale Übergangsmatrix: Status -> erlaubte Folgestatus.
    # Rücksprünge (z. B. zurück zu draft) sind bewusst erlaubt.
    # Ergebnisse der Beratung (Beschlossen, Erledigt, Abgelehnt, Zurückgezogen) lassen sich zurück
    # auf „Auf Tagesordnung“ setzen: Korrigiert die Verwaltung ein erfasstes Ergebnis, folgt die
    # Rückmeldung über diesen Weg (Issue #316).
    VALID_TRANSITIONS = {
        "draft": ["internal_review", "archived"],
        "internal_review": ["external_review", "approved", "draft", "rejected"],
        "external_review": ["approved", "internal_review", "draft", "rejected"],
        "approved": ["submitted", "internal_review", "draft"],
        "submitted": ["at_admin", "completed", "rejected", "withdrawn", "approved"],
        "at_admin": ["on_agenda", "completed", "rejected", "withdrawn", "submitted"],
        "on_agenda": ["adopted", "completed", "rejected", "withdrawn", "at_admin"],
        "adopted": ["completed", "archived", "on_agenda"],
        "completed": ["archived", "on_agenda"],
        "rejected": ["draft", "archived", "on_agenda"],
        "withdrawn": ["draft", "archived", "on_agenda"],
        "archived": ["draft"],
        # Legacy-Status: Bestandsdokumente können in die neue Pipeline wechseln
        "review": ["internal_review", "draft", "approved", "rejected"],
    }

    # Abgeschlossene Dokumente: keine Fristen-Erinnerungen, nicht „überfällig“
    CLOSED_STATUSES = ("completed", "adopted", "rejected", "withdrawn", "archived", "deleted")

    # Status, in denen der Inhalt bearbeitet werden darf (Status-Sperre).
    # Alle anderen Status frieren die Bearbeitung ein — im HTTP-Editor UND
    # im Live-Kollaborationsmodus. Ausnahme: motions.edit_all (siehe
    # apply_status_lock).
    EDITABLE_STATUSES = ("draft", "review", "internal_review", "external_review")

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
        verbose_name="Organisation",
    )

    # Type - can be dynamic (MotionType) or legacy (string)
    document_type = models.ForeignKey(
        MotionType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Dokumenttyp",
    )
    # Legacy field for backwards compatibility
    motion_type = models.CharField(
        max_length=20,
        choices=LEGACY_TYPE_CHOICES,
        default="motion",
        verbose_name="Typ (legacy)",
        blank=True,
    )

    # Ordner-Ablage: reine Organisations-Struktur, steuert KEINE Sichtbarkeit.
    # null = Wurzelebene "Alle Dokumente".
    folder = models.ForeignKey(
        DocumentFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Ordner",
    )

    title = models.CharField(max_length=500, verbose_name="Titel")
    summary = models.TextField(blank=True, verbose_name="Zusammenfassung", help_text="Öffentliche Kurzfassung")

    # Content (encrypted, stored as HTML from WYSIWYG editor)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft", verbose_name="Status")

    # Default edit mode for collaborators
    default_edit_mode = models.CharField(
        max_length=20,
        choices=EDIT_MODE_CHOICES,
        default="edit",
        verbose_name="Standard-Bearbeitungsmodus",
        help_text="Standard-Modus für neue Mitarbeiter",
    )

    # Target meeting for agenda integration
    target_meeting = models.ForeignKey(
        "work.FactionMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_motions",
        verbose_name="Ziel-Sitzung",
        help_text="Sitzung, in der dieser Antrag behandelt werden soll",
    )

    # Template used
    template = models.ForeignKey(
        MotionTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Vorlage",
    )

    # Letterhead for export
    letterhead = models.ForeignKey(
        OrganizationLetterhead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="motions",
        verbose_name="Briefkopf",
    )

    # Linkages to OParl
    related_paper = models.ForeignKey(
        "insight_core.OParlPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_motions",
        verbose_name="Verknüpfter Vorgang",
        help_text="OParl-Vorlage wenn eingereicht",
    )
    related_meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_motions",
        verbose_name="Ziel-Sitzung",
    )

    # For amendments - link to parent motion
    parent_motion = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="amendments",
        verbose_name="Bezugsantrag",
    )

    # Real-time collaboration state (Yjs document)
    yjs_document = models.BinaryField(
        blank=True,
        null=True,
        verbose_name="Yjs-Dokument",
        help_text="Binary state for real-time collaboration",
    )

    # Visibility (simplified permission system)
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="private",
        verbose_name="Sichtbarkeit",
        help_text="Wer kann dieses Dokument sehen?",
    )

    # Metadata
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="authored_motions",
        verbose_name="Autor",
    )
    # Deprecated: JSON-Tags, ersetzt durch topics (M2M auf tenants.Topic).
    # Wird nur noch für die Datenmigration behalten.
    tags = models.JSONField(default=list, blank=True, verbose_name="Tags")

    # Zuständigkeiten
    responsible = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="responsible_motions",
        verbose_name="Federführung",
    )
    contributors = models.ManyToManyField(
        "tenants.Membership",
        blank=True,
        related_name="contributing_motions",
        verbose_name="Mitarbeit",
    )

    # Themenkatalog
    topics = models.ManyToManyField(
        "tenants.Topic",
        blank=True,
        related_name="motions",
        verbose_name="Themen",
    )

    # Frist
    due_date = models.DateField(blank=True, null=True, verbose_name="Frist")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(blank=True, null=True, verbose_name="Eingereicht am")
    # Digitale Einreichung bei der Verwaltung (Issue #40): der im Session-RIS
    # angelegte Antrag; Statuswechsel dort laufen per Signal zurück.
    session_application = models.OneToOneField(
        "session.SessionApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_motion",
        verbose_name="Antrag bei der Verwaltung",
    )
    # Zuletzt aus der Rückmeldung der Verwaltung abgeleiteter Status (Issue #316). Der Work-Status
    # folgt der Verwaltung nur, wenn sich dieser Stand ändert – eine Hand-Korrektur der Fraktion
    # (z. B. „Erledigt“) bleibt stehen, bis in Session wieder etwas passiert.
    administration_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        blank=True,
        verbose_name="Stand laut Verwaltung",
    )
    deleted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Gelöscht am",
        help_text="Zeitpunkt der Löschung (30 Tage Papierkorb)",
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

    @classmethod
    def visible_to(cls, membership, *, include_deleted=False):
        """
        Queryset der Dokumente, die ein Mitglied sehen darf (analog can_access).

        Einzige Quelle für Dokumentlisten aller Art – Liste, Kacheln,
        Ordner-Zähler, Dashboard, Auswahlfelder, Papierkorb: eigene Dokumente,
        organisationsweite sowie persönlich geteilte. Gäste sehen ausschließlich
        persönlich freigegebene Dokumente sowie Dokumente in für sie
        freigegebenen Ordnern (rekursiv).

        Args:
            include_deleted: Auch Dokumente im Papierkorb liefern.
        """
        qs = cls.objects.filter(organization=membership.organization)
        if not include_deleted:
            qs = qs.exclude(status="deleted")
        if getattr(membership, "is_guest", False):
            # Ordner-Freigaben: organisationsweite und eigene Dokumente der freigebenden Person
            folder_q = models.Q(pk__in=[])
            for folder_ids, created_by_id in FolderGuestShare.shared_folder_scopes(
                membership.user, membership.organization
            ):
                applies = models.Q(visibility="organization")
                if created_by_id is not None:
                    applies |= models.Q(author__user_id=created_by_id)
                folder_q |= models.Q(folder_id__in=folder_ids) & applies
            return qs.filter(models.Q(shares__scope="user", shares__user=membership.user) | folder_q).distinct()
        return qs.filter(
            models.Q(author=membership)
            # Federfuehrung und Mitarbeit sehen das Dokument, fuer das sie
            # eingeteilt sind. Ohne das erhaelt die Federfuehrung zwar eine
            # Benachrichtigung ueber die Zuweisung, laeuft beim Oeffnen aber in
            # ein 404 — die Zuweisung waere folgenlos (Issue #249).
            | models.Q(responsible=membership)
            | models.Q(contributors=membership)
            | models.Q(visibility="organization")
            | models.Q(visibility="shared", shares__user=membership.user)
        ).distinct()

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

    def get_guest_share_level(self, membership) -> str | None:
        """
        Freigabe-Level eines Gast-Zugangs für dieses Dokument.

        Gäste erhalten Zugriff ausschließlich über persönliche Freigaben:
        Dokument-Freigaben (MotionShare, scope=user) ODER Ordner-Freigaben
        (FolderGuestShare) auf den Ordner des Dokuments bzw. einen seiner
        übergeordneten Ordner. Returns 'view'/'comment'/'edit'/'admin'
        oder None, wenn keine Freigabe existiert.
        """
        levels = set(self.shares.filter(scope="user", user=membership.user).values_list("level", flat=True))
        # Ordner-Freigaben gelten rekursiv: Ordner des Dokuments + Vorfahren
        node = self.folder
        while node is not None:
            for level, created_by_id in node.guest_shares.filter(user=membership.user).values_list(
                "level", "created_by_id"
            ):
                if self._folder_share_applies(created_by_id):
                    levels.add(level)
            node = node.parent
        for level in ("admin", "edit", "comment", "view"):
            if level in levels:
                return level
        return None

    def guest_share_since(self, membership):
        """
        Zeitpunkt, seit dem ein Gast-Zugang dieses Dokument sieht (oder None).

        Frühester Beginn aller wirksamen Freigaben: persönliche Dokument-
        Freigabe (MotionShare, scope=user) oder Ordner-Freigaben auf dem
        Ordner des Dokuments bzw. seinen Vorfahren. Datenschutz: Gäste sehen
        die Versionshistorie erst ab diesem Zeitpunkt – Entwürfe und
        Änderungen vor der Freigabe bleiben ihnen verborgen.
        """
        timestamps = list(self.shares.filter(scope="user", user=membership.user).values_list("created_at", flat=True))
        node = self.folder
        while node is not None:
            for created_at, created_by_id in node.guest_shares.filter(user=membership.user).values_list(
                "created_at", "created_by_id"
            ):
                if self._folder_share_applies(created_by_id):
                    timestamps.append(created_at)
            node = node.parent
        return min(timestamps) if timestamps else None

    def revisions_for(self, membership):
        """
        Versionshistorie, die ein Mitglied sehen darf.

        Mitglieder mit Zugriff sehen alle Versionen; Gäste nur die ab dem
        Beginn ihrer Freigabe (guest_share_since).
        """
        qs = self.revisions.all()
        if getattr(membership, "is_guest", False):
            since = self.guest_share_since(membership)
            if since is None:
                return qs.none()
            qs = qs.filter(created_at__gte=since)
        return qs

    def can_access(self, membership) -> bool:
        """
        Check if a membership has access to this document.

        Access is granted based on visibility:
        - private: Only the author
        - shared: Author + users with MotionShare entries
        - organization: Anyone in the same organization

        Gäste (Membership.is_guest) sehen unabhängig von der Sichtbarkeit
        NUR Dokumente mit persönlicher Freigabe (scope=user).
        """
        # Gäste: ausschließlich explizit freigegebene Dokumente
        if getattr(membership, "is_guest", False):
            return self.get_guest_share_level(membership) is not None

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
        Gäste: nur mit Freigabe-Level edit/admin.
        """
        if getattr(membership, "is_guest", False):
            return self.get_guest_share_level(membership) in ("edit", "admin")

        if self.author == membership:
            return True

        if self.visibility == "private":
            return False

        # For shared/organization, anyone with access can edit
        return self.can_access(membership)

    @property
    def is_status_locked(self) -> bool:
        """Status-Sperre: In diesem Status ist der Inhalt eingefroren."""
        return self.status not in self.EDITABLE_STATUSES

    def apply_status_lock(self, level: str, membership) -> str:
        """
        Status-Sperre zentral auf eine Zugriffsstufe anwenden.

        Einzige Stelle für die Sperr-Semantik — genutzt vom HTTP-Editor
        (DocumentEditorView._get_access_level) UND vom WebSocket-Consumer
        (über get_collab_access_level). In gesperrten Status werden
        Schreibstufen ('edit'/'admin') herabgestuft:
        - Gäste: auf 'comment' (wie bisher im HTTP-Editor)
        - Mitglieder: auf 'comment' (mit motions.comment) bzw. 'view'
        - Ausnahme: motions.edit_all ("alle Anträge bearbeiten") behält 'edit'
        Lesestufen ('comment'/'view') bleiben unverändert.
        """
        if level not in ("edit", "admin") or not self.is_status_locked:
            return level
        if getattr(membership, "is_guest", False):
            return "comment"
        if membership.has_permission("motions.edit_all"):
            return "edit"
        return "comment" if membership.has_permission("motions.comment") else "view"

    def editor_access_level(self, membership) -> str:
        """
        Zugriffsstufe im Editor: 'admin', 'edit', 'comment', 'view' oder 'none'.

        Einzige Stelle für die Stufenlogik – der HTTP-Editor (DocumentEditorView) und der
        WebSocket-Consumer (get_collab_access_level) nutzen sie gemeinsam:
        - Gäste: ausschließlich ihre persönliche Freigabe (nie Verwaltungsrechte)
        - Mitglieder brauchen ``motions.view``; Autor:in verwaltet, ``motions.edit_all``
          bearbeitet, eine persönliche Freigabe „Bearbeiten“ bearbeitet mit ``motions.edit``,
          sonst Kommentieren (``motions.comment``) oder Lesen
        - danach greift die Status-Sperre (apply_status_lock)
        """
        if not self.can_access(membership):
            return "none"

        if getattr(membership, "is_guest", False):
            level = self.get_guest_share_level(membership)
            if level is None:
                return "none"
            if level == "admin":
                level = "edit"  # Gäste erhalten nie Verwaltungsrechte
            return self.apply_status_lock(level, membership)

        if not membership.has_permission("motions.view"):
            return "none"
        if self.author_id == membership.id:
            level = "admin"
        elif membership.has_permission("motions.edit_all") or (
            membership.has_permission("motions.edit") and self._member_share_level(membership) in ("edit", "admin")
        ):
            level = "edit"
        elif membership.has_permission("motions.comment"):
            level = "comment"
        else:
            level = "view"
        return self.apply_status_lock(level, membership)

    def _member_share_level(self, membership) -> str | None:
        """Höchste persönliche Freigabestufe (scope=user) eines Mitglieds oder ``None``."""
        levels = set(self.shares.filter(scope="user", user_id=membership.user_id).values_list("level", flat=True))
        return next((level for level in ("admin", "edit", "comment", "view") if level in levels), None)

    def get_collab_access_level(self, membership) -> str | None:
        """
        Zugriffsstufe im Live-Kollaborationsmodus (WebSocket-Consumer).

        Dieselbe Stufe wie im HTTP-Editor (editor_access_level, inkl. Status-Sperre);
        'admin' zählt hier als 'edit'. Returns 'edit'/'comment'/'view' oder None ohne Zugriff.
        """
        level = self.editor_access_level(membership)
        if level == "none":
            return None
        return "edit" if level == "admin" else level

    def can_comment(self, membership) -> bool:
        """
        Check if a membership can comment on this document.

        With simplified permissions, anyone with access can comment.
        Gäste: nur mit Freigabe-Level comment/edit/admin.
        """
        if getattr(membership, "is_guest", False):
            return self.get_guest_share_level(membership) in ("comment", "edit", "admin")

        return self.can_access(membership)

    def can_share(self, membership) -> bool:
        """Darf das Mitglied das Dokument weiteren Personen zugänglich machen (Autor:in oder ``motions.share``)?"""
        if getattr(membership, "is_guest", False) or not self.can_access(membership):
            return False
        return self.author_id == membership.id or membership.has_permission("motions.share")

    def _folder_share_applies(self, created_by_id) -> bool:
        """
        Gilt eine Ordner-Freigabe (angelegt von ``created_by_id``) für dieses Dokument?

        Ordner-Freigaben umfassen organisationsweite Dokumente und die eigenen Dokumente der
        freigebenden Person – private oder gezielt geteilte Dokumente anderer bleiben außen vor.
        """
        if self.visibility == "organization":
            return True
        return created_by_id is not None and self.author.user_id == created_by_id

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

    def allowed_next_statuses(self) -> list[tuple[str, str]]:
        """Erlaubte Folgestatus als (value, label)-Liste für Dropdowns."""
        status_labels = dict(self.STATUS_CHOICES)
        return [(value, status_labels[value]) for value in self.VALID_TRANSITIONS.get(self.status, [])]

    # -------------------------------------------------------------------------
    # Status-Übergänge: einziger Weg, den Status zu ändern (Issue #316)
    # -------------------------------------------------------------------------

    def transition_to(self, new_status: str) -> None:
        """
        Einen Übergang der Übergangsmatrix ausführen und speichern.

        Raises:
            StatusTransitionError: Der Übergang ist in VALID_TRANSITIONS nicht vorgesehen.
        """
        if new_status not in self.VALID_TRANSITIONS.get(self.status, []):
            labels = dict(self.STATUS_CHOICES)
            raise StatusTransitionError(
                f"Ungültiger Statusübergang von '{labels.get(self.status, self.status)}' "
                f"zu '{labels.get(new_status, new_status)}'"
            )
        self.status = new_status
        fields = ["status", "updated_at"]
        if new_status == "submitted":
            self.submitted_at = timezone.now()
            fields.append("submitted_at")
        self.save(update_fields=fields)

    def transition_path(
        self, target: str, *, via: tuple[str, ...] | set[str] | frozenset[str] = ()
    ) -> list[str] | None:
        """
        Kürzeste Folge definierter Übergänge vom aktuellen Status zu ``target`` (ohne Startstatus).

        Zwischenschritte sind nur über Status aus ``via`` erlaubt; ``None``, wenn es keinen Weg gibt.
        """
        if target == self.status:
            return []
        allowed = set(via) | {target}
        previous: dict[str, str] = {}
        queue = [self.status]
        seen = {self.status}
        while queue:
            current = queue.pop(0)
            for nxt in self.VALID_TRANSITIONS.get(current, []):
                if nxt in seen or nxt not in allowed:
                    continue
                previous[nxt] = current
                if nxt == target:
                    path = [nxt]
                    while previous.get(path[0]) not in (None, self.status):
                        path.insert(0, previous[path[0]])
                    return path
                seen.add(nxt)
                queue.append(nxt)
        return None

    def advance_to(self, target: str, *, via: tuple[str, ...] | set[str] | frozenset[str] = ()) -> list[str]:
        """
        Über definierte Übergänge zum Zielstatus wechseln (jeder Schritt einzeln geprüft und gespeichert).

        Raises:
            StatusTransitionError: Es gibt keinen Weg über die erlaubten Zwischenschritte.
        """
        path = self.transition_path(target, via=via)
        if path is None:
            labels = dict(self.STATUS_CHOICES)
            raise StatusTransitionError(
                f"Kein Übergang von '{labels.get(self.status, self.status)}' zu '{labels.get(target, target)}'"
            )
        for step in path:
            self.transition_to(step)
        return path

    def get_pipeline_steps(self) -> list[dict]:
        """
        Status-Tracker für die Übersicht: Pipeline-Schritte mit Füllstand.

        Jeder Schritt: {"key", "label", "reached", "current"}.
        Bei abgelehnten/archivierten Dokumenten wird der Tracker
        entsprechend markiert (state "rejected"/"off").
        """
        status_labels = dict(self.STATUS_CHOICES)
        # „Beschlossen“ ist wie „Erledigt“ der letzte Schritt der Pipeline
        pipeline_status = "completed" if self.status == "adopted" else self.status
        try:
            current_index = self.PIPELINE_ORDER.index(pipeline_status)
        except ValueError:
            # Nicht auf der Pipeline (rejected, withdrawn, archived, legacy review)
            current_index = -1

        steps = []
        for index, key in enumerate(self.PIPELINE_ORDER):
            label = status_labels[key]
            if key == "completed" and self.status == "adopted":
                label = status_labels["adopted"]
            steps.append(
                {
                    "key": key,
                    "label": label,
                    "reached": current_index >= 0 and index <= current_index,
                    "current": index == current_index,
                }
            )
        return steps

    @property
    def pipeline_state(self) -> str:
        """Zustand für den Tracker: 'on' (auf Pipeline), 'rejected' oder 'off'."""
        if self.status == "rejected":
            return "rejected"
        if self.status in self.PIPELINE_ORDER or self.status == "adopted":
            return "on"
        return "off"

    @property
    def is_overdue(self) -> bool:
        """Frist überschritten und Dokument noch nicht abgeschlossen?"""
        if not self.due_date:
            return False
        if self.status in self.CLOSED_STATUSES:
            return False
        return self.due_date < timezone.localdate()

    @property
    def checklist_progress(self) -> dict:
        """Checklisten-Fortschritt {done, total, percent}. Nutzt Prefetch-Cache."""
        items = self.checklist_items.all()
        total = len(items)
        done = sum(1 for item in items if item.is_completed)
        percent = round(done / total * 100) if total else 0
        return {"done": done, "total": total, "percent": percent}

    def apply_default_checklist(self):
        """
        Legt die Standard-Checkliste des Dokumenttyps als Checklisten-Punkte an.

        Wird beim Erstellen eines Dokuments aufgerufen; tut nichts, wenn der
        Typ keine Standard-Checkliste hat oder bereits Punkte existieren.
        """
        if not self.document_type or not self.document_type.default_checklist:
            return
        if self.checklist_items.exists():
            return
        items = [
            MotionChecklistItem(motion=self, title=str(title)[:300], position=index)
            for index, title in enumerate(self.document_type.default_checklist)
            if str(title).strip()
        ]
        MotionChecklistItem.objects.bulk_create(items)

    @property
    def approval_summary(self) -> dict:
        """Freigabe-Zusammenfassung {approved, rejected, pending, total}. Nutzt Prefetch-Cache."""
        approvals = self.approvals.all()
        total = len(approvals)
        approved = sum(1 for a in approvals if a.approved is True)
        rejected = sum(1 for a in approvals if a.approved is False)
        return {
            "approved": approved,
            "rejected": rejected,
            "pending": total - approved - rejected,
            "total": total,
        }


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

    motion = models.ForeignKey(Motion, on_delete=models.CASCADE, related_name="shares", verbose_name="Antrag")

    # Scope and level
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, verbose_name="Bereich")
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default="view", verbose_name="Berechtigung")

    # Target (only one should be set based on scope)
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="motion_shares",
        verbose_name="Benutzer",
    )
    role = models.ForeignKey(
        "tenants.Role",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="motion_shares",
        verbose_name="Rolle",
    )
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_motions",
        verbose_name="Organisation",
    )
    party_group = models.ForeignKey(
        "tenants.PartyGroup",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_motions",
        verbose_name="Parteigruppe",
    )
    body = models.ForeignKey(
        "insight_core.OParlBody",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_motions",
        verbose_name="OParl Body",
    )

    # Metadata
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="created_motion_shares",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.TextField(blank=True, verbose_name="Nachricht", help_text="Optionale Nachricht an den Empfänger")

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
            if not self.body_id:
                return False
            org = membership.organization
            # Multi-Kommune: primäre Kommune (FK) ODER eine der M2M-Kommunen
            return org.body_id == self.body_id or org.bodies.filter(pk=self.body_id).exists()

        return False


class MotionDocument(models.Model):
    """
    File attachment for a motion.

    Supports PDF, Word, and other document formats.
    Text is extracted for search.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    motion = models.ForeignKey(Motion, on_delete=models.CASCADE, related_name="documents", verbose_name="Antrag")

    file = models.FileField(upload_to="motions/documents/%Y/%m/", verbose_name="Datei")
    filename = models.CharField(max_length=255, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(default=0, verbose_name="Dateigröße (Bytes)")

    # Extracted text for search
    text_content = models.TextField(blank=True, verbose_name="Extrahierter Text")

    # Metadata
    uploaded_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="uploaded_documents",
        verbose_name="Hochgeladen von",
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

    motion = models.ForeignKey(Motion, on_delete=models.CASCADE, related_name="revisions", verbose_name="Antrag")

    version = models.PositiveIntegerField(verbose_name="Version")

    # Snapshot of content (encrypted)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Metadata
    changed_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="motion_revisions",
        verbose_name="Geändert von",
    )
    change_summary = models.CharField(max_length=500, blank=True, verbose_name="Änderungszusammenfassung")
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

    motion = models.ForeignKey(Motion, on_delete=models.CASCADE, related_name="comments", verbose_name="Antrag")

    # Threading
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
        verbose_name="Antwort auf",
    )

    # Position in document (for inline comments)
    selection_start = models.PositiveIntegerField(null=True, blank=True, verbose_name="Auswahl Start")
    selection_end = models.PositiveIntegerField(null=True, blank=True, verbose_name="Auswahl Ende")
    selected_text = models.TextField(blank=True, verbose_name="Ausgewählter Text")

    # TipTap mark ID for inline comment highlighting
    mark_id = models.UUIDField(null=True, blank=True, verbose_name="Editor Mark ID")

    # Content
    content = models.TextField(verbose_name="Kommentar")

    # Metadata
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="motion_comments",
        verbose_name="Autor",
    )
    is_resolved = models.BooleanField(default=False, verbose_name="Erledigt")
    resolved_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_comments",
        verbose_name="Erledigt von",
    )
    resolved_at = models.DateTimeField(blank=True, null=True, verbose_name="Erledigt am")

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

    motion = models.ForeignKey(Motion, on_delete=models.CASCADE, related_name="approvals", verbose_name="Antrag")
    approver = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="motion_approvals",
        verbose_name="Genehmiger",
    )
    approval_type = models.CharField(max_length=20, choices=APPROVAL_TYPE_CHOICES, verbose_name="Genehmigungstyp")

    # Decision: None=pending, True=approved, False=rejected
    approved = models.BooleanField(null=True, default=None, verbose_name="Genehmigt")
    comment = models.TextField(blank=True, verbose_name="Kommentar")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="Entschieden am")

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


class MotionChecklistItem(models.Model):
    """Checklisten-Punkt eines Dokuments (Muster: tasks.TaskChecklistItem)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    motion = models.ForeignKey(
        Motion, on_delete=models.CASCADE, related_name="checklist_items", verbose_name="Dokument"
    )
    title = models.CharField(max_length=300, verbose_name="Titel")
    is_completed = models.BooleanField(default=False, verbose_name="Erledigt")
    completed_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_motion_checklist_items",
        verbose_name="Erledigt von",
    )
    completed_at = models.DateTimeField(blank=True, null=True, verbose_name="Erledigt am")
    position = models.PositiveIntegerField(default=0, verbose_name="Position")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Dokument-Checklisten-Punkt"
        verbose_name_plural = "Dokument-Checklisten-Punkte"
        ordering = ["position", "created_at"]

    def __str__(self):
        check = "✓" if self.is_completed else "○"
        return f"{check} {self.title}"


class OrganizationAITokenUsage(models.Model):
    """
    Aggregated AI token usage for organization-level budgeting.

    Stores token consumption per organization and period to enforce
    daily/weekly/monthly limits configured on the organization model.
    """

    PERIOD_DAY = "day"
    PERIOD_WEEK = "week"
    PERIOD_MONTH = "month"
    PERIOD_CHOICES = [
        (PERIOD_DAY, "Tag"),
        (PERIOD_WEEK, "Woche"),
        (PERIOD_MONTH, "Monat"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="ai_token_usage_entries",
        verbose_name="Organisation",
    )
    period_type = models.CharField(max_length=10, choices=PERIOD_CHOICES, verbose_name="Periode")
    period_start = models.DateField(verbose_name="Periodenbeginn")
    tokens_used = models.PositiveIntegerField(default=0, verbose_name="Verbrauchte Tokens")
    requests_count = models.PositiveIntegerField(default=0, verbose_name="Anfragen")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "KI Tokenverbrauch"
        verbose_name_plural = "KI Tokenverbraeuche"
        unique_together = [["organization", "period_type", "period_start"]]
        indexes = [
            models.Index(fields=["organization", "period_type", "period_start"]),
        ]
        ordering = ["-period_start", "period_type"]

    def __str__(self):
        return f"{self.organization.name} {self.period_type} {self.period_start}: {self.tokens_used}"

    @classmethod
    def _period_start(cls, period_type: str, ref_date):
        if period_type == cls.PERIOD_DAY:
            return ref_date
        if period_type == cls.PERIOD_WEEK:
            return ref_date - timedelta(days=ref_date.weekday())
        if period_type == cls.PERIOD_MONTH:
            return ref_date.replace(day=1)
        return ref_date

    @classmethod
    def get_tokens_used(cls, organization, period_type: str, ref_date=None) -> int:
        ref_date = ref_date or timezone.localdate()
        period_start = cls._period_start(period_type, ref_date)
        entry = (
            cls.objects.filter(
                organization=organization,
                period_type=period_type,
                period_start=period_start,
            )
            .only("tokens_used")
            .first()
        )
        return int(entry.tokens_used) if entry else 0

    @classmethod
    def increment_usage(cls, organization, total_tokens: int, ref_date=None) -> None:
        if total_tokens <= 0:
            return

        ref_date = ref_date or timezone.localdate()
        for period_type in (cls.PERIOD_DAY, cls.PERIOD_WEEK, cls.PERIOD_MONTH):
            period_start = cls._period_start(period_type, ref_date)
            obj, created = cls.objects.get_or_create(
                organization=organization,
                period_type=period_type,
                period_start=period_start,
                defaults={"tokens_used": total_tokens, "requests_count": 1},
            )
            if not created:
                cls.objects.filter(pk=obj.pk).update(
                    tokens_used=F("tokens_used") + total_tokens,
                    requests_count=F("requests_count") + 1,
                )


class AdministrationConnection(models.Model):
    """
    Verbindung einer Organisation zu einer Verwaltung (Session-Mandant) für die
    digitale Antragseinreichung (Issue #40). Die Verwaltung stellt einen
    Einreichungs-Token (SessionAPIToken) aus; hier liegt nur dessen Hash.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="administration_connection",
        verbose_name="Organisation",
    )
    tenant = models.ForeignKey(
        "session.SessionTenant",
        on_delete=models.CASCADE,
        related_name="work_connections",
        verbose_name="Verwaltung",
    )
    token_hash = models.CharField(max_length=64, verbose_name="Token (SHA-256)")
    token_prefix = models.CharField(max_length=8, blank=True, verbose_name="Token-Präfix")
    connected_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Verbunden von",
    )
    connected_at = models.DateTimeField(default=timezone.now, verbose_name="Verbunden am")
    last_used_at = models.DateTimeField(blank=True, null=True, verbose_name="Letzte Einreichung")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    class Meta:
        verbose_name = "Verbindung zur Verwaltung"
        verbose_name_plural = "Verbindungen zur Verwaltung"

    def __str__(self):
        return f"{self.organization} → {self.tenant}"

    def get_token(self):
        """Zugehöriger SessionAPIToken (None, wenn von der Verwaltung gelöscht)."""
        from apps.session.models import SessionAPIToken

        return SessionAPIToken.objects.filter(token=self.token_hash, tenant_id=self.tenant_id).first()


class MotionAdministrationEvent(models.Model):
    """
    Bereits gemeldete Rückmeldung der Verwaltung zu einem eingereichten Antrag (Issue #316).

    Der Schlüssel macht jede Meldung idempotent: „Beratung terminiert“ genau einmal je Termin (Sitzung,
    in der die Vorlage beraten wird), jeder Antragsstatus und jedes Beratungsergebnis genau einmal –
    egal, wie oft die Verwaltung die Station speichert, umsortiert oder neu nummeriert. Wird die Station
    auf eine andere Sitzung verschoben, ist das ein neuer Termin. Gespeichert wird bewusst nur
    der Schlüssel, kein Text: Die Anzeige in Work entsteht immer neu aus dem aktuellen, öffentlich
    zulässigen Stand. Wird eine Beratung später nicht-öffentlich, bleibt hier nichts davon lesbar.
    """

    KIND_CHOICES = [
        ("submitted", "Eingereicht"),
        ("status", "Antragsstatus"),
        ("paper", "Vorlagennummer"),
        ("scheduled", "Beratung terminiert"),
        ("result", "Beratungsergebnis"),
        ("decision", "Beschluss"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    motion = models.ForeignKey(
        Motion,
        on_delete=models.CASCADE,
        related_name="administration_events",
        verbose_name="Dokument",
    )
    key = models.CharField(max_length=200, verbose_name="Schlüssel")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, verbose_name="Art")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Gemeldet am")

    class Meta:
        verbose_name = "Rückmeldung der Verwaltung"
        verbose_name_plural = "Rückmeldungen der Verwaltung"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["motion", "key"], name="uniq_motion_administration_event"),
        ]

    def __str__(self):
        return f"{self.motion_id}: {self.key}"


def content_fingerprint(content: str | None) -> str:
    """
    Kurzer Fingerabdruck des gespeicherten Inhalts für die Konflikterkennung (#184).

    Der Editor bekommt ihn beim Laden und nach jedem Speichern; beim Speichern ohne
    Kollaborationsverbindung schickt er ihn zurück. Weicht er vom aktuellen Stand ab,
    hat jemand anderes inzwischen gespeichert, und der Server überschreibt nicht still.
    """
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]
