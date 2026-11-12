"""a digest per fact, chained, so no change to one is silent

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-06

Every fact already records who wrote it, when, in which session and with
which version of the package. All of that is believed because the database
says so. Anyone who can write SQL can change a fact's text, move its
endpoints, or rewrite its agent_id, and nothing afterwards can tell that it
happened - which is the gap that matters most for AI governance, because the
whole value of "which agent asserted this" is that it cannot be edited later
by whoever finds the answer inconvenient.

This stores a digest over each fact's own claims, chained to the previous
attestation in the same scope. Changing a fact breaks its digest. Deleting
one breaks the chain at that link. Neither can be repaired without
recomputing every digest after it, and with a key configured, not even then.

## What it proves, and what it does not

A digest in the same database as the data it attests is only worth something
if an attacker cannot recompute it. That needs ONE of two things:

- **A key.** `ECHO_MEMORY_ATTESTATION_KEY` makes each digest an HMAC, so
  somebody with SQL access and no key cannot forge one. The key must not live
  in the database; the natural place is the same secret store the erasure key
  uses.
- **An external witness.** Without a key the chain is still a chain: any
  change anywhere alters every digest after it, including the head. Recording
  the head somewhere outside this database - a ticket, a commit, a log the
  database cannot reach - makes a silent rewrite impossible even unkeyed.

With neither, this detects corruption and careless edits and nothing more.
`echo-memory verify` says which of the two is in force rather than implying a
guarantee that is not there, and the `keyed` column is on every row so a
scope that was attested unkeyed cannot later be described as keyed.

It also does not authenticate the author. `agent_id` is still a string the
writer supplies, so the attestation proves the recorded author has not been
changed since the write, not that it was true at the write.

Idempotent, because init-db re-applies every migration on every run.
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ag_catalog on the search_path FIRST, because `graphid` below is an
    # ag_catalog type and Alembic's connection does not put it there.
    #
    # This migration used to inherit the setting from an earlier one in the
    # same Alembic session, which is why nothing caught it: CI and every test
    # migrate from base, so 0026's `SET` was always still in force by the time
    # this ran. A deployment already sitting at 0027, 0028 or 0029 starts a
    # fresh session here, 0026 never executes, and this failed with
    # `type "graphid" does not exist`. Measured against a restore of the live
    # store, which was at 0028.
    #
    # So every migration that names an ag_catalog type sets this itself. It is
    # session-local and idempotent, so paying for it twice costs nothing, and
    # `tests/unit/test_migration_search_path.py` fails when a new migration
    # forgets.
    op.execute('SET search_path = ag_catalog, "$user", public')
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.fact_attestation (
            id          bigserial PRIMARY KEY,
            -- graphid, like every other edge reference in this schema. The id
            -- columns are not one type across it and a mismatch is silently
            -- false for every row rather than an error.
            edge_id     graphid NOT NULL,
            group_id    text NOT NULL,
            -- Position in this scope's chain. Per scope rather than global
            -- because scopes are tenants: one account's writes must not be
            -- able to perturb another account's chain, and a global sequence
            -- would mean every verification depends on every tenant.
            seq         bigint NOT NULL,
            prev_digest text NOT NULL,
            digest      text NOT NULL,
            -- Whether a key was in force. A scope attested unkeyed cannot
            -- later be described as keyed, and the difference is the whole
            -- difference between tamper-evident and tamper-proof.
            keyed       boolean NOT NULL,
            -- When the digest was computed, which is NOT when the fact was
            -- written. A fact attested long after the fact has a baseline
            -- from that moment and says nothing about the interval before it.
            attested_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One attestation per fact, so a second row cannot quietly replace the
    # digest a verification would have checked.
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS fact_attestation_edge_idx
             ON public.fact_attestation (edge_id)"""
    )
    # One link per position. Two facts claiming the same place in the chain is
    # a fork, and a fork means verification can pick the branch that passes.
    # Concurrent writes to one scope serialise on this.
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS fact_attestation_seq_idx
             ON public.fact_attestation (group_id, seq)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.fact_attestation")
