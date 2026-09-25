# Changelog

Notable changes per released version. Anything that changes what a user gets,
what a number means, or what the database looks like belongs here; refactors
and test work do not.

Versions before 0.4.0 predate this file. Their history is in the git log and in
the pull requests, which carry the reasoning rather than just the diff.

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
