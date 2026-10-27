"""add outlet profile tables

Revision ID: a1b2c3d4e5f6
Revises: 98cd1b46cce3
Create Date: 2026-10-26 19:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '98cd1b46cce3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'outlet_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('outlet_id', sa.Integer(), nullable=False),
        sa.Column('wikidata_qid', sa.String(length=40), nullable=True),
        sa.Column('founded', sa.String(length=100), nullable=True),
        sa.Column('owner', sa.String(length=300), nullable=True),
        sa.Column('headquarters', sa.String(length=300), nullable=True),
        sa.Column('official_website', sa.Text(), nullable=True),
        sa.Column('stock_exchange', sa.String(length=100), nullable=True),
        sa.Column('ticker', sa.String(length=40), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('confirmed_by_admin', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['outlet_id'], ['outlets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outlet_profiles_outlet_id'), 'outlet_profiles', ['outlet_id'], unique=True)
    op.create_index(op.f('ix_outlet_profiles_wikidata_qid'), 'outlet_profiles', ['wikidata_qid'], unique=False)

    op.create_table(
        'outlet_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('outlet_id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=40), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['outlet_id'], ['outlets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outlet_links_outlet_id'), 'outlet_links', ['outlet_id'], unique=False)

    op.create_table(
        'outlet_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('outlet_id', sa.Integer(), nullable=False),
        sa.Column('metric', sa.String(length=60), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('value_text', sa.String(length=200), nullable=False),
        sa.Column('source', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['outlet_id'], ['outlets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outlet_metrics_outlet_id'), 'outlet_metrics', ['outlet_id'], unique=False)
    op.create_index(op.f('ix_outlet_metrics_metric'), 'outlet_metrics', ['metric'], unique=False)

    op.create_table(
        'outlet_manual_facts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('outlet_id', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=60), nullable=False),
        sa.Column('fact', sa.Text(), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('as_of_date', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['outlet_id'], ['outlets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outlet_manual_facts_outlet_id'), 'outlet_manual_facts', ['outlet_id'], unique=False)


def downgrade() -> None:
    op.drop_table('outlet_manual_facts')
    op.drop_table('outlet_metrics')
    op.drop_table('outlet_links')
    op.drop_table('outlet_profiles')
