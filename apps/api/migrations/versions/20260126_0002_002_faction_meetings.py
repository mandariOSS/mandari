"""Add faction meeting tables.

Revision ID: 002
Revises: 001
Create Date: 2026-01-26

This migration creates all tables for faction meetings:
- faction_meeting_settings (organization-specific settings)
- faction_meetings (meetings with chaining for protocol approval)
- faction_agenda_items (agenda items with hierarchy and suggestion workflow)
- faction_decisions (decisions/resolutions with voting results)
- faction_attendances (attendance tracking with check-in/out)
- faction_protocol_entries (protocol entries of various types)
- faction_protocol_revisions (revision-safe protocol snapshots)
- faction_meeting_invitations (invitations with RSVP tracking)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum types for faction meetings
    op.execute(
        """
        CREATE TYPE meeting_status AS ENUM (
            'draft', 'scheduled', 'in_progress', 'completed', 'protocol_approved', 'cancelled'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE agenda_item_visibility AS ENUM (
            'public', 'internal', 'confidential'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE agenda_suggestion_status AS ENUM (
            'pending', 'approved', 'rejected'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE attendance_type AS ENUM (
            'in_person', 'online', 'guest'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE attendance_status AS ENUM (
            'present', 'absent', 'excused', 'late', 'left_early'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE protocol_entry_type AS ENUM (
            'discussion', 'decision', 'information', 'action_item', 'note'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE decision_type AS ENUM (
            'approved_unanimous', 'approved_majority', 'rejected_unanimous',
            'rejected_majority', 'postponed', 'withdrawn', 'noted', 'referred'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE rsvp_status AS ENUM (
            'pending', 'accepted', 'declined', 'tentative'
        )
        """
    )

    # Create faction_meeting_settings table
    op.create_table(
        "faction_meeting_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
            unique=True,
        ),
        # Defaults
        sa.Column("default_location", sa.String(500), nullable=True),
        sa.Column("default_conference_link", sa.String(500), nullable=True),
        sa.Column("default_start_time", sa.String(5), nullable=True),
        sa.Column(
            "default_duration_minutes", sa.Integer(), nullable=False, server_default="120"
        ),
        # Standard agenda items
        sa.Column(
            "auto_create_approval_item", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "auto_create_various_item", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "political_work_section_title",
            sa.String(200),
            nullable=False,
            server_default="Politische Arbeit: Vorlagen und Anträge",
        ),
        sa.Column(
            "press_section_title",
            sa.String(200),
            nullable=False,
            server_default="SoMe & Presse",
        ),
        # Email settings
        sa.Column("invitation_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("invitation_days_before", sa.Integer(), nullable=False, server_default="7"),
        sa.Column(
            "reminder_1_day_enabled", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "protocol_notification_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column("email_templates", postgresql.JSONB(), nullable=False, server_default="{}"),
        # Protocol settings
        sa.Column(
            "require_active_checkout", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "attendance_timeout_minutes", sa.Integer(), nullable=False, server_default="120"
        ),
        sa.Column(
            "auto_lock_protocol_on_end", sa.Boolean(), nullable=False, server_default="true"
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Create faction_meetings table
    op.create_table(
        "faction_meetings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("public_id", sa.String(12), nullable=False, unique=True, index=True),
        # Basic info
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Scheduling
        sa.Column("scheduled_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end", sa.DateTime(timezone=True), nullable=True),
        # Location
        sa.Column("location", sa.String(500), nullable=True),
        sa.Column("conference_link", sa.String(500), nullable=True),
        sa.Column("conference_details", sa.Text(), nullable=True),
        # Status
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "scheduled",
                "in_progress",
                "completed",
                "protocol_approved",
                "cancelled",
                name="meeting_status",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        # Meeting chaining
        sa.Column(
            "previous_meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_meetings.id"),
            nullable=True,
        ),
        # Protocol workflow
        sa.Column("protocol_approved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("protocol_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "protocol_approved_in_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_meetings.id"),
            nullable=True,
        ),
        # Protocol locking
        sa.Column("protocol_locked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("protocol_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "protocol_locked_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        # Attendance locking
        sa.Column("attendance_locked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("attendance_locked_at", sa.DateTime(timezone=True), nullable=True),
        # Public protocol
        sa.Column(
            "public_protocol_enabled", sa.Boolean(), nullable=False, server_default="false"
        ),
        # Metadata
        sa.Column(
            "created_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Create faction_agenda_items table
    op.create_table(
        "faction_agenda_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_meetings.id"),
            nullable=False,
        ),
        # Hierarchy
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_agenda_items.id"),
            nullable=True,
        ),
        # Basic info
        sa.Column("number", sa.String(20), nullable=False, server_default=""),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Visibility and ordering
        sa.Column(
            "visibility",
            postgresql.ENUM(
                "public",
                "internal",
                "confidential",
                name="agenda_item_visibility",
                create_type=False,
            ),
            nullable=False,
            server_default="internal",
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_public_section", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_approval_item", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_various_item", sa.Boolean(), nullable=False, server_default="false"),
        # Time estimate
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=True),
        # Suggestion workflow
        sa.Column(
            "suggestion_status",
            postgresql.ENUM(
                "pending",
                "approved",
                "rejected",
                name="agenda_suggestion_status",
                create_type=False,
            ),
            nullable=False,
            server_default="approved",
        ),
        sa.Column(
            "suggested_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        sa.Column(
            "approved_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        # Links
        sa.Column(
            "related_paper_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_papers.id"),
            nullable=True,
        ),
        sa.Column(
            "related_motion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("motions.id"),
            nullable=True,
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Create faction_decisions table
    op.create_table(
        "faction_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agenda_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_agenda_items.id"),
            nullable=False,
        ),
        # Decision details
        sa.Column(
            "decision_type",
            postgresql.ENUM(
                "approved_unanimous",
                "approved_majority",
                "rejected_unanimous",
                "rejected_majority",
                "postponed",
                "withdrawn",
                "noted",
                "referred",
                name="decision_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("decision_text", sa.Text(), nullable=False),
        # Voting results
        sa.Column("votes_for", sa.Integer(), nullable=True),
        sa.Column("votes_against", sa.Integer(), nullable=True),
        sa.Column("votes_abstain", sa.Integer(), nullable=True),
        sa.Column("is_unanimous", sa.Boolean(), nullable=False, server_default="false"),
        # Link to motion
        sa.Column(
            "motion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("motions.id"),
            nullable=True,
        ),
        # Metadata
        sa.Column(
            "created_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Create faction_attendances table
    op.create_table(
        "faction_attendances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_meetings.id"),
            nullable=False,
        ),
        # Member or guest
        sa.Column(
            "membership_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        sa.Column("guest_name", sa.String(200), nullable=True),
        sa.Column("is_guest", sa.Boolean(), nullable=False, server_default="false"),
        # Attendance details
        sa.Column(
            "attendance_type",
            postgresql.ENUM(
                "in_person",
                "online",
                "guest",
                name="attendance_type",
                create_type=False,
            ),
            nullable=False,
            server_default="in_person",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "present",
                "absent",
                "excused",
                "late",
                "left_early",
                name="attendance_status",
                create_type=False,
            ),
            nullable=False,
            server_default="present",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        # Check-in/out tracking
        sa.Column("check_in_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_checked_in", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_activity_time", sa.DateTime(timezone=True), nullable=True),
        # Calculated duration
        sa.Column("calculated_duration_minutes", sa.Integer(), nullable=True),
        # Digital signature
        sa.Column("signature_hash", sa.String(64), nullable=True),
        sa.Column("signature_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signature_ip_address", sa.String(45), nullable=True),
        # Manual adjustment
        sa.Column("manually_adjusted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("adjustment_reason", sa.Text(), nullable=True),
        sa.Column(
            "adjusted_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        sa.Column("adjusted_at", sa.DateTime(timezone=True), nullable=True),
        # Metadata
        sa.Column(
            "recorded_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Create faction_protocol_entries table
    op.create_table(
        "faction_protocol_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agenda_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_agenda_items.id"),
            nullable=False,
        ),
        # Entry details
        sa.Column(
            "entry_type",
            postgresql.ENUM(
                "discussion",
                "decision",
                "information",
                "action_item",
                "note",
                name="protocol_entry_type",
                create_type=False,
            ),
            nullable=False,
            server_default="discussion",
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        # For action items
        sa.Column(
            "assigned_to_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=True,
        ),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_completed", sa.Boolean(), nullable=False, server_default="false"),
        # Visibility override
        sa.Column(
            "visibility_override",
            postgresql.ENUM(
                "public",
                "internal",
                "confidential",
                name="agenda_item_visibility",
                create_type=False,
            ),
            nullable=True,
        ),
        # Metadata
        sa.Column(
            "created_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Create faction_protocol_revisions table
    op.create_table(
        "faction_protocol_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_meetings.id"),
            nullable=False,
        ),
        # Revision info
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False, server_default="draft"),
        # Full snapshot
        sa.Column("content_snapshot", postgresql.JSONB(), nullable=False),
        # Hash for integrity
        sa.Column("content_hash", sa.String(64), nullable=False),
        # Notes
        sa.Column("notes", sa.Text(), nullable=True),
        # Metadata
        sa.Column(
            "created_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("meeting_id", "revision_number", name="uq_meeting_revision"),
    )

    # Create faction_meeting_invitations table
    op.create_table(
        "faction_meeting_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faction_meetings.id"),
            nullable=False,
        ),
        sa.Column(
            "membership_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        # RSVP status
        sa.Column(
            "rsvp_status",
            postgresql.ENUM(
                "pending",
                "accepted",
                "declined",
                "tentative",
                name="rsvp_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("rsvp_note", sa.Text(), nullable=True),
        sa.Column("rsvp_responded_at", sa.DateTime(timezone=True), nullable=True),
        # Email tracking
        sa.Column("invitation_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        # Metadata
        sa.Column(
            "invited_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memberships.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "meeting_id", "membership_id", name="uq_meeting_member_invitation"
        ),
    )

    # Create indexes for common queries
    op.create_index(
        "ix_faction_meetings_organization",
        "faction_meetings",
        ["organization_id", "scheduled_date"],
    )
    op.create_index(
        "ix_faction_meetings_status", "faction_meetings", ["organization_id", "status"]
    )
    op.create_index(
        "ix_faction_agenda_items_meeting",
        "faction_agenda_items",
        ["meeting_id", "sort_order"],
    )
    op.create_index(
        "ix_faction_attendances_meeting", "faction_attendances", ["meeting_id"]
    )
    op.create_index(
        "ix_faction_protocol_entries_agenda",
        "faction_protocol_entries",
        ["agenda_item_id"],
    )
    op.create_index(
        "ix_faction_invitations_meeting", "faction_meeting_invitations", ["meeting_id"]
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_faction_invitations_meeting", "faction_meeting_invitations")
    op.drop_index("ix_faction_protocol_entries_agenda", "faction_protocol_entries")
    op.drop_index("ix_faction_attendances_meeting", "faction_attendances")
    op.drop_index("ix_faction_agenda_items_meeting", "faction_agenda_items")
    op.drop_index("ix_faction_meetings_status", "faction_meetings")
    op.drop_index("ix_faction_meetings_organization", "faction_meetings")

    # Drop tables in reverse order
    op.drop_table("faction_meeting_invitations")
    op.drop_table("faction_protocol_revisions")
    op.drop_table("faction_protocol_entries")
    op.drop_table("faction_attendances")
    op.drop_table("faction_decisions")
    op.drop_table("faction_agenda_items")
    op.drop_table("faction_meetings")
    op.drop_table("faction_meeting_settings")

    # Drop enum types
    op.execute("DROP TYPE rsvp_status")
    op.execute("DROP TYPE decision_type")
    op.execute("DROP TYPE protocol_entry_type")
    op.execute("DROP TYPE attendance_status")
    op.execute("DROP TYPE attendance_type")
    op.execute("DROP TYPE agenda_suggestion_status")
    op.execute("DROP TYPE agenda_item_visibility")
    op.execute("DROP TYPE meeting_status")
