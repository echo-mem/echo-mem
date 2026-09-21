# Echo Memory Design

Status: Draft, pre-implementation. This document is the source of truth for the
architecture and build sequence; see the PR Plan section for the current
dependency-ordered implementation order.

## Problem Statement

Two problems, one system:

1. **Personal, felt pain (the validated wedge):** switching between AI coding tools
   (Claude Code ↔ Cursor, etc.) loses context. You re-explain decisions and state every
   time you switch. This is the specific, tested pain v1a's exit criteria measure, real
   demand evidence, not a hypothesis.
2. **Market gap, confirmed by research:** no open-source agent-memory system combines
   (a) graph edges that carry real payload, (b) retrieval beyond plain cosine similarity,
   (c) explicit causal-relation typing, and (d) clean multi-tenancy, in one package,
   and, per your explicit decision, without depending on a third-party AI-memory framework
   (Graphiti, cognee, FalkorDB) that could constrain how the resulting tool is licensed
   or shipped.

**Vision, distinct from the validated wedge above:** the same architecture generalizes
past coding agents specifically, any MCP-compatible agent (chatbots, DevOps/ops agents,
deployed agentic systems, internal business tooling) can read/write the same memory
graph. **This is honestly a vision, not yet something v1a tests**, v1a's exit criteria
stay scoped to the validated coding-tool wedge.

**Repositioned core differentiator (corrected after this session's second pass): this is
NOT primarily a sharing/multi-tenancy product.** Multi-tenancy (single-agent → shared →
org-wide) is one feature of the system, not the headline. The actual differentiator is
**long-horizon memory quality**, an agent that remembers everything in the best possible
way over a long time horizon, where "best possible" is a property of the read/write
algorithm and the data structure, not just "we have a graph instead of a flat vector
store." See the new "Long-Horizon Memory Architecture" section below: this is the part
of the system meant to be genuinely novel, not an assembly of existing techniques.

Echo Memory is an open-source tool addressing all of this: your own daily pain first,
built as infrastructure you fully own and others, any agent, any scale, can adopt
without inheriting a vendor's license terms.

## What Makes This Cool

- Edges carry real payload (facts, confidence, temporal validity, provenance), hand-
  designed, not inherited from a framework's schema. Causal typing (below) is a v1b
  addition once the core loop is proven.
- **v1b:** retrieval combines vector + lexical + graph-centrality (Personalized PageRank,
  via `networkx`), then reranks the fused candidate pool with a cross-encoder, not just
  cosine similarity, cheaper and better at multi-hop "how did we get here" queries, and
  meaningfully more accurate than bi-encoder similarity alone. **v1a ships vector +
  lexical only**, proving basic recall works before adding multi-hop sophistication.
- **v1b:** causality modeled explicitly (`caused_by`, `led_to`, `enabled_by`, `blocked_by`,
  `contradicts`), set by the agent's own judgment at write-time, not statistically
  inferred. The honest, tractable version of "causal search."
- One database engine (Postgres, with pgvector and Apache AGE both in place from v1a)
  handles storage, vector search, and native graph traversal from day one, not three
  bolted-together systems, and the same engine that runs a solo user's local memory
  scales unmodified to org-wide concurrent multi-writer use. No migration debt at any
  point, not SQLite-to-Postgres, and not plain-tables-to-AGE either. (This does mean a
  running Postgres process, not a single file; see Distribution Plan. Separately: the
  server itself never calls an external LLM or embedding API. Extraction (entities/facts
  from raw text) is done by the calling agent, which is already an LLM reasoning over
  the conversation in the same turn, the same pattern graphify uses for its own
  subagent-driven extraction (see Competitor Analysis). Vector search uses a local
  embedding model, no API key, no external call; see Recommended Approach and MCP tool
  contract for the mechanics.)
- One tenancy mechanism scales from your own cross-tool memory to a shared team/org graph.
- **Day-to-day usage:** an agent calls the MCP server's write tool at natural checkpoints
  in a session (decisions made, preferences stated) and its query tool at session start or
  when it needs prior context, not automatic background injection in v1. **This
  mechanism itself is unverified**; see Premise 7 and the Next Steps spike.

## Constraints

- Solo build, no team yet.
- Must be genuinely open source and adoptable, not a personal script, and not built on
  a dependency whose own license terms could constrain that.
- Prioritize correctness and honesty about what's solved vs. research-stage over
  impressive-sounding but unproven techniques (e.g. statistical causal discovery,
  covariance-weighted distance).
- **v1 is single-user, local-only.** No cross-user auth model, no org-wide ACL enforcement,
  deferred to v1.1 (see Recommended Approach).
- **Sensitive data is an accepted v1 risk, not silently ignored.** Real coding-session data
  can contain secrets/tokens in `fact` strings even at single-user local scope. v1 accepts
  this the same way existing local session logs already on disk are accepted. A
  best-effort secret-scrubbing pass at ingestion is worth adding cheaply if it doesn't
  block the timeline; full redaction policy is v1.1.
- **v1a and v1b are a real, gated staging split**, not just a scheduling note: v1b work
  does not start until v1a's exit criteria (see Success Criteria) are met. `causal_hint`
  and PPR are v1b together; neither is needed to test whether basic recall reduces
  re-explaining, which is the actual felt pain this project exists to solve. (Apache AGE
  is no longer part of this split; it's foundational, gating v1a's start instead of
  v1b's, per Premise 6's reversal.)

## Premises

1. **Own the memory/graph engine outright; use permissively-licensed generic infra as
   foundations, not AI-memory frameworks.** Depending on Graphiti/cognee/FalkorDB carries
   license and vendor-control risk in the layer that matters (the memory logic itself).
   Generic, battle-tested, permissively-licensed building blocks (**PostgreSQL**,
   **pgvector**, **Apache AGE**, **numpy/scipy**, **networkx**; see Premise 6) are not
   competitors and carry none of that risk.
2. **Pick the final, scalable storage architecture now, no staged "simple v1, migrate
   later" plan.** SQLite structurally cannot support the org-wide, concurrent multi-writer
   tier this design commits to (Premise 4). Postgres+pgvector+AGE is the one engine used
   from single-agent through org-wide, no rewrite implied. Confirmed as the strongest fit
   on accessibility (universally hosted), cost (free-tier everywhere), correctness
   (decades of ACID hardening), and speed (real MVCC concurrency).
3. **Don't attempt statistical causal discovery, ever.** Genuinely unsolved for agent
   memory per landscape research; model causality as an explicit, agent-classified edge
   type instead (v1b).
4. **Multi-tenancy uses one mechanism at three scopes**, not three different systems:
   `group_id` namespacing for single-agent, multi-agent-same-user, and org-wide (with ACL
   on an `access` field). **v1 ships the first two scopes only; org-wide + ACL is v1.1.**
   The storage engine already supports the third scope's concurrency needs, v1.1 adds
   auth/ACL logic on top, not a database migration.
5. **This still solves the founder's own real pain** (cross-tool context loss), now scoped
   as something shareable, and now something you fully control the license of, on an
   architecture that doesn't need to be rebuilt as it grows.
6. **Reversed again: v1a is built on Apache AGE from the start, not decoupled from it.**
   The earlier AGE-decoupling correction (v1a ships on plain tables, AGE added later)
   avoided one migration (plain tables → AGE property graph) at the cost of reintroducing
   exactly the thing Premise 2 was created to prevent: a staged "simple now, upgrade
   later" storage decision. Applying the same reasoning that chose Postgres over SQLite
   (final architecture now, not a migration path), AGE is part of the storage substrate
   from v1a onward. **The real cost, stated plainly:** v1a's start now depends on the AGE
   maturity spike passing, since v1a is built directly on AGE property graph tables. This
   removes the "fast signal, AGE risk isolated to v1b" property the decoupling gave up.
   The spike itself is unchanged in rigor (a genuine go/no-go, not a formality, same named
   fallback of plain relational tables if it fails); it just now gates v1a's start
   instead of v1b's. `causal_hint` and PPR remain v1b-only. This reversal is specifically
   about the *storage substrate* (AGE vs. plain tables), not about pulling v1b's features
   forward; those stay staged on their own schedule. Separately, PPR is computed via
   **`networkx`'s `personalized_pagerank`** directly (BSD-licensed, generic graph-math
   library, same category as numpy/scipy, not an AI-memory framework) rather than
   hand-rolled sparse power iteration, unchanged from before.
7. **The core mechanism this project depends on, agents reliably calling `write_episode`/
   `query_memory` at natural checkpoints, is unverified and gets spiked before v1a
   proceeds.** An outside-voice review pointed out that if Claude Code/Cursor don't
   reliably invoke these tools without being told to, the whole value proposition (reduced
   re-explaining) doesn't materialize regardless of how good the retrieval is underneath.
8. **The differentiation is the data structure/algorithm layer, not the storage engine. Postgres stays, a new DBMS is explicitly rejected a second time.** Repositioned after
   this session's second pass: the product's actual value isn't "we have a graph and
   multi-tenancy," it's long-horizon memory quality, how well the system remembers,
   consolidates, and retrieves over time. Building a new database engine (transactions,
   durability, concurrency, crash recovery) to chase that would repeat the exact mistake
   Premise 1 already walked through and rejected once (a multi-year undertaking that
   produces zero memory-quality differentiation, since durability isn't where "remembers
   things well" lives). The novel work goes into the Temporal Hierarchical Memory Graph
   (below), a real data structure and retrieval algorithm, running on Postgres's proven
   storage, not replacing it.

## Long-Horizon Memory Architecture (added: this is the actual differentiator)

**The problem this section exists to solve:** a naive "keep every fact forever, traverse
the whole graph on every query" design degrades as history accumulates, more candidates
to search, more noise diluting retrieval, and (per the storage discussion below) growing
query cost. "Long-horizon" memory needs a structural property none of the design so far
provides: retrieval cost that stays bounded even as total accumulated history grows
unboundedly. This is where the actual R&D goes: not the storage engine (stays Postgres,
per Premise 8 below), but the data structure and algorithm for how memory is organized,
consolidated, and fetched.

### Storage substrate honesty (why Postgres+AGE, not a new DBMS)

**Corrected after an external review** (see Foundational spike section for the full
finding): an earlier draft justified pulling AGE forward from v1b into v1a as "index-free
adjacency, O(1) pointer-chase per hop, not an indexed join." That's wrong. AGE stores
graph elements as `agtype` (a JSONB-based type) in ordinary Postgres tables; Cypher
traversals compile to joins over those tables, resolved through indexes, same relational
storage model as everything else in Postgres. True index-free adjacency (Neo4j's model)
is a different storage engine, not a query language on top of one. And the property
wouldn't have mattered anyway: PPR pulls an entire subgraph in one bulk query, builds the
graph in application memory (`networkx`), and runs power iteration there; it never asks
Postgres to traverse hop-by-hop in SQL, so the operation the claimed property would have
accelerated isn't one this system performs.

**The real justification, and it doesn't require reversing PR0a's result:** Cypher is
genuinely more expressive than recursive CTEs for the ad-hoc, arbitrary-depth traversal
queries this system writes outside the PPR path (entity resolution's alias lookups,
`echo-memory why`'s audit trail, future multi-hop query features), and pattern matching
reads far better than the equivalent SQL. Weighed against real costs: AGE is a young
extension, `agtype` doesn't compose cleanly with plain btree/GIN indexes (worked around in
PR1's schema via `properties ->> '"key"'::agtype` expression indexes), and it complicates
`group_id` partitioning (open question below). **PR0a's actual measured result stands
independent of this correction**: real Cypher traversal latency (8-13ms for a 10K-edge
graph, see Foundational spike) was measured directly, not inferred from the
index-free-adjacency claim, so the empirical go/no-go is unaffected. What changes is only
the *reason* AGE was worth spiking before v1a rather than after; if that reason (Cypher
expressiveness for the non-PPR traversal queries) doesn't hold up under scrutiny either,
the plain-table fallback serves the two named access patterns (indexed 1-2 hop lookups,
bulk group-scoped scans) at full speed and this reverts to a v1b question. Either way,
building an entirely new database engine (transactions, durability, crash recovery,
concurrency control) to avoid this tradeoff would cost 6-12+ months before any of it is
trustworthy enough to store real data on. That's the same trap already avoided once this
session (see Premise 1's "fully from scratch" discussion). The differentiation lives in
the data structure and algorithm layer above storage, not in reinventing storage.

### Temporal Hierarchical Memory Graph (the actual novel structure)

Three tiers, not one flat graph:
- **Hot tier:** raw episodic facts, full fidelity, what's already designed (edges with
  `fact`, `causal_hint`, `confidence`, provenance).
- **Consolidated tier:** periodically, clusters of related facts that co-occurred
  temporally get summarized (via LLM) into a higher-level node, e.g. 50 individual facts
  about iterating on a decision collapse into one consolidated node stating the outcome
  and rationale, with edges back to every raw fact it summarizes. **Consolidation
  demotes; it never deletes.** Raw facts remain queryable (and remain what
  `echo-memory why`'s audit trail resolves against), just not part of the default
  retrieval path once consolidated.

  **Traceability is not preservation, and this design currently confuses the two.**
  Keeping an edge back to all fifty facts proves where a summary came from. It does not
  establish that the summary still says what they said. A summariser collapsing an
  iteration into "the outcome and rationale" is being asked, in as many words, to drop
  sequence, and sequence is where qualification and exception structure live: "we chose
  X" is a faithful summary of a thread that concluded "X, unless the queue is backed up,
  in which case Y", and an agent reading the summary acts on a claim nobody made.

  Retrieval metrics cannot see this. Hit rate and recall measure whether the right node
  came back, and the right node did. So consolidation needs an acceptance test that does
  not derive its questions from its own answers: generate claims from the source
  subgraph before and after, and compare temporal ordering, qualifiers, causal direction
  and exception coverage. Raised by Jayasurya Mahadevan by email on 2026-09-21, against
  this paragraph, before any of it was built.

  Two consequences for whoever builds it. The invariant check is a gate on shipping
  consolidation at all, not a metric to add afterwards. And "via LLM" above means the
  server calls a model on a background path, which is compatible with the write path
  never calling one but makes `docs/WRITE-COST.md` a claim about writes specifically
  rather than about the service, so that document needs a sentence when this lands.
- **Archived tier:** consolidated nodes that stop being accessed at all eventually
  demote further, excluded from retrieval by default, but never deleted (append-only
  principle holds throughout; see the audit log design).

**Node/edge schema addition:** `tier: enum (hot | consolidated | archived)`,
`temperature: float` (see decay formula below), `last_accessed: timestamp`.

**Temperature, decay/reinforcement scoring (spaced-repetition/cache-eviction inspired,
not a novel formula, but a deliberate choice among known ones):**
```
temperature(e, t) = access_count(e) · exp(-λ · (t - last_accessed(e)))
```
Frequently and recently accessed facts stay "hot" (high temperature); facts nobody
queries decay exponentially toward zero. `λ` (decay rate) is a tunable constant that
needs empirical tuning against real usage patterns, not a guessed value (added to Open
Questions below).

**Consolidation trigger:** periodically (e.g. after N new writes to a `group_id`, or on a
schedule), cluster hot-tier facts whose temperature has dropped below a threshold AND
that are temporally/topically co-located (same session, or within k hops in the graph,
within a time window), summarize via LLM, demote the originals to `consolidated`-tier
provenance of the new summary node.

**Bounded-cost retrieval, the actual payoff:** `query_memory` searches the
consolidated tier first (small, high-signal, cheap, a partial index on
`tier = 'consolidated'` keeps this fast regardless of total hot-tier size), and only
descends into the hot tier for nodes that pass a relevance threshold from the
consolidated-tier search. This bounds total candidates examined per query independent of
how much raw history has accumulated, the actual mathematical property "long-horizon"
requires, since retrieval cost stops growing linearly with total stored history.

**Why this is genuinely novel, not just "use HippoRAG" or "use GraphRAG":** HippoRAG's
associative PageRank retrieval has no consolidation or decay. It treats the whole graph
as equally "live" regardless of age. GraphRAG's community summarization happens once at
ingest time, not as an ongoing, access-pattern-driven process. This design combines
temporal decay (spaced-repetition-style), access-driven consolidation, and bounded
top-down retrieval into one structure addressing the specific problem of *unboundedly
growing* agent memory. None of the surveyed systems do this combination.

**Staging (this is a new phase, not folded into v1b):** consolidation only matters once
there's enough accumulated history for it to trigger. v1a's 3-week trial won't produce
enough volume to need this. This becomes **v1c**, gated on v1b (multi-hop retrieval)
proving out, not on v1a alone. See the updated PR Plan.

## Landscape (researched 2026-08-20, kept as context, not as a dependency list)

The research below is still valuable: it tells us what already exists (so we don't
duplicate solved ideas) and what's genuinely unsolved (so we don't oversell). We're
choosing NOT to depend on any of these systems directly. The schema/retrieval ideas they
validate are being hand-implemented instead.

| System | Open source? | Edge payload? | Retrieval | Causal-aware? | Multi-tenant model |
|---|---|---|---|---|---|
| **Graphiti** | Yes (Apache-2.0) | Yes, bi-temporal validity, provenance, facts | Vector + BM25 + graph traversal | No (temporal, not causal) | `group_id`, "group graphs" |
| **cognee** | Yes (Apache-2.0) | Yes. LLM-typed relations, provenance | Vector + graph traversal | No | Permission-check pipeline stage |
| **HippoRAG / HippoRAG2** | Yes (research-grade) | Triples only | Personalized PageRank over dual-node graph | No | Not a memory service |
| **FalkorDB** | Source-available (unverified redistribution terms) | Whatever schema added | Sparse-matrix Cypher engine | No | Whatever built on top |
| **Letta (MemGPT)** | Source-available | No. JSON memory blocks | Context-window paging | No | Shared blocks, last-write-wins risk |

**What's real vs. research-stage:** rich edge payloads with temporal validity, hybrid
vector+lexical+graph retrieval, and namespaced multi-tenancy are all proven ideas. We're
reimplementing proven *patterns*, not unproven ones. Statistical causal discovery over
memory edges is not solved anywhere; we're not attempting it (see Premise 3). Every
cross-vendor benchmark number in this space is self-serving; treat none as fact.

Sources: github.com/getzep/graphiti, github.com/topoteretes/cognee,
github.com/osu-nlp-group/hipporag, falkordb.com, docs.letta.com.

## Competitor Analysis (extended 2026-08-20, Honcho + graphify)

| System | What's genuinely good | Where it lacks | Where Echo Memory wins |
|---|---|---|---|
| **Honcho** (AGPL-3.0) | Real production infra (Postgres+pgvector, Redis, Docker Compose). "Theory of mind" framing tracks evolving belief about a peer, not static facts. Peer/Session model natively supports multi-agent. Hybrid BM25+vector retrieval validated in production. **Validates our own storage pivot**, same Postgres+pgvector choice, same reasoning (concurrent multi-tenant writes). | Not a graph at all, no edges, no payload, no traversal, no causal typing. AGPL-3.0 copyleft. | Graph structure (v1a, via AGE, in place from day one), causal typing (v1b), multi-hop PPR reasoning (v1b), on the same proven storage foundation. |
| **graphify** (Apache-2.0, installed locally) | Already solves several open problems: an honest EXTRACTED/INFERRED/AMBIGUOUS audit trail per edge (same spirit as this design's `confidence` enum); hybrid AST+LLM extraction; community detection for organization; incremental `--update` with manifest caching; an MCP server mode already built; NetworkX-backed (BSD, tested PageRank), **this design now also uses networkx directly for PPR, per Premise 6**; zero-dependency local operation. | Built for point-in-time corpus snapshots, not episodic memory, no bi-temporal edges, no causal typing, no multi-tenant scoping, no session-based ingestion, no statistical retrieval fusion at query time. | Temporal/episodic model, multi-tenancy, causal typing, retrieval fusion. |
| **Graphiti / cognee / HippoRAG / Mem0 / Letta / FalkorDB** | See Landscape table above. | See Landscape table above. | See Landscape table above. |

**Explicit decision:** despite graphify covering real gaps in this plan, the decision is
to **stay fully from-scratch on the memory/graph logic**. graphify's corpus-snapshot
model carries assumptions (file-based, not episodic-memory-based) not worth inheriting.
**Refined after the outside-voice review:** "from scratch" applies to the memory/graph
*logic* (schema, causal typing, tenancy, fusion). It does not mean re-deriving generic,
already-solved *graph math* like PPR, which is why `networkx` is used directly (Premise
6) rather than hand-rolled and separately validated against the same library.

## Recommended Approach: Staged, Final-Architecture, AGE-Native from v1a

### Foundational spike (before v1a, gates its start, per Premise 6's reversal)

**Apache AGE maturity/performance.** Since v1a is now built directly on an AGE property
graph, this spike moves earlier: it's no longer a v1b-only gate. Stand up Postgres +
pgvector + AGE, model a synthetic graph as an AGE property graph, confirm Cypher
traversal filtered by `group_id` and `t_valid`/`t_invalid` works correctly and at
acceptable latency. **Named fallback if it fails:** plain relational tables (the
previously-decoupled v1a design). The schema shape is designed to support either
without change (see Concrete Schema), so a failed spike doesn't strand any work; it just
means v1a builds on plain tables instead, same as the prior plan. This runs alongside (or
just after) the invocation-mechanism spike; both are gates before any other v1a work.

**Result: GO (run 2026-08-20).** A synthetic tenancy-scoped graph (5 groups, 2000 nodes,
8000 edges, matching the v1a node/edge shape below) loaded into Postgres 16 + pgvector
0.8.6 + AGE 1.5.0. Correctness: zero cross-tenant leaks across 8000 traversed edges, and
Cypher's `t_invalid IS NULL` active-fact filter matched a plain-SQL count exactly (7192
active edges both ways). Latency: 2-hop traversal p50 12.3ms/p95 12.7ms, 3-hop p50
8.0ms/p95 9.1ms, both well under the 200ms budget (design doc's PPR latency target,
reused here as the general traversal bar). One real finding worth keeping: AGE 1.5.0
doesn't support whole-entity `SET n = row`/`n += row` from a map bound via `UNWIND`, or
the openCypher `ALL(x IN list WHERE ...)` list predicate; both have working alternatives
(explicit per-property `CREATE`/`SET`, and named edge variables per hop instead of
`ALL(relationships(p) WHERE ...)`) and neither blocks v1a. Confound caught during the
spike, not after: the published `apache/age` Docker image is amd64-only, so on this
arm64 dev machine the first run went through QEMU emulation and returned a false
NO-GO (2-hop p50 302ms, 3-hop p50 701ms/p95 1285ms) purely from emulation overhead, not
from AGE itself. Rebuilding AGE from source on the native-arm64 `pgvector/pgvector:pg16`
image reproduced identical correctness and the latency numbers above. Production
deployment targets (Linux amd64 cloud VMs) won't hit this emulation tax at all; this was
purely a local-dev-on-Apple-Silicon artifact, worth flagging for `docs/DEVELOPMENT.md`
once PR1 lands so nobody re-triggers the same false signal.

**Correction after an external review (2026-08-21): the *reason* given for spiking AGE
before v1a was wrong; the *result* above wasn't.** "Index-free adjacency" doesn't apply
to AGE (it's `agtype` in ordinary Postgres tables, joins resolved through indexes, not a
different storage engine), and the property wouldn't have mattered anyway since PPR
bulk-scans into `networkx` rather than traversing hop-by-hop in SQL. That reasoning error
doesn't undo the measurement above, which was empirical (real latency numbers, not
inferred from the claim). See "Storage substrate honesty" for the corrected rationale and
what would actually justify keeping AGE.

### v1a, prove the core recall loop (AGE-native storage; no causal_hint, no PPR yet)

**Storage:** PostgreSQL (PostgreSQL License) with **Apache AGE** (Apache-2.0, Apache
Software Foundation-governed) from the start, the one canonical database and graph
substrate from single-agent through org-wide, no staged storage migration at any point
(Premise 2 and Premise 6). Nodes and edges live in an AGE property graph; the audit log
stays a plain Postgres table (it's a log, not something needing graph traversal, see
Concrete Schema). Real MVCC gives correct concurrent multi-writer behavior natively.

**Vector search:** **pgvector** (PostgreSQL License), mature, extremely widely adopted
(including by Honcho), lives in the same database, no separate vector store to sync.

**Lexical search:** Postgres's **native full-text search** (`tsvector`/`ts_rank`,
GIN-indexed, using `plainto_tsquery`/`websearch_to_tsquery`, never raw `to_tsquery` on
user input, per the security review), built-in, not a third-party dependency.

**Fusion:** Reciprocal Rank Fusion combining pgvector + full-text-search scores only (2
signals), v1a's *feature* scope is unchanged by the storage decision above. PPR isn't
part of v1a's fusion yet; that's still a v1b feature-staging decision, separate from
where the graph lives.

**Effort: M**, but now gated behind the AGE spike passing first (see above). The
"fast signal, isolate AGE risk to v1b" property the earlier decoupling provided is
explicitly given up here in exchange for zero future storage migration.

**Primary open risk:** Apache AGE's maturity, moved earlier in the schedule, still a
genuine go/no-go with a real fallback, not a formality.
**Secondary open risk:** does the invocation mechanism (agents actually calling the MCP
tools reliably) work at all? Spiked alongside the AGE spike, before other v1a work.
**Tertiary risk:** entity resolution quality (unchanged from before; see Concrete
Schema).

### v1b, causal typing, multi-hop retrieval (gated on v1a's exit criteria)

**Graph traversal:** already in place from v1a, no migration needed here anymore, since
AGE was the substrate from the start. This phase adds the *features* that use it.

**Causal typing:** `causal_hint` classification (see Concrete Schema) added to the edge
schema via migration; classified at ingestion by the same LLM call that extracts
`relation_type`, going forward, **not backfilled onto v1a-era data** (data ingested
during the v1a trial keeps `causal_hint: null`, which is a valid state, not a gap to fix).

**Graph centrality:** **`networkx`'s `personalized_pagerank`** (BSD-licensed) run over the
graph's adjacency structure, pulled via a Cypher query against AGE. Not hand-rolled, see
Premise 6.

**Fusion:** RRF now combines pgvector + full-text-search + PPR (3 signals).

**Reranking (added after a review of the vector-similarity signal):** cosine similarity
is a bi-encoder method, fast and scalable but structurally lossy since query and
document are embedded independently and never compared together. RRF fusion mitigates
this by combining signals, but the final ranking is still built from cheap first-pass
scores. v1b adds a **cross-encoder reranking** stage on top of the fused candidate pool:
retrieve broad via RRF (vector + full-text + PPR, e.g. top 50), then rerank that pool
with a cross-encoder (a model that scores query and candidate together, capturing
interaction the bi-encoder cannot) before returning the final top-K. This is standard
practice in production retrieval systems, not exotic research. See MATHS.local.md §10
for the full reasoning.

**Effort: M**, smaller than before, since the AGE integration work already happened
before v1a rather than being part of this phase. Reranking adds a modest amount of work
and per-query latency on top.

**Primary risk:** `causal_hint` classification quality, validated via the eval set,
with the explicit caveat that 5-10 items gives directional confidence only (needs a real
precision/recall threshold before the accepted `contradicts`-surfacing feature enables;
see the CEO plan).

**Scaling story for PPR:** full graph power iteration on every query has no scaling story
as the edge table grows indefinitely. v1b sets an explicit latency budget (target: under
200ms for a graph under 10K edges, realistic for a single user's memory over months) and
a stated fallback if that budget is blown: cap the graph size, or recompute PPR on a
schedule rather than per-query.

**Embedding model:** a local model (`sentence-transformers`, no API key, no external call,
no per-call cost), not a framework dependency in the AI-memory-framework sense, just a
generic, swappable embedding library the same way `networkx` is a swappable graph-math
library (Premise 6). **Resolved after this session's architecture pivot** (see Open
Questions): matches graphify's own "no API key needed" precedent, and keeps the server
from ever calling an external LLM/embedding API at all, consistent with pushing
extraction to the calling agent (below). Swappable to a hosted provider (Voyage, OpenAI)
later without touching the rest of the system if local-model quality proves
insufficient; that would be a real, visible tradeoff (cost/key vs. quality), not a
silent default. Applies to both v1a and v1b.

## Concrete Schema

### v1a schema (AGE property graph, or plain Postgres tables if the AGE spike fails)

Node and edge shapes below are identical either way. AGE vertex/edge properties, or
plain-table columns with the same names. Whichever the spike resolves to, the schema
itself doesn't change; only whether traversal happens via Cypher or SQL joins.

**Node (AGE vertex, or table row):**
```
node {
  name: string                    # canonical entity name
  type: string                    # e.g. "tool", "decision", "preference", "person"
  group_id: string                # tenancy scope, same as edge
  created_at, updated_at: timestamp
  aliases: [string]                # alternate mentions resolved to this node; see below
}
```
No separate `id` property: AGE's own `id(n)` graphid is the canonical identifier
(exposed as a string over MCP, per the tool contract). A synthetic `id` property only
made sense while a plain-table fallback was live, so a plain-table primary key would
exist independent of AGE. **Dropped after PR0a's spike resolved GO** (see Foundational
spike): no fallback is being built, so there's nothing left for a separate id to hedge
against.

**Entity resolution (v1a procedure, the most load-bearing piece of v1a, addressed
explicitly):** extraction itself (turning raw conversation text into entities and facts)
happens in the *calling agent*, not the server, per the architecture pivot below: the
calling agent is already an LLM reasoning over this exact text in the same turn, so
`write_episode` receives already-structured `entities`/`facts`, not raw text (see MCP
tool contract). Resolution runs server-side, per entity name, against existing nodes in
the same `group_id`, in two passes: (1) exact/near-exact string match against `node.name`
and `node.aliases` (cheap, deterministic, resolves silently); (2) if no exact match,
embedding similarity (local model, see Recommended Approach) against existing nodes'
embeddings. Below a low threshold: no real candidate, treat as a new node, no ambiguity.
Above a high threshold: confident match, resolve silently, append the new surface form to
`aliases`. Between the two thresholds: genuinely ambiguous, the server does **not** guess
and does **not** call an LLM itself; it returns the mention plus candidate(s) in
`write_episode`'s response as `ambiguous_entities` and defers those specific facts. The
calling agent (which can reason about this immediately, in the same turn) resolves them
by calling `write_episode` again with `entity_resolutions`, per the MCP tool contract.
**Both threshold values are placeholders, now grounded in real measurements rather than
a guess** (see MATHS.local.md §5 and Open Questions): low=0.45, high=0.92, revised after
an external review measured real cosine similarities against the actual embedder and
found the original low=0.75 would have silently missed a true duplicate (`AGE` vs
`Apache AGE`, 0.497). A deterministic guard (never silently auto-merge names differing
only by a negation affix or version token) was added alongside the thresholds, since
short technical identifiers don't separate cleanly on similarity alone. Still revisit
once the v1a trial has real ambiguous-match examples to tune against.

**Edge (AGE edge, or table row):**
```
edge {
  id, source_node_id, target_node_id
  relation_type: string          # semantic type, e.g. "prefers", "decided", "uses"
  fact: string                   # the actual natural-language fact this edge encodes
  confidence: enum                # extracted | inferred | ambiguous, calibration-honest
                                   # label (adapted from graphify's audit-trail pattern,
                                   # not its code) rather than a raw float, since LLM
                                   # self-reported numeric confidence is poorly calibrated
  t_valid, t_invalid: timestamp  # bi-temporal validity
  provenance: {session_id, source_episode_id}
  group_id: string               # tenancy scope; see below
}
```
No `causal_hint` field in v1a, added via migration in v1b (see Recommended Approach).
`access` field is **deferred to v1.1**, no `access` field on v1a/v1b edges.

**Audit log table (v1a):**
```
audit_entry {
  timestamp
  mutation_type: enum       # created | invalidated | fact_superseded | entity_resolved
  affected_edge_ids: [id]   # for fact-level mutations
  affected_node_id: id      # for entity_resolved entries
  before_fact, after_fact: string | null
  session_id: string
  summary: string            # e.g. "invalidated 'uses SQLite', superseded by 'uses Postgres'"
  resolution_detail: string | null  # for entity_resolved: which pass matched (exact/fuzzy),
                                     # and the calling agent's stated rationale for
                                     # fuzzy-pass confirmations (entity_resolutions), if any
}
```
**Renamed after the outside-voice review:** the mutation type formerly called `merged` is
now `fact_superseded`. "merged" was overloaded across three different concepts in
earlier drafts (entity-node resolution matches, this audit mutation type, and colloquial
"duplicate entity" talk), which risked real confusion during implementation. **New:** an
`entity_resolved` mutation type was added specifically so `echo-memory why` and the v1a
exit criteria (which require measuring entity-resolution correctness, duplicate/bad-merge
rates) have an actual audit trail to read, instead of requiring reconstruction from alias
lists and memory.

Mechanism: since the storage layer is owned directly, every mutation (edge write,
invalidation, supersession, or entity-resolution decision) is wrapped in application code
that also writes an `audit_entry` row in the same Postgres transaction, real ACID
guarantees, not a best-effort wrapper.

**`fact_superseded` trigger (v1a procedure):** fires only when two edges resolve to the
same `(source_node_id, target_node_id, relation_type)` tuple after entity resolution, the
same fact stated twice, not a general "these seem similar" heuristic. The older edge is
invalidated (`t_invalid` set), the newer one's `fact` and `confidence` win, and one
`audit_entry` with `mutation_type: fact_superseded` records both `affected_edge_ids`. No
LLM-judged fuzzy merging of *facts* in v1a. That's a real risk (false merges) explicitly
deferred, not silently dropped. (Entity-node resolution's fuzzy pass, above, is different
and does route genuinely ambiguous cases back to the calling agent for a judgment call:
that's a node-identity decision, not a fact-content decision. It's the calling agent's
judgment, via `entity_resolutions`, not a separate server-side LLM call.)

**`group_id` naming convention (two tiers in v1a/v1b, org tier in v1.1):**
- Single-agent: `group_id = "user:{user_id}:agent:{agent_id}"`, e.g. `user:ayush:agent:claude-code`
- Multi-agent, same user (shared): `group_id = "user:{user_id}:shared"`, e.g.
  `user:ayush:shared`, read/written by all of that user's agents.
- Org-wide (`org:{org_id}`) is **v1.1**.

### v1b schema additions (via migration, on top of v1a's tables)

**`causal_hint` column added to the edge table:**
```
causal_hint: enum | null   # caused_by | led_to | enabled_by | blocked_by | contradicts | null
```
`relation_type` vs. `causal_hint`: when `causal_hint` is non-null, `relation_type`
describes *what* the fact is (e.g. `"decided"`, `"uses"`), while `causal_hint` captures
*why/how it relates causally*, never `relation_type: "caused"` alongside
`causal_hint: "caused_by"`, which is redundant.

**`causal_hint` classification (v1b procedure):** since extraction happens in the calling
agent (not the server, per the architecture pivot above), a v1b-aware agent asks itself
the same question while it's already reasoning out `relation_type` for each fact: "did
the source fact directly cause, enable, or block the target fact, per what the
conversation/session explicitly states, not what you infer statistically?", and includes
`causal_hint` as an optional field per fact in `write_episode`'s `facts` payload. Answer
one of the five enum values or `null` if associative only.
Example: "switched to Postgres because SQLite couldn't handle concurrent writes" → edge
`{relation_type: "decided", causal_hint: "caused_by", fact: "switched to Postgres due to
SQLite concurrent-write limits"}`: `relation_type` says what happened, `causal_hint` says
why. "also considered using MongoDB" → `{relation_type: "considered", causal_hint: null}`.

No storage migration needed for this addition. `causal_hint` is a straightforward
column/property addition via the versioned migration tool (CEO plan finding 9A), since
the graph substrate (AGE, per the reversal above) was already in place from v1a.

### Configuration (server startup, per agent, answers "how is scope actually set")

`group_id` is an internal identifier, never typed or constructed by the calling agent.
Each agent's MCP client config (e.g. Claude Code's `.mcp.json`) launches its own Echo
Memory server process with environment variables identifying it:
```
ECHO_MEMORY_USER_ID=<user>              # required
ECHO_MEMORY_AGENT_ID=<agent, e.g. claude-code | cursor>  # required
ECHO_MEMORY_DATABASE_URL=postgres://...  # shared across all of a user's agents
```
Multiple agents (Claude Code, Cursor, etc.) run separate server processes with different
`ECHO_MEMORY_AGENT_ID` values but the same `ECHO_MEMORY_DATABASE_URL`, same underlying
Postgres data, different identity per process. `ECHO_MEMORY_ORG_ID` is added in v1.1.

### MCP tool contract (minimal, stable across v1a → v1b)

**Architecture pivot (this session): the server never calls an external LLM.** The
original contract had `write_episode` take raw `text` and extract entities/facts
server-side via its own LLM call, mirroring the still-unresolved "which LLM provider"
question in Open Questions. Prompted by checking how `graphify` (installed locally, see
Competitor Analysis) solves the same problem: it needs no API key for the common case
because *the calling agent itself* does the extraction (via Agent/subagent dispatch in
the same session), not a separate call the tool's own code makes. Applied here: the
calling agent is already an LLM reasoning over this exact conversation in this exact
turn, so `write_episode` takes already-structured `entities`/`facts`, and the server's
job is storage, resolution, and retrieval, never inference. This also resolves the
embedding-provider question (see above): the one remaining "real math" operation
(embedding similarity) uses a local model, so the server has no external API dependency
at all in v1a.

```
write_episode(
  scope: "solo" | "shared",
  session_id: string,
  entities: [{name: string, type: string, aliases?: [string]}],
  facts: [{source: string, target: string, relation_type: string, fact: string,
           confidence: "extracted" | "inferred" | "ambiguous", causal_hint?: string | null}],
  entity_resolutions?: [{mention: string, resolved_to: id | "new", rationale?: string}]
) -> {
  edges_created: [id],
  ambiguous_entities: [{mention: string, candidates: [{node_id: id, name: string, similarity: float}]}],
  onboarding_sample?: [{fact_id, fact, confidence, causal_hint, provenance}]
} | {error: string}

query_memory(scope: "solo" | "shared", query?, top_k: int = 10, max 100, digest: bool = false) -> {facts: [{fact_id, fact, confidence, causal_hint, provenance}]} | {error: string}
get_audit_log(scope: "solo" | "shared", since?: ISO8601 timestamp) -> {entries: [audit_entry]} | {error: string}
```

`digest` (added during PR-FF2, CEO plan scope item 2) skips ranking entirely and
returns the group's most recently written active facts, newest first; `query` is
ignored and may be omitted when `digest` is true. It's an opt-in "catch me up"
convenience, never triggered automatically.

`onboarding_sample` (added during PR-FF2, CEO plan scope item 6) appears on the
response only for a group's 3rd `write_episode` call ever, populated with that
group's own digest (same shape as a `query_memory` result's `facts`) so a first-time
user sees concretely what got remembered, without a separate call. It's a one-time
nudge, not a recurring field; every other call omits the key entirely.

`fact_id` (added during PR-FF1, see PR Plan) is each fact's edge id, so `echo-memory why
<fact_id>` can look up a specific fact's audit trail without ambiguous free-text
matching; the CEO plan called for this from the start, the original contract just never
carried it through.

`entities`/`facts` reference each other by `name`, not node id: node ids don't exist yet
for genuinely new entities at call time, and the server resolves `source`/`target` names
to ids (existing or newly created) internally. `causal_hint` is accepted from v1a onward
(part of the stable contract) but only meaningfully populated by a v1b-aware calling
agent; v1a agents simply omit it or send `null` (see Concrete Schema's `causal_hint`
classification).

**Entity resolution round-trip:** if resolution surfaces a genuinely ambiguous match
(see Concrete Schema), the server does not create that entity or the facts referencing
it. It returns `ambiguous_entities` and skips those specific facts, creating everything
unambiguous immediately. The calling agent resolves the rest by calling `write_episode`
again, in the same turn, with the same `entities`/`facts` plus `entity_resolutions`
naming what each `mention` actually is; the server is stateless between these two
calls. It doesn't need to remember anything: the second call just repeats the payload
with resolutions attached. `rationale` is optional free text the agent can supply,
stored on the resulting `entity_resolved` audit entry.

`scope` replaces a raw `group_id` parameter: the agent picks "solo" (this agent's own
private memory, `user:{USER_ID}:agent:{AGENT_ID}`) or "shared" (the pool all of this
user's agents read/write, `user:{USER_ID}:shared`); the server resolves the actual
`group_id` internally from its own startup config, per the naming convention above. This
removes any risk of an agent typo'ing or hand-constructing its way into the wrong scope.
v1.1 adds `scope: "org"`, gated by `ECHO_MEMORY_ORG_ID` being configured and the ACL
layer landing.

All three tools return a typed `{error: string}` object on failure rather than raising;
`top_k` defaults to 10, capped at 100; `since` is an ISO8601 timestamp, entries at or
after it. In v1a, `causal_hint` in `query_memory`'s response is always `null` unless a
v1b-aware agent populated it at write time (the field exists in the contract from the
start so v1b doesn't need a breaking API change).

## Open Questions

- **Does the invocation mechanism even work?** (Premise 7) Does Claude Code/Cursor
  reliably call `write_episode`/`query_memory` at natural checkpoints without being
  explicitly told to, or does this require constant manual prompting that undermines the
  whole "reduces re-explaining" value prop? Spiked first, before any other v1a work.
  **Confirmed after the eng-review outside voice:** whether an agent *chooses* to call a
  tool isn't meaningfully CI-testable (it depends on the calling model's behavior, not
  code under this project's control). PR0's spike plus the v1a trial's week of continuous
  real usage is the accepted signal, not ongoing automated coverage. Revisit only if
  invocation drift is actually observed later.
- **Apache AGE maturity/performance**, foundational now, gates v1a's start (reversed
  per Premise 6). Resolved by the PR0a spike before any other work begins, with a named
  fallback (plain relational tables, same schema shape) if it doesn't hold up.
- ~~Embedding model/provider choice~~, resolved: **a local `sentence-transformers`
  model, `all-MiniLM-L6-v2` (384-dim)**, no API key, matching graphify's precedent (see
  MCP tool contract's architecture pivot). A placeholder default picked during PR2, not
  a considered final choice; swappable to a hosted provider later if local quality
  proves insufficient.
- ~~Entity-resolution similarity thresholds~~, revised: **low=0.45, high=0.92** (see
  Concrete Schema), grounded in real measurements against the actual embedder after an
  external review caught the original low=0.75 silently missing a true duplicate. Still
  placeholders pending real v1a-trial calibration.
- **PR2 known limitation:** entity resolution only checks each entity against nodes
  already stored, not against other entities in the same `write_episode` call. Two new,
  near-duplicate names mentioned in one call (confirmed with a real test: "Postgres" +
  "PostgreSQL" together) both resolve as new and create two nodes, since neither has an
  embedding in the database yet to compare against. Revisit if this shows up as a real
  duplicate-node pattern during the v1a trial; see MATHS.local.md §5.
- **External review of MATHS.local.md (2026-08-21), v1b/v1c findings not yet acted on.**
  §5 (entity resolution) and §8 (AGE rationale) are corrected above; the rest is v1b/v1c
  scoped and deferred to when those PRs start, not lost: `ts_rank` has no IDF unlike BM25
  (§2, fixable via `setweight` A/B/D tagging); RRF's PPR seed isn't independent of the
  vector ranker unless seeded from entity links, not vector top-K (§3/§4); PPR's actual
  API is `nx.pagerank(G, alpha=.85, personalization=v)`, and it raises
  `PowerIterationFailedConvergence` uncaught (§4); hub nodes dominate every PPR result
  regardless of seed unless ranked by lift over plain PageRank (§4); the temperature
  formula multiplies an undecayed counter by single-timestamp decay instead of a proper
  decayed sum, and "bounded retrieval" isn't bounded without recursive consolidation (§9,
  the design's stated novelty argument rests on this). Full review, with worked numeric
  examples for each: https://claude.ai/code/artifact/c262494d-e5d0-4a43-88bc-aef63df7868e
- ~~RRF fusion weights~~, resolved for v1a (PR3): k=60, equal per-ranker weight, fixed
  list depth of 50, and score floors (cosine similarity ≥ 0.15, `ts_rank` > 0). All still
  placeholders pending real queries; `k` specifically is low-leverage and not worth
  retuning first (see MATHS.local.md §3). v1b's third signal (PPR) still needs its own
  weight/independence question resolved before it joins fusion (see above).
- **`causal_hint` precision/recall threshold**, the eval set gives directional confidence
  only at 5-10 items; a real number is needed before the `contradicts`-surfacing feature
  (accepted in the CEO plan) is safe to enable, not just "check the eval set."
- ~~License choice~~, resolved: **Apache-2.0**, matching the ecosystem this project's
  dependencies are governed under (Apache AGE, Graphiti, cognee) and offering an explicit
  patent grant, valuable for infrastructure meant to be built on.
- Hosting story for a future managed offering (v1.1): which providers support both
  pgvector *and* AGE together is worth checking before promising a hosted path.
- **Decay rate `λ` and consolidation trigger thresholds (v1c)**, need empirical tuning
  against real accumulated usage, not a guessed constant. Can't be tuned meaningfully
  until v1b has been running long enough to accumulate real history.
- **Consolidation summarization quality**, an LLM summarizing 50 facts into one
  consolidated node is itself a judgment call, same eval-not-mock treatment as
  `causal_hint` and entity resolution's fuzzy pass (see the test-strategy split).
- **Cross-encoder model choice and candidate-pool size for reranking**, not yet decided,
  needs empirical tuning once v1b has real queries to test against (MATHS.local.md §10).

## Success Criteria

### Foundational spikes (gate v1a's start)
1. **DONE, GO (2026-08-20).** Apache AGE spike passes; see the Foundational spike section
   above for the correctness/latency results and the native-vs-emulated-arch confound
   caught along the way. Plain-table fallback not needed.
2. The invocation-mechanism spike (Open Questions) shows agents actually call the MCP
   tools at natural checkpoints without constant manual prompting. This is a gate before
   the rest of v1a is worth building, not just a nice-to-have signal.

### v1a
1. Postgres+AGE (or plain-table fallback) store running via Docker Compose, ingesting
   real session data, including at least one real case where entity resolution correctly
   matches a new mention to an existing node, logged as an `entity_resolved` audit entry,
   not just observed anecdotally.
2. Two working `group_id` scopes demonstrated: single-agent and multi-agent-shared-user.
3. One concrete example where 2-signal hybrid retrieval (pgvector + full-text via RRF)
   answers a real question from your own memory better than either signal alone.
4. An append-only audit log entry for at least one fact mutation and at least one entity
   resolution decision, human-readable enough to answer "why did this happen."
5. A rough cost and latency baseline for a real ingestion + query cycle.
6. **v1a → v1b exit criteria** (over a trial of real cross-tool usage, capped at 3 weeks
   total, a hard cap, not indefinitely extendable): at least 3 real instances where a
   recalled fact saved re-explaining something to a different tool; at most 1 duplicate
   node created by entity resolution; zero cases of two distinct entities incorrectly
   merged into one node. If the bars aren't met, revisit the recall mechanism itself
   before starting any v1b work.

   **Recorded, not remembered (added 2026-08-23, two days into the trial).** Each of
   these three bars is a human judgement, and for the first days of the trial none of
   them had anywhere to be written down: the gate was decidable only from recollection,
   which is exactly the failure mode this whole project exists to fix. `echo-memory
   trial` now records a judgement per bar and `echo-memory status` reports the gate from
   stored data (see PR-FF3 in the PR plan). The automated half only *surfaces candidates*
   for the two error bars - node pairs similar enough that the resolver would have
   flagged them, and every non-exact `entity_resolved` entry - because a resolver that
   could detect its own splits and bad merges wouldn't have made them. The judgement
   stays human; only the bookkeeping is automated.

### v1b (gated on v1a's exit criteria being met)
1. A small eval set (5-10 real facts) manually checked for `causal_hint` classification
   quality, with a concrete precision/recall threshold set (not just "check the eval
   set") before enabling `contradicts` surfacing.
2. PPR (via `networkx`) validated for correctness on a small test graph (using a trusted
   library directly means this is closer to "confirm it's wired correctly" than "confirm
   our reimplementation is correct").
3. One concrete example where 3-signal hybrid retrieval (adding PPR) answers a real
   multi-hop question that 2-signal v1a retrieval missed or ranked poorly.
4. One concrete example where cross-encoder reranking promotes the actually-correct
   result above what RRF fusion alone ranked first, demonstrating the reranking stage
   earns its added latency.

### v1c (gated on v1b, and on enough accumulated history existing to matter)
1. At least one real consolidation event: a cluster of hot-tier facts correctly
   summarized into one consolidated node, with the originals still resolvable via
   `echo-memory why`, not deleted.
2. One concrete example where bounded-cost retrieval (consolidated tier first) returns a
   correct answer without touching the full hot-tier history, demonstrating the actual
   sublinear-cost property this phase exists to deliver.
3. A measured retrieval-latency comparison: with vs. without the consolidated-tier
   shortcut, on a graph large enough for the difference to be visible.

## Distribution Plan

**v1a:** A single-command **Docker Compose** setup bundling Postgres + pgvector + Apache
AGE (or, if the foundational spike fails, Postgres + pgvector only, per the named
fallback) plus a thin process implementing the MCP server. AGE is part of the footprint
from the start, no later Docker Compose change needed when v1b adds causal typing/PPR.
**v1b:** Same Docker Compose setup, no storage changes, just new application code
(`causal_hint` classification, PPR) on top of what v1a already has running.
**v1.1:** Hosted/managed option and org-onboarding, once identity/auth is resolved and the
managed-Postgres-provider question (Open Questions) is answered.
Open-source repository, Apache-2.0 licensed: github.com/ayushcodes10/echo-mem.

## Implementation Language

**Python**, chosen during /plan-eng-review because `networkx` (the PPR library, per
Premise 6) is Python-native, avoiding a cross-language bridge. Mature `pgvector-python`
and `psycopg` clients; Alembic for versioned migrations; the official MCP Python SDK.

## PR Plan (dependency-ordered: this repo is open source, so small independently
reviewable PRs matter more than a single large landing)

| PR | Scope | Modules | Depends on |
|---|---|---|---|
| PR0a | **Apache AGE maturity spike**: model a synthetic graph as an AGE property graph, confirm Cypher traversal filtered by `group_id`/`t_valid`/`t_invalid` works correctly and at acceptable latency. Real go/no-go; throwaway script/findings, not merged as product code. Fallback if it fails: plain relational tables (schema shape unaffected, per Concrete Schema) | N/A | N/A |
| PR0b | Invocation-mechanism spike (does Claude Code/Cursor actually call MCP tools reliably?), throwaway script/findings, not merged as product code | N/A | N/A |
| PR1 | Repo scaffold, CI (lint+test on push), Postgres+pgvector**+AGE** Docker Compose (or Postgres+pgvector only, per PR0a's fallback), migration tooling (Alembic), schema (node/edge as AGE property graph, audit_entry as a plain table, + pgvector ANN index + GIN full-text index + composite indexes on `(group_id, source_node_id)`/`(group_id, target_node_id)`/`(group_id, t_valid, t_invalid)`) | `infra/`, `migrations/` | PR0a **and** PR0b pass |
| PR2 (Lane A) | Entity resolution + `write_episode`, connection pooling, structured server-side logging, input-size cap | `ingestion/`, `db/` | PR1 |
| PR3 (Lane B) | Retrieval (pgvector+FTS+RRF, 2 signals) + `query_memory`, `top_k` cap, `plainto_tsquery`/`websearch_to_tsquery` (never raw `to_tsquery`) | `retrieval/` | PR1 |
| PR4 | Audit log wiring (`entity_resolved` + `fact_superseded` entries, same-transaction writes) + `get_audit_log` | `audit/` | PR2 |
| PR5 | MCP server packaging: wire all 3 tools together, localhost-bound only, typed `{error}` contract end-to-end | `mcp-server/` | PR2, PR3, PR4 |
| PR-FF1 | `echo-memory why`/`export` CLI | `cli/` | PR4 |
| *(v1a exit-criteria trial begins, real usage against the CEO plan's gated window, no new code)* | | | PR5 **and** PR-FF1 |
| PR-FF4 | Project dimension (`project` + `agent_id` on every fact, resolved server-side from cwd), deterministic capture queue fed by a memory-file hook, and a per-project dashboard with an edge inspector. Same "no new code" exception as PR-FF3: the trial was accumulating memory it could not attribute to a project, and every episode written before the dimension existed is one more edge to backfill by guesswork | `trial/`, `infra/`, `ingestion/`, `cli/`, `migrations/` | PR-FF3 |
| PR-FF3 | Trial instrumentation: record a judgement against each of criterion 6's three bars, surface duplicate-node and bad-merge candidates for review, report the gate in `echo-memory status`. A deliberate exception to the trial's "no new code": the trial was running with no way to record the very observations that decide its outcome | `trial/`, `cli/`, `migrations/` | PR5, PR-FF1 |
| PR-FF2 | Onboarding nudge + context digest | `ingestion/`, `retrieval/` (additive) | PR5 |
| PR-B1 (v1b, gated on v1a exit criteria, Lane A) | `causal_hint` classification at ingestion, a column/property addition via migration, no storage-substrate change needed (AGE was already in place from PR1) | `ingestion/`, `migrations/` | v1a exit criteria met |
| PR-B2 (Lane B) | PPR via `networkx.personalized_pagerank`, extend RRF to 3 signals | `retrieval/`, `graph/` | v1a exit criteria met |
| PR-B3 | Cross-encoder reranking of the fused RRF candidate pool before returning results | `retrieval/` | PR-B2 |
| PR-B4 | `contradicts` surfacing (query-time + local notification), gated on the causal_hint quality threshold | `retrieval/`, `notifications/` | PR-B1, PR-B2 |
| PR-C1 (v1c, gated on v1b) | `tier`/`temperature`/`last_accessed` schema addition, partial index on `tier = 'consolidated'`, table partitioning by `group_id` | `migrations/` | v1b shipped |
| PR-C2 | Temperature decay/reinforcement scoring + consolidation trigger (clustering + LLM summarization) | `consolidation/` | PR-C1 |
| PR-C3 | Bounded-cost top-down retrieval (consolidated tier first, descend to hot tier on relevance threshold) | `retrieval/` | PR-C1, PR-C2 |
| PR-1.1-* (v1.1) | Identity/auth → ACL enforcement → sensitive-data redaction, in that order | `auth/`, `db/`, `ingestion/` | v1b shipped |

**Parallel lanes:** PR0a ∥ PR0b (independent spikes, both gate PR1). PR2 ∥ PR3 once PR1
lands (PR1 establishes the shared `db/` connection-pooling utility once, so PR2/PR3 only
import it). PR-B1 ∥ PR-B2 once v1a's exit criteria are met, for the same reason: no
shared modules between them.

**Note:** this resequencing also retires a risk the eng review's outside voice flagged
against the old plan. AGE's internal `graphid` identity scheme no longer needs to be
reconciled against pre-existing plain-table IDs from a v1a trial, since there's no
plain-table-to-AGE migration happening after real data exists. That entire risk class is
gone, not just mitigated.

CI (basic: lint + test on push) is set up in PR1, not deferred, since it's cheap now and
catches regressions immediately as the rest of the PRs land.
