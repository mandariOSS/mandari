"""Create work module models for document workflow with coalition voting.

Revision ID: 001
Revises:
Create Date: 2026-01-25

This migration creates all tables for the work module:
- User management (users, organizations, memberships)
- Motion management (motion_types, motions, approvals)
- Co-author system (motion_co_authors, motion_co_author_invites)
- Coalition consultation (council_parties, coalition_consultations)
- Sharing (motion_share_logs)
- Working groups (workgroups, workgroup_memberships)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = "000"  # Depends on OParl tables
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum types
    op.execute(
        """
        CREATE TYPE membership_role AS ENUM (
            'admin', 'chair', 'vice_chair', 'managing_director',
            'council_member', 'faction_member', 'expert_citizen', 'member', 'viewer'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE motion_status AS ENUM (
            'draft', 'internal_review', 'external_review', 'on_agenda',
            'approved', 'submitted', 'accepted', 'rejected', 'withdrawn'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE motion_visibility AS ENUM (
            'private', 'shared', 'faction', 'organization', 'public'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE approval_status AS ENUM (
            'pending', 'approved', 'rejected', 'changes_requested'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE coalition_result AS ENUM (
            'pending', 'approved', 'rejected', 'modified', 'no_response'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE co_author_invite_status AS ENUM (
            'pending', 'accepted', 'declined'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE share_method AS ENUM (
            'email', 'link', 'download'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE workgroup_role AS ENUM (
            'speaker', 'member'
        )
        """
    )

    # Create users table
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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

    # Create organizations table
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("short_name", sa.String(50), nullable=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("oparl_organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("oparl_body_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("primary_color", sa.String(7), nullable=True),
        sa.Column("secondary_color", sa.String(7), nullable=True),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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
        sa.ForeignKeyConstraint(["parent_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["oparl_organization_id"], ["oparl_organizations.id"]),
        sa.ForeignKeyConstraint(["oparl_body_id"], ["oparl_bodies.id"]),
    )

    # Create memberships table
    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "admin",
                "chair",
                "vice_chair",
                "managing_director",
                "council_member",
                "faction_member",
                "expert_citizen",
                "member",
                "viewer",
                name="membership_role",
                create_type=False,
            ),
            nullable=False,
            server_default="member",
        ),
        sa.Column("permissions", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("title", sa.String(100), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("oparl_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["oparl_person_id"], ["oparl_persons.id"]),
        sa.UniqueConstraint("user_id", "organization_id", name="uq_user_organization"),
    )

    # Create council_parties table
    op.create_table(
        "council_parties",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("short_name", sa.String(20), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("contact_name", sa.String(255), nullable=True),
        sa.Column("is_coalition_member", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("coalition_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("oparl_organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["oparl_organization_id"], ["oparl_organizations.id"]),
    )

    # Create motion_types table
    op.create_table(
        "motion_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(50), nullable=True),
        sa.Column("color", sa.String(7), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "requires_coalition_approval", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "requires_faction_decision", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("default_approvers", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("workflow_steps", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("allowed_creator_roles", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("recommend_co_author", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "recommend_co_author_roles", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("co_author_recommendation_message", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.UniqueConstraint("organization_id", "slug", name="uq_org_motion_type_slug"),
    )

    # Create workgroups table (needed before motions for FK)
    op.create_table(
        "workgroups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("speaker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
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
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["speaker_id"], ["memberships.id"]),
        sa.UniqueConstraint("organization_id", "slug", name="uq_org_workgroup_slug"),
    )

    # Create motions table
    op.create_table(
        "motions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("motion_type_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "internal_review",
                "external_review",
                "on_agenda",
                "approved",
                "submitted",
                "accepted",
                "rejected",
                "withdrawn",
                name="motion_status",
                create_type=False,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "visibility",
            postgresql.ENUM(
                "private",
                "shared",
                "faction",
                "organization",
                "public",
                name="motion_visibility",
                create_type=False,
            ),
            nullable=False,
            server_default="private",
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_workgroup_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workgroup_recommendation", sa.String(20), nullable=True),
        sa.Column("workgroup_recommendation_note", sa.Text(), nullable=True),
        sa.Column("oparl_paper_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
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
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["motion_type_id"], ["motion_types.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["memberships.id"]),
        sa.ForeignKeyConstraint(["assigned_workgroup_id"], ["workgroups.id"]),
        sa.ForeignKeyConstraint(["oparl_paper_id"], ["oparl_papers.id"]),
    )

    # Create motion_co_authors association table
    op.create_table(
        "motion_co_authors",
        sa.Column("motion_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.ForeignKeyConstraint(["motion_id"], ["motions.id"]),
        sa.ForeignKeyConstraint(["membership_id"], ["memberships.id"]),
    )

    # Create motion_co_author_invites table
    op.create_table(
        "motion_co_author_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("motion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invited_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invited_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "accepted", "declined", name="co_author_invite_status", create_type=False
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["motion_id"], ["motions.id"]),
        sa.ForeignKeyConstraint(["invited_by_id"], ["memberships.id"]),
        sa.ForeignKeyConstraint(["invited_member_id"], ["memberships.id"]),
        sa.UniqueConstraint("motion_id", "invited_member_id", name="uq_motion_invited_member"),
    )

    # Create motion_approvals table
    op.create_table(
        "motion_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("motion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_type", sa.String(50), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "approved",
                "rejected",
                "changes_requested",
                name="approval_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["motion_id"], ["motions.id"]),
        sa.ForeignKeyConstraint(["approved_by_id"], ["memberships.id"]),
    )

    # Create coalition_consultations table
    op.create_table(
        "coalition_consultations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("motion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "result",
            postgresql.ENUM(
                "pending",
                "approved",
                "rejected",
                "modified",
                "no_response",
                name="coalition_result",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sent_via", sa.String(20), nullable=True),
        sa.Column("response_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_note", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["motion_id"], ["motions.id"]),
        sa.ForeignKeyConstraint(["party_id"], ["council_parties.id"]),
        sa.ForeignKeyConstraint(["sent_by_id"], ["memberships.id"]),
        sa.UniqueConstraint("motion_id", "party_id", name="uq_motion_party"),
    )

    # Create motion_share_logs table
    op.create_table(
        "motion_share_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("motion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shared_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shared_with_party_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("shared_with_email", sa.String(255), nullable=True),
        sa.Column(
            "method",
            postgresql.ENUM("email", "link", "download", name="share_method", create_type=False),
            nullable=False,
            server_default="email",
        ),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "shared_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["motion_id"], ["motions.id"]),
        sa.ForeignKeyConstraint(["shared_by_id"], ["memberships.id"]),
        sa.ForeignKeyConstraint(["shared_with_party_id"], ["council_parties.id"]),
    )

    # Create workgroup_memberships table
    op.create_table(
        "workgroup_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workgroup_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("speaker", "member", name="workgroup_role", create_type=False),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workgroup_id"], ["workgroups.id"]),
        sa.ForeignKeyConstraint(["member_id"], ["memberships.id"]),
        sa.UniqueConstraint("workgroup_id", "member_id", name="uq_workgroup_member"),
    )

    # Create indexes for common queries
    op.create_index("ix_motions_organization_status", "motions", ["organization_id", "status"])
    op.create_index("ix_motions_created_by", "motions", ["created_by_id"])
    op.create_index("ix_memberships_organization", "memberships", ["organization_id"])
    op.create_index(
        "ix_coalition_consultations_motion", "coalition_consultations", ["motion_id"]
    )
    op.create_index("ix_motion_approvals_motion", "motion_approvals", ["motion_id"])


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_motion_approvals_motion", "motion_approvals")
    op.drop_index("ix_coalition_consultations_motion", "coalition_consultations")
    op.drop_index("ix_memberships_organization", "memberships")
    op.drop_index("ix_motions_created_by", "motions")
    op.drop_index("ix_motions_organization_status", "motions")

    # Drop tables in reverse order (respecting foreign keys)
    op.drop_table("workgroup_memberships")
    op.drop_table("motion_share_logs")
    op.drop_table("coalition_consultations")
    op.drop_table("motion_approvals")
    op.drop_table("motion_co_author_invites")
    op.drop_table("motion_co_authors")
    op.drop_table("motions")
    op.drop_table("workgroups")
    op.drop_table("motion_types")
    op.drop_table("council_parties")
    op.drop_table("memberships")
    op.drop_table("organizations")
    op.drop_table("users")

    # Drop enum types
    op.execute("DROP TYPE workgroup_role")
    op.execute("DROP TYPE share_method")
    op.execute("DROP TYPE co_author_invite_status")
    op.execute("DROP TYPE coalition_result")
    op.execute("DROP TYPE approval_status")
    op.execute("DROP TYPE motion_visibility")
    op.execute("DROP TYPE motion_status")
    op.execute("DROP TYPE membership_role")
