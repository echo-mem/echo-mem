"""somewhere to record a fact's text being re-read for a cause it already states

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-26

Production holds 38,479 edges and 2 causal hints, both written by a smoke test.
`trace_cause` shipped in 0.5.0 and has nothing to walk, because a hint is only
ever written by a caller that read one in what a session said, and no session
before 0.5.0 was asked. The roadmap's answer (the-order-to-build-in.md, 2c) is
an opt-in offline pass that re-reads the fact text a store already holds and
types the edge when the sentence itself states the relation. That is extraction
done late, not causal discovery: the refusal this project keeps is on inferring
causation from co-occurrence, structure or correlation, and none of those is
consulted.

Two things need somewhere to live.

`causal_hint_scan` is one row per fact the pass has looked at, verdict
included. It exists for two reasons that a "just set the property" design
cannot serve. Resumability: a pass over 38,479 facts will be interrupted, and
without a record of what was examined a re-run pays the model again for every
sentence that turned out to state nothing - which is most of them. And review:
the pass is dry run by default, so the proposal has to survive between the run
that made it and the operator who reads it, or `--write` would have to call the
model a second time and could get a different answer than the one approved.

`quote` is the span of the fact that states the relation, and it is the check
that keeps the tool honest. A proposal whose quote is not literally present in
the fact's own text is discarded before it reaches the graph, so a model that
reasons its way to a plausible cause from world knowledge cannot get one
stored: it has to point at the words.

The two new audit mutation types are deliberate rather than a reuse of
`fact_superseded`. Nothing about the fact is superseded - its text is
untouched - and a reader of `echo-memory why` should be able to tell a hint
that was extracted late from one a session asserted at write time. The edge
also carries `causal_hint_origin`, which is what makes the pass reversible:
`--clear` removes only hints wearing that marker and never one a caller wrote.
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # graphid lives in ag_catalog, so the extension has to be loaded and on the
    # path before a column can be declared with it (see 0001's note about
    # everything after SET search_path needing public.* qualification).
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.causal_hint_scan (
            -- graphid, not text: this is joined against echo_memory."FACT".id
            -- by the database, which is the case 0002 reserved graphid for. A
            -- graphid is not a bigint and does not compare as one - reading it
            -- as one is what turned a targeted DELETE into a full one earlier
            -- this year - so every parameter is cast %s::text::graphid.
            edge_id      graphid PRIMARY KEY,
            group_id     TEXT NOT NULL,
            -- 'no_cue'   the sentence has no causal connective, so no model was
            --            asked. Recorded, because the cheapest way to not pay
            --            for a sentence twice is to remember it.
            -- 'no_cause' a model read it and found no stated relation.
            -- 'hint'     a hint was extracted, and `hint` and `quote` say what
            --            and from which words.
            -- 'cleared'  applied and then reversed by --clear. Kept rather
            --            than deleted so the pass does not propose again what
            --            an operator has already rejected.
            verdict      TEXT NOT NULL,
            hint         TEXT,
            quote        TEXT,
            model        TEXT,
            scanned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- NULL while a proposal is unapplied, which is every proposal a dry
            -- run makes. `--write` sets it, and `--clear` sets it back.
            applied_at   TIMESTAMPTZ
        )
    """)
    # Every query is "what has this scope already had examined" or "what does
    # this scope have waiting to apply", so the group leads.
    op.execute(
        "CREATE INDEX IF NOT EXISTS causal_hint_scan_group_idx "
        "ON public.causal_hint_scan (group_id, verdict)"
    )

    # Postgres 12+ allows this inside a transaction as long as the value is not
    # used in the same one. Nothing here uses it; the command that writes these
    # entries runs later, in its own connection.
    op.execute(
        "ALTER TYPE public.audit_mutation_type "
        "ADD VALUE IF NOT EXISTS 'causal_hint_set'"
    )
    op.execute(
        "ALTER TYPE public.audit_mutation_type "
        "ADD VALUE IF NOT EXISTS 'causal_hint_cleared'"
    )


def downgrade() -> None:
    # The enum values stay. Postgres cannot drop a value from a type, and
    # rebuilding audit_mutation_type would mean rewriting the audit log - an
    # append-only record - to undo two unused labels. A downgrade that leaves
    # them is the smaller lie, and `ADD VALUE IF NOT EXISTS` makes re-upgrading
    # find them already there.
    op.execute("DROP INDEX IF EXISTS causal_hint_scan_group_idx")
    op.execute("DROP TABLE IF EXISTS public.causal_hint_scan")
