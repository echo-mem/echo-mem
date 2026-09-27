# Changelog

Notable changes per released version. Anything that changes what a user gets,
what a number means, or what the database looks like belongs here; refactors
and test work do not.

Versions before 0.4.0 predate this file. Their history is in the git log and in
the pull requests, which carry the reasoning rather than just the diff.

## Unreleased

**`query_memory` takes `about`, one or two entity names.** Returns only the facts
recorded against them: one name gives the edges incident to that node, two give the
edges between them in either direction, which is "both names in the same fact"
without going near the fact text.

The first customer to want this had no way to ask, so they retrieved by resemblance
and filtered on the fact text at four call sites, word boundary matching names to drop
facts that were real, well ranked and about somebody else. Better ranking does not fix
that and it is not meant to: a query about one team returns a semantically close fact
about another team because the wrong fact genuinely does resemble the question.
Identity is not similarity, and a fact is an edge between two nodes, so which entity a
fact is about is structural and exact.

Matching is exact and case insensitive, never a substring, because a short name is a
substring of longer unrelated words. A name nothing is recorded under returns no facts
rather than the nearest thing, and so does a pair with no fact joining them. Two
refusals rather than a quietly wrong answer: more than two names raises, because an
edge has two endpoints and a fact about three entities is not a narrow question but an
inexpressible one, and `about` with `digest` raises, because the digest query does not
carry the filter and would return other entities' facts while the caller believed the
answer was scoped. The graph hop is filtered too, since it is the one channel that
expands outward and would otherwise put the wrong entity back in.

## 0.5.3

**The lexical channel ranks by BM25.** `ts_rank` counts term occurrences and
nothing else: no inverse document frequency, so a word in every fact of a scope
counts as much as one in three; no saturation, so repetition scales linearly;
no length normalisation, so a long fact is punished for being long. On 324 real
prompts the rank-3 and rank-4 scores were identical in 59% of them, which meant
Postgres scan order decided which fact a user saw. Reciprocal rank fusion reads
rank position and never the score underneath it, so a channel ordered by coin
flip hands the fusion a coin flip.

Measured before it was switched on, paired bootstrap on a real store:
entity_pair +0.0271 MRR [+0.0083, +0.0481], multihop +0.0271 [+0.0166, +0.0381],
the other two shapes inside noise, none regressing, and every shape answering in
12 to 20% fewer tokens. `ECHO_MEMORY_LEXICAL_BM25=0` returns to `ts_rank`.

**A scope gets its statistics without being told to run anything.** BM25 needs
corpus statistics, and falls back to `ts_rank` for any scope that has none, so
switching the default on without this would have read as on while every query
quietly took the old path. Statistics are rebuilt amortised on write, inside the
transaction that already holds the scope's advisory lock, and by `reindex`.
Staleness is two counters compared, not a corpus scan: migration 0027 records
the write counter the statistics were built at, and one predicate decides both
whether they are usable and whether they are due. A fixed rebuild interval
cannot bound a proportional drift tolerance, which is how an earlier version
switched BM25 off for four fifths of writes.

**`query_memory` takes `as_of`.** Every fact has carried `t_valid` and
`t_invalid` since the first migration and the read path had only ever asked
whether a fact is current, so the store paid to keep the whole record of what a
scope believed and could not answer the first question a post mortem asks. The
instant is unix seconds, and every channel honours it: vector, lexical, the
graph hop, the digest. Writing the same (source, target, relation_type) again
does not edit the old fact, it ends it, so the history is real rather than
reconstructed.

**`echo-memory infer-causal-hints` types the facts a store already holds.**
`trace_cause` shipped in 0.5.0 with nothing to walk: facts written before it carry
no hint, and on this author's production store 38,479 edges carried 2 hints, both
from a smoke test. The command re-reads the fact text already stored with your own
model (`ECHO_MEMORY_LLM_API_KEY`, `ECHO_MEMORY_LLM_MODEL`), types the edges whose
own sentence states the relation, and is a dry run until `--write`. `--clear
--write` takes it back, exactly the hints it wrote and never one a session
asserted at write time.

This is extraction done late, not causal discovery, and the difference is
enforced rather than promised. A sentence with no causal connective is never sent
to a model, so co-occurrence is refused before it costs anything, and every
proposal has to quote the words that state the relation: a quote not literally
present in the fact is dropped, so a model reasoning from the world instead of
reading the sentence gets nothing stored. Migration 0026 records one verdict per
fact examined, so a pass over 38,479 facts can be interrupted without paying the
model again for the sentences that stated nothing.

It does not change what the engine does on a write. Nothing under `ingestion/` or
`retrieval/` imports the module, it is deliberately not part of
`infra.config.Config` so the server cannot reach a provider key, and a store that
never runs the command never causes a model call.

**`echo-memory eval-external` runs LoCoMo and LongMemEval.** `eval` scores this
store against itself, which cannot be set beside anybody else's number. This
runs the corpora the published figures are taken on, through the real
`write_episode` and `query_memory` paths, and reports recall@k, hit@k, MRR and
session-level recall per question category, as a readable table and as JSON.

Neither dataset ships here: both are somebody else's to license and
longmemeval_s is 278MB, so the path is an argument and the report records the
file's size and sha256. `tests/fixtures/` holds a synthetic file in each schema.

**It reports no accuracy, and says why in the report.** Both benchmarks publish
model-judged QA accuracy; nothing here calls a model, so `accuracy` is null with
its reason in the field beside it. `answer_words@k` is the model-free
approximation, defined as the share of the gold answer's own content words
present in the returned facts, excluding the gold answers of under two content
words that ten facts would match by chance. It is a floor under what a reader
could have written and not an accuracy score.

It refuses to start against a database holding facts outside its own scopes,
because a full LongMemEval run writes 246,930 of them through the ordinary write
path and there is no undo. It also resumes: a scope already holding its full
complement is scored without being rewritten.

`scripts/locomo-bench.py` and `scripts/longmemeval-bench.py` are gone, replaced
by the subcommand. The numbers are unchanged, and
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) records the run that establishes that.

**Two ideas measured and not shipped, kept with their numbers.** Routing the
graph hop per query cuts its damage by about 80% and still gains nothing on the
shape it was built for: multihop comes out at -0.0002 MRR. Salience, ACT-R
base-level activation over the recall log migration 0021 has been filling since
September, regresses all four shapes with every interval clear of zero. Both are
off, behind `ECHO_MEMORY_ROUTE_EXPANSION` and `ECHO_MEMORY_SALIENCE`, and both
are kept so the next person to have the idea finds the experiment rather than
repeating it. They fail for the same reason: reciprocal rank fusion weights the
top of every list equally, so a list ordered by something that is not about this
query spends a top slot on a fact the query did not ask for.

**`echo-memory eval` scores those two as ablations.** `+ routed expansion` and
`+ salience` sit beside the existing rows, so the numbers above are something a
reader can reproduce rather than take on trust.


## 0.5.2

**`score` is present on every fact of every answer.** It was omitted when only
full text search found a fact, because only the vector channel computes a
similarity - an implementation detail reaching a field callers read as
relevance.

A customer sorted by it with nulls last, which is what a nullable relevance
number invites. Every lexically-found fact went to the back of the list, a
per-entity character budget truncated it, and the model filled the gap by
inventing a figure that contradicted data sitting in the store.

Their own calibration says the ordering was backwards and not merely
arbitrary: over ten questions with every fact judged against every question,
lexical-only scored R@3 0.640 against vector-only's 0.460. The channel being
demoted had the better recall.

`query_memory`'s description now also says which field to truncate by. `rank`
is the fused result of every channel; `score` is one input to it, and
re-sorting by a single input discards the fusion.

**A shutdown race that aborted the test suite after it passed.** The embedder
is warmed in a daemon thread, daemon threads are killed abruptly at exit, and
this one is inside torch when that happens - which aborts the process. CI saw
771 passed, 5 skipped, then exit code 134. Now joined at exit with a ten
second bound.

## 0.5.1

**The vector index has never been used.** pgvector's HNSW index answers
exactly one shape, `ORDER BY <distance> LIMIT n`, and both vector searches
here had a second sort key. Neither was ever answerable by the index it was
built with, so every query read every embedding in the scope and sorted them.

Measured against a real 38,169 fact scope: **52.47ms scanning against 3.85ms
using the index**, and the scan is O(n) - ten times the facts is half a
second, on every query.

The tiebreak was deliberate and its reasoning was sound: two facts at an
identical distance should resolve the same way every time. It did not need to
be in the ORDER BY, and now happens in Python on at most fifty rows.

`ECHO_MEMORY_HNSW_EF_SEARCH` controls how hard the index looks, defaulting to
200 rather than pgvector's 40 - which was below this code's own candidate
depth of 50. Recall of the exact top fifty, over twenty probes on that scope:

    ef_search   mean recall   worst   top 10 identical   mean ms
    40 (dflt)         0.817    0.24          10 of 20       1.85
    200               0.980    0.86          17 of 20       3.85
    exact             1.000    1.00          20 of 20      52.47

So: 98% of the exact answer for 7% of its cost, stated rather than implied.
On pgvector 0.7 and older there is no iterative scan to enable, and the
search stays exact and linear as it always has, with one warning in the log.

**Retrieval says why a fact is in the answer.** Every ranked fact now carries
`rank`, `score` and `matched`.

`score` is cosine similarity to the query, not the fusion score: RRF values
are sums of 1/(k+rank) and mean nothing from one query to the next, so a
caller thresholding on them would be thresholding on noise. It is `null`, not
zero, when only full text search found the fact - no similarity was computed
for it. `matched` names the channels that did.

This exists because a customer wrote relevance filters at four call sites to
reconstruct, from the fact text, something retrieval already knew.

## 0.5.0

**Licence.** This version and everything after it are under the Business
Source License 1.1, not Apache 2.0. Production use is free for an organisation
with fewer than 50 employees AND under US$5,000,000 in annual revenue, counting
parents and subsidiaries; development, testing, evaluation, research and
teaching are free for everybody at any size. Each version converts to Apache
2.0 four years after it is published.

**0.4.1 and everything before it stay Apache 2.0, permanently.** Relicensing
cannot reach backwards. `LICENSE-APACHE-2.0` is kept in the repository and
ships in the wheel for exactly that reason. For other arrangements:
hello@echo-mem.com.

**Causality is now something you can ask about.** `causal_hint` on a fact, one
of `caused_by`, `led_to`, `enabled_by`, `blocked_by`, `contradicts`, and a new
`trace_cause` tool that walks them.

Retrieval has always ranked facts by similarity and returned a flat list. That
answers "what do I know that looks like this" and cannot answer "why did this
happen", because the answer to why is an ordered chain and a chain is
structure rather than score.

`trace_cause(scope, subject, direction, max_hops)` anchors on the entities a
subject matches and walks only facts carrying a hint, in the direction the
hint says causality runs. "A led_to B" and "B caused_by A" are one claim
written from opposite ends, and both assemble into the same chain. Links
written by three sessions that never knew about each other come back ordered,
nearest cause first.

Nothing infers a cause. The server calls no model, and a guessed cause is
indistinguishable from a real one once it is stored. An empty answer says
which kind of empty it is.

**`assume_new` on `write_episode`.** A caller that already knows its entities
are new - a symbol it just read, a title it just coined - can say so once
instead of once per entity, and skip the round trip that asks which existing
node it meant. Explicit `entity_resolutions` still win, and it does not
override an exact name match.

**Ambiguity now offers only the candidates that caused it.**
`ambiguous_entities` shipped the whole top-5 regardless of score, so a
deferral triggered by a 0.708 match also listed neighbours at 0.10 and 0.067.
A reader of that list reasonably concluded the bar sat near 0.06.

**`ECHO_MEMORY_RESOLUTION_LOW`.** The bar for "worth asking about" is 0.45,
calibrated against prose-shaped names. Short technical identifiers sharing a
prefix do not separate there. A store that knows its own names can raise it
without waiting for a release. The default has not moved. The silent-merge
threshold is deliberately not configurable.

**Tool descriptions are published dedented.** The SDK sends a docstring
verbatim, indentation included, and Claude Code truncates at 2048 characters.
On `write_episode` that whitespace was 140 characters of the budget.

**Package metadata names the licence properly.** `License-Expression:
BUSL-1.1`, with both licence texts in the wheel.

## 0.4.1

**Security.** The database `quickstart` creates was reachable from the network,
and every install used the same password. Both are fixed. Anyone who has run
`echo-memory quickstart` before this version should recreate their container;
the command now says so on its final screen, and memory lives in a volume that
survives it:

```sh
docker rm -f echo-memory-db && echo-memory quickstart
```

**The port was published on every interface.** `docker run -p 5433:5432` binds
0.0.0.0 and [::], not loopback, so the database was reachable from any machine
on the same network. Verified by connecting to a real quickstart container as
superuser over a laptop's LAN address. A memory graph holds hostnames, account
numbers and client names, which is why this repository's own screenshots use a
synthetic store. Now published on 127.0.0.1.

`docker-compose.yml` had the same bug on both databases, and one of those holds
a real store.

**The password was the same everywhere.** It was the literal string `postgres`
on every machine that had ever run this command. It is now generated per
install and read back from the container when needed, so nothing new is stored
and a container made before this version keeps working.

This is the second lock and only the second: the binding is what decides
whether anything can reach the port.

**Correctness.** The connection string is composed from parts with the password
percent encoded, rather than interpolated into an f-string where `@` and `:`
are the netloc separators.

### 0.4.0 was tagged and never published

Its tag predates every fix above. Republishing it would have put a database on
the network for anyone who installed it, so the version was skipped rather than
released. There is nothing in 0.4.0 that is not also in 0.4.1.

## 0.4.0

### The write path got much faster as the store grows

Two lookups in `write_episode` went through Cypher, where
`MATCH ... WHERE id(x) = $id` cannot use an index because AGE expands the match
before filtering. Each therefore scanned the whole graph on every write: every
scope, and in a hosted deployment every tenant. A write cost what the entire
store had ever written rather than what the caller had.

Measured while ingesting a benchmark corpus, which is how it was found at all:

| Store size | Before | After |
|---|---:|---:|
| 1,057 nodes | 29ms | 22ms |
| 24,054 nodes | 129ms | 45ms |

The symptom was a slope rather than an error, which is why it survived so long.
Throughput over one ingest fell from 28 writes a second to 8, and that reads as
a large corpus rather than as a defect.

- `neighbourhood._endpoints` now reads the edge and node tables directly.
- `resolution._exact_match` now reaches `node_name_per_group_idx`, the index
  migration 0016 built for exactly that lookup and which had been unreachable
  from Cypher ever since.

### Migration 0024

Adds `node_id_idx`. AGE gives its parent table `_ag_label_vertex` a primary key
on `id`; the `Node` table that inherits from it gets nothing. `FACT` has had one
since migration 0013, added for a different query. Run `echo-memory init-db` to
apply it.

### `eval --context --sweep`

`eval --context` reports what a recall costs against injecting the whole scope,
as one number at one corpus size. A number without its denominator is easy to
read wrongly in both directions, so `--sweep` measures the same thing at several
sizes by building real sub scopes from a prefix of the store's own facts and
querying each one.

On the author's store it rises from 75.5% saving at 32 facts to 96.4% at 261,
while what a recall returns moves by 37%. That drift is why it measures rather
than extrapolating: `top_k` bounds how many facts come back, not how long they
are. `hit@10` prints in every row, because a configuration that returned
nothing would score a perfect saving.

### Benchmarks, published with their worst rows

`scripts/locomo-bench.py` and `scripts/longmemeval-bench.py`, plus
`docs/BENCHMARKS.md`. Neither calls a model: published LoCoMo and LongMemEval
figures are QA accuracy judged by a model, these measure retrieval, and
retrieval is a **ceiling on** QA accuracy rather than a substitute for it.

- LoCoMo, 1,982 questions over 5,882 turns: recall@10 0.601 overall, and multi
  hop at recall@1 0.099, which is the worst row in the table and the one v1b
  exists for.
- LongMemEval, 90 questions sampled 15 per type: session@10 0.940 against
  turn@10 0.727. The gap is the result worth reading, and it means the right
  conversation is nearly always found while the line inside it often is not.

### `docs/WRITE-COST.md`

An answer to "your writes are cheap because they do less", which is correct on
the mechanism. It concedes that first, then gives the measured costs of the
choice, including a claim in its own first version that this release's
measurements proved wrong.
