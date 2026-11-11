"""the inbox's idempotency namespace belongs to a scope, not to the deployment

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-12

Migration 0033 made `event_id` a global PRIMARY KEY and argued for it at
length: a CI system that times out cannot know whether its first call landed,
so `ON CONFLICT DO NOTHING` has to make a retry free, and a reporter that
builds the id from its own run gets that for nothing. All of that is right.
What it got wrong is the namespace the key lives in.

Every scope on a deployment shares one `event_id` space, and the envelope's
own docstring recommends deterministic ids derived from the reporter's run -
`deploy-{commit}-{env}`. Two customers deploying the same open source commit
is then an ordinary collision rather than a contrived one. Measured on a real
store:

    scope A enqueues 'deploy-abc1234'  -> accepted=True
    scope B enqueues 'deploy-abc1234'  -> accepted=False, duplicate=True

`duplicate: true` is the answer that tells a reporter to STOP retrying, the
surviving row keeps group_id=A, and B's event is never applied and B is never
told. One tenant can silence another tenant's reporting, and the refusal is
itself a signal that the other tenant used that id.

So the key becomes `(group_id, event_id)`. Two scopes may each hold the same
id; a replay WITHIN one scope is still the refusal 0033 built it to be, which
is the half that must not be lost.

## Safe on a populated table, and why that is not an assumption

A widening. The old key was unique over `event_id` alone, so no two existing
rows can agree on `event_id`, and therefore no two can agree on
`(group_id, event_id)` either - the new constraint is implied by the old one.
There cannot be a duplicate for the ADD to fail on, whatever is in the table.
Both tables are young enough that any real store is probably empty, but this
holds either way and is the reason it does not have to be checked first.

The downgrade is NOT symmetric and this is the honest statement: once a store
has used the new freedom, two rows will share an `event_id` across scopes and
re-adding the narrow key fails on them. That failure is correct. The
alternative would be deleting one tenant's accepted events to make a schema
change fit, which is not something a migration gets to decide.

## Indexes

`event_inbox_group_idx` on (group_id, received_at) is left alone. It orders
within a scope by arrival, which the new key does not, and the operator query
it serves - "what has this scope sent recently" - is unchanged.

Nothing references `event_inbox.event_id`, so there is no foreign key to
rebuild. `erasure` touches this table by `applied_edge_ids` and never by id.

Idempotent, because init-db re-applies every migration on every run: the drop
names the constraint IF EXISTS and the add re-creates it, so a second pass
lands on the same shape.
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.event_inbox DROP CONSTRAINT IF EXISTS event_inbox_pkey")
    op.execute(
        """ALTER TABLE public.event_inbox
             ADD CONSTRAINT event_inbox_pkey PRIMARY KEY (group_id, event_id)"""
    )


def downgrade() -> None:
    """Narrow the key back, which fails if any scope has used the widening.

    Deliberately left to fail rather than made to work. Two rows sharing an
    `event_id` across two scopes are two tenants' accepted events, and the
    only way to re-add the narrow key is to delete one of them. A downgrade
    that silently picked a tenant to lose would be worse than one that stops
    and says the schema no longer fits the data.
    """
    op.execute("ALTER TABLE public.event_inbox DROP CONSTRAINT IF EXISTS event_inbox_pkey")
    op.execute(
        """ALTER TABLE public.event_inbox
             ADD CONSTRAINT event_inbox_pkey PRIMARY KEY (event_id)"""
    )
