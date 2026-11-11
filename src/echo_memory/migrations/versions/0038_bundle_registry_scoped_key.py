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

## The downgrade is lossy, by the same argument 0037 makes at length

A store that has used the new freedom has two rows sharing a `bundle_id`, and
a unique index over duplicates does not exist, so the downgrade DELETES the
later one. What that destroys is one account's record of an import - and the
facts of that import stay on the graph in a quarantine scope whose registry
row has gone, which is the orphaned-scope state `import_bundle` puts the
registry INSERT inside its transaction to avoid.

Refusing instead would make `downgrade base` impossible on any store that had
used the feature, while the revision immediately below - 0035's own downgrade
- is `DROP TABLE public.bundle_registry`, which orphans every import scope.
Declining to delete one row above that is inconsistent rather than careful.

A downgrade of this table is destructive at any depth. Take a dump first.

## Indexes, and a claim of 0035's that was not true

`bundle_registry_group_idx` on (group_id) is dropped. It is now a strict
prefix of the primary key's index, so it cannot answer anything the key
cannot - but the reason it never earned its place is older than this
migration. 0035 justified it as "readable_scopes asks this on every fan-out
read ... without the index it is a sequential scan on the read path". That is
wrong, and the planner says so. `readable_scopes` filters with
`starts_with(group_id, prefix)`, which is a function call and not an
indexable predicate, so measured on 5,000 rows with BOTH indexes present:

    Seq Scan on bundle_registry
      Filter: (starts_with(group_id, 'acct:a1:...:import:') AND ...)

The index was never used by the query it was created for. Dropping it loses
nothing and stops write-amplifying every registry INSERT for a plan nobody
gets. Recreated on the downgrade so the two directions agree.

The sequential scan itself is real and is NOT addressed here: it is a read
path cost that wants either an indexable prefix form or a different predicate,
which is a change to `readable_scopes` rather than to its schema.

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
    """Narrow the key back, destroying the rows that cannot fit under it.

    `created_at` picks the survivor, with `group_id` as the tiebreak, so that
    two rows written in the same microsecond still give one deterministic
    winner. The earliest import is kept for the same reason 0037 keeps the
    earliest event: it is the one whose facts have been readable longest, so
    it is the one most likely to have been acted on.
    """
    op.execute(
        """DELETE FROM public.bundle_registry b
            WHERE EXISTS (
                SELECT 1 FROM public.bundle_registry k
                 WHERE k.bundle_id = b.bundle_id
                   AND (k.created_at, k.group_id) < (b.created_at, b.group_id)
            )"""
    )
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
