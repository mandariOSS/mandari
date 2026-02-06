# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation models for the Work module.

Allows members to prepare for public OParl meetings with:
- Personal notes (encrypted)
- Positions on agenda items
- Document attachments
- Collaborative notes
"""

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class MeetingPreparation(EncryptionMixin, models.Model):
    """
    Personal preparation for an OParl meeting.

    Each member can have their own preparation with notes and positions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="meeting_preparations",
        verbose_name="Organisation",
    )
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="meeting_preparations",
        verbose_name="Mitglied",
    )
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE,
        related_name="work_preparations",
        verbose_name="Sitzung",
    )

    # Personal notes (encrypted)
    notes_encrypted = EncryptedTextField(verbose_name="Notizen", help_text="Persönliche Notizen zur Sitzung")

    # Status
    is_prepared = models.BooleanField(default=False, verbose_name="Vorbereitet")
    prepared_at = models.DateTimeField(blank=True, null=True, verbose_name="Vorbereitet am")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sitzungsvorbereitung"
        verbose_name_plural = "Sitzungsvorbereitungen"
        unique_together = ["membership", "meeting"]
        ordering = ["-meeting__start"]

    def __str__(self):
        return f"{self.membership.user.email} - {self.meeting}"

    def get_encryption_organization(self):
        return self.organization


class AgendaItemPosition(EncryptionMixin, models.Model):
    """
    Member's position on an agenda item.

    Records the member's stance and reasoning for each item.
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

    preparation = models.ForeignKey(
        MeetingPreparation,
        on_delete=models.CASCADE,
        related_name="positions",
        verbose_name="Vorbereitung",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_positions",
        verbose_name="Tagesordnungspunkt",
    )

    # Position
    position = models.CharField(max_length=20, choices=POSITION_CHOICES, default="open", verbose_name="Position")

    # Private note (only user sees)
    notes_encrypted = EncryptedTextField(
        verbose_name="Private Notiz", help_text="Persönliche Notizen (nur für Sie sichtbar)"
    )

    # Discussion note (organization members see)
    discussion_note = models.TextField(
        blank=True,
        verbose_name="Diskussionsnotiz",
        help_text="Notiz für die Fraktionsdiskussion (für alle sichtbar)",
    )

    # Finalization
    is_final = models.BooleanField(
        default=False, verbose_name="Endgültig", help_text="Position als endgültig markieren"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TOP-Position"
        verbose_name_plural = "TOP-Positionen"
        unique_together = ["preparation", "agenda_item"]
        ordering = ["agenda_item__number"]

    def __str__(self):
        return f"{self.preparation.membership.user.email} - {self.agenda_item} ({self.position})"

    def get_encryption_organization(self):
        return self.preparation.organization


class AgendaItemNote(EncryptionMixin, models.Model):
    """
    Collaborative note on an agenda item.

    Can be visible to:
    - Only the author (private)
    - The whole organization
    - All party organizations
    - All regional organizations (same OParl Body)
    """

    VISIBILITY_CHOICES = [
        ("private", "Nur ich"),
        ("organization", "Meine Organisation"),
    ]

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

    # Visibility
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="organization",
        verbose_name="Sichtbarkeit",
    )

    # Content (encrypted)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Metadata
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="authored_notes",
        verbose_name="Autor",
    )
    is_decision = models.BooleanField(
        default=False,
        verbose_name="Als Beschluss markiert",
        help_text="Markiert diese Notiz als offiziellen Fraktionsbeschluss",
    )
    is_pinned = models.BooleanField(default=False, verbose_name="Angeheftet")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TOP-Notiz"
        verbose_name_plural = "TOP-Notizen"
        ordering = ["-is_pinned", "-is_decision", "-created_at"]

    def __str__(self):
        return f"Notiz zu {self.agenda_item} ({self.visibility})"

    @property
    def content(self):
        """Get decrypted content for templates."""
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization

    def is_visible_to(self, membership) -> bool:
        """Check if a membership can see this note."""
        if self.visibility == "private":
            return membership == self.author

        if self.visibility == "organization":
            return membership.organization == self.organization

        return False


class AgendaSpeechNote(EncryptionMixin, models.Model):
    """
    Speaking notes / teleprompter content for an agenda item.

    Allows members to prepare talking points that can be displayed
    in a full-screen teleprompter mode during the meeting.
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
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE,
        related_name="work_speech_notes",
        verbose_name="Sitzung",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_speech_notes",
        verbose_name="Tagesordnungspunkt",
    )

    # Content
    title = models.CharField(max_length=200, blank=True, verbose_name="Titel")
    content = models.TextField(verbose_name="Redetext", help_text="Text für den Teleprompter")
    estimated_duration = models.PositiveIntegerField(default=0, verbose_name="Geschätzte Dauer (Sekunden)")

    # Visibility
    is_shared = models.BooleanField(default=False, verbose_name="Mit Organisation teilen")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Redebeitrag"
        verbose_name_plural = "Redebeiträge"
        unique_together = ["author", "agenda_item"]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Rede: {self.title or self.agenda_item}"

    def get_encryption_organization(self):
        return self.organization


class AgendaDocumentLink(models.Model):
    """
    User-added document link for an agenda item.

    Allows members to add custom references beyond the official
    OParl documents attached to the agenda item.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="document_links",
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
        related_name="work_document_links",
        verbose_name="Tagesordnungspunkt",
    )

    # Document info
    title = models.CharField(max_length=255, verbose_name="Titel")
    url = models.URLField(verbose_name="URL")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Dokumentenlink"
        verbose_name_plural = "Dokumentenlinks"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class PaperComment(EncryptionMixin, models.Model):
    """
    Comment on an OParl Paper (Vorlage/Vorgang).

    Enables cross-committee collaboration: comments are tied to the Paper,
    not to a specific agenda item or meeting. This allows members from
    different committees consulting on the same paper to share feedback.

    Visibility levels:
    - private: Only the author can see
    - organization: All members of the author's organization
    - consulting: All organizations that have this paper in one of their committees
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

    # Visibility
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="organization",
        verbose_name="Sichtbarkeit",
    )

    # Content (encrypted)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Metadata
    is_recommendation = models.BooleanField(
        default=False,
        verbose_name="Als Empfehlung markiert",
        help_text="Markiert diesen Kommentar als offizielle Empfehlung der Organisation",
    )

    # Timestamps
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
        """Get decrypted content for templates."""
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization

    def is_visible_to(self, membership) -> bool:
        """
        Check if a membership can see this comment.

        For 'consulting' visibility, checks if the membership's organization
        has this paper in any of their committee consultations.
        """
        if self.visibility == "private":
            return membership == self.author

        if self.visibility == "organization":
            return membership.organization == self.organization

        if self.visibility == "consulting":
            # Check if the user's organization has a committee that consults on this paper
            from insight_core.models import OParlMeeting

            # Get committees the user's org is assigned to
            user_committees = membership.oparl_committees.all()
            if not user_committees.exists():
                return membership.organization == self.organization

            committee_external_ids = set(c.external_id for c in user_committees if c.external_id)

            # Check if any consultation for this paper is in one of those committees
            # Consultations link paper to meeting, meeting has organizations
            consultations = self.paper.consultations.all()
            meeting_external_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]

            if meeting_external_ids:
                # Get meetings with their organizations
                meetings = OParlMeeting.objects.filter(external_id__in=meeting_external_ids).prefetch_related(
                    "organizations"
                )

                for meeting in meetings:
                    for org in meeting.organizations.all():
                        if org.external_id in committee_external_ids:
                            return True

            # Fallback: same organization always sees it
            return membership.organization == self.organization

        return False

    @classmethod
    def get_visible_comments_for_paper(cls, paper, membership):
        """Get all comments visible to this membership for a given paper."""
        all_comments = cls.objects.filter(paper=paper).select_related("author", "author__user", "organization")
        return [c for c in all_comments if c.is_visible_to(membership)]
