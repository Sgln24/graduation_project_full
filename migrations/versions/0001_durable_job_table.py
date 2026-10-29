"""Add persistent job records for the staged background-job migration.

Revision ID: 0001_durable_job_table
Revises:
Create Date: 2026-10-27
"""

from alembic import op

from lms_models import DurableJob


revision = "0001_durable_job_table"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    DurableJob.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    DurableJob.__table__.drop(op.get_bind(), checkfirst=True)
