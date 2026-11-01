"""check run status

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of RunStatus at this revision (migrations must not import app code).
STATUSES = ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')


def upgrade() -> None:
    # batch mode: SQLite cannot add a constraint in place, so Alembic copies the table.
    with op.batch_alter_table('runs') as batch:
        batch.create_check_constraint(
            'ck_runs_status', f"status IN ({', '.join(repr(s) for s in STATUSES)})"
        )


def downgrade() -> None:
    with op.batch_alter_table('runs') as batch:
        batch.drop_constraint('ck_runs_status', type_='check')
