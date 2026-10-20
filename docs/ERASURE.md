# Erasure

Design record for `echo-memory forget`, written before the code and corrected
against it. Erasure is the one operation that deliberately breaks the
bi-temporal guarantee the rest of the store is built on, so the reasoning
belongs somewhere a reader can argue with it.

Written 2026-10-20 and shipped the same day: `src/echo_memory/erasure.py`,
migration `0028_erasure_events`, `tests/integration/test_erasure.py`. The
hosted side is `echo-cloud erase`, in echo-mem-cloud, which calls this module
rather than reimplementing it.

Where the implementation disagreed with the plan, this document has been
updated to say what was built and why, rather than left as a record of what
was expected. Those places are marked.

## Why this is needed

GDPR Article 17 and India's DPDP Act 2023 both require actual deletion of a
data subject's personal data on request. DPDP applies to Ateleir Tech
directly, not by extension.

There is no deletion path in the store today. `echo-memory --help` lists 34
subcommands and none of them delete anything: the words delete, forget, purge,
erase, remove and drop do not appear in it. `merge` folds one node into
another and `reattribute` rewrites a property, but nothing removes content.
The model supersedes instead: a fact that stops being true gets `t_invalid`
set and stays on the graph, which is correct for memory and insufficient for
law.

## Supersession is not erasure

A request to delete a fact, satisfied by setting `t_invalid`, leaves the text
in seven places. All seven are real tables in the current schema:

| Where | What survives | Column |
|---|---|---|
| `echo_memory."FACT"` | the fact text itself, every superseded version | edge properties |
| `echo_memory."Node"` | entity name, type, aliases | node properties |
| `public.audit_entry` | the text before and after every mutation | `before_fact`, `after_fact`, `summary`, `resolution_detail` |
| `public.causal_hint_scan` | a verbatim quote from the fact | `quote`, `hint` |
| `public.lexical_term` | every lexeme of the fact, as searchable rows | `lexeme` |
| `public.fact_embedding` | a 384 dimension vector of the fact | `embedding` |
| `public.node_embedding` | a vector of the entity name | `embedding` |

The embeddings matter more than they look. Embedding inversion reconstructs a
meaningful fraction of source text from a MiniLM vector, so leaving the vector
while deleting the row it describes is not erasure, it is obfuscation.

`lexical_term` is the one most likely to be missed: it holds the actual
lexemes, so the vocabulary of an erased fact stays queryable even after the
fact is gone.

## Three verbs, and the one that cannot be complete

```
echo-memory forget --fact <edge_id>      exact, complete
echo-memory forget --entity <name>       complete for structural membership
echo-memory forget --whole-scope         complete
echo-memory forget --search <term>       candidates only, never automatic
```

`--whole-scope` is not `--scope`, which is already a global flag selecting
which scope to act in. A command where `--scope` means two things is a command
that erases the wrong graph one day.

`--entity` removes the node, its aliases, every edge incident to it in every
version, and every derived row above. That is complete for anything the graph
knows structurally.

It is **not** complete for a person named inside the text of a fact whose two
endpoints are other entities. "Ayush approved the Razorpay change" is an edge
between an approval and Razorpay; erasing the node `Ayush` does not touch it.

Solving that needs a text scan, and a text scan cannot be trusted to act on
its own: a false positive silently destroys a fact nobody asked to erase, and
that is unrecoverable. So `--search` prints candidates with their ids and
exits. A human passes the ones that match to `--fact`. This limitation is
stated in the DPA rather than hidden: structural erasure is guaranteed, textual
mention erasure is best effort with human confirmation.

## Decisions

**1. Hard delete, not tombstones.** Rows are removed with `DELETE`, not nulled
in place. A tombstone that keeps the row keeps the shape of what was there, and
a nulled text column in a table someone later adds a trigger to is a leak
waiting to happen.

Postgres does not zero the page on `DELETE`; the tuple survives until `VACUUM`
and the change survives in WAL. Erasure therefore issues `VACUUM` on the
affected tables and the operation is documented as complete for the live
database at that point. Backups are handled separately, below.

**2. All versions of a triple, or none.** Edge identity is
`(source, target, relation_type)`. Erasing version 3 while versions 1 and 2
remain leaves a supersession chain with a hole, which reads as corruption and
tells a reader exactly where something was removed. `--fact` on any version
erases every version of that triple in that scope.

**3. Drop lexical statistics, do not decrement them.** Correct decrementing
requires re-tokenising the text that is about to be destroyed, getting the
arithmetic right, and deleting any `lexical_term` row that reaches zero, or the
lexeme lingers as evidence of an erased fact's vocabulary.

*Changed in implementation:* the plan was to mark the scope stale and let
`bm25.refresh` rebuild. Deleting the scope's `lexical_term` and
`lexical_scope` rows outright is strictly better and simpler, because a null
reads as stale to both readers anyway, so the next write rebuilds from the
survivors. The intermediate state has no statistics rather than wrong ones,
and there is no window in which an orphan lexeme exists.

**4. Strip ids from arrays, keep the rows.** `read_event.returned_fact_ids`,
`eval_run.returned`, `audit_entry.affected_edge_ids` and
`trial_observation.node_ids` are arrays of ids, not content. The rows are
evidence that a read or an evaluation happened, which the unit meter and
criterion 6 both depend on. Use `array_remove`: keep the event, drop the
pointer.

The exception is `trial_observation.note`, which is free text an agent wrote
and may quote a fact. Where an observation's `node_ids` intersected the erased
set, the note is nulled and the row kept. Criterion 6 keeps its counts and
loses its narrative; that is the right way round.

**5. Erasure refunds stored capacity.** The current behaviour, recorded as an
open question, is that a deleted fact does not return capacity because the
meter sums write events rather than counting the live store.

Resolve it as a refund. The plan's units are capacity, meaning how many facts
an account may hold, not an allowance for work performed. An account that holds
less should owe less. Charging for capacity a customer was legally obliged to
give up is punitive, and the obligation is not theirs to refuse.

The exploit this opens, writing to the ceiling and erasing to write again, is
theoretical at current volumes: the busiest observed month is 2,185 against a
1,500,000 ceiling. Add a rate limit when that stops being true, not before.
`--reason` is recorded as free text on the erasure event for the audit trail,
and deliberately does not gate the refund, because a reason enum that decides
money is a reason enum people lie to.

**6. Never an MCP tool, and not self serve either.** `forget` is reachable
from `echo-memory forget` and `echo-cloud erase`, and from nowhere else. A
test reads the tools the server actually publishes, so registering one later
fails loudly rather than quietly.

Exposing it over MCP would put irreversible deletion of a customer's whole
graph one prompt injection away from any agent holding a key.

*Narrower than planned:* there is no console action either. A self serve,
irreversible delete with no cooling off period is a support incident with a
legal deadline attached, and both GDPR and the DPDP Act allow 30 days to act
on a request rather than demanding it happen inside one click. A console path
should create a request an operator completes, not perform the deletion.

The standing rule that anything an agent does as part of a loop must be
reachable over MCP does not conflict with this: erasure answers a person's
legal request and is never a step in an agent's loop.

## It is one transaction, not an ordered sequence

This section said the opposite until the belief behind it was measured.

The plan was an ordered, resumable sequence whose every crash state was safe,
because the project records that a script mixing Cypher with ordinary SQL has
no transaction at all. That belief is mis-attributed. `infra.db.connect`
returns an autocommit connection, so nothing is in a transaction unless
something opens one, Cypher or not. Measured on 2026-10-20: inside an explicit
`with conn.transaction():`, both a direct `DELETE FROM echo_memory."FACT"` and
a Cypher `DELETE` followed by a raise roll back cleanly.

So erasure is atomic and the resumption machinery does not exist. The order
below is still the order the code runs in, because dependency order is worth
keeping readable, but no step depends on the previous one having committed.

```
1. derived content:  fact_embedding, node_embedding, causal_hint_scan,
                     eval_judgement
2. pointers:         array_remove from eval_run, read_event, audit_entry
3. audit text:       before_fact, after_fact, resolution_detail nulled;
                     summary replaced; rows kept
4. the content:      the AGE edges, then the nodes orphaned by them
5. statistics:       the scope's lexical_term and lexical_scope rows
6. the record:       erasure_event, status 'completed'
```

`VACUUM` runs after the transaction commits, because it cannot run inside one.
A failure there leaves the data logically erased and physically recoverable
from the raw files, so it is reported rather than swallowed.

`erasure_event.status` survives the simplification. A row is still written
'completed' inside the transaction rather than 'started' first, and the column
stays because a future multi-group erasure, which the cloud now does, is
several transactions and can genuinely stop halfway.

### Two things the schema decided, not the design

**audit_entry rows are redacted, never deleted.**
`trial_observation.audit_entry_id` is a `NO ACTION` foreign key, so deleting a
referenced row errors; and `summary` is `NOT NULL`, so it takes a fixed string
rather than a null. The row survives as evidence a mutation happened, with its
text gone and its ids stripped. This is better than the delete this document
originally specified: the count of mutations stays true.

**`eval_judgement.edge_id` is `text` where the others are `graphid`.** That is
the mismatch that reads as false for every row rather than raising, which is
how 191 of 191 embeddings were once deleted. Each query casts for the column
it is actually addressing.

## What survives: `erasure_event`

Proof that an erasure happened has to outlive the data, without describing it.

```sql
CREATE TABLE public.erasure_event (
    id              BIGSERIAL PRIMARY KEY,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT NOT NULL,          -- started | completed | failed
    group_id        TEXT NOT NULL,
    verb            TEXT NOT NULL,          -- fact | entity | scope
    subject_digest  TEXT,                   -- HMAC, never the name
    n_edges         INTEGER NOT NULL DEFAULT 0,
    n_nodes         INTEGER NOT NULL DEFAULT 0,
    n_audit_rows    INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    requested_by    TEXT NOT NULL,
    request_ref     TEXT                    -- the ticket, not its contents
);
```

No fact text, no entity name, no note. Storing the erased subject's name to
prove you erased it defeats the erasure.

`subject_digest` is `HMAC(server_key, normalised_name)`, which answers "did you
erase X" by recomputation without holding X. Names are low entropy, so a
compromised server key makes this a dictionary attack. That trade is accepted
and recorded here rather than left for someone to discover: the alternative is
storing the name, which is worse, or storing nothing, which makes the
compliance evidence unverifiable.

## Consequence: `as_of` stops being a faithful record

This is the part worth arguing about before any code is written.

The bi-temporal model exists so a question can be asked about what was believed
at a past instant. Erasure means an `as_of` query over a window before the
erasure now returns less than it would have, with no trace of why.

That is required by law and it is a real loss of a real guarantee. Hiding it
would be worse than the loss, so:

`as_of` queries whose window spans a recorded erasure instant in that scope
carry a line saying so, with the count and the date, drawn from
`erasure_event`. "This answer excludes 4 facts erased on 2026-11-02." The
time travel feature keeps its honesty without keeping the data.

The alternative, silently returning a thinner past, turns the store's most
defensible property into one that cannot be trusted. The whole reason to have
bi-temporality is that a reader can rely on it.

## Consequence: backups, and the restore hazard

Erasure is immediate for the live database and bounded for backups.

`~/Desktop/mypc/backups/echo-memory/` holds dumps. A dump taken before an
erasure still contains the erased data, and nothing in the erasure touches it.
Declare a 30 day backup retention in the DPA and state that erasure completes
for backups within it. This is standard and accepted; silently keeping dumps
forever is not.

The sharper hazard is restore. Restoring a dump taken before an erasure
resurrects the erased data, with no error and nothing to notice. So:

**Every restore must replay `erasure_event` afterwards.** The table is in the
dump too, so the events are available to replay. Put it in the runbook next to
the restore command, because an operator restoring a backup at 2am will not
derive this.

## Tests that have to exist

Not a wish list. Each of these corresponds to a way this design can be
implemented wrongly and still look correct.

1. After `--fact`, the text appears in none of the seven tables. Asserted by
   querying for the literal string across all of them, not by checking the
   edge is gone.
2. After `--entity`, every version of every incident edge is gone, and no
   supersession chain has a hole.
3. `lexical_term` contains no lexeme that occurred only in erased facts.
4. A `read_event` whose `returned_fact_ids` included an erased id keeps its
   row and its `n_facts`, with the id removed.
5. An `as_of` query spanning the erasure reports the exclusion.
6. Interrupting the sequence at each step and resuming produces the same end
   state as an uninterrupted run.
7. `stored_units` falls by the erased count.
8. `forget` is absent from the MCP tool list. A test that reads the registered
   tools, so adding it later fails loudly.
9. Restoring a pre erasure dump and replaying `erasure_event` leaves the data
   erased.

## What this does not do

- Textual mentions inside facts about other entities. `--search` surfaces
  candidates; a human decides.
- Anything about the cloud's own tables. `usage_event`, `account` and
  `member` are a separate erasure path in echo-mem-cloud and need their own
  decision record.
- Encryption. Crypto shredding, where discarding a key renders data
  unreadable, is a different mechanism with a different guarantee and is
  tracked with the BYOK work, not here.
