# Changelog

Notable changes per released version. Anything that changes what a user gets,
what a number means, or what the database looks like belongs here; refactors
and test work do not.

Versions before 0.4.0 predate this file. Their history is in the git log and in
the pull requests, which carry the reasoning rather than just the diff.

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
