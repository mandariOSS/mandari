"""Create OParl tables for RIS data synchronization.

Revision ID: 000
Revises:
Create Date: 2026-01-24

This migration creates the base OParl tables:
- oparl_sources (registered OParl API sources)
- oparl_bodies (municipalities/bodies)
- oparl_meetings (council meetings)
- oparl_papers (documents/papers)
- oparl_persons (council members)
- oparl_organizations (committees/factions)
- oparl_agenda_items (meeting agenda items)
- oparl_files (document attachments)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create oparl_sources table
    op.create_table(
        "oparl_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("contact_name", sa.String(255), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_sync", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create oparl_bodies table
    op.create_table(
        "oparl_bodies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_sources.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("short_name", sa.String(100), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("license_valid_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classification", sa.String(100), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create oparl_organizations table (before persons for FK)
    op.create_table(
        "oparl_organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "body_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_bodies.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("short_name", sa.String(100), nullable=True),
        sa.Column("organization_type", sa.String(100), nullable=True),
        sa.Column("classification", sa.String(100), nullable=True),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create oparl_persons table
    op.create_table(
        "oparl_persons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "body_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_bodies.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("family_name", sa.String(255), nullable=True),
        sa.Column("given_name", sa.String(255), nullable=True),
        sa.Column("title", sa.String(100), nullable=True),
        sa.Column("gender", sa.String(50), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(100), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create oparl_papers table
    op.create_table(
        "oparl_papers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "body_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_bodies.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column("paper_type", sa.String(100), nullable=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("locations", postgresql.JSONB(), nullable=True),
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

    # Create oparl_meetings table
    op.create_table(
        "oparl_meetings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "body_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_bodies.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("meeting_state", sa.String(100), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location_name", sa.String(500), nullable=True),
        sa.Column("location_address", sa.Text(), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create oparl_agenda_items table
    op.create_table(
        "oparl_agenda_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_meetings.id"),
            nullable=False,
        ),
        sa.Column("number", sa.String(50), nullable=True),
        sa.Column("order", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("public", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("resolution_text", sa.Text(), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create oparl_files table
    op.create_table(
        "oparl_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column(
            "paper_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oparl_papers.id"),
            nullable=True,
        ),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("access_url", sa.Text(), nullable=True),
        sa.Column("download_url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("oparl_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oparl_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False, server_default="{}"),
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

    # Create indexes for common queries
    op.create_index("ix_oparl_bodies_source", "oparl_bodies", ["source_id"])
    op.create_index("ix_oparl_meetings_body", "oparl_meetings", ["body_id"])
    op.create_index("ix_oparl_papers_body", "oparl_papers", ["body_id"])
    op.create_index("ix_oparl_persons_body", "oparl_persons", ["body_id"])
    op.create_index("ix_oparl_organizations_body", "oparl_organizations", ["body_id"])
    op.create_index("ix_oparl_agenda_items_meeting", "oparl_agenda_items", ["meeting_id"])
    op.create_index("ix_oparl_files_paper", "oparl_files", ["paper_id"])


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_oparl_files_paper", "oparl_files")
    op.drop_index("ix_oparl_agenda_items_meeting", "oparl_agenda_items")
    op.drop_index("ix_oparl_organizations_body", "oparl_organizations")
    op.drop_index("ix_oparl_persons_body", "oparl_persons")
    op.drop_index("ix_oparl_papers_body", "oparl_papers")
    op.drop_index("ix_oparl_meetings_body", "oparl_meetings")
    op.drop_index("ix_oparl_bodies_source", "oparl_bodies")

    # Drop tables in reverse order
    op.drop_table("oparl_files")
    op.drop_table("oparl_agenda_items")
    op.drop_table("oparl_meetings")
    op.drop_table("oparl_papers")
    op.drop_table("oparl_persons")
    op.drop_table("oparl_organizations")
    op.drop_table("oparl_bodies")
    op.drop_table("oparl_sources")
