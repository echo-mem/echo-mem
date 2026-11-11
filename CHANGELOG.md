# Changelog

Notable changes per released version. Anything that changes what a user gets,
what a number means, or what the database looks like belongs here; refactors
and test work do not.

Versions before 0.4.0 predate this file. Their history is in the git log and in
the pull requests, which carry the reasoning rather than just the diff.

## Unreleased

**A fact can now record whether it was ever checked, and by whom.**
`confidence` says how the writer came by a claim, which is a statement about
their process: an agent that inferred something confidently and wrongly leaves
exactly the same row as one that read it off a deploy log. Three new tables
close that gap.

`evidence` holds the artefact - a deployment, a test run, a human correction,
a metric, a document, an attestation head recorded elsewhere - with a digest
of what it said at the time, so a document edited after it was cited is
distinguishable from one that was not. `fact_evidence` links one artefact to
every claim it backs, and the link is a separate call from recording the
artefact because late arrival is the normal case: the test run that confirms a
decision happens after the decision was written.

`fact_verification` is the check itself, append only, newest row per fact
wins. Six states rather than a score, for the reason `trust/states.py` gives
at length: the one calibration this project has measured came out AUC 0.766
with a 95% interval of [0.437, 0.968] over five positives, an interval that
includes chance. Exactly one state, `rejected`, removes a fact from an answer,
and only an authorised verifier can set it.

**An expired verification degrades a fact, it never hides one.** A `verified`
row whose `expires_at` has passed reads as `stale` and the row is left
untouched. The fact is still returned and still findable, only ranked lower.
A lapsed check is not a refutation, and rewriting the row to say so would
destroy the evidence that the check ever passed. Expiry is judged against the
database's clock rather than the caller's, because a fact that flips between
`verified` and `stale` depending on which process asked is the container-skew
bug this project has already shipped once, with a worse symptom.

**Verifications get their own tamper-evident chain, separate from the facts'.**
`verification_attestation` mirrors `fact_attestation` and shares nothing with
it. Pouring verifications into the existing chain would mean every check
shifts the sequence numbers a fact's own digest chains through, so recording a
verification would alter the evidence about the claim being verified.

`ECHO_MEMORY_VERIFICATION_KEYS` holds `key_id:hexkey` pairs and
`ECHO_MEMORY_VERIFICATION_KEY_ID` names the one that signs, so a key can be
rotated: each link records the key that signed it and is verified with that
key. Without this, rotating would make the entire audit trail read as forged,
which is the same as having no rotation at all. A link whose key is no longer
configured is reported as unverifiable rather than altered, because being
unable to check a link is not evidence that it changed. As with
`fact_attestation`, unkeyed this is only as strong as a record of the head
kept outside the database, and `report().strength` says which is in force
rather than implying a guarantee that is not there.

Migrations 0031 and 0032. Nothing on the read path consumes these yet.

**Outcomes can be reported by machines, and conclusions are derived from them
rather than stored.** Until now this store could record that a decision was
made and never that it worked. The half that was missing arrives as two new
tables' worth of plumbing and no new data model: a decision is a `Node`, an
outcome is a `Node`, and "this decision led to that outcome" is a `FACT` edge
with `causal_hint=led_to`. So `trace_cause` already walks it, supersession
already retires an outcome that turns out wrong, erasure already reaches it and
attestation already covers it.

Reporters are CI runs, deploys and test suites, none of which speaks MCP. They
send a validated envelope to `public.event_inbox` (migration 0033) and a single
drain per graph applies it later. The latency is forced, not chosen:
`write_episode` locks per scope while Apache AGE keeps its label catalogue per
graph, so two writers in two scopes of one graph still corrupt it - four
benchmark shards writing four scopes killed three of the four with `relation
"echo_memory.-775.1utterance" does not exist`, and the run looked complete from
outside. A multi-tenant endpoint that writes straight through is that condition
permanently.

`event_id` is caller-supplied and the primary key, so a CI system that retries
on a timeout gets a free duplicate rather than a second deployment record or a
dropped one. An event is applied through `write_episode` and never around it,
and its result is checked: `unchanged` and a deliberately fact-free episode
count as applied, everything else is a `rejected` row carrying the reason.

A **lesson** - "deciding X tends to go badly" - is derived at read time and
never written. It needs two agreeing outcomes, unanimous, each `verified` or
`evidence_backed`; `mixed` and `unknown` outcomes are evidence the decision was
taken and are dropped before any counting, so ten of them yield nothing rather
than something weak. The basis is `verified_causal` only when every supporter
was verified by an authorised verifier naming a method, and `correlational`
otherwise, because evidence is not proof. Retiring one supporting outcome
weakens the lesson immediately and deletes nothing, which a stored conclusion
could not do.

`claim.verified` events need verification authority the reporting types do not.
A CI token can attach evidence all day and cannot say the evidence was
sufficient, and `verified` is the only state a lesson counts as causal.

Migration 0034 adds three partial indexes so asking for a scope's decisions
does not scan its nodes. `Node.type` stays free text: the server calls no model
and cannot tell a node typed `decision` from a decision, so the reserved words
are a convention the reading layer honours rather than a constraint that would
refuse nodes customers already have.

There is still no inference provider. `infra/provider.py` ships `NullProvider`,
`load({})` returns it, and a provider may only ever propose `correlational`
candidates off any read or write path.

**Memory can cross an organisational boundary, as a signed file rather than an
API.** `echo-memory exchange` builds a redacted bundle from one scope, signs
it, and on the other side verifies it and writes its facts into a quarantine
scope that ordinary retrieval is not given. There is no endpoint one store
calls on another, no shared trust root, and no live protocol.

An imported fact is somebody else's claim and is treated as one. It lands at
`confidence: inferred` whatever the exporter called it, carries content class
`imported-untrusted` and trust state `unverified`, and becomes readable only
when a named verifier promotes the bundle. `readable_scopes` is the only
function that hands out an import scope and it hands out promoted ones, so a
caller building its list from there cannot include a quarantined bundle by
accident.

Four decisions worth stating because each of them is a way this could have
been worse:

- **Redaction runs before signing.** The signature covers what was actually
  sent, so a recipient verifying it is verifying the bundle they received
  rather than one redacted afterwards - and a carrier that strips claims from
  a signed bundle cannot be mistaken for the exporter having done it.
  `exchange export` prints the audit, counted by the rule that removed each
  claim, before anything is written.
- **`verify` takes its trust anchors as an argument and has no default.** A
  verifier that reads its anchors from the same place the bundle came from is
  not verifying anything. An empty anchor set refuses every bundle, including
  a correctly signed one.
- **The signature algorithm comes from the anchor, never from the bundle.** An
  ed25519 anchor is a public key, published by design. If the bundle chose,
  anyone holding that key could use it as an HMAC shared secret and sign as
  its owner; there is a test for exactly that forgery.
- **An imported fact gets a new local `graphid`.** An AGE graphid is unique
  per database rather than globally, so two deployments will issue the same
  one for unrelated facts. The origin survives as
  `provenance.origin = "<deployment_id>:<original_fact_id>"`, and nothing
  joins on it. An attestation chain is also per scope, so an imported fact
  cannot carry the origin's; the manifest records the exporter's chain head
  instead, for comparison against one they published somewhere the importer's
  database cannot reach.

A replayed `bundle_id` is refused, because a bundle is a file and a file gets
replayed - and the second import would write every fact again as new local
nodes nothing afterwards could tell apart. Signature, digest and replay are
all checked before the first row is written, and the writes are one
transaction, so a bundle holding one fact this store cannot accept is refused
whole rather than leaving a subset nobody can identify.

**Revoking a bundle cannot recall facts already returned into an agent's
context.** They are in that conversation's history, in whatever the agent
wrote from them, and in anything downstream that kept the output. That is a
property of having handed data over rather than a gap, no further work here
would change it, and `exchange export` says so before it writes a bundle,
which is the last point at which the decision is still yours.

None of these is an MCP tool. `migration 0035` adds `bundle_registry`.

**Two writers in two scopes of one graph were never serialised, and nothing
tested it.** `write_episode` takes a per-scope advisory lock, so two agents
writing one scope are safe and there is a test. Apache AGE's label catalogue
is per GRAPH, so two writers in different scopes take different keys and
proceed genuinely concurrently, which is the condition that killed three of
four benchmark shards with `relation "echo_memory.-775.1utterance" does not
exist`.

`infra/graphlock.py` adds `graph_write_gate`, a graph wide lock, default one
writer per graph, turned off by `ECHO_MEMORY_MAX_GRAPH_WRITERS` above 1. It
is **not yet wired into the write path**; what ships here is the gate, its
cost and its tests.

The cost is real and is stated where the gate is. Eight writers, 500
one-fact episodes each into its own scope, Postgres 16 with AGE under Colima:

| configuration | run 1 | run 2 | episodes/s |
|---|---|---|---|
| no gate | 46.5s | 40.2s | 86 to 100 |
| gate, limit 1 | 122.1s | 112.8s | 33 to 35 |
| one writer alone, 500 episodes | 11.1s | | 45 |

Eight gated writers are **slower than one unimpeded writer**, because the
handoff is not free. Size an ingress for one writer's throughput minus a
quarter.

**The corruption did not reproduce on this hardware, and the tests say so
rather than implying otherwise.** 4, 8 and 16 concurrent cross-scope writers,
about 30,000 episodes, zero errors and an intact label catalogue every time.
`tests/integration/test_graph_concurrency.py` carries the full table of what
was tried and is a regression guard, not a reproduction. What it does show
positively is that cross-scope writers overlap in time today, so the hazard
window is real whether or not a laptop can make AGE fall into it.

**`hashtext` is 32 bit, which makes one of this package's stated guarantees
false at a measurable rate.** `attest()` says it locks per scope "because
scopes are tenants, and one account's writes must not be able to block
another's". Over 400,000 group ids in this store's shape, `hashtext` gives 22
colliding pairs and `lock_key`'s 64 bit derivation gives 0. A collision is
not corruption; it is two tenants blocking each other for the length of a
write, silently, looking like latency.

**What `instruction_like` catches is now a number.** `retrieval/envelope.py`
says false negatives are expected, which is the right claim and the wrong one
to leave unquantified. Measured over a 32 shape attack corpus and 24 real
memories: **24 of 32 flagged (recall 0.750)** and **4 of 24 benign memories
flagged (false positive rate 0.167)**. Both are pinned exactly, in both
directions, so broadening a pattern means re-reading the benign side rather
than passing a `>=`.

The four false positives are all genuine memory shapes - "you are now the
owner of the org", "the new rules of the billing plan" - which is why
`flagged` reports instead of dropping. Of the eight misses, four are one
narrow gap: `above` is in the ignore/disregard alternation and was meant to
be caught, but one intervening article defeats it, so "ignore the above" does
not match.

**Counters and timers that cannot log user memory.** `infra/metrics.py` is a
dict under a lock, emitted as one JSON line through the existing logger, with
an OpenTelemetry bridge on by default. `opentelemetry-api` turns out to be
present in every install already, as a transitive dependency of `mcp` rather
than anyone's choice; with no SDK configured its record costs 0.044 us and
nothing leaves the process. `ECHO_MEMORY_OTEL=0` turns it off. Label keys
that would carry fact, entity or query text are replaced with a visible
marker rather than dropped, and a label whose value varies per call cannot
grow the registry without bound.

**Migration 0036 adds no index, and records what was checked.** Every table
was listed against its indexes and the nine with only a primary key were each
checked against how they are queried; in every case the key is the access
path. The one real gap cannot be fixed with an index: `attestation.claims_of`
reads one fact by id on the write path, through Cypher, and AGE expands the
match before filtering, so the plan is a sequential scan of `FACT`. Adding an
unconditional id index changes neither the plan nor the time (3.75ms against
3.74ms at 3,000 edges); the same read against the edge table directly is an
Index Only Scan at 0.026ms. An index that does not change a plan costs write
amplification and leaves a comment claiming a query is fast.

**Several scopes are routed rather than fanned out, and the answer fits a token
budget.** `query_across` costs one full `query_memory` per scope, so an account
with eight memories paid eight times over for an answer that lived in one.
`retrieval/routing.py` decides which scopes are worth asking with **zero model
calls**, generalising the technique `needs_expansion` already uses: tokenise the
query, find the nodes whose name contains a term, count the live facts hanging
off them. One indexed query per scope, and no embedder anywhere near it, because
a vector probe per scope IS a query_memory and routing has to be cheaper than
the thing it decides about.

`routing.select` keeps the scope a caller named as its default whatever it
scored, and refuses a default from outside the authorised scopes. Routing that
can exclude a configured scope loses data and the answer comes back shaped
exactly like a correct one.

`retrieval/context.py`'s `assemble` composes routing, `query_across` and the
budget. With one scope and no budget it returns exactly what `query_across`
returns, byte for byte, so every published number is still the baseline. The
answer carries which scopes were asked, which were rejected and why, and what a
budget dropped, because an answer thinned by a budget otherwise looks exactly
like a thin store.

Weights proposed from the scores are **untuned**, as `weights_for` says in as
many words; `propose_weights=False` is plain RRF.

**The derived scratchpad has a number, and it loses.** `retrieval/scratchpad.py`
shipped on BEAM's claim that "the scratchpad alone suffices" and no measurement.
`eval/scratchpad_eval.py` compares it against `query_memory` at matched token
spend over a 60-fact synthetic corpus:

| shape | measure | query_memory | scratchpad | Δ | 95% CI |
|---|---|---:|---:|---:|---|
| entity_pair | MRR | 0.9639 | 0.0347 | -0.9292 | [-0.9736, -0.8764] |
| entity_pair | recall@10 | 1.0000 | 0.0667 | -0.9333 | [-0.9833, -0.8667] |
| prose | MRR | 1.0000 | 0.0347 | -0.9653 | [-0.9958, -0.9222] |

No interval contains zero. The block reaches the ranker's recall at 16x the
tokens, at which point it holds the whole scope and is the "inject everything"
baseline rather than a cheaper context.

The limitation is written down beside the result: `build_cases` makes one case
per fact, so presence is `k/n` by construction and the experiment **cannot
credit or discredit centrality ranking at all**. It is also the question
distribution least favourable to any query-independent block, and it is not the
one LIGHT's ablation ran on. Nothing about `scratchpad.py` changes on the
strength of it. See docs/BENCHMARKS.md.

**The ablation table reports an interval for recall@10, and it changes what
several past experiments mean.** Until now `compare` bootstrapped the
per-case reciprocal ranks and nothing else, so every row carried an interval
for ΔMRR and a bare point estimate for Δrecall@10 - the metric most retrieval
changes here actually move.

On the multihop shape at n=120 that was the difference between "no effect"
and a result:

| configuration | ΔR@10 | 95% CI | ΔMRR |
|---|---|---|---|
| + graph hop | **+0.1083** | [+0.017, +0.200] | +0.0034 ? |
| + routed expansion | **+0.0917** | [+0.008, +0.167] | +0.0131 ? |
| + expansion at 0.2 | **+0.0667** | [+0.025, +0.117] | +0.0041 ? |

`?` marks an interval containing zero. Every one of those rows was read as
noise on the strength of its ΔMRR interval, and every one moves recall@10 by
an amount whose interval is clear of zero.

`docs/BENCHMARKS.md` asked, in the weighted-expansion section, whether a small
recall gain at no MRR cost was worth having and answered "not answered,
because the harness bootstraps an interval for ΔMRR and not for Δrecall@10".
It is answered now, on this store: at weight 0.2, **+0.0667 [+0.025, +0.117]**
recall@10 with ΔMRR inside noise.

`Result` keeps the per-case rank for this. `hits_at` counts the same thing and
cannot be resampled: a bootstrap needs the per-case values, not their total.

**The embedding model is selectable, and it is the ceiling.** `LocalEmbedder`
has described `all-MiniLM-L6-v2` as "a placeholder default pending real
quality tuning, not a considered final choice" since the first commit. On
this author's store its embeddings have an effective rank of **43 of 384** by
participation ratio, with half the variance in nineteen dimensions and a
shared mean direction of norm 0.416. Anisotropy that severe is why unrelated
facts score as similar, and it is what `recall@1` of 0.087 on BEAM's 1M split
looks like from the outside.

`ECHO_MEMORY_EMBEDDING_MODEL` selects another one. **No default changes.**

Bounded by the schema on purpose: the embedding column is `vector(384)`, so
an unknown name is refused with the list of known 384-dimension models
*before* a few hundred megabytes are downloaded, and the loaded model's real
width is checked as well.

**The fusion never looked at the content again, and that was three failures
in one.** Reciprocal rank fusion sees rank position and nothing else, so every
list it is handed gets a top slot worth `1/(k+1)` whoever produced it. The
graph hop (-0.142 MRR on entity_pair), routing it per query (-0.0002 on the
shape it was built for) and session expansion (-0.107 hit@10 on BEAM 100K) all
added a list ordered by something that was not the query, and all three lost.

`query_memory(rerank=True)` reorders the fused candidates by similarity to the
query before truncating. The fusion still chooses which candidates are in
play, which is what it is good at; cosine decides their order, which it cannot
do at all. The embeddings are already stored and indexed, so this scores at
most `LIST_DEPTH` of them.

Measured on this author's store, n=200 per shape:

| shape | shipping | + rerank | + rerank + hop |
|---|---|---|---|
| entity_pair MRR | 0.741 | **0.848** | 0.848 |
| multihop MRR | 0.176 | 0.209 | **0.221** |
| multihop recall@10 | 0.570 | 0.680 | **0.720** |

Intervals clear of zero on both: +0.1063 [+0.0622, +0.1501] and +0.0444
[+0.0270, +0.0618].

It also **rescues the graph hop**. Alone the hop costs -0.215 MRR on
entity_pair; with the rerank that damage is gone and multi hop gains, because
the hop was always finding the second fact and ranking it badly. Still off by
default until BEAM confirms it on questions nobody derived.

**The embedder never saw half the text.** `all-MiniLM-L6-v2` truncates its
input at 256 word pieces, about 1,500 characters. The benchmark loaders
chunked at `MAX_FACT = 3500`, which is `write_episode`'s refusal limit and
has nothing to do with the embedding window. Nothing had ever compared the
two numbers.

The consequence is silent. A 3,000 character turn is stored whole, embedded
to its first ~1,500, and the back half can never be retrieved by the vector
channel because the model never read it. BEAM's turns are median 1,226
characters with p90 3,226, so this is most of its long ones, and it explains
the shape of those results: `session@10` 0.945 against `hit@10` 0.621 is what
"the turn is findable through its neighbours but its own tail is invisible"
looks like.

Chunking is now bounded by `EMBED_WINDOW = 1100`. On a BEAM conversation that
turns 32 stored turns into 66 retrievable units, median 850 characters, every
one inside the window, with the text unchanged.

It cuts the context cost at the same time: a returned unit was a 2,200
character turn of which a line or two was relevant, so thirty of them cost
about 16,400 tokens. At this size the same thirty cost about a third of that.

**The chunker also cut mid word.** When no sentence or paragraph boundary fell
in the back half of the window it cut at the window edge, leaving half a token
in each chunk - a word neither the embedder nor BM25 can match. Latent while
the window was 3,500 characters and the fixtures had boundaries; the moment it
shrank it started landing inside words. It falls back to the last space now.


**Benchmark scopes are keyed on the dataset file, and the old ids do not
match.** Every published corpus numbers its instances within a file, and all
three ship more than one: BEAM restarts `conversation_id` at 1 in each of its
four splits, LongMemEval publishes `_s`, `_m` and `_oracle` carrying the same
`question_id` over haystacks of very different sizes, and LoCoMo's `sample_id`
means nothing outside `locomo10.json`.

Scoping on that id alone meant a run over one file overwrote the scopes of a
run over another, and two runs at once wrote into the same `group_id` and
corrupted each other's haystacks. Nothing raises when it happens: a haystack
half overwritten by another file scores as a retrieval failure and reads as a
result.

Scopes now carry the file's digest, so the next benchmark run re-ingests once.


**`eval-external beam` runs BEAM, the third corpus.** *Beyond a Million
Tokens*, ICLR 2026: 100 conversations and 2,000 **human-written** questions at
128K, 500K, 1M and 10M tokens, over ten memory abilities. It is the first
corpus here whose questions were not derived from their own answers, and the
first that reaches a scale where this store's lack of consolidation is the
thing being measured rather than a caveat.

Abstention questions are kept and are **not** scored as retrieval misses.
There is nothing to retrieve, by design, so the turn-keyed metrics record
`None` and print as a dash; scoring them 0.0 would have deflated every
headline on this corpus by a tenth. The judged pass is where that ability is
answerable at all.

**An interrupted benchmark run can now actually be resumed.**
`already_ingested` said a scope interrupted midway is "rewritten rather than
trusted" and nothing made that true: ingest wrote every turn into a scope that
still held some of them, and the first turn already present came back
`unchanged`, which the backend raises on. So resume worked for a scope that
had finished and failed for the one case it exists to handle. Found by running
the same fixture twice.


**Test coverage is measured, and gated.** `pytest --cov` runs in CI with
`--cov-fail-under`, reporting **76.48%** of 6,806 statements and 2,096
branches. Branch coverage rather than line coverage: a suite that executes
every line while only ever taking one side of each `if` reports a much better
number than it has earned.

The first measurement is the argument for doing it. `forget`, `verify` and
`retain` - the three commands that erase, attest and expire customer data -
were the least covered code in the package at 6.6%, 8.6% and 9.8%. The modules
underneath them are tested hard; what nothing exercised was the layer where
`--confirm` lives. Twelve tests now drive those three through `main()`, and
`docs/OPENSSF-BADGE.md` answers `test_most` Met with a number instead of
listing it as one of three gaps.

The competitor adapters sit at 0% and are deliberately not excluded from the
figure. A coverage number that omits what is untested is not a coverage
number.

**Reciprocal rank fusion takes weights, and the graph hop was measured with
them.** Two earlier attempts to use graph expansion failed, and the diagnosis
written down at the time was that the fusion was at fault rather than the
walk: RRF gives every list's top slot the same value, so a list ordered by
proximity to what the query already matched spends a top slot on a fact the
query did not ask for.

Half of that is now confirmed. At a weight of 0.2 the expansion costs
**-0.0002 MRR** on entity_pair, where the unweighted hop costs -0.2409, with
an interval of [-0.0070, +0.0068]. The damage can be dialled out entirely.

The other half is not. Multi hop MRR still does not move outside noise, in
the third attempt running. What a weight changes is how badly the second fact
is ranked, not whether. `expansion_weight` therefore ships **off**, and
`eval --ablate` scores two weights beside the unweighted hop so the next
person to have this idea finds the experiment rather than repeating it.

**The Letta benchmark adapter could never have connected.** It passed
`token=` to a constructor whose parameter is `api_key=`, so it raised
`TypeError` the moment a token was set, which is the only way to reach Letta
Cloud, which is the only Letta that serves the REST API this adapter uses.
Nothing caught it because running it needs a paid account, and it was written
against an SDK that had been installed and read. A type checker found it on
the first run.

**mypy runs in CI.** Not strict mode: thirteen modules carry a baseline and
`disallow_untyped_defs` is off, because a gate nobody can pass is a gate
somebody deletes. The baseline only shrinks, so new code is checked from its
first line. `_render` in `retrieval/causality.py` also had a return type that
described one chain while returning several, which is the kind of thing a
reader believes.


## 0.5.7

**Every read surface says what it handed over.** `query_memory`, `trace_cause`
and `get_audit_log` all deliver facts to an agent, and only the first recorded
that it had. The three are now named in one place, `trial.reads.KINDS`, so a
caller cannot add a fourth read path and silently account for nothing. The
recording is the caller's job and the module says so: a second caller that
imports the retrieval function directly does not inherit the MCP wrapper's
side effects, which is how the hosted service reached 129,474 facts with zero
recorded reads.

`query_memory` also answers a graph hop as a third channel and reports
`attributed` and `attribution_rate`, so a caller can tell how much of an answer
carries provenance rather than assuming all of it does.

**`echo-memory retain`: how long a scope keeps a fact.** Erasure answers a
subject who asks to be forgotten. This is the standing rule that no
deletion-on-request path answers: GDPR Article 5(1)(e) and the DPDP Act both
require that data is not kept longer than its purpose needs. A policy records
the duration, the purpose it serves and the person who decided, the last two
being required columns because "we delete after 90 days" is not a compliance
answer. Replacing a policy retires the old one rather than overwriting it.

`retain --enforce` deletes what the policy no longer permits keeping, through
`erasure.erase` rather than beside it, so a retention deletion leaves exactly
the `erasure_event` record a subject request leaves. It removes the entity left
with no facts and vacuums the pages, because a name with no facts is still
personal data and a deleted tuple Postgres has not reclaimed is still readable.
Dry run unless `--confirm`, and it refuses outright when no policy exists: an
absent policy means nobody has decided, which is not the same as forever.

**`echo-memory verify`: whether any fact has been changed behind the store's
back.** Every fact now carries a digest over its own claims - the endpoint
names, the relation, the text, the confidence, the causal type, `t_valid`,
`agent_id`, the project and the session - chained to the previous attestation
in the same scope. Changing a fact breaks its digest; deleting one breaks the
chain. Until now all of that was believed because the database said so, and
anyone who could write SQL could rewrite an `agent_id` with nothing afterwards
able to tell.

What it is worth depends on one of two things, and `verify` says which is in
force on every run: `ECHO_MEMORY_ATTESTATION_KEY` makes each digest an HMAC, or
a record of the chain head kept outside the database makes a silent rewrite
impossible unkeyed. With neither it detects corruption and nothing more. It
does not authenticate the author, it says nothing about the interval before a
`--backfill`, and a missing fact is reconciled against `erasure_event` rather
than assumed to be tampering.

**Upgrading needs `init-db`, and skipping it now fails on write.** Migrations
0029 and 0030 add the retention and attestation tables, and the write path
inserts an attestation row, so a store running this code against an older
schema stops rather than writing facts nothing can verify.


**`writer_version` names the code, and old stores will say so.** The column
exists to answer "which code wrote this fact" and recorded the installed
distribution version instead, which in an editable checkout is frozen at `pip
install` time and never moves with the source. The same value sat on both sides
of `health`'s staleness comparison, so the check built to catch a version
mismatch could not fail: a process running 0.5.6 code with 0.3.0 metadata
stamped `0.3.0`, compared it against `0.3.0`, and reported healthy.

The stamp is now the install version plus a digest of the package's own source
as it stood at import. **Expect `health` to report `stale_writer` once on any
existing store**, because everything written before this carries a bare install
version. That is the finding working, not a regression: it is about the
provenance of old rows rather than anything being broken now, and what wrote
them cannot be recovered - only stopped from claiming otherwise. Reported by
#175, fixed in #180 by @bfs2021, with the startup-snapshot boundary tightened
after review by @NixEngine: a digest taken at import cannot identify a module
imported later, and the claim is narrowed to say so rather than widened to
pretend otherwise.

**`get_audit_log` says when it held entries back.** The result carries
`truncated`, and the cap was silent before: 500 entries came back whether the
log held 500 or 50,000, and `since` was the documented way to page with nothing
to say it was needed. #143, fixed in #169 by @bfs2021, with the boundary
corrected in #179 by @Logic769 after @DevdattaRane found the edge case while
reading the first fix - `truncated` is true only above the cap now, where
exactly 500 entries used to report it falsely.

**`eval-external --backend` measures another memory system.** Every benchmark
number this project published was about itself with no baseline beside it.
Mem0, Zep, Letta and cognee now run through the same harness, the same corpus
and the same scoring, because a comparison where one side ran a different code
path measures the harness. Verified on LoCoMo in full for this project, 1,982
questions in 310 seconds; the competitor runs and their caveats are in
`docs/BENCHMARKS.md`, including which backends have never actually run.

**`judge-external` grades a finished run.** Retrieval is a ceiling on QA
accuracy and not comparable to the accuracy figures hosted products publish, so
this is the comparable number: one model answers from only what a backend
returned, and two judges from different vendors grade the identical answer, with
raw agreement and Cohen's kappa reported so how far a verdict depends on the
judge is a number rather than a reassurance. Both prompts are in
`src/echo_memory/eval/qa.py` and are published with anything taken from them.
That makes 36 CLI commands.

**The MCP registry entry is `com.echo-mem/echo-mem`.** The old
`io.github.ayushcodes10/echo-mem` belonged to a personal account that no Actions
token could ever claim, so publishing could not be automated. It is deleted, not
deprecated: a deprecated server keeps holding its remote URL and the new name
could not be published while it did. Anyone with the old name in a config needs
to change it. #148.

## 0.5.6

**Apache 2.0, for all of it.** Versions 0.5.0 to 0.5.5 shipped under the
Business Source License, which made production use free only below fifty
people and $5M of revenue. That is withdrawn. Ateleir Tech holds the copyright
in the whole work and relicenses all of it, those versions included, so the
grant only widened and nobody lost a right they already had.

The restriction was protecting the wrong thing: what makes this hard to run is
not the source, it is a Postgres with Apache AGE and pgvector that somebody has
to operate, and the hosted plan sells exactly that. `LICENSE` is the canonical
Apache text, byte for byte so GitHub detects it, and `NOTICE` names the
copyright holder. `LICENSE-APACHE-2.0` is gone; `LICENSE` is that text now.

One consequence worth naming: the OpenSSF Best Practices badge was blocked
outright by `floss_license`, which requires an OSI approved licence. It is
obtainable now, and is documented as not yet claimed rather than claimed.

**A sign-off instead of a contributor agreement.** The CLA added one thing
over the Apache licence, the right to relicense a contribution later, and this
project does not use it: everything commercial lives in a separate private
repository. Replaced by the Developer Certificate of Origin, `git commit -s`.
The accepted cost is stated in `DCO.md`: no contributed code here can be moved
off Apache 2.0 without asking its author.

**`echo-memory why` says which scope the fact is in.** `recall` answers from
both scopes and hands back a fact id; `why` takes the global `--scope`, which
defaults to solo. So the obvious flow answered "No audit history found" for a
fact that had some, one scope over. Fixed by @Sonike in #156.

**Published to the MCP registry on every tag.** The registry served 0.5.3 for a
month while PyPI was on 0.5.5, because publishing was a manual runbook step.
The server is renamed `com.echo-mem/echo-mem` and authenticated by a DNS record
on a domain that survives an organisation move, which the old personal
namespace could not be. A scheduled check says so if the three versions ever
disagree again.

**A disclosure route that works.** `SECURITY.md` told researchers to use
GitHub's private vulnerability reporting, which was switched off. It is on, and
the file no longer claims there is no stable release.

**The README said the hosted plan was $99.** It is $149 for 1,500,000 units, and
a check in the cloud repository now fails when the two disagree.

## 0.5.5

**`echo-memory forget` erases facts outright.** GDPR Article 17 and India's DPDP
Act both require a data subject's personal data to be deleted on request, and the
store had no way to do it: supersession sets `t_invalid` and keeps the fact, which
is right for memory and insufficient for law.

Erase one fact, one entity with everything touching it, or a whole scope. Dry run
by default, so the only flag that deletes is `--confirm`.

**It removes the text from the seven places it lives**, not just the edge: the fact
and every superseded version of it, the entity node and its aliases, the audit log's
before, after, summary and detail text, any causal quote, the per scope term
statistics, and both embedding tables. The last two are the ones that would be
missed. `lexical_term` holds the actual lexemes, so an erased fact stays queryable
by its vocabulary; and embedding inversion recovers a meaningful fraction of the
source text from a vector, so deleting the row while keeping the vector is
obfuscation rather than erasure.

**One limit, stated rather than hidden.** A person named inside the text of a fact
whose two endpoints are other entities is not reachable structurally. Finding those
needs a text scan, and a scan must not act alone, because a false positive destroys
a fact nobody asked about and there is no undo. `--search` lists candidates and
exits; a human confirms. Structural erasure is guaranteed, textual mention erasure
is best effort with review.

**`query_memory` says when an `as_of` answer is thinner than the past it
describes.** A query about a past moment is a claim about what was believed then,
and an erasure since makes that claim incomplete. Responses now carry
`excluded_by_erasure` with the count and the dates when the window spans a recorded
erasure. The field is absent otherwise, so nothing changes for existing callers.
Returning a smaller past silently would turn the one property a reader is meant to
rely on into one they cannot.

**Not reachable over MCP, deliberately,** and a test reads the published tool list
so adding it later fails loudly. Irreversible deletion of a whole scope, one prompt
injection away from any agent holding a key, is not a trade worth making.

Migration 0028 adds `erasure_event`, which records that an erasure happened without
recording what it was: counts, a scope, a verb, who asked and a ticket reference,
and no fact text or entity name anywhere. A compliance record listing the names of
people who asked to be forgotten is the thing they asked to not exist.

## 0.5.4

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

**Releases carry their own build provenance.** Every release is built and published by
GitHub Actions through PyPI Trusted Publishing, which already issued a Sigstore
attestation on upload; the workflow now also attests the files itself and attaches the
distributions to the GitHub release, so the evidence is where somebody auditing a
release will look rather than only on PyPI. The five earlier releases were backfilled
with their published artifacts and provenance.

Every GitHub Action is pinned to a commit SHA rather than a tag, and the Postgres base
image in `docker/postgres.Dockerfile` is pinned to its OCI index digest, which keeps
the multi-arch property the comment above it depends on: production is arm64 and a
single architecture pin would have broken it. CodeQL runs on every push, pull request
and weekly. Dependabot keeps the pins current, because a pin nobody moves is a
dependency frozen at whatever it was the day it was pinned.

**`docs/COMPLIANCE.md` and `scripts/licence-inventory.py`.** What a buyer's security
review asks for, answered with things they recompute rather than take on trust. The
script walks the runtime dependency closure rather than listing what happens to be
installed: 61 distributions ship, none under strong copyleft, four under weak or file
level terms. It also says plainly that the OpenSSF Best Practices Badge is not
obtainable for a BSL project, rather than leaving that to be discovered during a
procurement review.

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
