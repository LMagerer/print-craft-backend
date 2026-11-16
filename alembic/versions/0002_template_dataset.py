"""link templates to a dataset

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16 13:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('templates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dataset_id', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_templates_dataset_id'), ['dataset_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('templates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_templates_dataset_id'))
        batch_op.drop_column('dataset_id')
