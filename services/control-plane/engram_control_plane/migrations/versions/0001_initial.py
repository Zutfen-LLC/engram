"""Control Plane initial scaffold revision (ENG-PORTAL-001A).

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-24
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish migration infrastructure only.

    No business tables (portal_users, memberships, sessions, subscriptions) are
    created in this slice. The dedicated Control Plane database exists with an
    ``alembic_version`` table (managed by Alembic) which the non-owner runtime
    role is granted SELECT on for readiness checks. A minimal
    ``control_plane_meta`` row documents the boundary (single, idempotent).

    These grants run as the owner/migration role. They cannot be done in
    ``control-db-bootstrap`` because that one-shot runs BEFORE the migration
    creates ``alembic_version`` (Alembic creates it during the first upgrade).
    The runtime role (``engram_control_app``) receives only SELECT on the
    readiness tables — never schema CREATE or migration authority.
    """
    # Idempotent marker table: lets readiness prove the runtime role can SELECT
    # within its own database without depending on any future business table.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS control_plane_meta (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO control_plane_meta (key, value) VALUES
            ('boundary', 'dedicated_database'),
            ('slice', 'ENG-PORTAL-001A'),
            ('identity_status', 'not_configured'),
            ('delegation_status', 'not_implemented'),
            ('billing_status', 'not_configured')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                        updated_at = now()
        """
    )
    # Grant the non-owner runtime role SELECT on readiness tables. Alembic has
    # just created alembic_version; control_plane_meta exists above. Each grant
    # is a separate op.execute because asyncpg cannot prepare multiple statements
    # in one call.
    op.execute("GRANT SELECT ON control_plane_meta TO engram_control_app")
    op.execute("GRANT SELECT ON alembic_version TO engram_control_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS control_plane_meta")
