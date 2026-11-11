"""reach a scope's decisions and outcomes without scanning its nodes

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-11

No new structure. A decision IS a node, an outcome IS a node, and "this
decision led to that outcome" IS a FACT edge with a causal_hint - so the whole
of the learning layer's storage is a vocabulary agreement over what this schema
already holds. `trace_cause` walks those chains today, supersession retires an
outcome that turns out wrong rather than deleting it, erasure reaches it, and
attestation covers it. None of that had to be written twice.

What the agreement needs from the database is an index. `lessons.candidates`
asks "every decision in this scope", which without one is a sequential scan of
every node the scope has ever had - and the node table is the one that grows
with the customer rather than with the feature.

## `Node.type` stays free text, and that is not a gap

Nothing here constrains `type`. Nothing could: the server does not call a
model and cannot read a sentence, so it has no way to tell a node whose type
says `decision` from one that is actually a decision. A CHECK constraint on
`type` would not add that ability - it would only move the failure, from "a
caller used the word loosely" to "a caller's existing nodes are refused at
write time".

That second failure is the expensive one. Customers already have nodes with
types this project never chose, written before any of this vocabulary existed.
Rejecting them to protect a convention the server cannot enforce anyway would
break working stores for nothing.

So the reserved types are a convention the intelligence layer reads, and the
consequence of using one loosely is documented rather than prevented: a node
typed `decision` that is not one will turn up in lesson candidates. See
intelligence/vocabulary.py, which says the same thing from the reading end.

## Three partial indexes rather than one

Each node has exactly one type, so three indexes partial on one type each hold
the same number of entries in total as a single index partial on all three -
there is no space to save by combining them. What the split buys is that the
planner never has to prove anything: `type = 'decision'` matches the predicate
of the decision index exactly, where against `type IN (...)` it would depend
on predicate implication working out. An index that is used only when the
planner is clever enough is an index whose benefit disappears on an upgrade.

The agtype cast idiom is 0001's and 0013's, unchanged: `properties ->> '"key"'`
with the KEY cast to agtype, and the whole expression parenthesised. A bare SQL
string for the key does not error, it silently matches nothing.

The type list is imported from `vocabulary` rather than written out here, so
the indexes and the words the reader looks for cannot drift apart. A second
copy of a derivation has gone blind in this codebase twice already; migration
0016 imports from the package for the same reason.
"""

from alembic import op

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.intelligence import vocabulary

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def prop(key: str) -> str:
    return f"(properties ->> '\"{key}\"'::agtype)"


def upgrade() -> None:
    # graphid and agtype live in ag_catalog, so the extension has to be loaded
    # and on the path before an expression mentioning either can be parsed.
    # Everything after this SET must be qualified public.* or it lands in
    # ag_catalog; see 0001's note.
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')

    # Sorted so the DDL this emits is the same on every run, which matters
    # because init-db re-applies every migration and a diff between two runs
    # is a thing somebody has to explain.
    for node_type in sorted(vocabulary.RESERVED_NODE_TYPES):
        op.execute(
            f'CREATE INDEX IF NOT EXISTS node_{node_type}_group_idx '
            f'ON {GRAPH}."Node" (({prop("group_id")})) '
            f"WHERE {prop('type')} = '{node_type}'"
        )


def downgrade() -> None:
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')
    for node_type in sorted(vocabulary.RESERVED_NODE_TYPES):
        op.execute(f"DROP INDEX IF EXISTS {GRAPH}.node_{node_type}_group_idx")
