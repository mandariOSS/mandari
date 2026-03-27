# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation models for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen:
1. Position/Beschluss (org-weit, ein Beschluss pro TOP)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar mit Organisation)
4. Fraktionsdiskussion (org-weit, Kommentare)
5. Dokumente (org-weit, Links + Uploads + OParl-Referenzen)
"""

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class MeetingPreparation(EncryptionMixin, models.Model):
    """
    Org-weite Vorbereitung einer OParl-Sitzung.

    EINE Preparation pro Organisation + Sitzung (nicht pro User).
    Alle Mitglieder der Organisation sehen denselben Stand.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="meeting_preparations",
        verbose_name="Organisation",
    )
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE,
        related_name="work_preparations",
        verbose_name="Sitzung",
    )

    # Phase 1: membership bleibt nullable für Datenmigration, wird in Phase 3 entfernt
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="meeting_preparations",
        verbose_name="Mitglied",
        null=True, blank=True,
    )

    # Org-weite allgemeine Notizen zur Sitzung (verschlüsselt)
    notes_encrypted = EncryptedTextField(
        verbose_name="Allgemeine Notizen",
        help_text="Org-weite Notizen zur Sitzung",
    )

    # Status
    is_prepared = models.BooleanField(default=False, verbose_name="Vorbereitet")
    prepared_at = models.DateTimeField(blank=True, null=True, verbose_name="Vorbereitet am")
    prepared_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prepared_meetings",
        verbose_name="Vorbereitet von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sitzungsvorbereitung"
        verbose_name_plural = "Sitzungsvorbereitungen"
        # Phase 1: unique_together entfernt (wird nach Datenmigration in Phase 3 als (org, meeting) gesetzt)
        ordering = ["-meeting__start"]

    def __str__(self):
        return f"{self.organization.name} - {self.meeting}"

    def get_encryption_organization(self):
        return self.organization


# =============================================================================
# Sektion 1: Beschluss / Position (org-weit)
# =============================================================================


class AgendaItemPosition(models.Model):
    """
    Org-weite Position zu einem Tagesordnungspunkt.

    EIN Beschluss pro Organisation + TOP. Alle Mitglieder können
    die Position ändern, bis sie als endgültig markiert wird.
    """

    POSITION_CHOICES = [
        ("open", "Noch offen"),
        ("for", "Zustimmung"),
        ("against", "Ablehnung"),
        ("abstain", "Enthaltung"),
        ("defer", "Vertagen"),
        ("refer", "Überweisen"),
        ("amended", "Mit Änderungen"),
        ("info", "Zur Kenntnis"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Phase 1: preparation bleibt nullable für Datenmigration, wird in Phase 3 entfernt
    preparation = models.ForeignKey(
        MeetingPreparation,
        on_delete=models.CASCADE,
        related_name="positions",
        verbose_name="Vorbereitung",
        null=True, blank=True,
    )

    # Phase 1: nullable für Datenmigration, wird in Phase 3 NOT NULL
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="agenda_positions",
        verbose_name="Organisation",
        null=True, blank=True,
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_positions",
        verbose_name="Tagesordnungspunkt",
    )

    position = models.CharField(max_length=20, choices=POSITION_CHOICES, default="open", verbose_name="Position")
    is_final = models.BooleanField(
        default=False, verbose_name="Endgültig", help_text="Position als endgültig markieren"
    )
    set_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="set_positions",
        verbose_name="Gesetzt von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TOP-Position"
        verbose_name_plural = "TOP-Positionen"
        # Phase 1: alte unique_together bleibt, wird in Phase 3 geändert
        ordering = ["agenda_item__number"]

    def __str__(self):
        org = self.organization.name if self.organization_id else "?"
        return f"{org} - {self.agenda_item} ({self.position})"


# =============================================================================
# Sektion 2: Private Notizen (pro User)
# =============================================================================


class AgendaPrivateNote(EncryptionMixin, models.Model):
    """
    Private Notiz eines Mitglieds zu einem TOP.

    Nur der Autor kann diese Notiz sehen.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="private_agenda_notes",
        verbose_name="Organisation",
    )
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="private_agenda_notes",
        verbose_name="Autor",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_private_notes",
        verbose_name="Tagesordnungspunkt",
    )

    content_encrypted = EncryptedTextField(verbose_name="Private Notiz")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Private TOP-Notiz"
        verbose_name_plural = "Private TOP-Notizen"
        unique_together = ["author", "agenda_item"]

    def __str__(self):
        return f"Notiz von {self.author} zu {self.agenda_item}"

    def get_encryption_organization(self):
        return self.organization


# =============================================================================
# Sektion 3: Redebeitrag (pro User, teilbar)
# =============================================================================


class AgendaSpeechNote(EncryptionMixin, models.Model):
    """
    Redebeitrag eines Mitglieds zu einem TOP.

    Ein Textfeld mit Toggle: privat oder mit Fraktion geteilt.
    Geteilte Beiträge sind für alle Org-Mitglieder sichtbar.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="speech_notes",
        verbose_name="Organisation",
    )
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="speech_notes",
        verbose_name="Autor",
    )
    # Phase 1: meeting bleibt für Abwärtskompatibilität, wird in Phase 3 entfernt
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE,
        related_name="work_speech_notes",
        verbose_name="Sitzung",
        null=True, blank=True,
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_speech_notes",
        verbose_name="Tagesordnungspunkt",
    )

    # Phase 1: alte Felder behalten, werden in Phase 3 entfernt
    title = models.CharField(max_length=200, blank=True, verbose_name="Titel")
    content = models.TextField(blank=True, verbose_name="Redetext (alt)")
    estimated_duration = models.PositiveIntegerField(default=0, verbose_name="Geschätzte Dauer (Sekunden)")

    # Neues verschlüsseltes Feld
    content_encrypted = EncryptedTextField(verbose_name="Redebeitrag")

    # Share-Toggle
    is_shared = models.BooleanField(default=False, verbose_name="Mit Fraktion teilen")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Redebeitrag"
        verbose_name_plural = "Redebeiträge"
        unique_together = ["author", "agenda_item"]
        ordering = ["-created_at"]

    def __str__(self):
        shared = " (geteilt)" if self.is_shared else ""
        return f"Rede: {self.author} zu {self.agenda_item}{shared}"

    def get_encryption_organization(self):
        return self.organization


# =============================================================================
# Sektion 4: Fraktionsdiskussion (org-weit)
# =============================================================================


class AgendaItemNote(EncryptionMixin, models.Model):
    """
    Org-weite Diskussionsnotiz zu einem TOP.

    Alle Mitglieder der Organisation sehen und können beitragen.
    Kann als Beschluss oder wichtig markiert werden.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="agenda_notes",
        verbose_name="Organisation",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_notes",
        verbose_name="Tagesordnungspunkt",
    )

    # Phase 1: visibility bleibt für Datenmigration, wird in Phase 3 entfernt
    VISIBILITY_CHOICES = [
        ("organization", "Meine Organisation"),
    ]
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="organization",
        verbose_name="Sichtbarkeit",
    )

    # Inhalt (verschlüsselt)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Autor
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="authored_notes",
        verbose_name="Autor",
    )

    # Markierungen
    is_decision = models.BooleanField(
        default=False,
        verbose_name="Als Beschluss markiert",
        help_text="Markiert diese Notiz als offiziellen Fraktionsbeschluss",
    )
    is_pinned = models.BooleanField(default=False, verbose_name="Angeheftet")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TOP-Diskussionsnotiz"
        verbose_name_plural = "TOP-Diskussionsnotizen"
        ordering = ["-is_pinned", "-is_decision", "-created_at"]

    def __str__(self):
        return f"Notiz zu {self.agenda_item}"

    @property
    def content(self):
        """Get decrypted content for templates."""
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization


# =============================================================================
# Sektion 5: Dokumente (org-weit)
# =============================================================================


class AgendaSupplementaryDocument(models.Model):
    """
    Ergänzendes Dokument zu einem TOP (org-weit).

    Unterstützt drei Typen:
    - link: Externer URL-Link
    - file: Hochgeladene Datei (PDF, DOCX etc.)
    - oparl: Referenz auf ein OParl-Dokument aus dem RIS
    """

    DOCUMENT_TYPE_CHOICES = [
        ("link", "Externer Link"),
        ("file", "Datei-Upload"),
        ("oparl", "OParl-Dokument"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="supplementary_documents",
        verbose_name="Organisation",
    )
    added_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="added_documents",
        verbose_name="Hinzugefügt von",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_supplementary_documents",
        verbose_name="Tagesordnungspunkt",
    )

    # Typ
    document_type = models.CharField(
        max_length=10,
        choices=DOCUMENT_TYPE_CHOICES,
        default="link",
        verbose_name="Typ",
    )

    # Gemeinsame Felder
    title = models.CharField(max_length=255, verbose_name="Titel")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Für Links
    url = models.URLField(blank=True, verbose_name="URL")

    # Für Datei-Uploads
    file = models.FileField(upload_to="meetings/documents/%Y/%m/", blank=True, verbose_name="Datei")
    filename = models.CharField(max_length=255, blank=True, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(default=0, verbose_name="Dateigröße (Bytes)")

    # Für OParl-Referenzen
    oparl_file = models.ForeignKey(
        "insight_core.OParlFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_supplementary_refs",
        verbose_name="OParl-Datei",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ergänzendes Dokument"
        verbose_name_plural = "Ergänzende Dokumente"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_document_type_display()})"

    @property
    def display_url(self):
        """Gibt die anzuzeigende URL zurück, je nach Typ."""
        if self.document_type == "link":
            return self.url
        elif self.document_type == "file" and self.file:
            return self.file.url
        elif self.document_type == "oparl" and self.oparl_file:
            return self.oparl_file.access_url
        return ""


# =============================================================================
# Paper-Kommentare (gremienübergreifend, unverändert)
# =============================================================================


# Rückwärtskompatibilität: Alias für Views die noch den alten Namen importieren
AgendaDocumentLink = AgendaSupplementaryDocument


class PaperComment(EncryptionMixin, models.Model):
    """
    Kommentar zu einem OParl-Vorgang (Paper).

    Ermöglicht gremienübergreifende Zusammenarbeit: Kommentare sind an den
    Vorgang gebunden, nicht an einen bestimmten TOP oder eine Sitzung.

    Sichtbarkeitsstufen:
    - private: Nur der Autor
    - organization: Alle Mitglieder der eigenen Organisation
    - consulting: Alle Organisationen, die diesen Vorgang in einem ihrer Gremien beraten
    """

    VISIBILITY_CHOICES = [
        ("private", "Nur ich"),
        ("organization", "Meine Organisation"),
        ("consulting", "Alle beratenden Gremien"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    paper = models.ForeignKey(
        "insight_core.OParlPaper",
        on_delete=models.CASCADE,
        related_name="work_comments",
        verbose_name="Vorgang",
    )
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="paper_comments",
        verbose_name="Organisation",
    )
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="paper_comments",
        verbose_name="Autor",
    )

    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="organization",
        verbose_name="Sichtbarkeit",
    )

    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    is_recommendation = models.BooleanField(
        default=False,
        verbose_name="Als Empfehlung markiert",
        help_text="Markiert diesen Kommentar als offizielle Empfehlung der Organisation",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Vorgang-Kommentar"
        verbose_name_plural = "Vorgang-Kommentare"
        ordering = ["-is_recommendation", "-created_at"]

    def __str__(self):
        return f"Kommentar zu {self.paper} ({self.visibility})"

    @property
    def content(self):
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization

    def is_visible_to(self, membership) -> bool:
        if self.visibility == "private":
            return membership == self.author
        if self.visibility == "organization":
            return membership.organization == self.organization
        if self.visibility == "consulting":
            from insight_core.models import OParlMeeting

            user_committees = membership.oparl_committees.all()
            if not user_committees.exists():
                return membership.organization == self.organization

            committee_external_ids = set(c.external_id for c in user_committees if c.external_id)
            consultations = self.paper.consultations.all()
            meeting_external_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]

            if meeting_external_ids:
                meetings = OParlMeeting.objects.filter(external_id__in=meeting_external_ids).prefetch_related(
                    "organizations"
                )
                for meeting in meetings:
                    for org in meeting.organizations.all():
                        if org.external_id in committee_external_ids:
                            return True

            return membership.organization == self.organization
        return False

    @classmethod
    def get_visible_comments_for_paper(cls, paper, membership):
        all_comments = cls.objects.filter(paper=paper).select_related("author", "author__user", "organization")
        return [c for c in all_comments if c.is_visible_to(membership)]
