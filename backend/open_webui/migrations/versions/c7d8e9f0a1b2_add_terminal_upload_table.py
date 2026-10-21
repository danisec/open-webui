"""Add the terminal upload registry used by terminal file cleanup.

Revision ID: c7d8e9f0a1b2
Revises: b8f2c6d91a04
Create Date: 2026-10-12 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'b8f2c6d91a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    existing_tables = set(sa.inspect(conn).get_table_names())

    if 'terminal_upload' not in existing_tables:
        op.create_table(
            'terminal_upload',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('chat_id', sa.Text(), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('path', sa.Text(), nullable=False),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Index('ix_terminal_upload_chat_id', 'chat_id'),
            sa.Index('ix_terminal_upload_path', 'path'),
            sa.UniqueConstraint('chat_id', 'path', name='uq_terminal_upload_chat_path'),
        )


def downgrade() -> None:
    conn = op.get_bind()
    existing_tables = set(sa.inspect(conn).get_table_names())

    if 'terminal_upload' in existing_tables:
        op.drop_table('terminal_upload')
