# Mandari API Migrations

Database migrations using Alembic.

## Usage

```bash
cd apps/api

# Apply all pending migrations
uv run alembic upgrade head

# Create a new migration (auto-generate from model changes)
uv run alembic revision --autogenerate -m "description of changes"

# View current revision
uv run alembic current

# View migration history
uv run alembic history

# Downgrade one revision
uv run alembic downgrade -1

# Downgrade to specific revision
uv run alembic downgrade <revision_id>
```

## Migration Files

Migrations are stored in `migrations/versions/` with naming convention:
`YYYYMMDD_HHMM_<revision>_<description>.py`

## Initial Migration

The `001_work_module_models.py` migration creates all tables for the work module:

### Core Tables
- `users` - User accounts
- `organizations` - Political organizations (factions, local chapters)
- `memberships` - User memberships in organizations

### Motion Management
- `motion_types` - Workflow configuration per document type
- `motions` - Documents (Anträge, Anfragen, etc.)
- `motion_approvals` - Approval workflow tracking
- `motion_co_authors` - Co-authorship (M2M)
- `motion_co_author_invites` - Co-author invitations

### Coalition
- `council_parties` - Parties in the municipal council
- `coalition_consultations` - Coalition voting documentation

### Sharing
- `motion_share_logs` - Audit log for sharing actions

### Working Groups
- `workgroups` - Working groups (AGs)
- `workgroup_memberships` - AG membership

## Enum Types

The migration creates the following PostgreSQL enum types:
- `membership_role` - Roles in organizations
- `motion_status` - Motion lifecycle states
- `motion_visibility` - Visibility levels
- `approval_status` - Approval states
- `coalition_result` - Coalition consultation results
- `co_author_invite_status` - Invitation states
- `share_method` - Sharing methods
- `workgroup_role` - Roles in working groups
