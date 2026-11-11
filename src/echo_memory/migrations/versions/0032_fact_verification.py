"""whether a claim was ever checked, who checked it, and a chain over that

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-11

`fact_attestation` makes a fact unrewritable. It says nothing about whether
the fact is true. Everything in this store is believed because somebody wrote
it, and `trust/states.py` carries the argument for why the answer to that is
six states rather than a score: the one calibration this project has measured
came out AUC 0.766 with a 95% interval of [0.437, 0.968] over five positives,
an interval that includes chance.

## Append only, newest row wins

A verification is an event. "Verified on the 3rd, disputed on the 9th, and
verified again on the 11th" is the history a reader needs, and an UPDATE in
place would leave only the last word with no way to ask who changed their
mind. So the current state of a fact is the newest row for its `edge_id`, and
nothing deletes.

That is also what makes expiry a degrade rather than a disappearance. A
`verified` row whose `expires_at` has passed is read as `stale` at query time
and the row itself is untouched, because a lapsed check is not a refutation
and rewriting it to say so would destroy the evidence that it ever passed.

## CHECK constraints, not Postgres enums

Both state columns are text with a CHECK. The reason is migration 0026's
downgrade, which had to leave two unused `audit_mutation_type` values behind:
Postgres cannot drop a value from a type, and rebuilding
`audit_mutation_type` would have meant rewriting the audit log - an
append-only record - to undo two labels nothing used. A CHECK is dropped and
replaced in one statement and leaves the data alone, and these vocabularies
are young enough that one of them will be wrong.

## Why the attestation chain here is a second chain

`verification_attestation` is deliberately not rows in `fact_attestation`.
That chain's digests are over what a fact says about itself, its sequence is
dense per scope, and `echo-memory verify` recomputes the whole of it. Pouring
a different kind of record into it would mean every verification shifts the
sequence numbers a fact's attestation depends on, so recording a check would
change the evidence about the claim being checked. Two chains, and neither
can perturb the other.

`chain_version` is here from the first row because the covered fields of a
verification will change - a scope column was added to this design twice
while it was being written - and a chain whose digest definition moved
without a version is a chain that reports every historical link as altered.

Idempotent, because init-db re-applies every migration on every run.
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # graphid is in ag_catalog and only resolvable with it on the path. See
    # the longer note in 0031, which hit this: a full `upgrade head` inherits
    # the setting from 0001, and an upgrade that starts at 0030 does not.
    op.execute('SET search_path = ag_catalog, "$user", public')
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.fact_verification (
            id            bigserial PRIMARY KEY,
            -- graphid, for the same reason migration 0030 spells out: edge id
            -- columns are not one type across this schema and a bigint here
            -- compares false for every row rather than raising.
            edge_id       graphid NOT NULL,
            group_id      text NOT NULL,
            -- The six values in trust/states.py ALL_STATES. Three of them are
            -- advisory and three decide what retrieval returns at all; the
            -- split is enforced in code, not here, because which states an
            -- authorised verifier may set is a policy question and this is a
            -- spelling check.
            state         text NOT NULL,
            verifier_kind text NOT NULL,
            -- Who, within that kind: a person, a service name, a rule id. Not
            -- authenticated here. The chain below proves this value has not
            -- changed since it was recorded, not that it was true when it was.
            verifier_id   text NOT NULL,
            -- How they checked. Free text on purpose: "ran the suite", "read
            -- the deploy log", "asked the author". A closed vocabulary here
            -- would have been guessed before anybody had verified anything.
            method        text NOT NULL,
            policy_id     text,
            -- The artefacts this check rested on, by evidence.id. Not a
            -- foreign key array - Postgres has no such thing - so a deleted
            -- evidence row leaves an id here that resolves to nothing, which
            -- is the honest record of a check whose evidence is gone.
            evidence_ids  text[] NOT NULL DEFAULT '{}',
            -- Which environment or project this holds for. A fact verified in
            -- staging is not verified in production, and a verification with
            -- no scope that silently meant "everywhere" is how a staging
            -- check ends up quoted about production.
            scope         jsonb,
            verified_at   timestamptz NOT NULL DEFAULT now(),
            -- When the check stops being worth anything. NULL means it does
            -- not lapse on its own, which is right for a human correction and
            -- wrong for a test run, and the caller is the only one that knows
            -- which this is.
            expires_at    timestamptz,
            detail        text,
            CONSTRAINT fact_verification_state_check CHECK (state IN (
                'unverified', 'evidence_backed', 'verified', 'disputed',
                'stale', 'rejected')),
            CONSTRAINT fact_verification_verifier_kind_check
                CHECK (verifier_kind IN ('human', 'service', 'rule'))
        )
        """
    )
    # "the newest row for this fact", which is the read every query does. The
    # descending id is the whole point: without it this is a sort per fact on
    # the read path that the trust channel runs for every candidate.
    op.execute(
        """CREATE INDEX IF NOT EXISTS fact_verification_edge_idx
             ON public.fact_verification (edge_id, id DESC)"""
    )
    # Partial, because `unverified` is the state of every fact written before
    # this existed and of every fact written since by a caller that attached
    # nothing. Indexing it would mean an index the size of the store answering
    # a question nobody asks, and the three states here are the ones an
    # operator actually looks for: what has been checked, what is contested,
    # and what was refused.
    op.execute(
        """CREATE INDEX IF NOT EXISTS fact_verification_group_state_idx
             ON public.fact_verification (group_id, state)
          WHERE state IN ('verified', 'disputed', 'rejected')"""
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.verification_attestation (
            id              bigserial PRIMARY KEY,
            -- bigint and not graphid: this chains verification rows, whose id
            -- is a bigserial. The one place in this pair where a bigint is
            -- the correct type.
            verification_id bigint NOT NULL,
            group_id        text NOT NULL,
            chain_version   int NOT NULL DEFAULT 1,
            seq             bigint NOT NULL,
            prev_digest     text NOT NULL,
            digest          text NOT NULL,
            -- The algorithm and the key this link was signed with, recorded
            -- per link rather than per chain. That is what lets a key be
            -- rotated: an old link is verified with the key it names, so
            -- rotation does not invalidate history, and a chain that is
            -- partly keyed cannot be described as keyed.
            alg             text NOT NULL,
            key_id          text NOT NULL,
            attested_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One link per position per chain version. Two rows claiming the same
    # place is a fork, and a fork lets a verification pick the branch that
    # passes. Concurrent writers to one scope serialise on this.
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS verification_attestation_seq_idx
             ON public.verification_attestation (group_id, chain_version, seq)"""
    )
    # "which link attests this verification", for reading one row's provenance
    # without walking the scope.
    op.execute(
        """CREATE INDEX IF NOT EXISTS verification_attestation_row_idx
             ON public.verification_attestation (verification_id)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.verification_attestation")
    op.execute("DROP TABLE IF EXISTS public.fact_verification")
