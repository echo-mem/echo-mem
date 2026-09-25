---
name: echo-memory
description: Persistent memory across sessions and tools for this project, backed by a local Postgres graph. Use when the user states a decision, corrects you, states a preference, or says "remember this"; and at session start, or any time recalling prior context would save the user from re-explaining something they already told a past session or a different tool.
---

# Echo Memory

Memory that survives the session. Facts are edges between entities in a graph,
scoped to this user and attributed to this project, readable by any other tool
pointed at the same store.

## Recall before you ask

Call `query_memory` at session start, and before asking the user anything they
plausibly already told a past session or another tool. Checking costs one call;
making them re-explain costs their patience and is the entire problem this
exists to solve.

```
query_memory(scope="shared", query="why is the deploy branch master")
query_memory(scope="shared", digest=True)   # "catch me up", ignores query
```

If the response carries a `pending_ingest` field, memory files changed on disk
and may not be recorded yet. Changed is not the same as missing: a past session
may have written the facts and never marked the file done. Read each listed
file, `query_memory` for its subject, and only call `write_episode` if what it
states is genuinely absent — a duplicate fact costs more than a skipped one.
Either way, close it: 

```bash
echo-memory pending --done <path>
```

## Write the moment it happens

Call `write_episode` **in the same turn**, not batched at the end, whenever:

- the user states a decision — "we're using X", "X only deploys from branch Y"
- the user corrects you — "actually, X not Y"
- the user states a preference or a standing rule
- the user says "remember this" / "for future reference" / "don't do that again"
- you discover something non-obvious that cost real time to learn

Skip genuinely throwaway exchanges: typo fixes, one-off questions with no
lasting relevance. A missed memory costs more than one extra call.

## The exact shape

The server never calls an LLM. **You** extract the entities and facts; it
stores, resolves and retrieves them.

```
write_episode(
  scope="shared",
  session_id="<this session's id>",
  entities=[
    {"name": "Postgres", "type": "tool"},
    {"name": "storage decision", "type": "decision"},
  ],
  facts=[
    {"source": "storage decision", "target": "Postgres",
     "relation_type": "uses",
     "fact": "Switched from SQLite to Postgres for durability, 2026-08-20.",
     "confidence": "extracted"},
  ],
)
```

- `entities[].name` — non-empty, unique within the call. `type` is free text
  you choose ("tool", "person", "decision", "bug", "policy"), not a fixed enum.
- `facts[].source` / `.target` — must each match an `entities[].name` **exactly**.
- `facts[].relation_type` — free text ("uses", "caused_by", "blocked_by").
- `facts[].fact` — the sentence to remember. Write it so it still makes sense
  read cold in six months by a different tool: name the thing, don't say "it".
- `facts[].confidence` — **exactly** one of `"extracted"` (the user said it),
  `"inferred"` (you deduced it), `"ambiguous"` (uncertain). Not a number, not
  `"high"`/`"low"`, never omitted. Anything else is rejected.
- `facts[].causal_hint` — optional, and only when the session **said** so: one
  of `"caused_by"`, `"led_to"`, `"enabled_by"`, `"blocked_by"`,
  `"contradicts"`. This is the field `trace_cause` walks. Omit it when the link
  is merely associative, which most are. Never infer one: a guessed cause is
  indistinguishable from a real one once it is stored.

## When it asks you to disambiguate

If a name is close to an existing node but not close enough to merge silently,
the response comes back with `ambiguous_entities` and **those facts are not
written**. Decide, then call again with only the missing facts:

```
write_episode(..., entity_resolutions={
  "eigon-prod-postgres": {"resolved_to": "new",
                          "rationale": "an RDS instance, not the Eigon product"},
  "Postgresql": {"resolved_to": "<node_id from the candidates>"},
})
```

Resolving to `"new"` when the entity already exists creates a duplicate; folding
two distinct things into one node is worse and harder to undo. Read the
candidate names before choosing.

If you already know **every** entity in the call is new — a symbol you just
read, a title you just coined — pass `assume_new=True` instead of naming each
one. Anything you do name in `entity_resolutions` keeps the answer you gave it.

Neither one bypasses an exact name match. Case-insensitive name equality is
this system's definition of identity, so saying "new" for a name the scope
already holds resolves to the existing node rather than minting a second one.

## solo vs shared

- `scope="shared"` — the default choice. Readable by every tool this user runs,
  which is the point: a fact written here is one Cursor or a future session
  won't have to be told again.
- `scope="solo"` — this agent only. For notes that would be noise to anything
  else, e.g. your own working conventions in this repo.

Never construct a `group_id`. Scope resolves it server-side. The project is
resolved from the working directory the same way and is likewise never passed
in.

## Asking why

`query_memory` ranks facts by similarity and returns a flat list. It cannot
answer "why did this happen", because the answer to why is an ordered chain.
`trace_cause` walks the chain:

```
trace_cause(scope="shared", subject="checkout 502s", direction="upstream")
trace_cause(scope="shared", subject="the pool rewrite")   # both directions
```

It follows only links somebody recorded with `causal_hint`, in the direction
the hint says causality runs, so `"A led_to B"` and `"B caused_by A"` — the
same claim written from opposite ends — assemble into the same chain. Use it
for "why", "what did this break", "what was this a consequence of".

An empty answer means nobody asserted a cause, not that none exists. That is
why recording `causal_hint` at write time matters: you are the only one who
reads the sentence that states it.

## Reading it back outside the tools

```bash
echo-memory dashboard --serve     # live graph, all projects; click a link for
                                  # what a fact says, who wrote it, when, why
echo-memory why <fact_id>         # one fact's full audit trail
echo-memory --scope shared graph  # terminal view
echo-memory export --out ./export # markdown dump
```

## If MCP isn't available

Same engine, called in-process:

```python
from echo_memory.client import EchoMemory
memory = EchoMemory()
memory.write_episode(scope="shared", session_id="...", entities=[...], facts=[...])
memory.query_memory(scope="shared", query="...")
```

Set `ECHO_MEMORY_PROJECT` when the process's working directory isn't the
project — a container, a long-running service, a multi-tenant loop — or every
fact it writes lands under the wrong project.
