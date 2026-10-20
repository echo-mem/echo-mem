"""what an erasure removed, without recording what it was

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-20

GDPR Article 17 and India's DPDP Act both require actual deletion on a data
subject's request, and the store had no way to do it: supersession sets
t_invalid and keeps the fact, which is right for memory and insufficient for
law. See docs/ERASURE.md for the whole design.

This table is the half of erasure that has to survive it. An erasure has to be
provable afterwards, and the proof cannot be a copy of what was erased - a
compliance record listing the names of people who asked to be forgotten is the
thing they asked to not exist.

So: counts, a scope, a verb, who asked and a ticket reference. No fact text, no
entity name, no note.

subject_digest is an HMAC of the normalised name, which answers "did you erase
X" by recomputation without holding X. It is nullable and stays null unless
ECHO_MEMORY_ERASURE_KEY is set, deliberately: the key must live outside the
database or a dump holder can recompute every candidate name offline, and
generating one and storing it here would produce the appearance of protection
rather than protection. Counts-only evidence is the honest default.

status exists because erasure cannot be one transaction. It mixes Cypher
against AGE with ordinary SQL, and a script that mixes them has no transaction
at all, so the row is written 'started' before anything is deleted. A row left
'started' is a crashed erasure, and every step is idempotent so it can be
re-run. Intent survives even when the work does not.

as_of reads this table too. A query over a window spanning an erasure now
returns less than it would have, and saying so with a count and a date is what
keeps time travel honest while the data goes. That is the one place where this
table is read on a path somebody waits for, hence the index on
(group_id, completed_at).
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.erasure_event (
            id              BIGSERIAL PRIMARY KEY,
            requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at    TIMESTAMPTZ,
            -- started | completed | failed. 'started' with no completed_at is a
            -- crashed run, and re-running the same request is safe.
            status          TEXT NOT NULL,
            group_id        TEXT NOT NULL,
            -- fact | entity | scope. Which verb was used, because "4 facts
            -- erased" means something different for each.
            verb            TEXT NOT NULL,
            -- HMAC of the normalised subject, or null when no key is set.
            -- Never the subject itself.
            subject_digest  TEXT,
            n_edges         INTEGER NOT NULL DEFAULT 0,
            n_nodes         INTEGER NOT NULL DEFAULT 0,
            n_audit_rows    INTEGER NOT NULL DEFAULT 0,
            -- Free text for the audit trail. Deliberately does not gate the
            -- capacity refund: a reason enum that decides money is a reason
            -- enum people lie to.
            reason          TEXT,
            requested_by    TEXT NOT NULL,
            -- The ticket, not its contents.
            request_ref     TEXT
        )
        """
    )
    # as_of asks "did anything get erased in this scope before now", which is
    # this index exactly. Partial on completed: a started-but-crashed erasure
    # has removed an unknown amount and must not be reported as a clean count.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS erasure_event_scope_idx
            ON public.erasure_event (group_id, completed_at)
         WHERE status = 'completed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.erasure_event_scope_idx")
    op.execute("DROP TABLE IF EXISTS public.erasure_event")
