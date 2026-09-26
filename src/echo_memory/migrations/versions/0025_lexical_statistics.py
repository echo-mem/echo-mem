"""corpus statistics, so the lexical channel can rank by more than term count

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-26

ts_rank counts term occurrences. It has no inverse document frequency, no
term frequency saturation and no length normalisation, so a word that appears
in every fact in the scope counts for as much as one that appears in three.

The consequence was measured on 324 real prompts: the rank-3 and rank-4
scores were identical in 59% of them. The prompt hook injects three facts, so
which fact a user saw was decided by Postgres scan order. A tiebreak added
later made that arbitrary ordering reproducible; it did not make it right.

BM25 fixes all three, and two of its three inputs can be computed at query
time from the rows that already matched: a fact's term frequencies and its
length come out of its own tsvector. Only the corpus-wide part cannot, which
is what these tables hold.

Per scope, because inverse document frequency across tenants is meaningless:
the word "deploy" being rare in one customer's memory says nothing about
another's.

Not on the write path. Document frequency tolerates staleness - a term's
rarity does not swing on one more fact - so these are refreshed on demand
rather than maintained per write. The whole argument for this system's write
cost is that writing calls no model and touches few rows, and that must not
erode to make reads better.
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.lexical_term (
            group_id   TEXT   NOT NULL,
            lexeme     TEXT   NOT NULL,
            doc_count  BIGINT NOT NULL,
            PRIMARY KEY (group_id, lexeme)
        )
        """
    )
    # The scope-wide constants BM25 needs beside each term: how many facts
    # there are to be rare among, and how long the average one is.
    #
    # refreshed_at and refreshed_docs together answer "are these stale enough
    # to matter", which is a judgement the reader makes rather than a rule
    # this table enforces.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.lexical_scope (
            group_id       TEXT        PRIMARY KEY,
            doc_count      BIGINT      NOT NULL,
            avg_doc_length DOUBLE PRECISION NOT NULL,
            refreshed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            refreshed_docs BIGINT      NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.lexical_scope")
    op.execute("DROP TABLE IF EXISTS public.lexical_term")
