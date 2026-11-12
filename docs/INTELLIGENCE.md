# The intelligence platform

Six capabilities on top of the memory graph: a way for machines to report what
happened, a read across several scopes, a record of whether a claim was ever
checked, routing and budgeting for that read, a file format for handing
knowledge to another organisation, and the identity layer that holds the
credentials. All six are in the engine, so a self hosted install gets them.
The hosted service adds identity, tenancy and an HTTP door, and nothing else.

This document is organised the way `attestation.py` is: what each capability
does, the API, what it costs, and a section on what it does not do. The last
section of each is the one to read first if you are about to rely on it.

Two things to know before any of the rest.

**Three of these are off by default.** `ECHO_MEMORY_TRUST_RETRIEVAL` and
`ECHO_MEMORY_LESSONS` are unset, so verification and derived lessons do not
reach a read until an operator turns them on. `ECHO_MEMORY_MAX_GRAPH_WRITERS`
is unset, so the graph wide write gate does nothing. The full list is in
[Environment variables](#environment-variables).

**None of the six is an MCP tool.** The seven MCP tools are unchanged. The
reporting door is a function the hosted service calls, the exchange surface is
`echo-memory exchange` on the command line, and the retrieval additions are
parameters on existing Python functions. `cli/forget.py` records the reasoning
and it applies to all of this: granting trust, or handing an organisation's
memory to another organisation, must not be one prompt injection away from any
agent holding a key.

---

## Contents

1. [Collective learning](#1-collective-learning)
2. [Knowledge networks](#2-knowledge-networks)
3. [Agent identity](#3-agent-identity)
4. [Trust and verification](#4-trust-and-verification)
5. [Context intelligence](#5-context-intelligence)
6. [Knowledge exchange](#6-knowledge-exchange)
7. [Hardening](#7-hardening)
8. [Environment variables](#environment-variables)
9. [Migrations](#migrations)

---

## 1. Collective learning

### What it does

Until now this store could record that a decision was made and never that it
worked. Three things close that: a validated door machines report through, a
queue and a single writer behind it, and a read time derivation that turns
agreeing outcomes into a lesson.

There is no new data model. A decision is a `Node`, an outcome is a `Node`, and
"this decision led to that outcome" is a `FACT` edge carrying
`causal_hint=led_to`. So `trace_cause` already walks it, supersession already
retires an outcome that turned out wrong, erasure already reaches it and
attestation already covers it.

```
Node(type=decision, name="migrate-payments-to-razorpay")
  -[FACT relation_type=observed_on, causal_hint=led_to]->
Node(type=outcome, name="checkout-error-rate-2026-11 :: failure")
```

The reserved vocabulary is in `intelligence/vocabulary.py`:

| Kind | Values |
|---|---|
| Node types | `decision`, `action`, `outcome` |
| Relation types | `evidenced_by`, `observed_on`, `supersedes_decision`, `taken_under` |
| Outcome signs | `success`, `failure`, `mixed`, `unknown` |
| Lesson bases | `observed`, `correlational`, `verified_causal` |

The sign lives in the outcome node's **name**, separated by `" :: "`, and that
is a correctness requirement rather than a convenience. Node names are unique
per scope since migration 0016 and edge identity is
`(source, target, relation_type)`, so a success and a later failure of the same
named thing would be one edge, and the second report would supersede the first.
Two real observations, one survivor, and the supersession would read as a
correction rather than as the loss of a data point. `intelligence/naming.py`
writes the name and reads the sign back, in one module, so the two sides cannot
disagree.

### The event door

`events/envelope.py` is the acceptance policy and is pure: no database, no
clock except the one passed in, so the whole of what may be accepted is
testable without a server.

```python
from echo_memory.events import envelope, inbox
from echo_memory.intelligence import outcomes

event = envelope.parse({
    "schema_version": 1,
    "event_id": "deploy-9f2c1a-prod",       # yours, and the idempotency key
    "event_type": "deployment.completed",
    "occurred_at": "2026-11-11T09:14:00+00:00",
    "actor": {"agent": "ci-runner", "run_id": "build-4120"},
    "scope": {"memory": "prod", "scope": "shared"},
    "payload": {
        "decision": "migrate-payments-to-razorpay",
        "outcome": "checkout-error-rate-2026-11",
        "sign": "failure",
        "detail": "error rate 4.1% against a 0.5% baseline",
    },
})
inbox.enqueue(conn, group_id, event)
```

Seven event types, a closed set:

| Event type | Writes |
|---|---|
| `decision.recorded` | one decision node, no fact |
| `action.executed` | an action node and an edge to its decision, `causal_hint=enabled_by` |
| `outcome.observed` | an outcome node and the decision to outcome edge |
| `deployment.completed` | the same shape, named for what it is |
| `test.completed` | the same shape |
| `evidence.attached` | an `evidence` row and a link. No fact |
| `claim.verified` | a `fact_verification` row. Needs verification authority |

The envelope's four load bearing fields:

| Field | Why |
|---|---|
| `event_id` | idempotency, within your scope. A CI run that times out cannot know whether the first call landed, so the second returns `duplicate: true` rather than an error. Another account using the same id is not your problem: the inbox key is `(group_id, event_id)`. 8 to 128 characters of letters, digits, dot, colon, underscore or hyphen |
| `occurred_at` | replay protection, with `MAX_AGE`. A naive timestamp is refused rather than assumed UTC |
| `actor.agent` | attribution. Bound to the credential by the caller, never read out of the payload |
| `schema_version` | so the drain refuses what it does not understand instead of reading a familiar looking field out of an unfamiliar envelope |

Limits, all in `events/envelope.py`: `MAX_PAYLOAD_BYTES` 64 KiB,
`MAX_CLOCK_SKEW` 15 minutes ahead, `MAX_AGE` 7 days. `scope` names a memory and
`solo` or `shared`; a `group_id` key in it is **refused**, not stripped,
because a caller that believes it may name a group id may name somebody else's.

One more in `events/inbox.py`: `MAX_PENDING_PER_SCOPE`, 100, settable with
`ECHO_MEMORY_MAX_PENDING_EVENTS`. `enqueue` refuses an event into a scope whose
**pending** backlog is already at it. This bounds the queue and not the rate -
nothing here counts events per second, and a reporter that drains between
bursts may send any number - so it is a backstop for self-hosted deployments
and for anything calling `enqueue` directly, while the hosted door rate limits
with the credential that knows who is calling. 100 is `drain`'s
`DEFAULT_LIMIT`: one drain pass deep, past which the drain is not keeping up
and every further event delays every other reporter on the single writer graph.
Counted on `pending` alone, so an applied history is not a lifetime quota.

### The drain

`events/inbox.drain(conn, apply=...)` applies pending events as the graph's
single writer. The latency is forced rather than chosen. `write_episode` locks
per scope, which serialises two writers into one scope correctly, but Apache
AGE keeps its label catalogue per **graph**, so two writers in two scopes of
one graph take different keys and proceed concurrently. Four benchmark shards
writing four scopes at once killed three of the four with
`relation "echo_memory.-775.1utterance" does not exist`, and the run looked
complete from outside because each shard reported its own progress. A multi
tenant endpoint that writes straight through is that condition permanently.

```python
import functools
from echo_memory.intelligence import outcomes

apply = functools.partial(outcomes.apply, embedder=embedder, allow_verify=False)
inbox.drain(conn, limit=100, apply=lambda conn, gid, env: apply(conn, gid, env))
```

Four inbox states: `pending`, `applied`, `rejected`, `quarantined`. The whole
drain runs in one transaction holding a graph wide advisory lock, and each
event runs inside its own savepoint, so one inapplicable payload is marked
`rejected` with its reason and the batch carries on. A hard failure rolls the
whole batch back, marks included, which leaves every event `pending`.

Every event goes through `write_episode` and never around it, and its result is
checked. `stored` is applied; `unchanged` is applied, because a re-sent event
the graph already holds word for word is what idempotency looks like from this
end; `no_facts` is applied only for an episode that deliberately asserted none,
which is `decision.recorded`. Anything else raises and becomes a `rejected`
row.

### Lessons

A lesson is "deciding X tends to go badly". `intelligence/lessons.py` derives
them at read time and **writes nothing**. There is a test that reads the
module's source and asserts it does not mention the write path.

```python
from echo_memory.intelligence import lessons

rows = lessons.candidates(conn, group_id)
# [{"kind": "inferred", "basis": "verified_causal", "decision": ...,
#   "sign": "failure", "support": 3, "agreement": 1.0, "as_of": ...,
#   "outcomes": [...], "lesson": "... has 3 agreeing failure outcomes and
#   none against (verified, with a method recorded on each)."}]

warnings = lessons.repeated_failures(conn, group_id, min_occurrences=2)
order = lessons.for_query(conn, group_id, edge_ids)   # a reordering, never a filter
```

The bar a lesson has to clear:

| Threshold | Value | Meaning |
|---|---|---|
| `MIN_SUPPORT` | 2 | one outcome is an anecdote and the store already returns it |
| `REQUIRED_AGREEMENT` | 1.0 | unanimity. One outcome pointing the other way kills it |
| `SUPPORTING_STATES` | `verified`, `evidence_backed` | an unverified outcome is a claim nobody checked |

`mixed` and `unknown` outcomes are dropped before any counting, via
`SIGN_VALUE`, so a decision with ten mixed outcomes yields nothing rather than
something weak.

Every row is `kind="inferred"`, whatever its support. `basis` is
`verified_causal` only when **every** supporting outcome is `verified`; one
merely `evidence_backed` outcome makes the whole row `correlational`.

### What it costs

`candidates` is four queries per call: the scope's decision nodes, the live
outcome edges hanging off them, one batched trust state read, and one batched
evidence count. Migration 0034's partial indexes are what make the first cheap;
without them it is a sequential scan of every node the scope has ever held.

`for_query` short circuits on one cheap query when the result set contains no
outcome edges, which is the common case, and does the full derivation
otherwise. It is off by default behind `ECHO_MEMORY_LESSONS`.

A `decision.recorded` event is one `write_episode` plus one node read back. The
other reporting events are one `write_episode` each. `evidence.attached` and
`claim.verified` write no fact and embed nothing.

### What it does not do

**Nothing here reads a sentence.** The payload names its entities. There is no
extraction, no model and no inference about what a report means. A missing field
is an error naming the field, because a reporter is a program and will keep
sending exactly what it sent until somebody reads a message saying what was
wrong with it.

**`Node.type` is a convention, not a constraint.** It stays free text. Nothing
refuses a node typed `decision` that is not one, and nothing could: the server
calls no model. What migration 0034 adds is a partial index so asking for a
scope's decisions does not scan it. A caller using `decision` for something
else will find it in lesson candidates.

**Attaching evidence does not promote a fact.** A fact with five evidence rows
still reads `unverified` until a verifier says the evidence was sufficient.
That is the mechanism rather than an oversight: if attaching an artefact
promoted a trust state, a CI reporter could attach its own deploy log to its
own outcome and admit that outcome into lesson support.

**A reporter cannot verify.** `claim.verified` is in
`envelope.REQUIRES_VERIFY`, and `outcomes.apply` also requires
`allow_verify=True` as a keyword with no default. Either check alone would be
weaker than it looks: the type set on its own says which events need the
capability without anybody holding it, and the flag on its own would let a
future event type inherit verification authority by being added to the wrong
set.

**A lesson is a snapshot, and a supersession weakens it with no job run.**
Retire one supporting outcome and the lesson simply stops being produced. The
same query tomorrow can legitimately return less, which is what `as_of` on each
row is for.

**`repeated_failures` is laxer than `candidates`, on purpose.** It is not
filtered by trust state. It claims no causality, carries no basis, and says
only "this decision has failed more than once, go and look". A caller must not
render it as a finding.

**An empty answer has several causes and `echo-memory trust lessons` names
which.** No lessons with no evidence anywhere means nothing is reporting. No
lessons with evidence arriving means artefacts are being attached and nobody is
adjudicating them, which is the likely state of a new install, because
`evidence_backed` is never derived from evidence existing. The
`lesson_candidates` log line carries `supporting` and `evidence_rows`, and the
command prints the counts against each of the three bars plus the command that
fixes it, because a log line is not where an operator looks.

The reason that command exists at all is the failure it was written after:
until `echo-memory trust` there was no caller for `trust.verify.record`
anywhere, so NO deployment could put a fact into a supporting state, and this
empty list was the permanent and only output of the whole capability.

**An erased event keeps its `event_id` and loses its envelope.**
`echo-memory forget` replaces an applied event's envelope with
`{"redacted": true, "event_type": ...}` and drops the erased edges from
`applied_edge_ids`, and leaves the row itself. The key has to survive: it is
the only thing stopping the reporter's next retry from being accepted as a new
event and rewriting what was just erased. The envelope does not, because once
the edges are gone the payload is the last copy of what the reporter sent.

**There is still no inference provider.** `infra/provider.py` ships
`NullProvider`, `load({})` returns it, and `ECHO_MEMORY_INFERENCE_PROVIDER` set
to anything other than the explicit off values raises rather than falling back.
A provider may only ever propose `correlational` candidates off any read or
write path. Nothing a provider produces can reach `verified_causal`, which
needs a verifier naming a method, and a model is not one.

---

## 2. Knowledge networks

### What it does

`query_memory` takes one `group_id` and that is not changing.
`retrieval/across.py` adds the thing a customer with a `dev` memory and a
`prod` memory wants: ask once, get the answer from whichever holds it.

```python
from echo_memory.retrieval.across import query_across

result = query_across(
    conn,
    ["acct:a1:memory:dev:shared", "acct:a1:memory:prod:shared"],
    "why did checkout start 502ing",
    top_k=10,
    embedder=embedder,
    weights=None,          # None is plain RRF
    with_context=False,    # see Hardening for what the context block is
)
# {"facts": [{..., "source": "acct:a1:memory:prod:shared"}],
#  "routing": {"considered": [...], "weights": {...}}}
```

It is a fan out and one fusion over a pipeline that is already measured.
`reciprocal_rank_fusion` has always been generic over N weighted ranked lists
of opaque ids; it does not care that today's lists are scopes rather than
channels.

With a single `group_id` the result is **byte identical** to `query_memory`,
and there is a test pinning that. That is what keeps every number the eval
harness has produced valid as a baseline. `with_context=True` is the one thing
that adds a key on that path, and it is off by default, which is what keeps the
identity.

`MAX_GROUPS` is 8, and it is a latency bound rather than a capability claim.

Two keys arrive on a multi scope answer that a single scope answer does not
have: `source` on every fact, naming the `group_id` it came from, and
`unreadable_scopes` when a scope could not be read. `excluded_by_erasure`
becomes per scope, because an `as_of` answer spanning scopes has to report
every scope that had an exclusion or it understates what is missing.

### What it costs

One full `query_memory` per scope: two indexed candidate queries, an HNSW probe
and a fusion, each. The cost is linear in the number of scopes. A multi scope
call asks each scope for `min(MAX_TOP_K, max(top_k * 3, 30))` candidates rather
than `top_k`, because a fact that ranks 11th in its own scope can rank 3rd
overall.

One extra indexed query per call verifies, against the database, that every
fact about to be returned belongs to one of the `group_id`s that were passed
in. A stray fact raises `ScopeViolation` rather than being filtered, because a
filter turns an authorization bug into a quietly thinner answer.

### What it does not do

**It does not know what a principal is, and must not learn.** `group_ids` has
to already be the scopes the caller is authorised for. The hosted service
resolves authorization and passes strings, which is what keeps a tenancy
decision in one place.

**Scope weights are untuned.** The default is 1.0 for every scope, which is
plain RRF. `routing.weights_for` can propose numbers and the eval harness can
score them; this module does not choose. The only weight this codebase has ever
measured is `expansion_weight`.

**A repeated `group_id` is refused, not deduplicated.** The same scope twice
would get two votes in the fusion and silently double its weight.

**It is not a federation protocol.** This reads several scopes in one database.
Reading another organisation's memory is [knowledge
exchange](#6-knowledge-exchange), which is a file.

---

## 3. Agent identity

This capability is in the cloud repository rather than the engine, because the
engine has no concept of a principal: every function takes a connection and
strings. Migration `0020_agent_identity.sql` and `src/echo_cloud/agents.py`.

### What it does

An account owns N agents. An agent is a thing that keeps working; a key is how
it authenticates today. Until now those were the same string.

`api_key.agent_id` is free text typed into a form and is stamped on the
`agent_id` property of every `FACT` edge. That column survives untouched and
is still the attribution. What is new is `public.agent`, a registry describing
those strings, so one can be renamed, retired, or told apart from a typo.

```python
from echo_cloud import agents

agent = agents.create(conn, account_id, "claude-code", name="Claude Code")
agents.listing(conn, account_id)
agents.get(conn, account_id, "claude-code")       # raises, naming what exists
agents.retire(conn, account_id, "claude-code")

run = agents.start_run(conn, agent.id, runtime="claude-code",
                       model="claude-opus-5", framework="mcp")
agents.end_run(conn, run.id)
agents.runs_for(conn, agent.id)
```

An unknown agent name is **refused rather than created**. Auto creating would
make a mistyped agent indistinguishable from a new one: the operator sees a
fleet of two where they have one, and nothing reports a problem.

`agent_run` is the second half. An agent's value to its owner is that it is the
same agent next week, and that is not observable from the `agent` row, which
looks identical before and after a model swap. A run records the runtime, the
model and the framework, so "this agent kept working across a model change"
becomes two rows with different `model` values under one `agent.id` rather than
an assertion.

### Key capabilities and expiry

Three columns on `api_key`:

| Column | NULL means |
|---|---|
| `agent_ref` | this key is not bound to a registered agent. Metadata only |
| `capabilities` | **not narrowed**. The holder's role decides, which is what every key issued so far does |
| `expires_at` | no expiry, which is what `apikeys.py` has always documented |

```python
from echo_cloud import apikeys

issued = apikeys.issue(
    conn, account_id, "ci-runner",
    capabilities=["events.report"],
    expires_at=datetime(2026, 12, 1, tzinfo=UTC),
)
```

A capability on a key is a **ceiling, never a grant**.
`roles.capabilities_of(principal)` intersects the role's set with the key's, in
that order, so a reader presenting a key stamped `memory.write` is still a
reader. An **empty** list is refused at issue time, because
`Principal.capabilities` is a frozenset whose empty value already means "not
narrowed", so an empty array in the column would read back as an unrestricted
key: a narrowing that silently became a widening.

An expired key is indistinguishable from a revoked one at the door, and the
comparison is against the database's clock rather than the container's.

### The new role capabilities

No new role. The four roles that exist are the four things the product
distinguishes, and a fifth has to be explicable to a customer before it is
explicable to a config file. A new power is a capability, which is also what
makes it grantable per credential.

| Capability | owner | admin | member | reader |
|---|:--:|:--:|:--:|:--:|
| `memory.read` | yes | yes | yes | yes |
| `memory.write` | yes | yes | yes | |
| `memory.erase` | yes | yes | | |
| `keys.manage` | yes | yes | | |
| `members.manage` | yes | yes | | |
| `billing.manage` | yes | | | |
| `account.delete` | yes | | | |
| `trust.verify` | yes | yes | | |
| `events.report` | yes | yes | yes | |
| `trust.policy` | yes | | | |
| `exchange.manage` | yes | yes | | |

`events.report` and not `trust.verify` for a member: reporting is what an agent
does in the course of working, while verifying is a judgement about whether
something is to be believed, which is the account's call rather than a
participant's. `trust.policy` is the owner's alone, because setting what the
account will trust is a standing decision about every future read, which puts
it beside billing and deletion.

A reader gets nothing new. An agent that only recalls has no events of its own
to report.

### The quarantine address

`tenancy.quarantine_group_id(account_id, memory, bundle_id)` builds
`acct:<id>:memory:<m>:import:<bundle>`. It is a fourth kind of address and
deliberately **not** a fourth scope: `solo` and `shared` are the two things a
caller may ask for, and this one is named by the service. `group_ids_for`
iterates `VALID_SCOPES`, which has two members, so no ordinary read can reach
one.

### What it does not do

**`agent_ref` and `run_id` decide nothing.** Neither appears in any group id.
`agent_id` is in the solo scope's shape and must stay there, because it is the
string every fact already carries; `agent_ref` is a surrogate key for a row
describing that string, and addressing the same private graph two ways would
mean the second address names an empty graph. A run is a window in time and a
group id is an address: if a run could narrow which facts are reachable, an
agent would lose its own memory when it restarted.

**A NULL `agent_ref` is not a defect.** `agent_key_shape` refuses a colon and
accepts 1 to 48 characters of lowercase letters, digits and inner dots,
underscores or hyphens. Live rows read "Claude Code", with a space and a
capital. Those keys keep working with `agent_ref` NULL and keep their meaning
in `api_key.agent_id`. Migration 0020's backfill filters on the shape for that
reason: without the predicate the INSERT would violate the constraint and take
`init-db` down for everybody.

**`apikeys.issue` does not register an agent.** `agents.ensure_for_key` is the
step for a key issued since the migration ran, and it is deliberately not
automatic: registering a name is an act an operator takes rather than a side
effect of issuing a credential. The consequence is that `listing` does not
mention an unbound key's agent.

**A run is not inferred from a key being used.** That would manufacture the
evidence the table exists to hold. An agent with no runs recorded is honestly
silent about what it ran on.

**Two of the four new capabilities are now required by a route, and two are
not.** `events.report` gates `POST /v1/events` and `trust.verify` gates both
`POST /v1/trust/verify` and the `claim.verified` event type, which is the
separation that makes a reporter credential safe to put in a CI config: it can
attach evidence all day and cannot say the evidence was sufficient.

`trust.policy` and `exchange.manage` are still defined and asked for by
nothing. There is no exchange route in the hosted service; building, importing
and promoting a bundle is `echo-memory exchange` on the engine.

**A key narrowed to `events.report` ALONE cannot report an event.**
`grants.from_key` returns NONE for a credential narrowed to capabilities that
are not about memory, and names `events.report` as its example, because a key
issued to report events should not be able to recall the account's memory as a
side effect. The events route checks `memory.write` on the target memory, since
an event becomes facts in that graph, so the two rules meet and refuse. The
ordinary CI credential is a member-role key with no narrowing, which carries
both. Reconciling the narrowed shape is a change to `grants.from_key` and has
not been made.

---

## 4. Trust and verification

### What it does

`confidence` on a fact says how the writer came by it: extracted, inferred,
ambiguous. That is a statement about provenance, not about truth. An agent that
inferred something confidently and wrongly leaves exactly the same row as one
that read it off a deploy log. Nothing recorded whether a claim had been
**checked**.

`trust/` is that record. Four modules: `states` is the vocabulary, `evidence`
is the artefact behind a claim, `verify` is the append only record of who
checked what, and `vattest` is a digest per verification, chained.

It is deliberately not a number. A score invites arithmetic across claims
established in different ways, and the one calibration this project has
measured came out AUC 0.766 with a 95% interval of [0.437, 0.968] over five
positives, an interval that includes chance.

### The six states

| State | Kind | Meaning |
|---|---|---|
| `unverified` | advisory | recorded, nothing checked. Every fact written before this existed |
| `evidence_backed` | advisory | a verifier recorded that the evidence was sufficient. Not derived from evidence existing |
| `verified` | **authoritative** | an authorised verifier asserted it, naming a method and a policy |
| `disputed` | **authoritative** | contradicting evidence exists. Returned **with** the contradiction, not suppressed |
| `stale` | advisory | a verification that has expired. A lapsed check is not a refutation |
| `rejected` | **authoritative** | an authorised verifier refused it. The only state that removes a fact from an answer |

The authoritative and advisory split is the line that matters:

```
authoritative   changes WHICH facts retrieval returns
advisory        changes only the ORDER they come back in
```

`query_memory` already records the governing principle on `COSINE_FLOOR`:
hiding a real memory is worse than including a mediocre one. Every state
computed from a heuristic is therefore advisory, and **only `rejected` removes
a fact from an answer, and only a verifier can set it.** That is also what
stops this becoming a denial of service on your own memory: a rule that could
mark a thousand facts `stale` can reorder them and cannot hide one.

`suppresses(state)` is true for exactly one member of `SUPPRESSED`, which is
`rejected`.

`TRUST_WEIGHTS` are RRF weights, not probabilities, and are **starting values
rather than measured ones**: `verified` 1.0, `evidence_backed` 0.6,
`disputed` 0.3, `unverified` 0.0, `rejected` 0.0, `stale` -0.3. Nothing has
measured them. They are there so an ablation has something to move. An
unrecognised state weighs 0.0 rather than defaulting to something, because a
state this version does not know was written by a newer one, and guessing is
how a future `rejected` becomes a boost.

### The API

A Python API and, since `echo-memory trust`, a command. The command came second
and that order was the problem: every function below shipped with no caller, so
in a real deployment nothing could put a fact into `verified` or
`evidence_backed`, and `lessons.candidates` filters on exactly those two. The
result was an empty lesson list in every store, indistinguishable from a scope
whose outcomes disagree with themselves.

```bash
echo-memory trust show     --scope shared --fact <id>    # state, history, evidence
echo-memory trust list     --scope shared [--state ...]  # the scope, by state
echo-memory trust verify   --scope shared --fact <id> --verifier <who> --method <how>
echo-memory trust dispute  --scope shared --fact <id> --verifier <who> --method <how>
echo-memory trust reject   --scope shared --fact <id> --verifier <who> --method <how>
echo-memory trust evidence --scope shared --fact <id> --kind test_run --uri <url>
echo-memory trust lessons  --scope shared                # and WHY, when empty
```

`--verifier` is required on the three states a verifier sets on purpose, and an
empty one is refused: a verification nobody can be identified by is one nobody
can be held to, and it would still count towards a lesson. There is no
`--confirm`, because nothing here deletes - the table is append only, so a
wrong call is corrected by recording the right one and the mistake stays
readable.

None of these is an MCP tool, and a test reads the published tool list so that
registering one later fails. See §4's **What it does not do**.

```python
from echo_memory import trust

# the artefact, recorded apart from the claim
ev = trust.record_evidence(
    conn, group_id, "test_run",
    uri="https://ci.example/build/4120",
    detail={"failed": 3}, reporter_agent="ci-runner",
    occurred_at="2026-11-10T22:00:00+00:00",
    content_digest="sha256:...",
)
trust.link_evidence(conn, group_id, edge_id, ev)
trust.evidence_for_fact(conn, group_id, edge_id)
trust.evidence_count_for(conn, group_id, edge_ids)   # one query, zero for every id

# the check itself
trust.record_verification(
    conn, group_id, edge_id, trust.VERIFIED,
    verifier_kind=trust.HUMAN, verifier_id="ayush",
    method="read the build log and the deploy diff",
    policy_id="deploy-review-v1", evidence_ids=[ev],
    expires_at="2027-02-01T00:00:00+00:00",
)

# reading it back
trust.state_of(conn, group_id, edge_id)
trust.states_of(conn, group_id, edge_ids)    # ONE query, every id comes back
trust.summary(conn, group_id, edge_ids)      # a count per state, zeros included
trust.suppressed(conn, group_id, edge_ids)   # exactly the rejected ones
trust.ranked(conn, group_id, edge_ids)       # reordered, never added to

# the chain
trust.chain_head(conn, group_id)             # (seq, digest) to record elsewhere
report = trust.chain_report(conn, group_id)
report.is_clean(), report.strength, report.key_ids
```

Six evidence kinds: `deployment`, `test_run`, `human_correction`, `metric`,
`external_doc`, `attestation`. Three verifier kinds: `human`, `service`,
`rule`.

`record` appends and never updates, so "verified on the 3rd, disputed on the
9th, verified again on the 11th" stays answerable, and "who changed their mind"
has an answer rather than a last word. The current state of a fact is the
newest row for its edge.

`states_of` is one query for every candidate a read is about to return, and
`state_of` is written in terms of it, so there is exactly one place where a
newest row becomes a state.

### Expiry degrades, it never hides

A `verified` row whose `expires_at` has passed reads as `stale` and **the row
is left exactly as it was**. The fact is still returned, still findable, and
only ranks lower. Rewriting the row to say `stale` would destroy the evidence
that the check ever passed, which is the only thing a reader can use to decide
whether to re-run it.

A lapsed `rejected` or `disputed` is deliberately left alone. `expires_at` says
how long a positive check is good for; applying it to a refusal would make
suppression expire on a clock.

Expiry is compared against `now()` inside the query. `now=` exists for tests.
A few tens of milliseconds of skew between a container and its host has already
turned "3 days left" into 4 in this project, and a fact flipping between
`verified` and `stale` depending on which process asked would be the same bug
with a worse symptom.

### Verification is a separate chain

`verification_attestation` mirrors `fact_attestation` and shares nothing with
it. `fact_attestation` is dense per scope and `echo-memory verify` recomputes
the whole of it, so appending verifications to it would mean every check shifts
the sequence numbers a fact's own digests chain through: recording a
verification would alter the evidence about the claim being verified.

**`fact_attestation` and `attestation.COVERED` are untouched.** Existing
customer digests do not move. There is a test,
`test_verification_leaves_the_fact_attestation_chain_bit_identical`, and
another one asserting that tampering with a verification leaves the fact chain
clean.

The serialisation is reimplemented in `vattest` rather than imported from
`attestation`, which looks like duplication and is not: a shared canonical form
means a change made for one chain silently rewrites every digest in the other.

`vattest.COVERED` is `edge_id`, `state`, `verifier_kind`, `verifier_id`,
`method`, `policy_id`, `evidence_ids`, `scope`, `verified_at`, `expires_at`.
`verified_at` is covered so a check cannot be backdated, and `expires_at` so a
lapsed check cannot be quietly extended, which is the cheapest useful edit
available against this table.

### Keys and rotation

`ECHO_MEMORY_VERIFICATION_KEYS` holds `key_id:hexkey` pairs, comma separated,
so more than one key is known at once. `ECHO_MEMORY_VERIFICATION_KEY_ID` names
the one that signs from now on; absent, the first entry signs.

Each link records the `key_id` that signed it and is verified with that key.
Without the per link `key_id`, rotating would make every verification in
history read as forged, which is the same as having no rotation at all, because
nobody rotates a key that invalidates their own audit trail.

A `key_id` that is no longer configured is reported as **unverifiable** rather
than altered, and `Report.is_clean()` is deliberately not false for it. Not
being able to check a link is not evidence that it changed, and a report that
conflates those two trains a reader to ignore it.

A malformed entry in the variable raises rather than being skipped. A chain
unkeyed because of a missing colon, described as keyed by whoever set the
variable, is the shape of failure this project has already shipped twice.

### Reaching a read

Off by default, behind `ECHO_MEMORY_TRUST_RETRIEVAL`, or the `trust=` keyword
on `query_memory`. When on, two things happen and they are different in kind:

1. **Suppression.** `rejected` facts are removed, **before** truncation. A
   rejected fact filtered out of the final ten would already have cost a real
   fact its slot, which is a quietly thinner answer.
2. **Reweighting.** `verify.ranked` enters as one more weighted ranked list for
   RRF to fuse over the candidates the content channels already found.

Nothing here can add a candidate. That restraint is measured: the graph hop
could add facts to an answer and cost -0.142 MRR on `entity_pair`, the shape it
was meant to help least. A trust signal is a worse place to relax it, because
"this fact is verified" is not an answer to an unrelated question.

### What it costs

Two extra queries per read with the flag on: one batched `states_of` for the
reweighting and one for the suppression set. Both are `DISTINCT ON (edge_id)`
over the `(edge_id, id DESC)` index migration 0032 creates.

Every existing store has no verification rows at all, which is why the default
is off: turning it on by default would change nothing for anybody except by
adding two queries per read.

`record_verification` attests in the same transaction as the row, taking a per
scope advisory lock first. A verification attested in a second step is attested
for the rows nobody is suspicious about.

### What it does not do

**It does not authenticate the verifier.** `verifier_id` is a string the caller
supplies, so the chain proves the recorded verifier has not changed since the
row was written, not that it was true when it was. The same limitation
`attestation` has about `agent_id`.

**It does not prove the claim.** Evidence is not proof: a deployment log saying
the deploy happened does not establish that the decision behind it was right.
That is why `evidence_backed` is a separate and weaker state than `verified`.

**Unkeyed, the chain is only as strong as an external record of the head.**
Keyed, a digest cannot be recomputed without the matching key. With neither,
this detects corruption and careless edits and nothing else.
`report().strength` says which is in force on every call rather than implying a
guarantee that is not there, and reports a mixed keyed chain as its own case,
because that is different from rotation.

**`evidence_backed` is never computed.** A verifier records it like any other
state. A fact with five evidence rows reads `unverified` until somebody
adjudicates them. The counted evidence alternative was considered and rejected:
it would let a reporter admit its own outcome into lesson support.

**`trust.summary` has no caller.** The function exists and is tested; nothing in
the engine puts a `trust_summary` into a response yet. `envelope.describe`
accepts one, and nothing passes it.

**The MCP tool exposes no trust parameters.** `query_memory` over MCP takes
`scope`, `query`, `top_k`, `digest`, `as_of` and `about`. The flag is the only
way to change its behaviour, which is deliberate: per call trust gating would
let a caller turn suppression off.

**An erasure strips a verification rather than deleting it.**
`echo-memory forget` reaches these tables, split the way the rest of the
erasure path splits. `fact_evidence` goes, being a pointer. An `evidence` row
goes once nothing else cites it, because evidence is many to many and deleting
one a surviving fact still points at would take away that fact's provenance. A
`fact_verification` row **stays**, with `method`, `detail` and `scope`
stripped: deleting it would break `verification_attestation` the way deleting a
fact breaks `fact_attestation`, and the row is the record that makes the chain
over it mean anything. So after an erasure a verification survives carrying the
`edge_id` of a fact that is gone, which is what lets an auditor reconcile the
gap against `erasure_event` rather than wonder about it.

**And that makes the chain report the link as `altered`.** `method` and `scope`
are both in `COVERED`, so stripping them changes the digest. Measured: a scope
reporting clean before an erasure reported one `altered` link afterwards, with
nothing `vanished` and no broken links. `bundle_registry` is left untouched on
purpose, because a bundle row describes a transfer rather than a subject. See
the erasure section of `docs/COMPLIANCE.md`.

---

## 5. Context intelligence

### What it does

Three pieces: deciding which scopes are worth asking, fitting the answer to a
token budget, and a query independent block derived from the graph.

#### Routing

`retrieval/routing.py` decides which scopes to ask **with zero model calls**,
generalising a technique already in `query_memory`: `needs_expansion`
tokenises the query, finds nodes whose name contains a term, and asks whether
an edge joins them.

```python
from echo_memory.retrieval import routing

scores = routing.score_scopes(conn, group_ids, query)
# [ScopeScore(group_id=..., matched_terms=3, candidate_facts=41,
#             score=0.7421, reason="names 3 of 4 query terms over 41 live facts")]

chosen, rejected = routing.choose(scores, max_scopes=3, min_score=0.0,
                                  default="acct:a1:memory:prod:shared")
weights = routing.weights_for([s for s in scores if s.group_id in chosen])

kept, report = routing.fit_to_budget(facts, 2000)
# report: {"budget": 2000, "estimated_tokens": 1864, "dropped": 3}
```

One indexed query per scope and **nothing embeds the query per scope**. A
vector probe per scope *is* a `query_memory`, and if a scope is worth probing it
is worth keeping the answer from. Routing has to be cheaper than the thing it is
deciding about or it is not routing.

Constants, each marked in the source with what it is a guess about:
`MAX_MATCHED_NODES` 32, `DEPTH_HALF_FACTS` 10, `DEPTH_WEIGHT` 0.25,
`MIN_WEIGHT` 0.1, `CHARS_PER_TOKEN` 4.

`select` keeps the scope a caller named as its `default` whatever it scored, and
refuses a `default` from outside the scopes being scored. Routing that can
exclude a configured scope loses data, and the loss is silent: the answer comes
back shaped exactly like a correct one.

#### Assembly

`retrieval/context.py`'s `assemble` composes routing, `query_across` and the
budget. It does not re-rank, re-fuse or reach into a scope.

```python
from echo_memory.retrieval.context import assemble

result = assemble(
    conn, group_ids, query, top_k=10, embedder=embedder,
    token_budget=2000, max_scopes=3,
    default_group="acct:a1:memory:prod:shared",
    propose_weights=True, explain=True,
)
# result["routing"] = {"considered", "weights", "chosen", "rejected",
#                      "scores", "budget", "fallback"?}
```

With one scope and no budget it returns exactly what `query_across` returns,
byte for byte, pinned by a test. With one scope there is nothing to route, so
the scorer does not run and costs nothing.

When routing picks nothing, `assemble` falls back to the caller's scopes and
records that under `routing.fallback`. Loud rather than silent: a query whose
words name no entity can still be answered by the vector channel, which the
scorer cannot see, and an empty result is indistinguishable from an empty
store.

Two keys are easy to confuse. `chosen` is what routing picked. `considered` is
`query_across`'s, and is `chosen` minus any scope that turned out unreadable,
which is why the weights are keyed on it. An empty `scores` means the scorer
never ran.

#### The scratchpad

`retrieval/scratchpad.py` derives a block of what a scope currently believes,
ranked by the summed degree of a fact's two endpoints.

```python
from echo_memory.retrieval.scratchpad import scratchpad, render

facts = scratchpad(conn, group_id, limit=40, about=["checkout"], as_of=None)
text = render(facts)
```

A temporal property graph already is a key value record with validity: nodes
are the keys, edges are the values, and `t_invalid IS NULL` is the definition
of currently believed. So this needs no extraction model, no compression model,
and nothing to re-run when a fact is superseded. It is compact for the same
reason: a generated summary of a conversation that changed its mind twice
carries all three versions, and this carries the surviving one.

### What it costs

Routing: one indexed query per scope, plus a term count that is at most
`MAX_TERMS` early exiting index scans. A query with no searchable terms costs
no queries at all.

`estimate_tokens` is characters over four, the same constant
`eval/retrieval.py` uses for every token figure in `docs/BENCHMARKS.md`, so a
budget here is comparable with those.

Scratchpad: one query, no embedding call.

### What it does not do

**Routing cannot see a semantic match.** Lexical containment over node names
cannot find the scope whose relevant fact is phrased in words the query does
not use, and `query_memory` has a vector channel for exactly that case. A scope
rejected by the scorer can genuinely hold the answer. Two things lean against
it: containment rather than exact match, so a scope errs toward being kept, and
`select` never drops the caller's default.

**Nothing in routing is tuned.** `weights_for` says so in as many words. The
shape it returns is the claim being made, which is that a scope answering to
twice as much of the query is worth twice as much in the fusion, and that claim
is untested. `propose_weights=False` reproduces unweighted RRF.

**`estimate_tokens` undercounts, in one direction.** It undercounts code and
identifiers, where a BPE tokeniser splits `fact_embedding_hnsw_idx` into pieces
far shorter than four characters, and it undercounts non English badly: CJK and
Devanagari often run near one token per character, so it can be off by a factor
of three or four. It also counts the fact text alone, not the subject name
`render` prepends nor any JSON framing. A budget can therefore be exceeded in
real tokens while the report says it was honoured. It is kept because the
alternative is loading a tokeniser on the read path, and the budget's job is to
compare two configurations at the same spend.

**The budget truncates from the tail, never from the middle.** The list arrives
ranked, so a gap in it is a reordering nobody asked for: dropping a long fact at
rank 3 to fit a short one at rank 4 silently promotes the short one. A fact that
does not fit ends the list.

**A budget smaller than the top fact returns nothing, and that looks exactly
like an empty store.** The report is the only thing that distinguishes them,
which is why it is present even when no budget was set.

**The derived scratchpad was measured and it loses.** `eval/scratchpad_eval.py`
compared it against `query_memory` at matched token spend over a 60 fact
synthetic corpus, n=60 cases, on 2026-11-11:

| shape | measure | query_memory | scratchpad | Δ | 95% CI |
|---|---|---:|---:|---:|---|
| entity_pair | MRR | 0.9639 | 0.0347 | -0.9292 | [-0.9736, -0.8764] |
| entity_pair | recall@10 | 1.0000 | 0.0667 | -0.9333 | [-0.9833, -0.8667] |
| prose | MRR | 1.0000 | 0.0347 | -0.9653 | [-0.9958, -0.9222] |

No interval contains zero. The block reaches the ranker's recall at 16x the
tokens, at which point it holds all 60 facts, which is the whole scope, so at
parity it is the "inject everything" baseline rather than a cheaper context.

The limitation is as important as the result. `build_cases` makes one case per
live fact, so presence is `k/n` by construction and **the experiment cannot
credit or discredit centrality ranking at all**. It is also the question
distribution least favourable to any query independent block, and not the one
the claim it was built on was tested against. Latency was not measured, and the
scratchpad is cheaper on that axis. Nothing about `scratchpad.py` changed on
the strength of it. See `docs/BENCHMARKS.md` for the full table, the sweep and
the caveats.

**"Salient" here is degree, and degree is not importance.** A fact can matter
enormously and touch two entities mentioned once. `salience.py` exists for a
different quantity, how often a fact has actually been recalled, and is off
because the eval said it loses.

---

## 6. Knowledge exchange

### What it does

A bundle is a file. One deployment builds it from one scope, redacts it, signs
it and hands it over; another verifies it and writes its facts into a
quarantine scope that ordinary retrieval is never given. There is no endpoint
one store calls on another, no shared trust root, and no live protocol to get
wrong.

Four modules, in the order a bundle passes through them: `redact` decides what
may leave, `bundle` is the wire format and the content digest, `sign` is a
signature over the manifest, and `importer` does verification, replay refusal,
quarantine, promotion and revocation.

### The operator surface

```bash
echo-memory exchange export --scope shared --policy minimal \
  --deployment-id acme-prod --account acme --memory prod \
  --peer-account partnerco --out ./bundles        # dry run
echo-memory exchange export ... --confirm         # now it writes

echo-memory exchange anchor --key-id k1           # what a partner needs

echo-memory exchange import --manifest b.manifest.json --facts b.facts.ndjson \
  --trust-file ./partner.anchors --deployment-id acme-prod   # dry run
echo-memory exchange import ... --confirm

echo-memory exchange promote <bundle-id> --verifier ayush --confirm
echo-memory exchange revoke <bundle-id> --reason "partner withdrew the claim"
echo-memory exchange reject <bundle-id> --verifier ayush --reason "..."
echo-memory exchange list --state quarantined

# --group-id only when two scopes hold one bundle id, which is possible since
# 0038. An ambiguous id is refused rather than guessed: promoting the wrong
# account's import is not visible afterwards.
echo-memory exchange promote <bundle-id> --verifier ayush \
  --group-id acct:a:memory:main:import:<bundle-id> --confirm
```

Which commands need `--confirm` is asymmetric on purpose:

| Command | `--confirm` | Why |
|---|---|---|
| `export` | required | data leaves and cannot be recalled |
| `import` | required | another organisation's claims enter the store |
| `promote` | required | those claims become answers this store will give |
| `revoke` | no | only ever makes less readable |
| `reject` | no | only ever makes less readable |

A gate belongs in front of the direction that cannot be undone. An operator who
has just realised a bundle should not have been trusted should be able to
revoke it in one command, at two in the morning, without reading a prompt
first.

### The format

Two byte strings. The facts blob is newline delimited canonical JSON, one
redacted claim per line. The manifest is one JSON object carrying
`format_version`, `bundle_id`, `deployment_id`, `account`, `memory`,
`created_at`, `redaction_policy_id`, `fact_count`, `content_digest`,
`source_chain_head` and `signature`.

The digest is what makes the pair a unit: `sign` signs the manifest, the
manifest carries `content_digest`, and the digest covers the facts, so one
signature over a small object transitively covers however many facts the bundle
holds. An importer has to do both checks, because a valid signature over a
manifest paired with somebody else's facts blob proves nothing.

Three ceilings, refused in `bundle.parse` before the bytes they bound are read
and again in `import_bundle`:

| Limit | Value | What it bounds |
|---|---|---|
| `MAX_FACTS` | 10,000 | the scope's advisory lock. Every fact is written inside one transaction, measured at 16.9 ms each, so ~2.8 minutes at the ceiling and more with a network embedder |
| `MAX_BLOB_BYTES` | 16 MiB | memory. The blob is read whole, then held as a list of dicts for the length of the transaction |
| `MAX_MANIFEST_BYTES` | 64 KiB | the first thing read. A fully populated signed manifest measures 681 bytes |

The declared `fact_count` and the real parsed count are both checked against
`MAX_FACTS`: a manifest claiming 10 facts in a blob of 10,000 is an attack on
exactly the reader that trusts the cheap number.

Four things never leave, and three of them are rules of the format rather than
policy choices a bundle can make:

- **`group_id`.** The exporter's tenancy boundary. It means nothing to the
  recipient, and carrying it would invite an importer to write facts into a
  scope named by the sender.
- **An AGE `graphid` as an id.** A graphid is unique per *database*, not
  globally. Two deployments will issue the same one for unrelated facts, so the
  origin travels as `origin_fact_id` and is only ever used to build a qualified
  provenance string. Nothing joins on it.
- **A superseded version of a claim.** Only live facts are exported.
- **The origin's attestation chain.** A chain is per scope. What the manifest
  records instead is the exporter's chain head at build time, for comparison
  against one they published somewhere the importer's database cannot reach.

### Redaction

Two policies ship. `minimal` is the default, so forgetting to choose cannot
export more than choosing would.

| | `minimal` | `full` |
|---|---|---|
| `drop_entity_types` | `person`, `people`, `individual` | none |
| `drop_relations` | none | none |
| `drop_matching` | secret and contact shapes | the same shapes |
| `keep_agent_id` | no | yes |
| `keep_provenance` | no | yes |

**Redaction runs before signing, always.** The signature covers the redacted
content, so a recipient who verifies it is verifying the bundle that was
actually sent. Signing first would mean the signature attests to a bundle
nobody received, and a middle box could strip claims from a signed bundle with
the recipient unable to tell whether the exporter or the carrier had done it.
`bundle.build` enforces the order by construction.

`redact.audit(policy, facts)` is what an exporter reads before anything leaves:
counts per rule, the relations and entity types that were dropped, what was
stripped from the facts that were kept, and the text of the first 10 dropped
claims. The preview stays on the exporter's side by construction, since nothing
in the format has a field for it.

### Signing

```python
from echo_memory.exchange import sign

signature = sign.sign(manifest.unsigned())            # uses the env keys
anchor = sign.trust_anchor("k1")                       # what a partner needs
anchors = sign.parse_anchors("k1:ed25519:abc123...")   # key_id:alg:hexkey
ok, reason = sign.check(manifest, anchors=anchors)
```

`ECHO_MEMORY_EXCHANGE_KEYS` holds the signing keys and
`ECHO_MEMORY_EXCHANGE_KEY_ID` names the one that signs. ed25519 when
`cryptography` is installed, HMAC-SHA256 otherwise. An ed25519 private seed is
32 bytes; an HMAC secret under 16 bytes is refused as an obvious placeholder.

Two properties the module is careful about:

**`verify` takes its trust anchors as an argument and has no default.** A
verifier that reads its anchors from the same place the bundle came from is not
verifying anything; it is confirming that a file is internally consistent,
which anybody who wrote the file can arrange. An empty anchor set refuses every
bundle, including a correctly signed one.

**The algorithm comes from the anchor, never from the bundle.** An ed25519
trust anchor is a public key, published by design. If the verifier let the
bundle choose, an attacker could take that public key, use it as an HMAC shared
secret, sign a bundle of their own claims with `alg: hmac-sha256`, and the
verifier would compute the same HMAC and pass it. There is a test for exactly
that forgery.

### Importing

`importer.import_bundle` checks four things **before the first row is
written**, because a refusal after a partial write leaves facts behind that
nothing will clean up:

0. the size, against `bundle.MAX_FACTS`. First, because the digest below is
   recomputed over every claim, so on a huge bundle the safety checks are
   themselves the denial of service
1. the signature, against the `anchors` the caller supplied
2. the content digest, recomputed from the claims themselves
3. the `bundle_id` **and this import's scope**, against what that scope has
   already imported

The replay refusal is per scope, not per deployment. Two accounts on one
deployment may each import the same partner bundle, and one account may import
it into two of its own memories; what is still refused is the same bundle into
the same scope twice, which would write every fact again as new local nodes
that nothing afterwards could tell apart. `bundle_registry`'s key is
`(group_id, bundle_id)` for that reason: on the old global key the refusal
disclosed another account's import timestamp, direction and trust state.

Then, inside one transaction, it registers the bundle as `quarantined` and
writes each fact through `write_episode`, one episode per fact, at
`confidence="inferred"` with `agent_id="import:<deployment_id>"` and
`provenance.origin = "<deployment_id>:<origin_fact_id>"`. Facts never go
through raw Cypher, which is what gets them an embedding, an audit entry and a
link in this scope's attestation chain.

A bundle holding one fact this store cannot accept is refused **whole**.
Silently dropping the three claims that were too long would make the imported
scope a subset nobody can identify: the operator sees 197 of 200 facts and no
record of which three are missing.

### Imported knowledge is quarantined

The scope is `acct:<account>:memory:<memory>:import:<bundle_id>`, and
**ordinary retrieval is never given it**. Scope is the boundary this store
already enforces: `query_memory` takes one `group_id`, `query_across` takes a
list, and every index is keyed on it. A flag on each fact would need every
reader to filter on it, there are several readers, one of them in another
repository, and the failure mode of a forgotten filter is imported text
reaching a model's context with nothing to stop it.

`importer.readable_scopes(conn, account, memory)` is the only function that
hands out an import scope, and it hands out `promoted` ones. A caller that
builds its list for `query_across` from there cannot include a quarantined
bundle by accident, because the string it would need is not in the list.

Five registry states:

| State | Meaning |
|---|---|
| `created` | an export left. This deployment cannot know what happened to the file |
| `quarantined` | imported, written, not readable |
| `promoted` | a named verifier accepted it. Readable from now on |
| `rejected` | a verifier looked and said no. Nothing was ever trusted |
| `revoked` | was trusted and is not any more |

`reject` and `revoke` are distinct because the difference is not bookkeeping: a
reader of the log can tell "we looked and said no" from "we said yes and then
changed our mind", and only the second raises the question of what was already
acted on.

`promote` requires a `verifier_id` and records it. The engine cannot check
whether that verifier is entitled to promote anything, because it has no
concept of a principal; the capability is `roles.trust.verify` in cloud.
Requiring the argument anyway is the point: six months later the only question
anybody asks about an imported scope is who accepted it.

### What it does not do

**Facts already returned into an agent's context cannot be recalled by a
revocation.** They are in that conversation's history, in whatever the agent
wrote from them, and in anything downstream that kept the output. Revoking the
bundle does not reach any of them.

That is a property of having handed data over, not a gap in this
implementation, and no amount of further work here would change it: the same
statement is true of every system that returns data to a caller it does not
control. It is why the warning is printed at the **export** confirmation rather
than at revocation, which is the last point at which the decision is still
yours.

**Revocation does not reach the exporter's copy.** Revoking an import is this
deployment declining to believe a partner. The partner still holds what they
sent.

**Promotion does not check the claims.** The signature says who sent them and
the digest says they arrived unaltered. Neither says they are true. Promotion
is a person deciding to believe a partner, recorded.

**A signature does not prove the claims are honest.** A partner can sign a
bundle of inventions. That is why an imported fact lands at
`confidence="inferred"` whatever its signature says.

**With HMAC a signature does not prove who.** An HMAC key is shared, so the
verifier can produce any signature the signer could. It establishes that
somebody holding the shared secret produced the bundle, which between two
parties who each already trust the other is often enough and is worth nothing
as evidence to a third party. `TrustedKey.describe()` says which one a given
anchor gives you rather than letting a report imply the stronger claim.

**`drop_entity_types` matches a string the writer chose.** An entity's `type`
is free text, not a category this store validates. Dropping type `person`
catches a convention, not a class of data: a store whose writers used `human`,
`employee` or `x` keeps every one of them.

**`drop_matching` is a pattern list, not a classifier.** It catches the shapes
it names, and a person's name in ordinary prose matches nothing. A clean
`audit` says "none of these rules fired", not "no personal data is leaving".
The control that actually bounds an export is the scope it is taken from.

**Rejecting a bundle does not remove its facts.** They stay in the quarantine
scope. Removing them is `echo-memory forget --scope`, which is erasure, a
separate operator confirmed act with no undo.

**An unsigned bundle can be built and no importer will accept one.**
`--unsigned` exists and prints a warning saying so.

---

## 7. Hardening

### The graph write gate

`infra/graphlock.py` adds a graph wide advisory lock, and
`infra/graph_gate.enter` takes it as a statement from inside a transaction
`write_episode` already opened.

**It is off by default**, and the default was reversed deliberately.
`ECHO_MEMORY_MAX_GRAPH_WRITERS` unset means `UNLIMITED` and the gate does
nothing. Set to 1, one writer per graph. Set above 1, the gate turns off rather
than admitting exactly that many: a counting semaphore over advisory locks
would need a slot to poll for and a release path to forget, and an operator who
sets this to 4 is not asking for 4, they are saying they accept the risk.
Present but unparseable, including empty, fails safe at 1, because a typo must
not silently remove a protection somebody believed they had turned on.

The cost, measured: eight writer threads, 500 one fact episodes each into its
own scope, 4,000 episodes per run, migrated from base before every run, on
Postgres 16 with AGE under Colima.

| configuration | run 1 | run 2 | episodes/s |
|---|---|---|---|
| no gate | 46.5s | 40.2s | 86 to 100 |
| gate, limit 1 | 122.1s | 112.8s | 33 to 35 |
| gate off by env (limit 8) | 57.2s | | 70 |
| one writer alone, 500 episodes | 11.1s | | 45 |

2.6x to 2.8x slower in aggregate. The row that matters most is the last: with
the gate on, eight writers reach 33 to 35 episodes/s while **one** writer with
no contention at all reaches 45. Size an ingress for one writer's throughput
minus about a quarter. One sample, so treat it as an order of magnitude.

**The corruption did not reproduce on this hardware**, and the tests say so
rather than implying otherwise: 4, 8 and 16 concurrent cross scope writers,
about 30,000 episodes, zero errors and an intact label catalogue every time.
`tests/integration/test_graph_concurrency.py` carries the table of what was
tried and is a regression guard, not a reproduction. The gate is bought on the
production report, not on a local repro. What the tests do show positively is
that cross scope writers overlap in time today, so the hazard window is real
whether or not a laptop can make AGE fall into it.

So the default matches the evidence. The shape of a normal deployment is one
writer: an MCP server process per client, one scope per writer, and a hosted
service that is a single container. The production failure was four benchmark
shards doing bulk ingest, which is an operator running a specific job and able
to set a variable. Defaulting the gate on would charge every existing
deployment 2.6x on every write to guard a fault none of them is positioned to
hit.

An ingress that needs more than one writer per graph needs a different answer,
and there are two: a graph per tenant, since AGE's catalogue is per graph, or
separate databases, which is what cloud already does per customer. Neither is
in scope for this module.

### Lock keys

`graphlock.lock_key(namespace, value)` is a 64 bit signed key from
`sha256(namespace || NUL || value)`. `hashtext()` returns int4, so the existing
keys live in 2^32 values and two scopes can collide:

| scopes | 32 bit (`hashtext`) | 64 bit (`lock_key`) |
|---|---|---|
| 1,000 | 0.0116% | 1 in 36,914,751,044,020 |
| 10,000 | 1.157% | 1 in 368,935,825,950 |
| 100,000 | 68.781% | 1 in 3,689,349,193 |
| 1,000,000 | ~100.00% | 1 in 36,893,489 |

1.2% at ten thousand scopes is not a corruption risk. It is a tenancy one, and
it falsifies a claim this package makes out loud: `attestation.attest` says it
locks per scope "because scopes are tenants, and one account's writes must not
be able to block another's". Two colliding `group_id`s would block each other
for the length of a write, silently, looking like ordinary latency.

The two new locks, `verify|<gid>` and `graph|<graph>`, use the 64 bit key. The
two older ones deliberately stay on `hashtext`, because changing them is a
transition to manage and the new chains have nothing deployed holding the old
key.

### The retrieval envelope

`retrieval/envelope.py` marks what retrieval hands back so a caller can tell
memory from input.

```python
from echo_memory.retrieval import envelope

envelope.instruction_like(text)   # bool
envelope.flagged(facts)           # the fact_ids whose text reads as an instruction
envelope.instruction_like_fields(value)   # the paths inside a nested value
envelope.flagged_evidence(rows)   # the evidence_ids carrying those shapes
envelope.describe(facts, content_class=envelope.MEMORY, trust_summary=None,
                  evidence=None)
```

The same detector runs over evidence. `trust.evidence.for_fact` returns an
`instruction_like` list per row naming the paths - `uri`, `detail.summary`,
`detail.log.lines[1]` - and `echo-memory trust show` prints a warning against
each. Evidence is the one surface built to be read by an **operator** deciding
whether to verify a claim, so it is where an injected sentence reaches a
decision maker, and it was the only one of the three text surfaces (fact,
lesson, evidence) with no flag on it. The text is still stored and shown
verbatim: an attacker's URL is evidence of the attack, and truncating it would
destroy what the operator needs.

Three content classes: `memory`, `derived-inferred`, `imported-untrusted`. The
block carries a `handling` sentence shipped in the response rather than only in
documentation, because the consumer is frequently an LLM reading the tool
result and not a developer who read the docs.

`describe` returns `None` for plain memory with nothing flagged, so the common
response shape does not change and every existing MCP client keeps working
untouched.

**This is not a prompt injection fix, and the module's own docstring says
labelling is not a defence.** Text cannot be made safe by being labelled; a
model that will follow "ignore previous instructions" in a retrieved fact will
often follow it with a label attached. Anyone who ships labelling and calls the
problem solved has moved the risk rather than reduced it.

The controls that actually work are two, and neither is in this module:

1. **Authorization.** You cannot be injected by a fact you cannot retrieve. Per
   memory grants, resolved before the engine is entered.
2. **Trust gating.** `rejected` is never returned, and imported facts sit in a
   quarantine scope that ordinary retrieval does not reach until a verifier
   promotes them.

What this adds is a third structural layer, facts travelling in a data field a
caller can keep out of an instruction position, and a fourth diagnostic one.

Flagging **reports, it never removes**. A fact that reads like an instruction is
often a legitimate memory: "deploy only from the release branch" is a real one
from a real customer. Removing them would delete the product. Reporting them
means an operator can see a scope that has started filling with imperatives,
which is what an attack looks like in aggregate.

What the pattern list catches is counted rather than asserted. Over a 32 shape
attack corpus and a 24 string benign corpus, measured on 2026-11-11 and
recorded in `tests/unit/test_injection_detection.py`:

```
attacks flagged     26 of 32    recall 0.812
benign flagged       4 of 24    false positive rate 0.167
```

Both are pinned exactly, in both directions, so broadening a pattern means
re-reading the benign side rather than passing a `>=`. Four of the eight misses
are one narrow gap: `above` is in the ignore and disregard alternation and was
meant to be caught, but one intervening article defeats it, so "ignore the
above" does not match. The other four are the category no regex list closes: a
homoglyph, base64, a negated imperative, and a reframing with no keyword at
all. The four false positives are all genuine memory shapes.

**The context block is only reachable through `query_across(with_context=True)`
today**, on both the single scope and the fan out path. `query_memory` does not
emit one, nor does the MCP tool, and nothing passes a `trust_summary` to
`describe` anywhere in the engine.

### Metrics

`infra/metrics.py` is counters and timers aggregated in process and emitted as
one JSON line through the existing logger.

```python
from echo_memory.infra import metrics

metrics.counter("facts_written", 3, scope="shared")
with metrics.timer("query", channel="vector"):
    ...
metrics.observe("query", 12.4, channel="lexical")
metrics.snapshot()    # plain JSON serialisable
metrics.emit()        # one line
metrics.reset()
```

**It never records fact text, entity names or query text.** `infra/logging.py`
exists in its shape so logs are safe to ship off box without also shipping user
memory, and a metric label is a log field with a longer life, because it
reaches a collector and is retained there. Label keys that would carry content
are replaced with a visible `<rejected>` marker rather than dropped, so the
snapshot shows the rejection. Rejected rather than raised: telemetry must not
be able to fail a write.

Bounds: `MAX_LABEL_LENGTH` 64 characters, `MAX_SERIES_PER_NAME` 1000 distinct
label combinations per metric, past which observations land under
`<overflow>` so the total stays correct and the snapshot says what happened.

The OpenTelemetry bridge is **on by default**, which is the one decision in the
module worth defending. `opentelemetry-api` is already present in every install
as a transitive dependency of `mcp` rather than anyone's choice, so
importability is no evidence that anybody wants it on, and the registry is a
dict rather than an SDK that may have no provider behind it. With no provider
configured, `get_meter` hands back a proxy instrument and a record costs
0.044 us, measured over 200,000 calls against a loop floor of 0.013 us: about
30 ns of real work against a `write_episode` that takes tens of milliseconds.
So the bridge cannot put anything in a collector nobody configured, and cannot
be the reason a write is slow. `ECHO_MEMORY_OTEL=0` turns it off.

**No percentiles.** count, sum, min and max are exact and need no samples. A
p95 would need a reservoir, and a p95 from a bounded reservoir over a process
that has been up for a week is a p95 of whatever survived the sampling. If
percentiles are needed they are needed in a collector that has the raw
observations, which is what the bridge is for.

---

## Environment variables

Every variable this branch adds, plus the two retrieval flags that gate it.

| Variable | Default | What it does |
|---|---|---|
| `ECHO_MEMORY_TRUST_RETRIEVAL` | **unset, off** | Lets verification reach a read: suppresses `rejected` before truncation and reweights the rest. Two extra queries per read |
| `ECHO_MEMORY_LESSONS` | **unset, off** | Lets derived lessons enter as a reordering channel. Never adds a candidate |
| `ECHO_MEMORY_RERANK` | **unset, off** | Reorders the fused candidates by cosine to the query before truncating |
| `ECHO_MEMORY_MAX_GRAPH_WRITERS` | **unset, gate off** | 1 takes a graph wide write lock. Above 1 turns the gate off. Present but unparseable fails safe at 1. Costs 2.6x to 2.8x write throughput when on |
| `ECHO_MEMORY_VERIFICATION_KEYS` | unset, unkeyed | `key_id:hexkey[,...]` for the verification chain. A malformed entry raises |
| `ECHO_MEMORY_VERIFICATION_KEY_ID` | unset, first entry | Which of those signs from now on. A named key that is absent raises |
| `ECHO_MEMORY_EXCHANGE_KEYS` | unset | `key_id:hexkey[,...]` for signing bundles. ed25519 seed or HMAC secret |
| `ECHO_MEMORY_EXCHANGE_KEY_ID` | unset | Which signs. Absent is an error at signing time, not an implicit choice |
| `ECHO_MEMORY_INFERENCE_PROVIDER` | unset, `NullProvider` | `""`, `null`, `none` or `off` are explicit off. Anything else raises `UnknownProvider`, because a silent fallback makes a configured provider indistinguishable from an absent one |
| `ECHO_MEMORY_OTEL` | `1`, on | `0`, `false`, `no` or `off` disables the OpenTelemetry bridge |

The two key variables deliberately live in the environment and not in the
database: a key stored next to what it protects protects nothing.

## Migrations

| Migration | Adds |
|---|---|
| engine `0031_evidence` | `evidence`, `fact_evidence` |
| engine `0032_fact_verification` | `fact_verification`, `verification_attestation`, the `(edge_id, id DESC)` index |
| engine `0033_event_inbox` | `event_inbox` |
| engine `0034_decision_vocabulary` | three partial indexes for the reserved node types |
| engine `0035_bundle_registry` | `bundle_registry` |
| engine `0037_event_inbox_scoped_key` | widens `event_inbox`'s key to `(group_id, event_id)` |
| engine `0038_bundle_registry_scoped_key` | widens `bundle_registry`'s key to `(group_id, bundle_id)`, drops the redundant `bundle_registry_group_idx` |
| cloud `0020_agent_identity` | `agent`, `agent_run`, and `api_key.agent_ref`, `expires_at`, `capabilities`, with a backfill |

Nothing in this set drops or rewrites a column. `api_key.agent_id` survives
untouched and still NOT NULL, because its value is written into the `agent_id`
property of every `FACT` edge, and rewriting it to point at agent ids would
orphan the attribution on every fact already stored.
