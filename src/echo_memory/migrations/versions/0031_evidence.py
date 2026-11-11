"""what a claim is backed by, kept apart from the claim itself

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-11

A fact in this store records who wrote it and how they came by it. Neither of
those is evidence. `confidence` says `extracted` or `inferred`, which is a
statement about the writer's process, and an agent that inferred something
confidently and wrongly produces exactly the same row as one that read it off
a deployment log.

This is the artefact. A deployment, a test run, a human correction, a metric,
a document, an attestation head recorded elsewhere: things that exist outside
this database and can be gone and looked at. `uri` says where, and
`content_digest` says what it said at the time, so a document edited after it
was cited is distinguishable from one that was not.

## Why it is its own table rather than a column on the fact

One artefact backs many claims. A single deploy log is evidence for every
fact asserted about that release, and a column would store the same uri on
twenty edges with twenty chances to disagree. More than that, evidence
arrives late: the test run that confirms a decision happens after the
decision was written, and a model that can only attach evidence at write time
can never record the confirmation at all.

`fact_evidence` is the join, and it carries `group_id` so a scope's links can
be read and erased without touching the graph.

## What it does not claim

Evidence is not proof, and nothing here says otherwise. A deployment log
saying the deploy happened does not establish that the decision behind it was
right, which is why `trust/states.py` has `evidence_backed` as a separate and
weaker state than `verified`. The weight of an artefact is a judgement a
verifier makes and records in `fact_verification`; this table only holds the
artefact.

`reporter_agent` is a string the caller supplies, the same way `agent_id` is
on a fact. It says which agent claimed to have seen this, not that it did.

Idempotent, because init-db re-applies every migration on every run.
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # graphid lives in ag_catalog, so the type is only resolvable with that
    # schema on the path. Migration 0001 sets this and so does 0026, which is
    # why 0030 gets away without it: a full `upgrade head` runs the whole
    # chain in one session and inherits the setting. Upgrading from 0030
    # alone, which is what a downgrade-and-retry does, starts a session where
    # it was never set and dies on `type "graphid" does not exist`. Set it
    # here, and qualify everything public.* from this point as 0026 notes.
    op.execute('SET search_path = ag_catalog, "$user", public')
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.evidence (
            -- text rather than bigserial because an id is handed back to the
            -- caller and then travels: into a verification's evidence_ids,
            -- into a ticket, into another agent's prompt. A number invites
            -- being guessed at and off by one.
            id             text PRIMARY KEY,
            group_id       text NOT NULL,
            kind           text NOT NULL,
            -- Where the artefact is, if it is anywhere a reader can reach. A
            -- human correction typed into a chat has no uri and is still
            -- evidence, so this is nullable.
            uri            text,
            -- What it said when it was cited. Without this, a document that
            -- backed a claim and was then rewritten still looks like it backs
            -- the claim.
            content_digest text,
            detail         jsonb,
            -- Who reported it. A string the caller supplies, like agent_id on
            -- a fact: it records the claim of authorship, not its truth.
            reporter_agent text NOT NULL,
            reporter_run   text,
            -- When the artefact came to be, which is NOT when we heard about
            -- it. A test run from last Tuesday reported today is evidence
            -- about last Tuesday, and collapsing the two would make a stale
            -- report look fresh.
            occurred_at    timestamptz,
            received_at    timestamptz NOT NULL DEFAULT now(),
            -- A CHECK and not an ENUM. Postgres cannot drop a value from a
            -- type, so an enum here would mean every kind added in haste is
            -- permanent; migration 0026's downgrade carries the long version
            -- of why that matters for the audit log.
            CONSTRAINT evidence_kind_check CHECK (kind IN (
                'deployment', 'test_run', 'human_correction', 'metric',
                'external_doc', 'attestation'))
        )
        """
    )
    # "what deployments back anything in this scope", which is the question an
    # operator asks, so the group leads.
    op.execute(
        """CREATE INDEX IF NOT EXISTS evidence_group_kind_idx
             ON public.evidence (group_id, kind)"""
    )
    # Newest first within a scope, for "what has arrived lately".
    op.execute(
        """CREATE INDEX IF NOT EXISTS evidence_group_received_idx
             ON public.evidence (group_id, received_at)"""
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.fact_evidence (
            -- graphid, like every other edge reference in this schema. The id
            -- columns are not one type across it and a mismatch is silently
            -- false for every row rather than an error, so a bigint here
            -- would make every link invisible and nothing would complain.
            edge_id     graphid NOT NULL,
            evidence_id text NOT NULL
                REFERENCES public.evidence(id) ON DELETE CASCADE,
            group_id    text NOT NULL,
            linked_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (edge_id, evidence_id)
        )
        """
    )
    # The other direction: every claim one artefact backs. That is the query a
    # retraction needs - a deploy log found to be from the wrong release
    # affects whatever cited it - and the primary key cannot serve it.
    op.execute(
        """CREATE INDEX IF NOT EXISTS fact_evidence_evidence_idx
             ON public.fact_evidence (evidence_id)"""
    )


def downgrade() -> None:
    # fact_evidence first: it references evidence, and dropping the parent
    # would need a CASCADE that takes whatever else has come to reference it.
    op.execute("DROP TABLE IF EXISTS public.fact_evidence")
    op.execute("DROP TABLE IF EXISTS public.evidence")
