"""record the write counter a scope's statistics were built at

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-26

Whether BM25's corpus statistics still describe a scope was answered by
counting its facts and comparing against the count stored at the last
rebuild. That works and it costs a scan of the scope, on a path that runs
while somebody waits.

Worse, it was checked on an interval, and an interval cannot bound a ratio.
A scope rebuilt at 500 facts goes stale at 600, and a rebuild every 500
writes does not arrive until 1000, so the ranker switched itself off for four
fifths of the writes while its setting said it was on. Shrinking the interval
narrows the window without closing it: the bound needed is proportional to
the scope, and a constant is not.

group_state.write_episode_count is already maintained per scope, incremented
inside the same transaction as the write. Storing its value at rebuild time
turns "has this grown by more than the tolerance" into arithmetic on two
integers the database is already keeping, with no scan and no window.

Nullable, and null means "built before this column existed". Both readers
treat that as stale, so the first write after an upgrade rebuilds once and
the question is answerable from then on.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.lexical_scope "
        "ADD COLUMN IF NOT EXISTS refreshed_write_count BIGINT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.lexical_scope DROP COLUMN IF EXISTS refreshed_write_count"
    )
