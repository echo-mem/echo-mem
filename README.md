# Echo Memory

[![CI](https://github.com/echo-mem/echo-mem/actions/workflows/ci.yml/badge.svg)](https://github.com/echo-mem/echo-mem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/echo-mem)](https://pypi.org/project/echo-mem/)
[![Python](https://img.shields.io/pypi/pyversions/echo-mem)](https://pypi.org/project/echo-mem/)
[![License](https://img.shields.io/badge/license-BSL%201.1-blue)](LICENSE)

**Shared memory for AI agents, as a graph in your own database.** What Claude Code learns,
Cursor and Codex can recall. Every fact records who wrote it and when, and the server
never calls a model to store one.

> Your agents start every session from zero. The usual fix is a notes file you paste into
> context, which grows until it is mostly irrelevant to whatever you are asking. Echo
> Memory is the other shape: facts connected to each other, and a query that returns the
> few that matter. On the author's own store that is **96.7% less context** for the same
> answer, with the answer still present 87.2% of the time across 1,190 questions.

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

echo-memory eval                   # retrieval quality against your own store
echo-memory eval --context         # what a recall costs against injecting everything
echo-memory eval --context --sweep # the same, as a curve across corpus size
echo-memory calibrate              # is entity resolution trustworthy on your data
echo-memory benchmark              # write, query and digest latency
```

### The six MCP tools

| Tool | What it does |
|---|---|
| `write_episode` | Store entities and the facts connecting them. No model call. |
| `query_memory` | Hybrid vector and full text retrieval, fused by reciprocal rank. |
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

**Provenance on every fact.** Who wrote it, which tool, which project, when, and which
reads returned it. A superseded fact is never deleted. It stops being drawn and stays
reachable with its history.

**Causal typing, designed but not built.** The plan is that an edge can be tagged
`caused_by`, `led_to`, `blocked_by` or `contradicts` by the agent's own read of the
conversation rather than inferred statistically. Today `relation_type` is a free string
and nothing writes a causal tag: `causal_hint` is returned on every query result and is
always null, because migration 0001 reserved it for v1b and no write path sets it. Like
consolidation above, this paragraph used to claim it in the present tense.

**No inference on the write path.** Extraction happens in the calling agent, so storing
a memory invokes no model on the server. The cost moved rather than vanished: the agent
has to arrive with entities and facts already extracted, which is what the
[tool contract](docs/DEVELOPMENT.md) spells out. The comparison that makes this matter is
Zep/Graphiti, the closest architectural match, whose own description of ingestion is that
"every episode triggers multiple LLM calls" and that "write cost scales with volume".

The obvious reply is that cheap writes are cheap because they do less, and that reply is
correct on the mechanism. [`docs/WRITE-COST.md`](docs/WRITE-COST.md) answers it properly,
including the two measured costs of the choice: the Stop gate fired seven times and
produced one fact, and a write touching an ambiguous entity is deferred while the call
returns as though it succeeded.

**Any MCP client.** A coding assistant, a chatbot, an ops agent, or something built in
house. Coding agents are where this is proven, not what it is limited to.

## Numbers, and how they were taken

Every figure comes from this repository or a live store, on a date, with the command that
reproduces it on yours. The corpus is small and the noise floor is stated, because a
difference nobody sized is not a result.

| Measure | Value | Reproduce |
|---|---|---|
| Context per recall vs injecting everything | **96.7% less**, hit@10 0.872 over 1,190 questions | `echo-memory eval --context` |
| The same saving across 8x of corpus growth | 75.5% at 32 facts rising to **96.4% at 261**, hit@10 0.900 to 0.946 | `echo-memory eval --context --sweep` |
| LoCoMo retrieval, 1,982 questions, 5,882 turns | recall@10 **0.601**, hit@10 0.658, MRR 0.460 | `scripts/locomo-bench.py` |
| LongMemEval retrieval, 90 questions, 15 per type | session@10 **0.940**, turn@10 0.727 | `scripts/longmemeval-bench.py --per-type 15` |
| Server side model calls per write | **0** | `echo-memory benchmark` |
| Write, query, digest latency (median) | 15ms, 8ms, 1ms | `echo-memory benchmark` |
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

**Retrieval** Hybrid vector and full text search fused by reciprocal rank in v1a.
Personalised PageRank via `networkx` lands in v1b for multi hop associative retrieval.

**Interface** [Model Context Protocol](https://modelcontextprotocol.io), so any compliant
agent reads and writes the same graph.

## Status

Early and staged, on purpose. See [`docs/designs/`](docs/designs/) for the architecture
and the v1a to v1b plan.

| | |
|---|---|
| **v1a, built** | Basic recall. Six MCP tools, thirty CLI commands, on PyPI and in the MCP registry. |
| **v1b, gated** | Causal typing and multi hop retrieval. 187 questions no single fact answers score MRR 0.212 today; the number to beat exists before the feature does. |
| **v1c, designed** | Consolidation: hot, consolidated and archived tiers, so retrieval cost stops tracking total facts written. Nothing implemented. |
| **v1.1, planned** | Organisation wide tenancy: per agent, per team, or org wide graphs. |

The validated wedge driving v1a is memory shared across coding agents, which is the
author's own daily pain and the case with the most evidence behind it. Everything else is
the target this architecture is built toward.

## Hosted

Running it yourself is free under the Business Source License for any non production use,
and free in production for organisations under 50 people and under $5M revenue, with no
account and no feature held
back. [app.echo-mem.com](https://app.echo-mem.com) runs the database for you at $99 a
month if you would rather not.

<details>
<summary>Contributing</summary>

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and pull requests welcome; please read the
design docs first so proposals fit the staged build plan. A first pull request is asked to
sign the [Contributor License Agreement](CLA.md), once, in the PR thread.

The most useful contribution is a measurement that disagrees with one of the numbers
above. Run `echo-memory eval`, `calibrate` or `benchmark` on your own store and open an
issue with the output.

</details>

## License

Business Source License 1.1. See [`LICENSE`](LICENSE).

The source is public and stays public. What changed on 25 September 2026 is who may run it
in production without an agreement:

| | |
|---|---|
| Development, testing, evaluation, research, teaching | free, any size |
| Production, under 50 employees and under $5M revenue | free |
| Production, above that | [talk to us](mailto:hello@echo-mem.com) |
| Offering it to third parties as a hosted service | [talk to us](mailto:hello@echo-mem.com) |

Each released version converts to **Apache 2.0 four years after it is published**, and that
conversion is automatic and irrevocable.

Versions published before this change remain under Apache 2.0 permanently. That includes
everything up to and including 0.4.1 on PyPI. Relicensing cannot reach back, and this note
exists so nobody has to work that out from a git history. The Apache text those versions
were released under is kept at [`LICENSE-APACHE-2.0`](LICENSE-APACHE-2.0).

<!-- The MCP registry proves you own a PyPI package by finding this line in the
     package's own description. It has to survive into the built distribution,
     which is why it lives in the README rather than in a workflow. -->
mcp-name: io.github.ayushcodes10/echo-mem
