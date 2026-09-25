# What else belongs in echo-mem

Written 2026-09-25, against the store as it actually is rather than as the
README describes it. Every claim below was checked in the code, and where a
number appears it was measured. Anything speculative says so.

The question this answers: what would make echo-mem better for **every**
product that uses it, not for one customer's shape of data.

## What it already does

Storage is a bi-temporal property graph with an audit entry per mutation, one
node per name per scope enforced by the database, and a tenancy boundary that
is a single derived string. Retrieval is pgvector and Postgres full text
search fused with reciprocal rank fusion. The write path calls no LLM, and
what that does and does not buy is argued out in docs/WRITE-COST.md.

Causality is now typed and traversable (`causal_hint`, `trace_cause`), which
was the one thing in the design doc's v1b list with no substitute.

The rest of this document is what is missing.

## Tier 1: the store already pays for these and cannot use them

### As-of reads

Every fact carries `t_valid` and `t_invalid`. The read path filters
`t_invalid IS NULL` and has never done anything else with either column. So
the store carries the full history of what it believed, at the cost of
storing it, and cannot answer a single question about it.

"What did we believe about the deploy process on 3 September" is the question
a post mortem asks first, and it is one predicate away.

- **What it is:** `as_of` on `query_memory`: `t_valid <= as_of AND (t_invalid
  IS NULL OR t_invalid > as_of)`.
- **Cost:** small. One filter in three candidate queries; the indexes exist.
- **How we would know it worked:** supersede a fact, then retrieve both the
  current and the prior answer from the same scope by date.

### Entity resolution inside the batch

`resolution.py` documents this as a known v1a limitation: each entity is
checked against nodes already in the database, never against the other
entities in the same call. Two near-duplicate new names in one
`write_episode` both resolve as new and create two nodes. A later call
mentioning either resolves correctly, so the damage is exactly one duplicate
pair per batch that contains one.

This is the duplicate bar the trial counts, reached by the one path that
skips entity resolution.

- **What it is:** embed the batch's names once (already done, `_prefetch_names`),
  compare them to each other before creating any node, and route a pair over
  the low threshold into the same `ambiguous_entities` round trip that
  database matches use.
- **Cost:** small. The embeddings are already in hand.
- **How we would know:** one call naming "Postgres" and "PostgreSQL", neither
  in the store, returns one ambiguity instead of creating two nodes.

### Say so when a new fact contradicts an old one

Supersession is keyed on `(source, target, relation_type)`: the same triple
written again replaces the old one. A fact that contradicts an existing fact
through a *different* triple is stored quietly beside it, and both come back
in the same query, ranked by similarity, with nothing saying they disagree.

`contradicts` exists as a `causal_hint` and `trace_cause` reports it, but only
when a caller thought to record it. Nothing detects one.

- **What it is:** at write time, the neighbourhood is already fetched for
  `related_entities`. Surface the facts in it whose entities match and whose
  claim the new fact displaces, as `conflicts_with` on the response. Report
  only; never resolve, because resolving means choosing, and choosing without
  being asked is how a store loses the fact that was right.
- **Cost:** medium, and the risk is precision. A noisy `conflicts_with` is
  worse than none.
- **How we would know:** a labelled set of contradicting pairs with a stated
  precision bar, the same discipline `calibrate` already applies to the merge
  threshold. No shipping on a hunch.

## Tier 2: worth building, bigger

### Consolidation

Designed, unbuilt, and the README had to stop claiming it in the present
tense (PR #89). `tests/unit/test_consolidation_invariants.py` is the
acceptance criteria, contributed by a reader who believed the claim and built
a validation harness for a system that was not there. The objection those
tests encode is the right one: traceability is not preservation. An edge from
a summary back to its facts says where the summary came from, not that it
still says what they said.

- **Cost:** large, and it is the only item here that needs an LLM at write
  time or on a schedule, which changes the cost model.
- **How we would know:** the file above, which already exists and mostly
  skips.

### Rank on what has actually been recalled

Read events have recorded which fact ids a read returned since migration
0021. Retrieval never looks at them. A fact recalled forty times and a fact
recalled never rank identically on similarity alone.

- **What it is:** a fourth signal in the fusion, or a small multiplier on the
  fused score. Fusion is the honest place for it: it stays one vote among
  several rather than a feedback loop that buries anything not yet found.
- **Cost:** small to medium.
- **How we would know:** the existing eval harness, ablated one change at a
  time, the same way the lexical channel and the adaptive floor were
  measured. It ships only if MRR moves and the interval clears zero.

### Cross-encoder reranking

In the design doc's v1b list. Cosine similarity is a bi-encoder score: query
and fact are embedded separately and never compared together. Rerank the
fused top 50 with a model that sees both at once, then return top K.

- **Cost:** medium, plus real per-query latency, which matters because the
  read path is currently fast enough that nobody thinks about it.
- **How we would know:** the eval harness. Same bar.

## Tier 3: product shape rather than engine

- **Retention per memory.** Nothing ever deletes. For a metered service that
  is an unbounded liability on both sides: our storage and a customer's
  exposure. A per-memory retention window, applied by demoting rather than
  dropping, is the same shape consolidation needs.
- **A change feed.** Products that cache anything derived from memory have no
  way to know when it moved. Speculative: no customer has asked.
- **Entity type vocabulary per memory.** `type` is free text today. A store
  that declares its own vocabulary gives resolution a cheap prior, and gives a
  graph view something to colour by.

## What we should not add

- **Statistical causal discovery.** The design doc's standing rule, and the
  reason `causal_hint` is asserted rather than inferred. A guessed cause is
  indistinguishable from a real one once stored, which makes the field worth
  less than not having it.
- **Silent merging.** Off since 2026-09-13, on evidence: precision at the
  0.92 bar was 50% over two reviewed pairs, and the unattended path had fired
  exactly once in the store's history. It bought almost nothing and was the
  only way two entities could be joined with nobody watching.
- **A bulk write endpoint.** `write_episode` already accepts 50 entities and
  200 facts per call. The 26,633-fact load that prompted this review sent one
  call per fact. That is a client and documentation problem, and a second
  endpoint would not fix it.
- **Moving `LOW_THRESHOLD` for everybody.** The calibration is AUC 0.766 with
  a 95% interval of [0.437, 0.968] over 5 positives. The interval includes
  chance. `ECHO_MEMORY_RESOLUTION_LOW` lets a store that knows its own names
  raise the bar; retuning the default on five positives would be fitting
  noise.

## Order

As-of reads and in-batch resolution first: both are small, both are things
the store already pays for and cannot use, and neither needs a judgement call
about precision. Contradiction surfacing next, gated on a labelled set.
Consolidation after that, because it is the only one that changes the cost
model and it has its acceptance criteria written already.
