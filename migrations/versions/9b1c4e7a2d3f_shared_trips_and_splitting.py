"""shared trips (trip_members, invite_version) and expense splitting

Expand-only, like every migration here: new table, new nullable/defaulted
columns. The backend version before this one keeps working on the new schema
during a rolling update (it just ignores them).

Revision ID: 9b1c4e7a2d3f
Revises: edd21af5869e
Create Date: 2026-10-28 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9b1c4e7a2d3f'
down_revision = 'edd21af5869e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('trip_members',
    sa.Column('trip_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('trip_id', 'user_id')
    )
    with op.batch_alter_table('trip_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trip_members_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('trips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invite_version', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('expenses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paid_by', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('split_between', sa.JSON(), nullable=True))
        batch_op.create_foreign_key('fk_expenses_paid_by_users', 'users', ['paid_by'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('expenses', schema=None) as batch_op:
        batch_op.drop_constraint('fk_expenses_paid_by_users', type_='foreignkey')
        batch_op.drop_column('split_between')
        batch_op.drop_column('paid_by')

    with op.batch_alter_table('trips', schema=None) as batch_op:
        batch_op.drop_column('invite_version')

    with op.batch_alter_table('trip_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trip_members_user_id'))

    op.drop_table('trip_members')
