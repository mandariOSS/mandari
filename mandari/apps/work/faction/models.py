# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting models for the Work module.

Internal meetings for political organizations with:
- Recurring schedules
- Agenda management
- Attendance tracking
- Protocol/minutes with approval workflow
"""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class FactionMeetingSchedule(models.Model):
    """
    Recurring meeting schedule.

    Defines when faction meetings happen regularly
    (e.g., every Monday at 18:00).
    """

    RECURRENCE_CHOICES = [
        ("weekly", "Wöchentlich"),
        ("biweekly", "Alle 2 Wochen"),
        ("monthly", "Monatlich"),
        ("monthly_first", "Jeden 1. im Monat"),
        ("monthly_last", "Jeden letzten im Monat"),
    ]

    WEEKDAY_CHOICES = [
        (0, "Montag"),
        (1, "Dienstag"),
        (2, "Mittwoch"),
        (3, "Donnerstag"),
        (4, "Freitag"),
        (5, "Samstag"),
        (6, "Sonntag"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="meeting_schedules",
        verbose_name="Organisation",
    )

    name = models.CharField(max_length=200, verbose_name="Name", help_text="z.B. 'Wöchentliche Fraktionssitzung'")

    # Timing
    recurrence = models.CharField(
        max_length=20, choices=RECURRENCE_CHOICES, default="weekly", verbose_name="Wiederholung"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES, verbose_name="Wochentag")
    time = models.TimeField(verbose_name="Uhrzeit")
    duration_minutes = models.PositiveIntegerField(default=120, verbose_name="Dauer (Minuten)")

    # Location defaults
    default_location = models.CharField(max_length=500, blank=True, verbose_name="Standard-Ort")
    default_video_link = models.URLField(blank=True, verbose_name="Standard-Video-Link")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Sitzungsplan"
        verbose_name_plural = "Sitzungspläne"
        ordering = ["weekday", "time"]

    def __str__(self):
        return f"{self.name} ({self.get_weekday_display()}, {self.time})"


class FactionMeetingException(models.Model):
    """
    Exception to a meeting schedule.

    Used for cancellations, postponements, or special dates.
    """

    EXCEPTION_TYPE_CHOICES = [
        ("cancelled", "Abgesagt"),
        ("rescheduled", "Verschoben"),
        ("special", "Sondertermin"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    schedule = models.ForeignKey(
        FactionMeetingSchedule,
        on_delete=models.CASCADE,
        related_name="exceptions",
        verbose_name="Sitzungsplan",
    )

    # The date that is affected
    original_date = models.DateField(verbose_name="Ursprüngliches Datum")

    # Optionales Enddatum (Issue #61): Urlaubs-/Ausfallzeitraum von
    # original_date bis einschließlich end_date — alle Termine der Reihe
    # in diesem Zeitraum entfallen ersatzlos
    end_date = models.DateField(
        blank=True,
        null=True,
        verbose_name="Enddatum",
        help_text="Optional: Zeitraum bis einschließlich dieses Datums (z.B. Urlaub)",
    )

    exception_type = models.CharField(max_length=20, choices=EXCEPTION_TYPE_CHOICES, verbose_name="Art")
    reason = models.CharField(max_length=500, blank=True, verbose_name="Grund")

    # For rescheduled meetings
    new_date = models.DateField(blank=True, null=True, verbose_name="Neues Datum")
    new_time = models.TimeField(blank=True, null=True, verbose_name="Neue Uhrzeit")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ausnahme"
        verbose_name_plural = "Ausnahmen"
        unique_together = ["schedule", "original_date"]
        ordering = ["-original_date"]

    def __str__(self):
        return f"{self.schedule.name} - {self.original_date} ({self.exception_type})"

    def covers(self, date) -> bool:
        """Fällt das Datum in den Ausnahmezeitraum?"""
        if self.end_date:
            return self.original_date <= date <= self.end_date
        return self.original_date == date


class FactionSuspensionRule(models.Model):
    """
    RIS-Ausfallregel für eine Sitzungsreihe (Issue #61).

    "Nach einer Sitzung von Gremium X fällt die nächste Fraktionssitzung
    aus" — z.B. nach jeder Ratssitzung. Die Gremien-Auswahl stammt aus den
    OParl-Organizations der mit der Organisation verknüpften Kommune(n).
    Ausgefallene Termine werden ersatzlos gestrichen (als "entfällt"
    sichtbar), es wird nicht verschoben.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    schedule = models.ForeignKey(
        FactionMeetingSchedule,
        on_delete=models.CASCADE,
        related_name="suspension_rules",
        verbose_name="Sitzungsplan",
    )

    ris_organization = models.ForeignKey(
        "insight_core.OParlOrganization",
        on_delete=models.CASCADE,
        related_name="faction_suspension_rules",
        verbose_name="RIS-Gremium",
        help_text="Nach einer Sitzung dieses Gremiums entfällt die nächste Fraktionssitzung",
    )

    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "RIS-Ausfallregel"
        verbose_name_plural = "RIS-Ausfallregeln"
        unique_together = ["schedule", "ris_organization"]
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.schedule.name}: nach {self.ris_organization.name}"


class FactionMeeting(EncryptionMixin, models.Model):
    """
    Internal faction/organization meeting.

    Separate from public OParl meetings - these are internal.
    """

    STATUS_CHOICES = [
        ("draft", "Entwurf"),
        ("planned", "Geplant"),
        ("invited", "Eingeladen"),
        ("ongoing", "Läuft"),
        ("completed", "Abgeschlossen"),
        ("cancelled", "Abgesagt"),
    ]

    PROTOCOL_STATUS_CHOICES = [
        ("draft", "Entwurf"),
        ("pending", "Zur Genehmigung"),
        ("approved", "Genehmigt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="faction_meetings",
        verbose_name="Organisation",
    )

    # Basic info
    title = models.CharField(max_length=200, verbose_name="Titel")
    description = models.TextField(blank=True, verbose_name="Beschreibung")
    meeting_number = models.PositiveIntegerField(
        default=0, verbose_name="Sitzungsnummer", help_text="Fortlaufende Nummer der Sitzung"
    )

    # Link to previous meeting (for protocol approval workflow)
    previous_meeting = models.OneToOneField(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_meeting",
        verbose_name="Vorherige Sitzung",
        help_text="Die vorherige Fraktionssitzung (für Protokollgenehmigung)",
    )

    # Schedule (optional link)
    schedule = models.ForeignKey(
        FactionMeetingSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meetings",
        verbose_name="Sitzungsplan",
    )

    # Erzeugung aus der Sitzungsreihe (Issue #61): Solltermin des Plans —
    # macht die rollierende Erzeugung idempotent (ein Termin je Plan+Datum)
    scheduled_date = models.DateField(
        blank=True,
        null=True,
        verbose_name="Plantermin",
        help_text="Solltermin laut Sitzungsreihe (für automatisch erzeugte Sitzungen)",
    )

    # Ausfallgrund (Issue #61): warum der Termin ersatzlos entfällt
    cancellation_reason = models.CharField(max_length=300, blank=True, verbose_name="Ausfallgrund")

    # Timing
    start = models.DateTimeField(verbose_name="Beginn")
    end = models.DateTimeField(blank=True, null=True, verbose_name="Ende")

    # Location
    location = models.CharField(max_length=500, blank=True, verbose_name="Ort")
    is_virtual = models.BooleanField(default=False, verbose_name="Online-Sitzung")
    video_link = models.URLField(blank=True, verbose_name="Video-Link")

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft", verbose_name="Status")

    # Invitations
    invitation_sent = models.BooleanField(default=False, verbose_name="Einladung versendet")
    invitation_sent_at = models.DateTimeField(blank=True, null=True, verbose_name="Einladung versendet am")
    invitation_sequence = models.PositiveIntegerField(
        default=0,
        verbose_name="Einladungs-Sequenz",
        help_text="ICS-SEQUENCE: wird bei jeder Aktualisierung/Nachladung erhöht",
    )
    invitation_updated_at = models.DateTimeField(
        blank=True, null=True, verbose_name="Aktualisierte Einladung versendet am"
    )
    reminder_sent_at = models.DateTimeField(blank=True, null=True, verbose_name="Erinnerung versendet am")

    # Einladungslogik je Organisation (Issue #62): Im Freigabe-Modus wird der
    # Versand erst nach ausdrücklicher Freigabe durch Vorstand/Vorsitz
    # ausgelöst. Stellv. Vorsitz darf ohne formale Delegation direkt
    # freigeben — auditiert wird schlicht, WER es war.
    invitation_released_at = models.DateTimeField(
        blank=True, null=True, verbose_name="Einladungsversand freigegeben am"
    )
    invitation_released_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="released_faction_invitations",
        verbose_name="Freigegeben von",
    )
    # Freigabe-Hinweise an Vorstand/Vorsitz (Standard: 24 h und 3 h vor dem
    # geplanten Versandzeitpunkt) — je Sitzung höchstens einmal
    release_notice_first_sent_at = models.DateTimeField(
        blank=True, null=True, verbose_name="Freigabe-Hinweis (24 h) versandt am"
    )
    release_notice_final_sent_at = models.DateTimeField(
        blank=True, null=True, verbose_name="Freigabe-Hinweis (3 h) versandt am"
    )

    # Teilnahme-Workflow (Issue #67): Nach der Sitzung bestätigt der
    # Vorstand (Vorsitz/stellv. Vorsitz) die Teilnahmen final — mit
    # Zeitstempel und auditiertem Bestätiger. Bestätigte Teilnahmen sind
    # die Grundlage für den Teilnahmenachweis (Issue #68).
    attendance_confirmed_at = models.DateTimeField(blank=True, null=True, verbose_name="Teilnahmen bestätigt am")
    attendance_confirmed_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_confirmed_meetings",
        verbose_name="Teilnahmen bestätigt von",
    )

    # Protocol (encrypted)
    protocol_encrypted = EncryptedTextField(verbose_name="Protokoll")
    protocol_status = models.CharField(
        max_length=20,
        choices=PROTOCOL_STATUS_CHOICES,
        default="draft",
        verbose_name="Protokollstatus",
    )
    protocol_approved = models.BooleanField(default=False, verbose_name="Protokoll genehmigt")
    protocol_approved_at = models.DateTimeField(blank=True, null=True)
    protocol_approved_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_protocols",
        verbose_name="Genehmigt von",
    )
    protocol_approved_in = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_previous_protocols",
        verbose_name="Genehmigt in Sitzung",
        help_text="Die Sitzung, in der dieses Protokoll genehmigt wurde",
    )

    # Link to public meeting if preparing for one
    related_meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="faction_preparations",
        verbose_name="Vorbereitete Sitzung",
        help_text="Öffentliche Sitzung die in dieser Fraktionssitzung vorbereitet wird",
    )

    # Metadata — null bei automatisch aus der Sitzungsreihe erzeugten
    # Sitzungen (Issue #61)
    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="created_faction_meetings",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fraktionssitzung"
        verbose_name_plural = "Fraktionssitzungen"
        ordering = ["-start"]
        indexes = [
            models.Index(fields=["organization", "start"]),
            models.Index(fields=["organization", "status"]),
        ]
        constraints = [
            # Idempotente Erzeugung aus der Sitzungsreihe (Issue #61)
            models.UniqueConstraint(
                fields=["schedule", "scheduled_date"],
                condition=models.Q(scheduled_date__isnull=False),
                name="uniq_faction_meeting_schedule_scheduled_date",
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.start.date()})"

    def get_encryption_organization(self):
        return self.organization

    @property
    def is_upcoming(self) -> bool:
        return self.start > timezone.now()

    @property
    def is_past(self) -> bool:
        return self.end and self.end < timezone.now()

    @property
    def has_pending_protocol_approval(self) -> bool:
        """Check if the previous meeting's protocol needs approval."""
        if not self.previous_meeting:
            return False
        return not self.previous_meeting.protocol_approved

    def get_faction_settings(self) -> dict:
        """Get faction-specific settings from organization."""
        settings = self.organization.settings or {}
        return settings.get("faction", {})

    def create_approval_agenda_item(self) -> "FactionAgendaItem":
        """
        Create the first agenda item for TO/protocol approval.

        Uses organization settings for custom title template with placeholders:
        - {datum_letzte_sitzung} - Date of previous meeting
        - {titel_letzte_sitzung} - Title of previous meeting
        - {nr} - Meeting number
        - {datum} - Date of current meeting
        """
        settings = self.get_faction_settings()

        # Default-Texte (Issue #63): verbindliche Standard-Formulierung,
        # je Organisation weiterhin anpassbar
        default_title_with_prev = "Tagesordnung festlegen und letztes Protokoll genehmigen"
        default_title_no_prev = "Tagesordnung festlegen"

        # Get custom template or use default (leere Vorlagen fallen auf den
        # Default zurück — Issue #63)
        if self.previous_meeting:
            title_template = settings.get("first_agenda_title_with_previous") or default_title_with_prev
        else:
            title_template = settings.get("first_agenda_title_no_previous") or default_title_no_prev

        # Replace placeholders
        title = self._replace_placeholders(title_template)

        # Get description template
        description_template = settings.get("first_agenda_description", "")
        description = self._replace_placeholders(description_template)

        # Check if approval item already exists
        existing = self.agenda_items.filter(is_approval_item=True).first()
        if existing:
            # Update existing
            existing.title = title
            existing.set_description_encrypted(description)
            existing.approves_meeting = self.previous_meeting
            existing.save()
            return existing

        # Create new approval item - can't use set_description_encrypted during create
        # because get_encryption_organization() needs the meeting relationship
        item = FactionAgendaItem(
            meeting=self,
            number="1",
            title=title,
            visibility="public",
            is_approval_item=True,
            approves_meeting=self.previous_meeting,
            order=0,  # Always first
        )
        # Now we can encrypt since the meeting relationship is set
        if description:
            item.set_description_encrypted(description)
        item.save()
        return item

    def _replace_placeholders(self, template: str) -> str:
        """Replace placeholders in a template string."""
        if not template:
            return ""

        replacements = {
            "{nr}": str(self.meeting_number) if self.meeting_number else "",
            "{datum}": self.start.strftime("%d.%m.%Y") if self.start else "",
            "{titel}": self.title or "",
        }

        if self.previous_meeting:
            prev = self.previous_meeting
            replacements.update(
                {
                    "{datum_letzte_sitzung}": prev.start.strftime("%d.%m.%Y") if prev.start else "",
                    "{titel_letzte_sitzung}": prev.title or "",
                    "{nr_letzte_sitzung}": str(prev.meeting_number) if prev.meeting_number else "",
                }
            )
        else:
            # Remove placeholders referencing previous meeting
            replacements.update(
                {
                    "{datum_letzte_sitzung}": "",
                    "{titel_letzte_sitzung}": "",
                    "{nr_letzte_sitzung}": "",
                }
            )

        result = template
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)

        return result

    def approve_previous_protocol(self, approved_by: "apps.tenants.models.Membership"):  # noqa: F821
        """
        Approve the previous meeting's protocol.

        Called when the approval agenda item is passed.
        """
        if not self.previous_meeting:
            return False

        prev = self.previous_meeting
        if prev.protocol_approved:
            return False  # Already approved

        prev.protocol_status = "approved"
        prev.protocol_approved = True
        prev.protocol_approved_at = timezone.now()
        prev.protocol_approved_by = approved_by
        prev.protocol_approved_in = self
        prev.save(
            update_fields=[
                "protocol_status",
                "protocol_approved",
                "protocol_approved_at",
                "protocol_approved_by",
                "protocol_approved_in",
            ]
        )

        return True

    def submit_protocol_for_approval(self):
        """Submit the protocol for approval in the next meeting."""
        if self.protocol_status == "approved":
            return False  # Already approved
        self.protocol_status = "pending"
        self.save(update_fields=["protocol_status"])
        return True

    @classmethod
    def get_next_meeting_number(cls, organization) -> int:
        """Get the next meeting number for an organization."""
        last = cls.objects.filter(organization=organization).order_by("-meeting_number").first()

        if last and last.meeting_number:
            return last.meeting_number + 1
        return 1

    @classmethod
    def find_previous_meeting(cls, organization, before_date=None):
        """Find the most recent completed meeting before a given date."""
        qs = cls.objects.filter(
            organization=organization,
            status__in=["completed", "ongoing"],
        )
        if before_date:
            qs = qs.filter(start__lt=before_date)

        return qs.order_by("-start").first()


class FactionAgendaItem(EncryptionMixin, models.Model):
    """
    Agenda item for a faction meeting.

    Can be linked to a public agenda item for preparation.
    Supports hierarchy: TOP 1, TOP 1.1, TOP 1.2, etc.
    """

    VISIBILITY_CHOICES = [
        ("public", "Öffentlich"),
        ("internal", "Nicht-öffentlich"),
    ]

    # Proposal status for allowing Sachkundige Bürger*innen to propose agenda items
    PROPOSAL_STATUS_CHOICES = [
        ("active", "Aktiv"),
        ("proposed", "Vorgeschlagen"),
        ("rejected", "Abgelehnt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        FactionMeeting,
        on_delete=models.CASCADE,
        related_name="agenda_items",
        verbose_name="Sitzung",
    )

    # Hierarchy - parent for sub-items (TOP 1.1, 1.2, etc.)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordneter TOP",
        help_text="Für Unterpunkte wie TOP 1.1, 1.2",
    )

    # Item info
    number = models.CharField(max_length=20, verbose_name="TOP-Nr.", blank=True)
    title = models.CharField(max_length=500, verbose_name="Titel")

    # Visibility - public or internal (non-public)
    visibility = models.CharField(
        max_length=20, choices=VISIBILITY_CHOICES, default="public", verbose_name="Sichtbarkeit"
    )
    description_encrypted = EncryptedTextField(verbose_name="Beschreibung")

    # Special approval item for protocol/agenda approval workflow
    is_approval_item = models.BooleanField(
        default=False,
        verbose_name="Genehmigungs-TOP",
        help_text="Automatisch erstellter TOP für Protokoll-/TO-Genehmigung",
    )
    approves_meeting = models.ForeignKey(
        FactionMeeting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approval_agenda_items",
        verbose_name="Genehmigt Sitzung",
        help_text="Die vorherige Sitzung deren Protokoll hier genehmigt wird",
    )

    # Link to public agenda item
    related_agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="faction_items",
        verbose_name="Öffentlicher TOP",
    )

    # Decision (encrypted)
    decision_encrypted = EncryptedTextField(verbose_name="Beschluss")
    has_decision = models.BooleanField(default=False, verbose_name="Beschluss gefasst")

    # Voting result
    votes_for = models.PositiveIntegerField(default=0, verbose_name="Ja-Stimmen")
    votes_against = models.PositiveIntegerField(default=0, verbose_name="Nein-Stimmen")
    votes_abstain = models.PositiveIntegerField(default=0, verbose_name="Enthaltungen")

    # Linked motions (DMS)
    related_motions = models.ManyToManyField(
        "work.Motion",
        blank=True,
        related_name="linked_agenda_items",
        verbose_name="Verknüpfte Anträge",
    )

    # Linked OParl Papers (RIS-Vorlagen)
    related_papers = models.ManyToManyField(
        "insight_core.OParlPaper",
        blank=True,
        related_name="linked_faction_items",
        verbose_name="Verknüpfte RIS-Vorlagen",
    )

    # Reference links (internal/external documents)
    reference_links = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Referenz-Links",
        help_text="Links zu internen oder externen Dokumenten [{label, url}]",
    )

    # Ordering
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")

    # Proposal system (for Sachkundige Bürger*innen)
    proposal_status = models.CharField(
        max_length=20,
        choices=PROPOSAL_STATUS_CHOICES,
        default="active",
        verbose_name="Vorschlagsstatus",
        help_text="Für TOPs die von Sachkundigen Bürger*innen vorgeschlagen wurden",
    )
    proposed_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_agenda_items",
        verbose_name="Vorgeschlagen von",
    )
    proposed_at = models.DateTimeField(null=True, blank=True, verbose_name="Vorgeschlagen am")
    reviewed_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_agenda_items",
        verbose_name="Geprüft von",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Geprüft am")
    rejection_reason = models.TextField(blank=True, verbose_name="Ablehnungsgrund")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fraktions-TOP"
        verbose_name_plural = "Fraktions-TOPs"
        ordering = ["order", "number"]
        indexes = [
            models.Index(fields=["meeting", "proposal_status"]),
        ]

    def __str__(self):
        return f"{self.number}: {self.title}"

    def get_encryption_organization(self):
        return self.meeting.organization

    def accept_proposal(self, reviewed_by):
        """Accept a proposed agenda item."""
        if self.proposal_status != "proposed":
            return False
        self.proposal_status = "active"
        self.reviewed_by = reviewed_by
        self.reviewed_at = timezone.now()
        self.save(update_fields=["proposal_status", "reviewed_by", "reviewed_at"])
        return True

    def reject_proposal(self, reviewed_by, reason=""):
        """Reject a proposed agenda item."""
        if self.proposal_status != "proposed":
            return False
        self.proposal_status = "rejected"
        self.reviewed_by = reviewed_by
        self.reviewed_at = timezone.now()
        self.rejection_reason = reason
        self.save(update_fields=["proposal_status", "reviewed_by", "reviewed_at", "rejection_reason"])
        return True

    @property
    def is_proposal(self) -> bool:
        """Check if this is a proposed item (not yet active)."""
        return self.proposal_status == "proposed"

    @property
    def is_rejected(self) -> bool:
        """Check if this proposal was rejected."""
        return self.proposal_status == "rejected"


class FactionAttendance(models.Model):
    """
    Attendance tracking for faction meetings.
    """

    STATUS_CHOICES = [
        ("invited", "Eingeladen"),
        ("confirmed", "Zugesagt"),
        ("declined", "Abgesagt"),
        ("tentative", "Vielleicht"),
        ("present", "Anwesend"),
        ("absent", "Abwesend"),
        ("excused", "Entschuldigt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        FactionMeeting, on_delete=models.CASCADE, related_name="attendances", verbose_name="Sitzung"
    )
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="faction_attendances",
        verbose_name="Mitglied",
    )

    # Guest support
    is_guest = models.BooleanField(default=False, verbose_name="Ist Gast")
    guest_name = models.CharField(max_length=200, blank=True, verbose_name="Name des Gastes")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="invited", verbose_name="Status")

    # Teilnahmeart (Issue #67): "vor Ort" oder "online" — digitale
    # Teilnahme gilt als Vollteilnahme, die Unterscheidung dient
    # ausschließlich Auswertungen
    PARTICIPATION_TYPE_CHOICES = [
        ("onsite", "Vor Ort"),
        ("online", "Online"),
    ]
    participation_type = models.CharField(
        max_length=10,
        choices=PARTICIPATION_TYPE_CHOICES,
        default="onsite",
        verbose_name="Teilnahmeart",
    )

    # Finale Bestätigung durch den Vorstand (Issue #67) — Snapshot je
    # Teilnahme als Grundlage für den Teilnahmenachweis (Issue #68)
    confirmed_final_at = models.DateTimeField(blank=True, null=True, verbose_name="Final bestätigt am")
    confirmed_final_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finally_confirmed_attendances",
        verbose_name="Final bestätigt von",
    )

    # Response
    response_message = models.TextField(blank=True, verbose_name="Nachricht", help_text="Begründung bei Absage")
    responded_at = models.DateTimeField(blank=True, null=True, verbose_name="Antwort am")

    # Check-in
    checked_in_at = models.DateTimeField(blank=True, null=True, verbose_name="Eingecheckt um")
    checked_out_at = models.DateTimeField(blank=True, null=True, verbose_name="Ausgecheckt um")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Anwesenheit"
        verbose_name_plural = "Anwesenheiten"
        ordering = ["is_guest", "membership__user__last_name", "guest_name"]

    def __str__(self):
        if self.is_guest:
            return f"{self.guest_name} (Gast) @ {self.meeting.title}"
        return f"{self.membership.user.email} @ {self.meeting.title}"

    def get_display_name(self):
        """Return display name for member or guest."""
        if self.is_guest:
            return self.guest_name
        return self.membership.user.get_display_name() if self.membership else "Unbekannt"

    @property
    def duration(self) -> timedelta | None:
        """Calculate attendance duration if checked in and out."""
        if self.checked_in_at and self.checked_out_at:
            return self.checked_out_at - self.checked_in_at
        return None


class FactionProtocolEntry(EncryptionMixin, models.Model):
    """
    Protocol entry during a faction meeting.

    Captures live notes during the meeting including:
    - Speech contributions (Wortbeiträge)
    - Decisions (Beschlüsse)
    - Action items (Aufgaben)
    - General notes
    """

    ENTRY_TYPE_CHOICES = [
        ("speech", "Wortbeitrag"),
        ("decision", "Beschluss"),
        ("action", "Aufgabe"),
        ("note", "Notiz"),
        ("vote", "Abstimmung"),
        # Nachtrag (Issue #63): einziger nach endgültiger Protokoll-
        # Genehmigung noch zulässiger Eintragstyp — sichtbar gekennzeichnet,
        # das Original wird niemals verändert
        ("addendum", "Nachtrag"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        FactionMeeting,
        on_delete=models.CASCADE,
        related_name="protocol_entries",
        verbose_name="Sitzung",
    )

    agenda_item = models.ForeignKey(
        FactionAgendaItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="protocol_entries",
        verbose_name="TOP",
    )

    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES, default="note", verbose_name="Art")

    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Speaker (for speech entries)
    speaker = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="protocol_speeches",
        verbose_name="Redner",
    )

    # Action item specifics
    action_assignee = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="protocol_actions",
        verbose_name="Verantwortlich",
    )
    action_due_date = models.DateField(null=True, blank=True, verbose_name="Fällig bis")
    action_completed = models.BooleanField(default=False, verbose_name="Erledigt")

    # Ordering
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")

    # Metadata
    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="created_protocol_entries",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Protokolleintrag"
        verbose_name_plural = "Protokolleinträge"
        ordering = ["order", "created_at"]

    def __str__(self):
        content = self.get_content_decrypted()
        preview = content[:50] if content else ""
        return f"{self.get_entry_type_display()}: {preview}..."

    @property
    def audit_repr(self) -> str:
        """
        Beschreibung für die Änderungshistorie (Issue #66) — enthält NIEMALS
        den entschlüsselten Inhalt (im Gegensatz zu __str__).
        """
        label = self.get_entry_type_display()
        try:
            if self.agenda_item_id and self.agenda_item:
                return f"{label} zu TOP {self.agenda_item.number}"
        except Exception:
            pass
        return f"{label} (ohne TOP)"

    @property
    def content(self):
        """Get decrypted content for templates."""
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.meeting.organization

    # -- Endgültige Protokollsperre (Issue #63) ---------------------------

    def _protocol_locked(self) -> bool:
        """Ist das Protokoll der zugehörigen Sitzung endgültig genehmigt?"""
        if not self.meeting_id:
            return False
        return FactionMeeting.objects.filter(pk=self.meeting_id, protocol_approved=True).exists()

    def save(self, *args, **kwargs):
        """
        Endgültige Sperre (Issue #63): Nach der Genehmigung ist das
        Vorprotokoll ENDGÜLTIG gesperrt — auch für Admins. Bestehende
        Einträge sind unveränderbar; neue Einträge sind ausschließlich als
        sichtbarer Nachtrag (entry_type="addendum") zulässig.
        """
        if self._protocol_locked():
            if not self._state.adding:
                raise ValueError(
                    "Das Protokoll ist endgültig genehmigt — Originaleinträge sind unveränderbar. "
                    "Korrekturen sind nur als Nachtrag möglich."
                )
            if self.entry_type != "addendum":
                raise ValueError("Das Protokoll ist endgültig genehmigt — neue Einträge sind nur als Nachtrag möglich.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Endgültige Sperre (Issue #63): keine Löschung genehmigter Protokolleinträge."""
        if self._protocol_locked():
            raise ValueError("Das Protokoll ist endgültig genehmigt — Einträge können nicht gelöscht werden.")
        super().delete(*args, **kwargs)


class FactionDecision(models.Model):
    """
    Voting result for a faction agenda item.

    Tracks the outcome of votes taken during the meeting.
    """

    RESULT_CHOICES = [
        ("accepted", "Angenommen"),
        ("rejected", "Abgelehnt"),
        ("postponed", "Vertagt"),
        ("modified", "Geändert angenommen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    agenda_item = models.OneToOneField(
        FactionAgendaItem, on_delete=models.CASCADE, related_name="decision", verbose_name="TOP"
    )

    # Voting result
    votes_yes = models.PositiveIntegerField(default=0, verbose_name="Ja-Stimmen")
    votes_no = models.PositiveIntegerField(default=0, verbose_name="Nein-Stimmen")
    votes_abstain = models.PositiveIntegerField(default=0, verbose_name="Enthaltungen")

    result = models.CharField(max_length=20, choices=RESULT_CHOICES, verbose_name="Ergebnis")

    # Decision text (if different from agenda item)
    decision_text = models.TextField(
        blank=True, verbose_name="Beschlusstext", help_text="Nur ausfüllen wenn abweichend vom TOP"
    )

    # Notes
    notes = models.TextField(blank=True, verbose_name="Anmerkungen")

    # Metadata
    recorded_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="recorded_decisions",
        verbose_name="Erfasst von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Abstimmungsergebnis"
        verbose_name_plural = "Abstimmungsergebnisse"

    def __str__(self):
        return f"{self.agenda_item.number}: {self.get_result_display()}"

    @property
    def total_votes(self):
        return self.votes_yes + self.votes_no + self.votes_abstain

    @property
    def passed(self):
        return self.result in ["accepted", "modified"]


class FactionAuditLog(models.Model):
    """
    Änderungshistorie für Fraktionssitzungen (Issue #66).

    Revisionssichere Protokollierung aller Aktionen rund um
    Fraktionssitzungen — wer hat was wann geändert. Einträge entstehen
    automatisch über Model-Signale (apps/work/faction/audit.py) sowie
    explizit für Spezial-Ereignisse (Einladungsversand, Genehmigung,
    Teilnahme-Änderungen, automatische Erzeugung/Ausfälle).
    """

    ACTION_CHOICES = [
        ("create", "Erstellt"),
        ("update", "Geändert"),
        ("delete", "Gelöscht"),
        ("status", "Statuswechsel"),
        ("invitation_sent", "Einladung versandt"),
        ("invitation_updated", "Aktualisierte Einladung versandt"),
        ("reminder_sent", "Erinnerung versandt"),
        ("protocol_submitted", "Protokoll zur Genehmigung"),
        ("protocol_approved", "Protokoll genehmigt"),
        ("participation", "Teilnahme geändert"),
        ("proposal", "TOP vorgeschlagen"),
        ("proposal_accepted", "TOP-Vorschlag angenommen"),
        ("proposal_rejected", "TOP-Vorschlag abgelehnt"),
        ("decision", "Abstimmung erfasst"),
        ("generated", "Automatisch erzeugt"),
        ("auto_cancelled", "Automatisch entfallen"),
        ("invitation_released", "Einladungsversand freigegeben"),
        ("release_notice_sent", "Freigabe-Hinweis versandt"),
        ("addendum", "Nachtrag erfasst"),
        ("attendance_confirmed", "Teilnahmen bestätigt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="faction_audit_logs",
        verbose_name="Organisation",
    )

    # Akteur (Membership kann später gelöscht werden — Label bleibt erhalten)
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="faction_audit_entries",
        verbose_name="Mitglied",
    )
    actor_label = models.CharField(max_length=200, blank=True, verbose_name="Akteur")
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-Adresse")
    user_agent = models.TextField(blank=True, verbose_name="User-Agent")

    # Aktion
    action = models.CharField(max_length=50, choices=ACTION_CHOICES, verbose_name="Aktion")

    # Betroffenes Objekt
    model_name = models.CharField(max_length=100, verbose_name="Modell")
    object_id = models.UUIDField(verbose_name="Objekt-ID")
    object_repr = models.CharField(max_length=500, blank=True, verbose_name="Objekt-Beschreibung")

    # Zugehörige Sitzung (für Deep-Links/Filter; nullable, überlebt Löschung nicht)
    meeting_id_ref = models.UUIDField(null=True, blank=True, verbose_name="Sitzungs-ID")

    # NÖ-Kennzeichnung (Issue #64): Einträge zu nicht-öffentlichen TOPs
    # werden Nicht-Vereidigten nur maskiert angezeigt
    is_internal = models.BooleanField(default=False, verbose_name="Nicht-öffentlicher Inhalt")

    # Änderungs-Diff (verschlüsselte Felder maskiert)
    changes = models.JSONField(default=dict, blank=True, verbose_name="Änderungen")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Fraktions-Audit-Eintrag"
        verbose_name_plural = "Fraktions-Änderungshistorie"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "model_name", "object_id"]),
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["organization", "meeting_id_ref"]),
        ]

    def __str__(self):
        return f"{self.actor_label or 'System'}: {self.action} {self.model_name} {self.object_repr}"

    def save(self, *args, **kwargs):
        """Revisionssicherheit: Einträge sind nach dem Anlegen unveränderbar."""
        if not self._state.adding:
            raise ValueError("Audit-Einträge sind unveränderbar und können nicht aktualisiert werden.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Revisionssicherheit: Einträge können nicht gelöscht werden."""
        raise ValueError("Audit-Einträge sind unveränderbar und können nicht gelöscht werden.")


class FactionAgendaItemAttachment(models.Model):
    """Datei-Anhang eines Fraktions-TOPs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agenda_item = models.ForeignKey(
        FactionAgendaItem,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="TOP",
    )
    file = models.FileField(upload_to="faction/attachments/%Y/%m/", verbose_name="Datei")
    filename = models.CharField(max_length=255, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(default=0, verbose_name="Dateigröße (Bytes)")
    uploaded_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        related_name="faction_item_attachments",
        verbose_name="Hochgeladen von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "TOP-Anhang"
        verbose_name_plural = "TOP-Anhänge"
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename

    @property
    def size_human(self) -> str:
        """Menschenlesbare Dateigröße."""
        size = self.file_size
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"

    @property
    def icon_name(self) -> str:
        """Lucide Icon-Name basierend auf MIME-Typ."""
        if self.mime_type.startswith("image/"):
            return "image"
        elif self.mime_type == "application/pdf":
            return "file-text"
        elif self.mime_type.startswith("video/"):
            return "film"
        elif self.mime_type.startswith("audio/"):
            return "music"
        elif "spreadsheet" in self.mime_type or "excel" in self.mime_type:
            return "table"
        elif "presentation" in self.mime_type or "powerpoint" in self.mime_type:
            return "presentation"
        return "file"
