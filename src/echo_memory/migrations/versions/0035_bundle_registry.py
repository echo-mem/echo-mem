"""one row per knowledge bundle that left this deployment or arrived in it

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-11

Capability F is cross-organisation exchange, and the shape it takes here is a
signed file somebody carries from one deployment to another. Not a federation
API: there is no endpoint one store calls on another, no shared trust root and
no live protocol to get wrong. One direction, one file, one operator at each
end.

That design leaves exactly one thing that has to be durable, and it is this
table. Everything else about a bundle is in the file.

## Why a registry at all

Three questions have no answer without it, and each of them is a real failure.

**Has this bundle already been imported?** A bundle is a file, and a file gets
replayed - re-run from a shell history, re-imported from a second copy, or sent
twice by a well-meaning partner. Without a record of `bundle_id` the second
import silently doubles every fact, and because the facts land as new local
nodes there is nothing afterwards that can tell the two copies apart. The
PRIMARY KEY on `bundle_id` is the replay refusal.

**Is this scope allowed to be read yet?** Imported facts land in a quarantine
scope and ordinary retrieval is never given it. What promotes them is a row
here moving to 'promoted', naming the verifier who did it. Keeping that state
on the facts themselves would mean a bulk property write per promotion and no
record of who decided.

**What left, and to whom?** An export hands data to another organisation and
cannot be undone. The exporter's own record of which scope, which redaction
policy, how many facts and which peer is the only thing that can answer
"what did we send them" six months later.

## graphid is deliberately absent

No column here references a fact. An AGE `graphid` is unique per DATABASE, not
globally, so two deployments will issue the same id for unrelated facts - and
a bundle carrying origin graphids into a table keyed on graphid would collide
the moment a second partner sent one. The origin id survives as a string on
each imported fact's `provenance.origin`, qualified by the deployment that
issued it, where no join can mistake it for a local id.

## CHECK, not ENUM

`direction` and `state` are CHECK constraints on text. 0026 records the reason
at length: a Postgres enum cannot have a value removed, so a label added by a
migration is permanent, and `audit_mutation_type` is now carrying two values
only because dropping them would have meant rewriting an append-only log. A
CHECK is one ALTER away from saying something different.

Idempotent, because init-db re-applies every migration on every run.
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.bundle_registry (
            -- Chosen by the exporter and carried in the manifest. The primary
            -- key is what makes a replay a refusal rather than a double
            -- import: there is no other field a second copy of the same file
            -- would differ in.
            bundle_id         text PRIMARY KEY,
            direction         text NOT NULL
                              CHECK (direction IN ('export', 'import')),
            -- For an import, the QUARANTINE scope the facts were written to.
            -- For an export, the source scope they were read from. One column
            -- rather than two because a row is only ever one of the two and a
            -- nullable pair invites reading the wrong one.
            group_id          text NOT NULL,
            -- Who the other end was, as the operator named them. Free text,
            -- and not trusted for anything: the signature is what identifies a
            -- peer, and these two columns are for a human reading the log.
            peer_deployment   text,
            peer_account      text,
            format_version    int NOT NULL,
            -- sha256 over the canonical bundle bytes. Recorded here as well as
            -- in the manifest so a later question about a bundle does not
            -- depend on still having the file.
            content_digest    text NOT NULL,
            -- Nullable because an unsigned bundle is a real thing an operator
            -- can produce, and recording it as unsigned is more honest than
            -- refusing to record it. An IMPORT of one is refused in code; this
            -- column exists so the refusal can say what was missing.
            signature_alg     text,
            signature_key_id  text,
            fact_count        int NOT NULL DEFAULT 0,
            redaction_policy_id text,
            -- 'created'      an export was built. Says nothing about whether
            --                it was delivered; this deployment cannot know.
            -- 'quarantined'  an import landed. Facts exist, nothing reads them.
            -- 'promoted'     a verifier accepted it. The scope becomes
            --                readable, and `detail` names who decided.
            -- 'revoked'      withdrawn after the fact. The scope stops being
            --                readable. It does NOT reach facts already handed
            --                to an agent; see importer.revoke.
            -- 'rejected'     a verifier refused it. Distinct from 'revoked':
            --                nothing was ever trusted, so nothing was withdrawn.
            state             text NOT NULL
                              CHECK (state IN ('created', 'quarantined',
                                               'promoted', 'revoked',
                                               'rejected')),
            created_at        timestamptz NOT NULL DEFAULT now(),
            promoted_at       timestamptz,
            revoked_at        timestamptz,
            -- The exporter's attestation head at build time. An attestation
            -- chain is per scope, so an imported fact cannot carry the
            -- origin's chain - the local chain it joins is this deployment's.
            -- What an importer can check is the BUNDLE against a head the
            -- exporter published somewhere this database cannot reach, which
            -- is the same external-witness argument 0030 makes.
            source_chain_head jsonb,
            detail            jsonb
        )
        """
    )
    # Every operator query is "what is waiting for review" or "what happened
    # recently", so the state leads and the time orders within it.
    op.execute(
        """CREATE INDEX IF NOT EXISTS bundle_registry_state_idx
             ON public.bundle_registry (state, created_at)"""
    )
    # readable_scopes asks this on every fan-out read: which import scopes for
    # this account are promoted. Without the index it is a sequential scan on
    # the read path.
    op.execute(
        """CREATE INDEX IF NOT EXISTS bundle_registry_group_idx
             ON public.bundle_registry (group_id)"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS bundle_registry_group_idx")
    op.execute("DROP INDEX IF EXISTS bundle_registry_state_idx")
    op.execute("DROP TABLE IF EXISTS public.bundle_registry")
