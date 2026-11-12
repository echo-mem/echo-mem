"""a door for machines that is not also a writer

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-11

Outcomes come from things that are not agents: a CI run, a deploy, a test
suite. None of them speaks MCP, and handing each one a key that can write
arbitrary facts is the wrong trade twice over - too much authority for a
reporter, and too much concurrency for the graph.

The second half is the one that forces this table. `write_episode` takes a
per-scope advisory lock as the first statement in its transaction, so two
writers into ONE scope serialise correctly. Apache AGE's label catalogue is
per GRAPH. Two writers in different scopes of the same graph take different
locks, proceed concurrently, and AGE then builds table names out of garbage
memory:

    relation "echo_memory.-775.1utterance" does not exist

Observed repeatedly when four benchmark shards wrote four scopes at once;
three of the four died, and the run still looked complete from outside. A
multi-tenant HTTP endpoint that writes straight through is exactly that
condition, continuously.

So the door and the write are separated. The door validates an envelope and
INSERTs one row, which is safe at any concurrency because an INSERT is not a
Cypher statement. A single drain per graph then applies them in order. The
latency is the point: it is what buys a safe ingress.

## Why the columns are what they are

`event_id` is the primary key and the whole idempotency story. A CI system
that retries on a timeout cannot know whether the first call landed, so the
second call has to be free - `ON CONFLICT DO NOTHING` makes a duplicate an
answer rather than an error, and a caller that builds the id from its own run
("deploy-<commit>-<env>") gets that for nothing.

`envelope` keeps the whole validated event, not just the fields the drain
reads today. An event is testimony about something that happened once; a
schema change later cannot go back and ask the reporter again.

`occurred_at` and `received_at` are both stored because they answer different
questions. One is when the thing happened, which only the reporter knows; the
other is when we heard, which is the only one of the two this database can
vouch for. A single timestamp column would have to pick, and whichever it
picked would be wrong for the other question.

`applied_edge_ids` is what the drain created. Without it a replay cannot be
reconciled: this project has already had a reconciliation over all history
find the write that was doing the tampering, and the lesson was that an
applied change has to say what it touched.

`state` is a CHECK, not an ENUM. Postgres cannot drop a value from a type, so
migration 0026 had to leave two unused audit labels behind permanently on its
own downgrade and say so. A CHECK is editable by a later migration; an enum is
a decision that outlives the reason for it.
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.event_inbox (
            -- Caller supplied, and the idempotency key. Text rather than a
            -- uuid because the useful ids are derived from the reporter's own
            -- run and a uuid generated per attempt is the one shape that
            -- cannot be idempotent.
            event_id         text PRIMARY KEY,
            group_id         text NOT NULL,
            event_type       text NOT NULL,
            -- Stored per row rather than assumed from the package version.
            -- The drain refuses a version it does not know; it cannot do that
            -- if the version is only in the code that is doing the refusing.
            schema_version   int  NOT NULL,
            envelope         jsonb NOT NULL,
            -- When the reporter says it happened.
            occurred_at      timestamptz NOT NULL,
            -- When we heard. The only one of the two this database can vouch
            -- for, and therefore the one the drain orders by.
            received_at      timestamptz NOT NULL DEFAULT now(),
            -- pending      accepted, not yet applied
            -- applied      the drain wrote it; applied_edge_ids says what
            -- rejected     the drain refused it; error says why. Terminal: a
            --              malformed event does not become valid by waiting.
            -- quarantined  held out of the drain by an operator. Separate from
            --              rejected because it is a human decision about a
            --              well formed event, and the two must not be
            --              indistinguishable afterwards.
            state            text NOT NULL DEFAULT 'pending'
                             CHECK (state IN ('pending', 'applied',
                                              'rejected', 'quarantined')),
            -- Counted even on the terminal paths. A reporter whose events are
            -- all rejected on the first attempt looks identical to one whose
            -- events are retried forever, unless somebody counted.
            attempts         int  NOT NULL DEFAULT 0,
            processed_at     timestamptz,
            error            text,
            -- What the drain created. text[] rather than graphid[] on purpose:
            -- this is a record of what happened, and it must survive the
            -- erasure of the facts it names. A graphid[] would be a dangling
            -- reference to a row the retention policy is entitled to remove.
            applied_edge_ids text[]
        )
        """
    )

    # The drain's only query: the oldest pending events. Partial, because the
    # pending share of this table only ever shrinks - every row that is
    # applied or rejected stays as history, exactly like a retired fact - and
    # indexing the terminal rows would waste the space twice over. Migration
    # 0013 made the same call on FACT for the same reason.
    op.execute(
        """CREATE INDEX IF NOT EXISTS event_inbox_pending_idx
             ON public.event_inbox (state, received_at)
           WHERE state = 'pending'"""
    )
    # "What has this scope sent recently", which is the operator's question
    # and the one a reporter asks about its own events. Not partial: the
    # interesting rows here are usually the rejected ones.
    op.execute(
        """CREATE INDEX IF NOT EXISTS event_inbox_group_idx
             ON public.event_inbox (group_id, received_at)"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.event_inbox_group_idx")
    op.execute("DROP INDEX IF EXISTS public.event_inbox_pending_idx")
    op.execute("DROP TABLE IF EXISTS public.event_inbox")
