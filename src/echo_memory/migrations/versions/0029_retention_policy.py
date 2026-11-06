"""how long a scope may keep a fact, and who said so

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-06

Erasure answers a subject who asks to be forgotten. This answers the other half
of the same obligation: GDPR Article 5(1)(e) and the DPDP Act both require that
data is not kept longer than the purpose needs, which is a standing rule rather
than a request. A store with erasure and no retention can delete on demand and
cannot say why it still holds anything.

Nothing here deletes. A policy is a STATEMENT - this scope keeps facts for N
days, for this reason, decided by this person on this date - and enforcement is
a separate deliberate act that goes through `erasure.erase`, so retention
deletes exactly the way a subject request does and leaves the same evidence in
erasure_event. A second deletion path would be a second place to get orphans
and versions wrong.

Why a policy carries a reason and an author. "We delete after 90 days" is not a
compliance answer; "we delete after 90 days because the support transcripts
this scope holds are only needed for the dispute window, decided by X on Y" is.
The reason is the part an auditor reads and the part nobody can reconstruct
later, so the column is NOT NULL.

Why max_age_days and not a cron expression. The question a reviewer asks is
"how long do you keep it", and a duration answers that directly while a
schedule answers when the machine runs. When enforcement runs is an operational
detail and belongs in whatever invokes it.

Idempotent, because init-db re-applies every migration on every run.
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.retention_policy (
            id           bigserial PRIMARY KEY,
            group_id     text NOT NULL,
            -- How long a fact may be kept, from the instant it became true.
            -- A CHECK rather than application validation: a zero or negative
            -- retention means "delete everything immediately", which is never
            -- what somebody meant to type.
            max_age_days integer NOT NULL CHECK (max_age_days > 0),
            -- What an auditor reads. Not nullable on purpose: a policy with no
            -- stated purpose cannot answer the question it exists to answer.
            reason       text NOT NULL,
            -- Who decided. A policy is a decision by a person, and a
            -- compliance record naming no decider names nobody accountable.
            decided_by   text NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            -- Withdrawn rather than deleted, so "why did we stop deleting
            -- after 90 days" stays answerable.
            retired_at   timestamptz
        )
        """
    )
    # The lookup every enforcement run makes: the live policies for one scope.
    op.execute(
        """CREATE INDEX IF NOT EXISTS retention_policy_live_idx
             ON public.retention_policy (group_id)
          WHERE retired_at IS NULL"""
    )
    # One live policy per scope. Two policies disagreeing about how long a
    # scope keeps data is not a configuration, it is a question with no
    # answer - and the shorter one silently winning is the kind of behaviour
    # nobody chose. A second policy has to retire the first.
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS retention_policy_one_live_idx
             ON public.retention_policy (group_id)
          WHERE retired_at IS NULL"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.retention_policy")
