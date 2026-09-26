# The order to build in

Written 2026-09-26, after a customer's first weeks of real use and a round of
reading the 2026 literature. Every number here was measured on this system or
quoted from a named source. Where a claim is an author's rather than one this
repository has verified, it says so.

This document exists because the obvious order is wrong, and the reason it is
wrong is measurable.

## The principle

**Repair the inputs before adding layers on top of them.**

Retrieval here is two ranked lists fused by reciprocal rank fusion. RRF reads
**rank position only** - never the underlying score. So a channel whose
ordering is arbitrary hands the fusion arbitrary input, and everything
downstream inherits it. Adding a reranker on top of that reranks noise more
expensively.

One of the two channels is currently ordered by coin flip. That is where to
start, and it is not where anybody would start by instinct.

## Phase 0: the live defect

`score` was omitted when only full text search found a fact. A customer sorted
by score with nulls last, so lexically-found facts went to the back of the
list, a character budget truncated them, and the model invented a replacement
for the fact it lost. The invented figure contradicted data sitting in the
store.

Fixed: the number is computed rather than omitted, and the tool description
now says to truncate by `rank` - the fused result of every channel - rather
than by `score`, which is one input to it.

The general lesson is worth more than the fix: **a field that is sometimes
absent invites callers to sort by it**, and sorting by presence is never what
anybody means.

## Phase 1: give the lexical channel a real ranking function

### The evidence

From this repository's own comment in `_lexical_any_candidates`, measured on
324 real prompts:

> `ts_rank` has no IDF and no length normalisation, so scores collapse onto a
> few values and exact ties at the cut are the common case, not the exception:
> on 324 real prompts the rank-3 and rank-4 scores were identical in **59%** of
> them, and the hook injects three. Which fact a user sees was decided by
> Postgres scan order.

The tiebreak added at the time made that arbitrary ordering *reproducible*. It
did not make it *right*.

Postgres's own documentation of the gap, via Tiger Data's write-up of
`pg_textsearch`: `ts_rank` "does not account for corpus-wide statistics (IDF,
length normalisation, TF saturation)". BM25 supplies all three - IDF so rare
discriminating terms outweigh common ones, saturation so repetition cannot
game a score, length normalisation so a long fact is not punished for being
long.

Now compose that with the customer's own calibration - ten questions, every
fact judged against every question, not retrieval-biased:

| channel | P@1 | R@3 | MRR |
|---|---|---|---|
| lexical only | 0.700 | **0.640** | 0.812 |
| vector only | 0.800 | 0.460 | 0.800 |
| fused | 0.800 | 0.567 | 0.900 |

The channel being ranked by coin flip past rank 2 is **the channel with the
best recall**, and it beats the fused ranking outright on 2 of the 10
questions. This is not a small correction to a minor signal.

### Why in SQL, and not an extension

- `pg_search` (ParadeDB) is **AGPL-3.0**. This project is BSL 1.1 and ships a
  Postgres image customers run themselves. Adding AGPL to that image is a
  question nobody wants to answer on a sales call.
- `pg_textsearch` (Tiger Data) is memory-resident, 64MB per index by default,
  and young.
- A customer may bring their own Postgres, where neither extension exists.

BM25 needs two things Postgres can supply: per-term document frequency, and
document length. Both per scope, because IDF across tenants is meaningless.
IDF tolerates staleness, so a periodically refreshed materialised view is
enough and keeps it off the write path - which is the one thing about this
system that must not get more expensive.

### Acceptance

The existing harness (`src/echo_memory/eval/`, 219 cases, MRR with confidence
intervals) ablated one change at a time. Ships only if MRR moves and the
interval clears zero. Behind a flag until then.

### A correction

An earlier draft of this plan put an external benchmark first, on the grounds
that nothing could be decided without one. That was wrong: the harness above
already decides internal questions and is what measured the graph hop at
-0.142. An external benchmark (LongMemEval, LoCoMo) is needed for *comparable*
claims - Zep publishes LongMemEval numbers - and for catching overfitting to
one store. It runs in parallel. It does not block.

## Phase 2: make the graph earn its place in retrieval

Today the graph contributes nothing to `query_memory`. Retrieval is vector
plus full text, fused. The one-hop expansion is built and off, on evidence:

| shape | shipping | + hop | dMRR | 95% CI |
|---|---|---|---|---|
| entity_pair | 0.641 | 0.499 | -0.142 | [-0.182, -0.104] |
| entity_single | 0.690 | 0.591 | -0.099 | [-0.135, -0.062] |
| prose | 0.974 | 0.849 | -0.125 | [-0.158, -0.094] |
| multihop | 0.196 | 0.200 | +0.003 | [-0.022, +0.028] |

The received reading is "graph expansion hurts". That reading is incomplete.
The same experiment raised **multihop recall@10 from 0.635 to 0.769**: the hop
finds the second fact and ranks it badly. And the hop was *indiscriminate* -
it expanded to every neighbour of every seed, so on a question one fact
answers it flooded the pool.

Two consequences, in order.

### 2a. Route instead of choosing globally

Adaptive retrieval - classify the query, then pick the operator and evidence
budget - is the 2026 consensus (Adaptive RAG, PAGE-RAG). The routing signal
here needs no model: a query naming two entities that resolve to two different
nodes is the relational case; everything else is not. Expansion on, expansion
off, decided per query rather than per deployment.

### 2b. A causal channel, as a third ranked list

`causal_hint` edges only, seeded from what the content channels already found,
direction chosen by the question. This is the opposite of the generic hop: it
walks only edges a human deliberately typed, so it adds few candidates and
each one was asserted to be connected.

Fused through RRF like everything else, so it needs no new machinery and is
measurable by the same harness.

**The point of it**: vector search answers "what resembles this", BM25 answers
"what contains these words". Neither can answer "why" at any quality, because
the answer is a path and neither has a notion of paths. Competitors can match
recall. They cannot answer a question class their data model forecloses.

### 2c. The cold start, stated plainly

Production today: **38,479 edges, 2 with a causal hint, both written by a
smoke test.** The causal channel would be dead weight on every existing store.

Three ways out, and one is refused:

1. **Prompting at write time.** Shipped - `causal_hint` is in the tool
   description, the skill and the public docs. Slow, honest, compounds.
2. **An opt-in backfill.** `echo-memory infer-causal-hints` re-reads existing
   fact text with the caller's own model and writes hints as ordinary audited
   edges. This does not contradict the standing refusal below: refusing
   statistical causal discovery means refusing to infer causation from
   co-occurrence. Re-reading a sentence that already says "the pool was sized
   5 SO checkout returned 502s" and typing that edge is extraction - the same
   act the calling agent performs for entities and facts, done late. Offline,
   explicit, reversible, audited.
3. **Inferring from graph structure.** Refused. See below.

### 2d. What causality plus bi-temporality gives that nobody has

Causal chains over edges that carry `t_valid`/`t_invalid` answer "what did we
believe caused this, as of last Tuesday". That is the question a post mortem
opens with. Both halves are already stored and neither is used.

## Phase 3: the cheap things already paid for

### 3a. Salience from the read log

Migration 0021 has recorded which fact ids every read returned since
September. Retrieval has never looked at them. A fact recalled forty times and
one recalled never rank identically.

This is ACT-R's base-level activation - recency plus frequency - and the data
is already on disk. As a fourth RRF list it stays one vote among several
rather than a feedback loop that buries anything not yet found.

### 3b. As-of reads

Every fact carries `t_valid` and `t_invalid`. The read path filters
`t_invalid IS NULL` and has never done anything else with either. The store
pays to keep the whole history of what a scope believed and cannot answer one
question about it. One predicate.

## Phase 4: expensive, and only once the inputs are sane

### 4a. Cross-encoder reranking

Reported consistently: 10-25% precision on top of hybrid retrieval, NDCG@10 up
5-15 points, for under 200ms. Retrieve wide, rerank, return the top few.

Demoted from first place in an earlier draft. It is a multiplier on input
quality, and Phase 1 is about input quality. It also costs the sub-second read
that is currently a property worth having, so it belongs per query rather than
globally.

### 4b. Entity resolution as a classifier rather than a constant

The highest ceiling here and the last to start, because it needs labels.

Resolution compares two independently embedded names with a cosine threshold.
Calibration on this store: **AUC 0.766, 95% interval [0.437, 0.968]** - the
interval includes chance. The instinct is "more labels". The literature says
the shape is wrong: replacing a trained classifier with a cosine threshold
costs **16-18%** (Beyond Scale and Generation), and cross-encoders reach
83.0-84.1 F1 on entity matching against bi-encoders at 72.5-81.5, because a
threshold cannot see the interaction between two names.

The standard architecture is blocking then pairwise classification. The
blocking half exists (HNSW top-5). The classifier is a constant.

One caveat that lands on work done this week: Entity Resolution in Practice
reports systems reaching their best by "fixing blocking recall from 0.879 to
1.000, not by scoring better". Candidate recall dominates scoring, and the
vector channel moved to 0.980 recall on 2026-09-25. Worth watching.

## Standing refusals

- **Memory reconsolidation.** Retrieval-Driven Memory Reconsolidation proposes
  rewriting memories when retrieval makes them relevant again. It is the most
  brain-faithful idea in the 2026 literature and it should be refused; the
  counter-evidence is in the same literature (Useful Memories Become Faulty
  When Continuously Updated by LLMs). Rewriting on recall is how human memory
  manufactures false memories. Append-and-supersede is better for a system of
  record, and "we never rewrite what you told us" is sellable.
- **Statistical causal discovery.** Unchanged by the 2026 work. A guessed
  cause is indistinguishable from a real one once stored.
- **Silent merging.** Off since 2026-09-13: precision 50% over two reviewed
  pairs, and the path had fired once in the store's history.
- **A bulk write endpoint.** `write_episode` already takes 200 facts a call.
  The 26,633-fact load that prompted the question sent one call per fact.

## Sources

Tiger Data, *Introducing pg_textsearch: true BM25 ranking for Postgres*.
ParadeDB, *Hybrid Search in PostgreSQL: The Missing Manual*, and `pg_search`
on PGXN for its AGPL-3.0 licence. *Beyond Scale and Generation: Understanding
Language Model-based Entity Matching* (arXiv 2607.24688). *Entity Resolution
in Practice* (arXiv 2607.26298). *Lightweight Query Routing for Adaptive RAG*
(arXiv 2604.03455). *PAGE-RAG* (arXiv 2607.19301). *Retrieval-Driven Memory
Reconsolidation* (arXiv 2609.16053). *Useful Memories Become Faulty When
Continuously Updated by LLMs* (arXiv 2605.12978). *FSFM* (arXiv 2604.20300),
abstract only - its improvement figures are the authors' and are not verified
here. *Zep: A Temporal Knowledge Graph Architecture for Agent Memory* (arXiv
2501.13956).
