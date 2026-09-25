# Integrating Echo Memory into your own agent

This is for agents that aren't Claude Code or Cursor: a support chatbot, a DevOps agent,
a booking agent, or any custom loop with its own function-calling system. If your
framework already speaks [MCP](https://modelcontextprotocol.io), skip straight to
[`DEVELOPMENT.md`'s "Registering with Claude Code"](DEVELOPMENT.md#registering-with-claude-code)
section and point it at the same server; the JSON shape there works for any MCP client,
not just Claude Code.

If it doesn't speak MCP, use `EchoMemory` from `echo_memory.client` directly. It's the
same engine the MCP server runs (`write_episode`, `query_memory`, `trace_cause`,
`get_audit_log`),
called in-process as plain Python methods instead of over stdio. No separate server
process, no protocol framing.

```python
from echo_memory.client import EchoMemory

mem = EchoMemory()  # reads ECHO_MEMORY_USER_ID/AGENT_ID/DATABASE_URL from the environment
```

## The one rule that doesn't change

Echo Memory never calls an LLM itself. It has no API key and does no "understanding" -
it stores and recalls whatever structured entities/facts you hand it. **Your agent has
to do the extraction**: turn a raw conversation or event into `entities` (things) and
`facts` (relationships between them) before calling `write_episode`. If your agent is
already an LLM with tool-calling, this is usually just one more instruction in its
system prompt ("when something worth remembering happens, call write_episode with...").
If it's a simpler rules-based bot, you write that extraction step yourself.

## Example 1: a support chatbot

Remembers what a returning user already told it, across separate conversations.

```python
from echo_memory.client import EchoMemory

mem = EchoMemory()

# End of a conversation: the bot decided this is worth remembering
mem.write_episode(
    scope="shared",
    session_id="conv-4471",
    entities=[
        {"name": "user-8823", "type": "customer"},
        {"name": "Pro plan", "type": "product"},
    ],
    facts=[
        {
            "source": "user-8823", "target": "Pro plan", "relation_type": "subscribed_to",
            "fact": "customer 8823 is on the Pro plan and asked about annual billing discounts",
            "confidence": "extracted",
        }
    ],
)

# Next conversation, possibly days later
result = mem.query_memory(scope="shared", query="what plan is this customer on")
for fact in result["facts"]:
    print(fact["fact"])
# -> "customer 8823 is on the Pro plan and asked about annual billing discounts"
```

`scope="shared"` here because "the chatbot" is really one logical agent regardless of
which server process answers a given conversation; every instance should use the same
`ECHO_MEMORY_AGENT_ID` and reach into the shared pool for this customer's history.

## Example 2: a DevOps agent

Remembers past incidents and infrastructure decisions so it stops re-diagnosing the
same root cause every time it recurs.

```python
from echo_memory.client import EchoMemory

mem = EchoMemory()

mem.write_episode(
    scope="solo",
    session_id="incident-2026-08-21-01",
    entities=[
        {"name": "checkout-service", "type": "service"},
        {"name": "connection pool exhaustion", "type": "root_cause"},
    ],
    facts=[
        {
            "source": "checkout-service", "target": "connection pool exhaustion",
            "relation_type": "caused_by",
            "fact": "checkout-service 502s at 2026-08-21 03:14 UTC were caused by DB "
                    "connection pool exhaustion after a deploy dropped max_connections",
            "confidence": "extracted",
        }
    ],
)

# Weeks later, a similar page comes in
digest = mem.query_memory(scope="solo", query="checkout-service 502 errors", top_k=5)
```

`scope="solo"` here because incident history is specific to this one agent's
deployment; there's no other DevOps agent instance to pool it with. If you run this
agent against multiple environments (staging, prod), give each its own
`ECHO_MEMORY_AGENT_ID` so their incident histories don't mix.

## Example 3: a booking agent

Remembers a traveler's preferences so it doesn't ask the same questions on every trip.

```python
from echo_memory.client import EchoMemory

mem = EchoMemory()

mem.write_episode(
    scope="shared",
    session_id="booking-9931",
    entities=[
        {"name": "traveler-5502", "type": "customer"},
        {"name": "aisle seat", "type": "preference"},
    ],
    facts=[
        {
            "source": "traveler-5502", "target": "aisle seat", "relation_type": "prefers",
            "fact": "traveler 5502 always requests an aisle seat and flies economy",
            "confidence": "extracted",
        }
    ],
)

# Next booking request
prefs = mem.query_memory(scope="shared", query="seat and cabin preference for this traveler")
```

Same shared-scope reasoning as the chatbot example: whichever instance of the booking
agent handles the next request should still see this traveler's preferences.

## Choosing solo vs shared

- **`solo`**: this agent's own memory, private to one `ECHO_MEMORY_AGENT_ID`. Use it
  when the memory genuinely doesn't apply anywhere else (an agent's own operational
  history, like the DevOps example).
- **`shared`**: a pool every agent under the same `ECHO_MEMORY_USER_ID` can read and
  write. Use it for memory about the outside world - a customer, a traveler, a decision
  - that more than one agent instance or tool should be able to recall.

## Asking why

`query_memory` ranks facts by similarity and returns a flat list. That answers "what do I
know about this" and cannot answer "why did this happen", because the answer to why is an
ordered chain.

Record the link while you are already reading the sentence that states it:

```python
mem.write_episode(
    scope="shared", session_id=session,
    entities=[{"name": "checkout 502s", "type": "incident"},
              {"name": "pool size 5", "type": "config"}],
    facts=[{
        "source": "pool size 5", "target": "checkout 502s",
        "relation_type": "caused", "confidence": "extracted",
        "causal_hint": "led_to",
        "fact": "the pool was sized 5 and checkout started returning 502s under load",
    }],
)
```

Then ask:

```python
chain = mem.trace_cause(scope="shared", subject="checkout 502s", direction="upstream")
for links in chain["causes"]:
    print(" <- ".join(link["fact"] for link in links))
```

`causal_hint` is one of `caused_by`, `led_to`, `enabled_by`, `blocked_by`, `contradicts`,
and only ever what the conversation said. Nothing infers it, so an empty answer means
nobody recorded a cause rather than that none exists. Omit it when the link is merely
associative, which most are: filling the graph with causality nobody asserted makes the
field worth less than not having it.

`"A led_to B"` and `"B caused_by A"` are the same claim written from opposite ends. Use
whichever fits the sentence you just read; both assemble into the same chain.

## digest and get_audit_log

`query_memory(scope, query=None, digest=True)` skips relevance ranking and returns the
most recently written active facts - a "catch me up" call worth making at the start of
a session instead of guessing a query. `get_audit_log(scope, since=None)` returns a
chronological trail of what was written, invalidated, or superseded and why; useful for
debugging why an agent said what it said, or for a human reviewing what an agent has
been remembering about them.

## Seeing what's actually stored

`echo-memory --scope solo graph` (or `--scope shared graph`) prints a scope's memory
graph in the terminal; `--watch` refreshes it live as new writes land. See
[`DEVELOPMENT.md`'s CLI section](DEVELOPMENT.md#cli).
