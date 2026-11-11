"""no index is needed, and here is what was checked

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-11

A deliberate no-op. This stream added a graph wide advisory lock and an in
process metrics registry, neither of which touches a table, so it introduced
no query that wants an index. Rather than leave the slot empty, this records
what was looked at, because "we checked" is only worth something if somebody
can see what was checked.

## Every table was listed against its indexes

Nine tables have no index beyond their primary key: bootstrap_state,
eval_judgement, eval_run, group_state, lexical_scope, lexical_term,
project_analysis, schema_moment, stop_gate_fired. Each was looked at against
how it is actually queried, and in every case the primary key is the access
path:

- `group_state` and `lexical_scope` are read by group_id, which IS the key.
- `lexical_term`'s key is (group_id, lexeme). The BM25 join matches on both
  and `refresh` deletes by group_id, which the key's leading column serves.
- `stop_gate_fired` is read by session_id, the key.
- The eval and analysis tables are harness state, not a read path.

The attestation table that migration 0030 added is covered by its own two
indexes: `head()` is group_id equality with an ORDER BY seq DESC LIMIT 1, and
`fact_attestation_seq_idx` on (group_id, seq) is exactly that shape.

## The one real gap cannot be fixed with an index, which is why this is empty

`attestation.claims_of` reads one fact by id, and it is on the write path:
`attest()` calls it for every fact written. It asks through Cypher,

    MATCH (a)-[e:FACT]->(b) WHERE id(e) = $eid AND e.group_id = $gid

and AGE expands the match before filtering, so the plan is a sequential scan
of FACT. Migration 0013's own test says this about a different query, and the
same thing is true here. Measured on 2026-11-11 at 3,000 edges, 1,500 of them
retired:

    plan for claims_of                         Seq Scan on "FACT"   3.75 ms
    plan with an unconditional id index added  Seq Scan on "FACT"   3.74 ms
    the same read against the table directly   Index Only Scan      0.026 ms

Adding the index changes neither the plan nor the time, because the planner
never gets to see a predicate it could use one for. The partial index 0013
already has, `fact_active_id_idx ... WHERE t_invalid IS NULL`, is no help for
the same reason, and would not cover a retired fact even if it were.

And it grows, because a sequential scan must: 3.75 ms at 3,000 edges and
9.49 ms at 8,917, on the write path, once per fact written. That is the shape
migration 0024 describes - "write cost rose from 29ms at 1,057 nodes to 129ms
at 24,054... read as the corpus being large rather than as a defect".

140x is available and the only way to collect it is to ask the question
differently: read the edge table with per-property `->>` projection, the way
migration 0013's test and this package's own hot read paths already do.

That is a change to `attestation.py`, not to the schema, and it is reported
rather than made here. There is a second reason to want it: `->>` returns
real text, where the Cypher path returns agtype whose JSON escapes the current
reader does not decode.

That second reason is also why the change is not a drop-in, and whoever takes
it needs to know before they start. `claims_of` feeds `canonical()`, which
feeds the digest. `_value` currently does `json.loads(str(raw))`, which is
correct for the quoted agtype Cypher returns and raises on the bare text
`->>` returns. So the projection cannot move without `_value` moving with it,
and the moment a covered property decodes to a different Python value the
digest over it changes - for every fact already attested, in every store,
including customers'. The chain would report the whole scope as altered.

So the work is: change the read, change the decode, and then prove on a
populated store that `report()` is still clean and `head()` has not moved.
`tests/integration/test_trust.py` already has the shape of that assertion
for the verification chain. Without that proof this is not a performance fix,
it is a silent integrity event.

An index that does not change a plan is worse than no index. It costs write
amplification and space, and it leaves a comment claiming a query is fast.
"""

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
