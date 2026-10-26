# Echo Memory

[![CI](https://github.com/echo-mem/echo-mem/actions/workflows/ci.yml/badge.svg)](https://github.com/echo-mem/echo-mem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/echo-mem)](https://pypi.org/project/echo-mem/)
[![Python](https://img.shields.io/pypi/pyversions/echo-mem)](https://pypi.org/project/echo-mem/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

**Shared memory for AI agents, as a graph in your own database.** What Claude Code learns,
Cursor and Codex can recall. Every fact records who wrote it and when, and the server
never calls a model to store one.

> Your agents start every session from zero. The usual fix is a notes file you paste into
> context, which grows until it is mostly irrelevant to whatever you are asking. Echo
> Memory is the other shape: facts connected to each other, and a query that returns the
> few that matter. On the author's own 325 fact store that is **96.7% less context** for
> the same answer, with the answer still present 87.2% of the time across 1,190 queries
> the harness derived from their own answers.

```
write_episode                          query_memory
  billing ──uses──▸ Razorpay             "how do we take payments"
    │ written by claude-code               ▸ billing uses Razorpay, not Stripe
    │ supersedes ──▸ Stripe                  written by claude-code, 3 days ago
    └ no model invoked                     ▸ 1,372 tokens, not 41,838
```

## Install

**Requires:** Python 3.11+ and Docker (for the database).

```bash
pipx install echo-mem
echo-memory quickstart
```

`quickstart` starts the database, applies the schema, and prints the `claude mcp add`
line that registers it, filled in with the port it actually used. The Postgres image is
published, so nothing compiles.

Or use the hosted service and run no database at all:

```bash
pipx install echo-mem
echo-memory connect <key>          # a key from https://app.echo-mem.com
```

Then once per machine, so an agent knows *when* to record and recall rather than only
that the tools exist:

```bash
echo-memory install --global
```

> **Restart your client afterwards.** An MCP server is a long lived process that holds
> the code and config it started with, and an editable install does not change that.

> **The PyPI name is `echo-mem`, not `echo-memory`.** That name belongs to an unrelated
> hosted product. The import package and the CLI are both `echo_memory` / `echo-memory`;
> only the distribution name differs.

## Upgrading

```bash
pipx upgrade echo-mem
echo-memory init-db                # apply any new migrations
echo-memory reindex                # rebuild what the new code derives
```

Both steps, in that order, and neither is optional on a store that predates the version
you just installed.

**Skipping `init-db` fails loudly.** A read against a schema older than the code raises an
undefined column, which is the good case: it stops rather than answering from something it
half understands.

**Skipping `reindex` fails silently, which is worse.** Derived data that the new code reads
differently is still the old data, and nothing errors. Concretely, on an upgrade to 0.5.3
the lexical corpus statistics are treated as stale until rebuilt, so BM25 stays off and
your queries keep ranking by `ts_rank` while the setting reports as on. `reindex` rebuilds
the embeddings and those statistics together. `echo-memory health` tells you afterwards.

## Usage

```bash
echo-memory status                 # what each scope holds, and which agents have written
echo-memory health                 # a score, what is weak, and what to do about it
echo-memory dashboard --serve      # the graph, in a browser, localhost only

echo-memory why <fact_id>          # the full audit trail for one fact
echo-memory recall "<question>"    # query the store from a terminal
echo-memory export                 # everything, as JSON

echo-memory install --for cursor   # wire one client, project scoped
echo-memory adopt                  # wire every MCP client on the machine, each with its own id

echo-memory infer-causal-hints     # type the facts whose own sentence states a cause (dry run)

echo-memory eval                   # retrieval quality against your own store
echo-memory eval --context         # what a recall costs against injecting everything
echo-memory eval --context --sweep # the same, as a curve across corpus size
echo-memory eval-external locomo <path>       # LoCoMo, the corpus the published figures use
echo-memory eval-external longmemeval <path>  # LongMemEval, the same
echo-memory calibrate              # is entity resolution trustworthy on your data
echo-memory benchmark              # write, query and digest latency
```

### The seven MCP tools

| Tool | What it does |
|---|---|
| `write_episode` | Store entities and the facts connecting them. No model call. |
| `query_memory` | Hybrid vector and full text retrieval, fused by reciprocal rank. Full text ranks by BM25. Takes `as_of` to read the store as it stood at an instant, and `about` to return the facts recorded against one or two named entities. |
| `trace_cause` | Causal chains through the entities a subject matches, not a ranked list. |
| `record_recall_save` | Mark that a recalled fact saved re explaining something. Refuses a fact no read returned. |
| `get_audit_log` | Every change to memory, with a plain language reason. |
| `pending_documents` | Memory files this project wrote that the graph has not heard about. |
| `mark_ingested` | Close one of those out. |

## What you get

**A graph, not a list.** Entities are nodes and a fact is an edge between two of them.
Two sessions that never knew about each other resolve onto the same entity by name, so
the second inherits what the first learned.

**Bounded retrieval, designed but not built.** The plan is that old, rarely read memory
demotes into higher level summaries over time, with nothing discarded and every summary
still edged back to the facts it came from. None of it exists yet: there is no tiering,
no summarisation, and retrieval today walks every active fact in the scope. It is
described in [`docs/designs/`](docs/designs/) and listed below as v1c, and this paragraph
used to claim it in the present tense.

**A ranked answer that says why each fact is in it.** Every returned fact carries
`score`, its cosine similarity to the query, `rank`, its position, and `matched`, the
channels that found it. `score` is deliberately not the fusion number: reciprocal rank
values are sums of 1/(k+rank) and mean nothing from one query to the next, while a
similarity means the same thing every time, which is what a caller thresholding on it
needs. It is computed for every fact returned, including the ones only full text search
found, because which channel retrieved a fact is an implementation detail and has no
business reaching a field callers read as relevance.

**The full text channel ranks by BM25.** Postgres's `ts_rank` counts term occurrences and
nothing else: no inverse document frequency, so a word in every fact of a scope counts as
much as one in three; no saturation, so repetition scales linearly; no length
normalisation, so a long fact is punished for being long. On 324 real prompts the rank 3
and rank 4 scores were identical in 59% of them, and reciprocal rank fusion reads rank
position and never the score underneath it, so a channel ordered by scan order handed the
fusion a coin flip.

BM25 is computed in SQL rather than installed, so no extension is added to your database.
Corpus statistics are rebuilt amortised on write inside the transaction that already holds
the scope's lock, and by `echo-memory reindex`; a scope that has none falls back to
`ts_rank` rather than ranking on statistics it does not have. `ECHO_MEMORY_LEXICAL_BM25=0`
returns to `ts_rank` everywhere.

**Facts about an entity, which resemblance cannot express.** `query_memory` takes
`about`, one or two entity names, and returns only the facts recorded against them. One
name gives the edges incident to that node; two give the edges between them in either
direction, which is "both names in the same fact" without going near the fact text.

This is not a ranking improvement and it is not a substitute for one. A query about one
team returns a semantically close fact about a different team because the wrong fact
genuinely does resemble the question, and no amount of better ranking removes it.
Identity is not similarity, and a fact is an edge between two nodes, so asking which
entity a fact is about is structural and exact.

Name matching is exact and case insensitive, never a substring, because a short name is a
substring of longer unrelated words. A name nothing is recorded under returns no facts
rather than the nearest thing, and so does a pair with no fact joining them: that is real
absence and it means nobody recorded this, not here is something adjacent. More than two
names is refused rather than answered empty, because an edge has two endpoints and a fact
about three entities is a question this model cannot express.

**The history the store has always kept is readable.** Every fact has carried `t_valid`
and `t_invalid` since the first migration, and the read path had only ever asked whether a
fact is current, so the store paid to keep the whole record of what a scope believed and
could not answer the first question a post mortem asks. `query_memory` takes `as_of` in
unix seconds and every channel honours it: vector, full text, the graph hop, the digest.

Supersession is what makes that more than a curiosity. Writing the same source, target and
relation again with a **changed** claim does not edit the old fact, it ends it: the old
edge takes a `t_invalid` and stays queryable. Nothing is ever rewritten, so the history is
real rather than reconstructed, which is the property the memory reconsolidation
literature gives up and the reason this project refuses that idea.

Restating an **unchanged** claim is not a change and no longer counts as one. If the fact
text, the confidence and the causal hint all match the edge already there, the write is
skipped and reported under `restated`. It used to create a second edge, end the first, and
emit a supersession whose `before_fact` equalled its `after_fact`. That is the mechanism
behind one customer edge carrying 47 versions of a single sentence, and behind 543 writes
producing 112 facts while every one of them was billed. It also makes a retry safe: with
no idempotency key, a caller retrying a write that had actually landed used to burn a
version on it.

**Every write answers whether it stored anything.** `write_episode` can end with an empty
`edges_created` four different ways, and from the caller's side they used to be
indistinguishable without knowing which other key to inspect. Each one now reports
`stored: false` and names itself in `not_stored`: `refused` (validation rejected it),
`deferred` (an entity mention was close enough to an existing node to be worth asking
about and not close enough to merge), `unchanged` (every fact already existed word for
word) and `no_facts` (the episode asserted none). A refusal carries the same keys as a
success, so one check works on every path, and `retryable` separates a database blip worth
retrying from a validation error that will fail forever. A write that stored some of its
facts and held the rest says `partial`.

The deferral is the one that cost real data, because it looks most like success: a well
formed response, no error, `isError` false, and nothing written. A customer loading 26,633
facts read that as 26,633 successes and stored 1,650.

**Provenance on every fact.** Who wrote it, which tool, which project, when, and which
reads returned it. A superseded fact is never deleted. It stops being drawn and stays
reachable with its history.

**Causal typing, and a walk over it.** A fact can carry `causal_hint`: one of
`caused_by`, `led_to`, `enabled_by`, `blocked_by` or `contradicts`, set by the agent's
own read of what the session said and never inferred statistically. `trace_cause` walks
those links and returns chains rather than a ranked list, because the answer to "why did
this happen" is an ordered chain and similarity cannot produce one. "A led_to B" and "B
caused_by A" are one claim written from opposite ends, and both assemble into the same
chain.

A fact with no hint is associative, which is the default and usually correct. An empty
answer from `trace_cause` says which kind of empty it is: nobody recorded a cause here is
a different fact about a store than there are no chains.

**Filling it in, for a store that predates it.** Facts written before 0.5.0 carry no
hint, so on an existing store `trace_cause` has nothing to walk.
`echo-memory infer-causal-hints` re-reads the fact text already stored with your own
model (`ECHO_MEMORY_LLM_API_KEY`, `ECHO_MEMORY_LLM_MODEL`) and types the edges whose own
sentence states the relation. Dry run by default; `--write` applies what it printed and
`--clear --write` takes it back.

This is extraction done late, not causal discovery, and the difference is enforced rather
than promised. A sentence with no causal connective is never sent to a model, so
co-occurrence is refused before it costs anything. Every proposal has to quote the words
that state the relation, and a quote that is not literally in the fact is dropped, so a
model reasoning from the world instead of reading the sentence gets nothing stored. Each
hint written is audited and marked as extracted late, which is what makes `--clear`
able to remove exactly these and never one you wrote at write time.

**No inference on the write path.** Extraction happens in the calling agent, so storing
a memory invokes no model on the server. `infer-causal-hints` above is not an exception
to that and is worth being precise about: it is a command a person types against facts
already stored, it never runs as part of a write, a migration or a hook, and a store that
never runs it never causes a model call. The cost moved rather than vanished: the agent
has to arrive with entities and facts already extracted, which is what the
[tool contract](docs/DEVELOPMENT.md) spells out. The comparison that makes this matter is
Zep/Graphiti, the closest architectural match, whose own description of ingestion is that
"every episode triggers multiple LLM calls" and that "write cost scales with volume".

The obvious reply is that cheap writes are cheap because they do less, and that reply is
correct on the mechanism. [`docs/WRITE-COST.md`](docs/WRITE-COST.md) answers it properly,
including the two measured costs of the choice: the Stop gate fired seven times and
produced one fact, and a write touching an ambiguous entity is deferred rather than
stored. The deferral is still the cost; what has changed is that the call now says so
instead of returning as though it had succeeded.

**Any MCP client.** A coding assistant, a chatbot, an ops agent, or something built in
house. Coding agents are where this is proven, not what it is limited to.

## Numbers, and how they were taken

Every figure comes from this repository or a live store, with the command that reproduces
it on yours and the size of the store it was taken against. The corpus is small and the
noise floor is stated, because a difference nobody sized is not a result.

Two of these describe the store rather than the product. `eval --context` and
`eval --ablate` build their queries from the facts they are scoring, so the labels are
free and the absolute values belong to this store alone; they are honest about a
difference between configurations and say nothing about how echo-mem would rank on
somebody else's data. The two external rows are the ones to compare against anything.

| Measure | Value | Reproduce |
|---|---|---|
| Context per recall vs injecting everything | **96.7% less** on a 325 fact store, hit@10 0.872 over 1,190 queries derived from their own answers, so read it as a token ratio with a floor attached rather than as accuracy | `echo-memory eval --context` |
| The same saving across 8x of corpus growth | 75.5% at 32 facts rising to **96.4% at 261**, hit@10 0.900 to 0.946 | `echo-memory eval --context --sweep` |
| LoCoMo retrieval, 1,982 questions, 5,882 turns | recall@10 **0.601**, hit@10 0.658, MRR 0.460, session@10 0.850 | `echo-memory eval-external locomo` |
| LongMemEval retrieval, 90 questions, 15 per type | session@10 **0.937**, recall@10 0.727, MRR 0.428 | `echo-memory eval-external longmemeval --per-type 15` |
| Lexical BM25 against `ts_rank` | **+0.0271** MRR on entity_pair [+0.0083, +0.0481] and on multihop [+0.0166, +0.0381], the other two shapes inside noise, none worse | `echo-memory eval --ablate`, the `without BM25` row with its sign reversed |
| Server side model calls per write | **0** | `echo-memory benchmark` |
| Write, query, digest latency (median) | 24ms, 17ms, 4ms, against a 250 fact scope | `echo-memory benchmark` |
| Entity resolution AUC | 0.666, 95% CI [0.421, 0.881] | `echo-memory calibrate` |

That last row is the one that went the wrong way, and it is here on purpose. The interval
includes chance, so the unattended merge is switched off: at the automatic bar precision
was 50% over two reviewed pairs, and the audit log showed that path had fired once in the
system's entire history. A near match is now offered for confirmation instead.

The LoCoMo row is retrieval, not QA accuracy. Published LoCoMo results have a model write
an answer and a second model judge it; this asks only whether the turn holding the answer
came back, which is a ceiling on QA accuracy rather than a substitute for it, and is not
comparable to anybody's published QA figure. It also feeds raw dialogue turns, which skips
the extraction step this design pushes to the calling agent, so it is a floor as well as a
ceiling. The worst row, multi hop at recall@1 0.099, is in
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) with the rest.

The context saving is measured against a specific baseline, stated so it cannot be read
as more than it is. Not "no memory at all", which is however long a human spends re
explaining and is unmeasurable. It is the thing people do instead: keep the project's
notes in one file and paste the whole file. On that store the file is 325 facts, about
41,838 tokens; a recall returned 1,372 on average. The hit rate belongs beside it, because
a recall that returned nothing would score 100%.

## The graph

Memory is a graph, not a list of notes. Entities are nodes; a fact is an **edge** between
two of them. That is the whole data model, and everything else follows from it.

![The memory graph](docs/images/graph-overview.png)

Three projects here. `checkout-api`, `mobile-app` and `data-pipeline` were recorded in
separate sessions and never told about each other, yet the picture already separates them,
because separation is a property of the edges rather than a label anyone applied.

**Clusters come from structure.** Densely connected facts are grouped by label propagation
over the edges, and each cluster is named after its most connected node. That is why
`data-pipeline` sits apart: nothing it knows touches payments. It is also why
`checkout-api` and `mobile-app` share a cluster despite being different codebases. They
genuinely share an idea, and the graph found it rather than being told.

**Components are the stronger claim.** Two nodes in different components have no path
between them at all, which is the strongest statement this graph can make that two
memories are unrelated.

### Click a node: everything it takes part in

![A node selected](docs/images/graph-node-selected.png)

`idempotency keys` is the largest node here and nobody made it large: seventeen facts from
several services resolved onto one entity by name. The panel lists every one, with which
agent wrote it and when.

### Click a link: why memory believes it

![A fact selected](docs/images/graph-fact-selected.png)

Not a tooltip. Who wrote the fact, in which project, when, and how each of its entities
resolved. `echo-memory why <fact_id>` prints the same trail in a terminal.

### Seeing your own

```bash
echo-memory dashboard --serve --open
```

The images above come from a synthetic dataset (`scripts/demo-seed.py`) rather than a real
store, for the obvious reason: a real memory graph is full of hostnames, account numbers
and client names.

## Wiring more than one tool

**Give each client its own `ECHO_MEMORY_AGENT_ID`.** Cursor should say `cursor`, Claude
Desktop `claude-desktop`. Memory is shared either way, but a fact records which tool
learned it, and two tools claiming the same id makes cross tool recall impossible to see
afterwards.

```bash
echo-memory adopt                  # every MCP client on the machine, each with its own id
echo-memory install [path]         # one project: MCP config plus a skill, committed with the code
```

`adopt` shows the diff before writing anything. For an agent that does not speak MCP, see
[`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md).

## Is the graph in good shape?

```bash
echo-memory health
```

A score, what is strong, what needs attention, and what to do about each, including what
recall has cost: how often memory was read, how often a read returned anything, roughly
how many tokens were injected, and how many saves those reads produced. Writes were
counted from the start; reads were not counted at all, so nothing could answer whether
recall earns what it costs. It exists to be run when you have no question, because a store
can look healthy by every other number while most of its facts came from a bulk import,
the last real write was a week ago, and only one of several wired agents has ever written
anything. `--json` for machine readable output.

Nothing in it is gated. The paid plan sells hosting; diagnostics about your own data are
not a thing to withhold from the person whose data it is.

## Architecture

**Storage** PostgreSQL with `pgvector` and Apache AGE, from a single local agent up to an
organisation wide shared graph, with no forced migration later. The novel work is the
memory structure and the read/write algorithm on top of it, not a new database engine.

**Retrieval** Hybrid vector and full text search fused by reciprocal rank in v1a, with
the full text side ranked by BM25 computed in SQL over per scope corpus statistics.
Personalised PageRank via `networkx` lands in v1b for multi hop associative retrieval.

**Interface** [Model Context Protocol](https://modelcontextprotocol.io), so any compliant
agent reads and writes the same graph.

## Status

Early and staged, on purpose. See [`docs/designs/`](docs/designs/) for the architecture
and the v1a to v1b plan.

| | |
|---|---|
| **v1a, built** | Basic recall. Seven MCP tools, thirty CLI commands, on PyPI and in the MCP registry. |
| **v1b, part built** | Causal typing and `trace_cause` shipped in 0.5.0. Multi hop associative retrieval has not: 187 questions no single fact answers score MRR 0.212 today, and that number is what the rest of v1b has to beat. |
| **v1c, designed** | Consolidation: hot, consolidated and archived tiers, so retrieval cost stops tracking total facts written. Nothing implemented. |
| **v1.1, planned** | Organisation wide tenancy: per agent, per team, or org wide graphs. |

The validated wedge driving v1a is memory shared across coding agents, which is the
author's own daily pain and the case with the most evidence behind it. Everything else is
the target this architecture is built toward.

## Hosted

[app.echo-mem.com](https://app.echo-mem.com) runs the database for you: **$149 a month**
for 1,500,000 units, where a unit is one fact written or one fact returned. It is paid
from the first key, because hosting is the thing it charges for, and it runs the same
published package you would install yourself.

Running it yourself is free for development, testing and evaluation at any size, and free
in production for organisations under 50 people and under $5M revenue, with no account and
no feature held back. Above either threshold, production use needs an agreement:
[talk to us](mailto:hello@echo-mem.com).

<details>
<summary>Contributing</summary>

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and pull requests welcome; please read the
design docs first so proposals fit the staged build plan. Commits need a sign-off line,
`git commit -s`, which is the [Developer Certificate of Origin](DCO.md). There is no
contributor agreement to read.

The most useful contribution is a measurement that disagrees with one of the numbers
above. Run `echo-memory eval`, `calibrate` or `benchmark` on your own store and open an
issue with the output.

</details>

## License

**Apache 2.0.** See [`LICENSE`](LICENSE). Run it, fork it, build on it, ship it in your
product, at any size, with no agreement and nobody to ask.

For about a month this was not true. Versions 0.5.0 to 0.5.5 shipped under the Business
Source License, which made production use free only below fifty people and $5M of revenue.
That is withdrawn. Ateleir Tech holds the copyright in the whole work and relicenses all of
it, those versions included, so the grant only ever got wider and nobody lost a right they
already had.

The reasoning, since a project that changes its licence twice owes you one: the restriction
was protecting the wrong thing. What makes this hard to run is not the source, it is a
Postgres with Apache AGE and pgvector that somebody has to operate, back up and upgrade.
[The hosted plan](https://echo-mem.com) sells exactly that and is unaffected by the
licence. Meanwhile the restriction was costing the thing the project most needs, which is
for people to be able to use it without reading a grant first.

What is not Apache 2.0 is the hosted service itself: the metering, billing and tenancy that
only exist because somebody is being invoiced. None of it sits in the read or write path.

<!-- The MCP registry proves you own a PyPI package by finding this line in the
     package's own description. It has to survive into the built distribution,
     which is why it lives in the README rather than in a workflow. -->
mcp-name: io.github.ayushcodes10/echo-mem
