"""create flows and runs

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('flows',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('definition', sa.JSON(), nullable=False),
    sa.Column('state_machine_arn', sa.String(length=2048), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_flows_owner_created', 'flows', ['owner_id', 'created_at', 'id'], unique=False)
    op.create_table('runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('flow_id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('input', sa.JSON(), nullable=False),
    sa.Column('output', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('current_step', sa.String(length=64), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=True),
    sa.Column('request_hash', sa.String(length=64), nullable=True),
    sa.Column('execution_ref', sa.String(length=2048), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['flow_id'], ['flows.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'idempotency_key', name='uq_runs_owner_idempotency_key')
    )
    op.create_index(op.f('ix_runs_flow_id'), 'runs', ['flow_id'], unique=False)
    op.create_index('ix_runs_owner_created', 'runs', ['owner_id', 'created_at', 'id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_runs_owner_created', table_name='runs')
    op.drop_index(op.f('ix_runs_flow_id'), table_name='runs')
    op.drop_table('runs')
    op.drop_index('ix_flows_owner_created', table_name='flows')
    op.drop_table('flows')
