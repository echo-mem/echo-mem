"""a bundle's replay refusal belongs to the scope that imported it

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-12

Migration 0035 made `bundle_id` a global PRIMARY KEY and called it "the replay
refusal". The refusal is real and stays. What was wrong is that it was
deployment wide, so it answered a question no tenant asked.

Measured on a real store: account `acct-ours` imports a bundle, then account
`acct-theirs` importing the SAME file is refused with

    bundle '<id>' was already imported here on <timestamp> and is 'quarantined'

which hands a tenant the other tenant's import timestamp, direction and trust
state for a bundle it cannot otherwise see exists. Two consequences, and the
second is the one a customer hits first:

  - **Disclosure.** The refusal is a read of another account's registry row.
  - **Function.** A partner cannot send one bundle to two accounts on a shared
    deployment, and one account cannot import a partner's bundle into two of
    its own memories, which is an ordinary thing to want.

So the key becomes `(group_id, bundle_id)`. For an import, `group_id` is the
per-bundle quarantine scope, so the pair is the account's own namespace and
the replay check in `importer.import_bundle` looks up the exact row rather
than scanning the deployment.

Note what this does NOT loosen: the same memory still cannot hold one bundle
twice. `quarantine_group_id` derives the same scope string from the same
(account, memory, bundle_id), so a second import into the same memory
produces the same pair and hits the same key. There is a test for each half,
kept apart on purpose so that fixing the second cannot quietly lose the first.

## Safe on a populated table

A widening, by the same argument as 0037. The old key was unique over
`bundle_id` alone, so no two existing rows agree on it, so none can agree on
`(group_id, bundle_id)`. The new constraint is implied by the old one and the
ADD cannot find a duplicate to fail on.

The downgrade is not symmetric and that is the honest statement: a store that
has used the new freedom has two rows sharing a `bundle_id`, and re-adding the
narrow key fails on them. The alternative is deleting one account's import
record to make a schema change fit, which would also orphan the facts that row
is the only proof of.

## Indexes

`bundle_registry_group_idx` on (group_id) is now a strict prefix of the
primary key's index and is therefore redundant - `readable_scopes` filters on
group_id with a prefix match and the PK index serves that at least as well.
Dropped, because a redundant index is write amplification nobody is reading,
and recreated on the downgrade so the two directions agree.

Idempotent, because init-db re-applies every migration on every run.
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.bundle_registry DROP CONSTRAINT IF EXISTS bundle_registry_pkey"
    )
    op.execute(
        """ALTER TABLE public.bundle_registry
             ADD CONSTRAINT bundle_registry_pkey PRIMARY KEY (group_id, bundle_id)"""
    )
    op.execute("DROP INDEX IF EXISTS public.bundle_registry_group_idx")


def downgrade() -> None:
    """Narrow the key back, which fails if two scopes have shared a bundle_id.

    Left to fail for the reason 0037's downgrade is: the only way to re-add
    the narrow key over two accounts' rows is to delete one account's import
    record, and a migration does not get to choose which tenant loses theirs.
    """
    op.execute(
        """CREATE INDEX IF NOT EXISTS bundle_registry_group_idx
             ON public.bundle_registry (group_id)"""
    )
    op.execute(
        "ALTER TABLE public.bundle_registry DROP CONSTRAINT IF EXISTS bundle_registry_pkey"
    )
    op.execute(
        """ALTER TABLE public.bundle_registry
             ADD CONSTRAINT bundle_registry_pkey PRIMARY KEY (bundle_id)"""
    )
