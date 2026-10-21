# Local Development

## Prerequisites

- Python 3.11+
- Docker and Docker Compose

No API key needed for v1a: extraction happens in the calling agent (not this server),
and embeddings use a local `sentence-transformers` model. See the design doc's MCP tool
contract section for why.

## First-time setup

```bash
git clone git@github.com:ayushcodes10/echo-mem.git
cd echo-mem

# Start Postgres with pgvector + Apache AGE (built from source on top of
# pgvector/pgvector:pg16, not the apache/age image; see docker/postgres.Dockerfile
# and the design doc's Foundational spike section for why)
docker compose up -d

# Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# edit .env: set ECHO_MEMORY_USER_ID, ECHO_MEMORY_AGENT_ID, ECHO_MEMORY_DATABASE_URL

# Apply migrations
alembic upgrade head
```

## Running the MCP server locally

```bash
python -m echo_memory.server
```

Runs over stdio (the `mcp` SDK's default transport), not a network listener, so there's
nothing to bind or expose beyond the local process. Each client should run its own
instance with a distinct `ECHO_MEMORY_AGENT_ID`; see the design doc's "Configuration"
section for how `scope: "solo" | "shared"` resolves per agent.

### Registering with Claude Code

Recommended: **user scope**, so the server is available across every project you work
in, not just this repo. The actual wedge use case (cross-tool memory) only shows up
under real, ambient usage, not usage confined to working on Echo Memory itself.

```bash
claude mcp add --scope user echo-memory \
  -e ECHO_MEMORY_USER_ID=your-user-id \
  -e ECHO_MEMORY_AGENT_ID=claude-code \
  -e ECHO_MEMORY_DATABASE_URL="postgresql://postgres:postgres@localhost:5433/echo_memory" \
  -- /path/to/echo-mem/.venv/bin/python -m echo_memory.server
```

Check it connected: `claude mcp list` (look for `echo-memory ... Connected`). Start a
*new* Claude Code session afterward; sessions already running when you register won't
pick it up. Undo with `claude mcp remove echo-memory --scope user`.

For per-repo registration instead (a distinct memory scope for one project, or testing
without touching your global config), use a project-local `.mcp.json`:
```json
{
  "mcpServers": {
    "echo-memory": {
      "command": "/path/to/echo-mem/.venv/bin/python",
      "args": ["-m", "echo_memory.server"],
      "env": {
        "ECHO_MEMORY_USER_ID": "your-user-id",
        "ECHO_MEMORY_AGENT_ID": "claude-code",
        "ECHO_MEMORY_DATABASE_URL": "postgresql://postgres:postgres@localhost:5433/echo_memory"
      }
    }
  }
}
```

Either way: each MCP client (Claude Code, Cursor, etc.) should point at its own instance
of this command with a distinct `ECHO_MEMORY_AGENT_ID`, sharing the same
`ECHO_MEMORY_DATABASE_URL` so `scope: "shared"` actually pools across them.

## CLI

```bash
echo-memory --scope solo why <fact_id>          # plain-language audit trail for one fact
echo-memory --scope solo export --out ./export  # markdown dump of a scope's memory
echo-memory --scope solo graph                  # terminal view of a scope's memory graph
echo-memory --scope solo graph --watch          # same, live-refreshing every 2s (--interval to change)
echo-memory --scope solo graph --html out.html  # alias for `dashboard --out` (every scope, not just --scope)
echo-memory status                              # v1a trial status: which Success Criteria are met so far
echo-memory trial check                         # criterion 6: the gate, and what's awaiting your judgement
echo-memory trial start                         # start the 3-week trial clock (--on YYYY-MM-DD to backdate)
echo-memory trial save "<what>" --from cursor   # log a recalled fact that saved re-explaining something
echo-memory trial log                           # every trial observation recorded so far
echo-memory dashboard --serve                   # one live page over every scope and project
echo-memory dashboard --out memory.html         # ...or a self-contained snapshot file
echo-memory --scope shared reattribute --list   # which session wrote which project's facts
echo-memory pending                             # memory files noticed but not yet in the graph
echo-memory session-brief                       # what memory knows about this project
```

`--scope` defaults to `solo`. `fact_id` is whatever `query_memory` returned for that fact
(see the MCP tool contract); there's no raw `group_id` argument, `--scope` resolves it the
same way the server does. `status` checks both scopes at once, so it ignores `--scope`.

`status` reports against the design doc's v1a Success Criteria section: whether both
`solo` and `shared` scopes have real data, whether at least one `entity_resolved` audit
entry and one fact-mutation audit entry exist, plus criterion 6's tallies (below).
Criterion 3 (a real question where hybrid retrieval beats either signal alone) is the one
criterion still with nowhere to record it - `status` says so explicitly rather than
guessing.

### Criterion 6: the v1a -> v1b gate

Criterion 6 decides whether v1b starts: **3+ real instances where a recalled fact saved
re-explaining something to a different tool, at most 1 duplicate node, zero incorrectly
merged entities, over a trial capped at 3 weeks.** Every bar is a human judgement, which
is why they need recording somewhere rather than being remembered at the end. `trial`
records them; `status` and `trial check` report them. Nothing here judges anything on its
own.

```bash
echo-memory trial start --on 2026-08-21        # the clock, so the 3-week cap is measured
echo-memory trial check                        # the gate + everything awaiting judgement
echo-memory trial save "..." --from cursor     # --from is the tool that wrote the fact;
                                               # --into defaults to ECHO_MEMORY_AGENT_ID
```

### Recording a recall save from the agent

The bar that matters is "3+ real instances where a recalled fact saved re-explaining
something to a different tool". That sat at 0/3 for three days, not because recall was
broken but because logging one meant noticing, switching to a terminal and typing a CLI
command. `write_episode` fires dozens of times a day precisely because an agent can call
it inline, so recall saves now have the same affordance:

```
record_recall_save(scope="shared", fact_id="1125899906842710", note="...")
```

`fact_id` is the id carried by the `query_memory` result that helped. Nothing
about who was involved is a parameter: the server reads the writer from that
edge's own `agent_id` and uses its own configured agent id as the reader.

Both used to be caller-supplied, and each in turn let the model being graded
type its own evidence. `written_by` was free text until 2026-08-29, so the
number gating v1a was a string the agent chose. `recalled_by` survived until
end-to-end testing on 2026-09-03 passed `recalled_by="codex"` from a
claude-code server and watched it produce a counted cross-tool save.

A same-tool save is recorded but does not count toward the bar. Recording the
identical note twice is a no-op, so a retry after an error is safe. A fact whose
`agent_id` is `unknown`, or absent because it predates attribution, is refused
outright — it cannot evidence which tool wrote it.

**This makes the gate agent-reported rather than human-judged.** That is a deliberate
tradeoff: the alternative, an agent-proposes/human-confirms flow, keeps the evidentiary
rigour but reintroduces exactly the friction that produced 0/3. The mitigations are that
neither side of the comparison is caller-supplied, same-tool saves never count, an
identical retry cannot move the counter, and every note stays readable in
`echo-memory trial log`. **Read the log
before declaring the gate met** - the count alone is a self-report from the system under
test.

`trial check` surfaces two kinds of open item, each printed with the exact command that
records a verdict on it:

- **Similar nodes that stayed separate** - one entity possibly split in two. Candidates
  are pairs scoring above the resolver's own `LOW_THRESHOLD`, i.e. pairs it would have
  flagged as ambiguous had they met in one call (see `ingestion/resolution.py`'s
  documented in-batch limitation). **Pairs spanning unrelated projects are suppressed by
  default**: `dugout-be` and `Eigon` scoring 0.6 is two codebases sharing vocabulary, not
  a split entity. The suppressed count is always printed, and `--all-projects` reviews
  them. A node referenced from several projects still counts as same-project with either
  side, since it genuinely belongs to both. Judge with `trial dup <a> <b> "<why>"` or
  `trial not-dup <a> <b>`.
- **Entity resolutions not yet reviewed** - two entities possibly merged into one. Judge
  with `trial merge-ok <audit_id>` or `trial bad-merge <audit_id> "<why>"`. Exact-name
  matches are excluded by default as near-always correct; `--all` includes them.

A judged pair or reviewed resolution never comes back, and a second contradictory verdict
on the same one is refused rather than silently overwriting the first. A save where
`--from` and `--into` are the same tool is recorded but doesn't count toward the bar: the
criterion is specifically about a *different* tool not needing to be re-told.

The page is graph-first: the canvas is full-bleed and every control floats over
it. An earlier version put the graph in a column between a cluster list and an
inspector, and the thing you came to look at got half the screen.

Labels appear for hubs, on hover and on selection — not for every node at once,
which is illegible at this density and was the main reason the earlier view read
as noise. Selecting a node dims everything outside its neighbourhood; selecting a
link dims everything except that one fact.

It commits to dark. A dense graph of luminous nodes reads far better on
near-black, and a washed-out light variant would be a worse view of the same
data rather than an equal one.

`--html` is now an alias for `dashboard --out`, kept so a command shipped last week still
works. Two behaviour changes it prints a note about: it renders **every** scope rather than
just `--scope`, and it includes superseded facts as history rather than hiding them. Prefer
`echo-memory dashboard` going forward.

`graph` is a read-only observability aid, not a CEO-plan scope item: it prints each active
fact as `source --[relation]--> target` plus any nodes with no active facts, so you can see
what an agent has actually written without a separate `query_memory` call. `--watch` polls
the database on an interval and clears/reprints, so it reflects new writes as they land.

## Publishing

The distribution is **`echo-mem`**, not `echo-memory`. That name is already
taken on PyPI by an unrelated hosted product in this same category (Textstone
Labs, published 2026-03-29), so `pip install echo-memory` installs theirs. The
import package stays `echo_memory` — the CLI, the MCP registration command
(`-m echo_memory.server`) and every existing install reference it, and renaming
the module would break live configuration to fix a cosmetic mismatch.

PyPI has no name reservation: claiming `echo-mem` means publishing a real
distribution.

```bash
pip install -e ".[dev]"
rm -rf dist && python -m build
twine check dist/*                          # must pass before uploading
twine upload --repository testpypi dist/*   # rehearse first
twine upload dist/*                         # claims the name, permanently
```

A published name and version can never be reused on PyPI, even after deletion,
so treat the first upload as one-way.

## Running tests

```bash
pytest                    # full suite
pytest -m "not eval"      # skip LLM-quality eval tests (slow, costs tokens)
pytest tests/unit/         # deterministic unit tests only
```

Per the CEO review's test-strategy split: deterministic logic (entity resolution
matching, RRF fusion, PPR correctness, audit log transactions) gets fast mocked unit
tests. The server has no LLM-judgment code path of its own to eval-test (extraction,
entity-resolution confirmation, and `causal_hint` classification all happen in the
calling agent, see the design doc's MCP tool contract); the `eval` marker (`pytest -m
eval`) is reserved for future retrieval-quality evals, not in use yet.

`tests/integration/` applies real migrations against `ECHO_MEMORY_DATABASE_URL` (its
`migrated_db` fixture runs `alembic upgrade head` before each test and `downgrade base`
after) and checks the resulting schema; it self-skips if that database isn't reachable.
CI runs it too now (see `.github/workflows/ci.yml`), against its own throwaway database.

**Point it at the `db-test` service, not `db`**, if you also have `echo-memory`
registered as a real MCP server (e.g. via `claude mcp add`) pointing at `db`'s database.
Running the suite against a database a real client is also using will downgrade that
schema to nothing mid-test-run, from underneath whatever's actually using it - this
happened once, and then happened again and emptied a live graph of 148 facts, because
a warning in a file is not a check.

So it is a check now. `tests/integration/conftest.py` refuses to collect at all
unless the database name ends in `_test` or it is the `db-test` service on port 5434,
and there is no override flag, because an escape hatch exported once in a shell
profile puts the hazard straight back. CI names its throwaway database
`echo_memory_test` for the same reason.

```bash
docker compose --profile test up -d db-test   # separate container, separate volume, port 5434
export ECHO_MEMORY_DATABASE_URL="postgresql://postgres:postgres@localhost:5434/echo_memory"
pytest
```

## Database migrations

```bash
alembic revision -m "short description"   # create a new migration
alembic upgrade head                       # apply
alembic downgrade -1                       # roll back one step
```

## Common issues

- **`alembic upgrade head` fails with "role postgres does not exist" even though
  `docker compose up` succeeded.** Something else on your machine (a native Postgres
  install, a homebrew service, an SSH tunnel) is already listening on 5432, and your
  connection is silently hitting that instead of the container. Check with
  `lsof -nP -iTCP:5432 -sTCP:LISTEN`. This is exactly why `docker-compose.yml` maps
  the container to host port 5433, not 5432; if you changed that back, change it back
  again, or point `ECHO_MEMORY_DATABASE_URL` at whatever port you actually chose.


## Installing into one project only

The user-scoped registration above is the right default on a personal machine:
register once, every session in every repo can write. When that isn't what you
want - a single Claude project, a repo whose memory shouldn't mingle with the
rest, a Cursor workspace, a shared machine - install per project instead:

```bash
echo-memory install                 # the current directory, for Claude Code
echo-memory install ../some-repo --for cursor
echo-memory install . --for both --project my-name
```

| target | writes |
|---|---|
| `claude` | `.claude/skills/echo-memory/SKILL.md` and a project-scoped `.mcp.json` |
| `cursor` | `.cursor/mcp.json` and `.cursor/rules/echo-memory.mdc` (`alwaysApply`) |

The skill is the half that matters even where the MCP server is already
registered globally: the tools being *available* is not the same as an agent
knowing to call them at the right moments, which is most of what SKILL.md is
for. Cursor has no skills, so the rule file carries the same guidance.

`ECHO_MEMORY_PROJECT` is pinned in the generated config rather than left to cwd
detection, because a project-scoped install is a statement about which project
this is: an agent launched from a subdirectory shouldn't be able to file its
facts somewhere else.

Existing config is merged, never replaced - `.mcp.json` commonly already holds
other servers, and a malformed one is refused rather than overwritten. Commit
the generated files to share the setup with the repo, or gitignore them to keep
it to yourself.

## Error contract

Every MCP tool returns `{"error": "..."}` rather than raising, for two classes of
failure: a bad `scope` (`ConfigError`) and a database outage
(`psycopg.OperationalError`, which covers connection loss, server down, and
`PoolTimeout` as a subclass). An agent gets something it can act on and relay to
you, instead of a stack trace.

The catch is deliberately narrow. `ProgrammingError` and `IntegrityError` are
**not** `OperationalError` subclasses, so a bad query or a violated constraint
still propagates loudly. Swallowing those into a polite "database unavailable"
message would hide a real bug behind an outage story.

## Cost and latency baseline

```bash
echo-memory benchmark              # 5 rounds against a throwaway benchmark group
echo-memory benchmark --rounds 20
```

v1a success criterion 5. Writes throwaway probe facts into a dedicated
`benchmark:scratch` group, never your real memory. Representative local run:

Re-measured 2026-10-21 against a **250 fact** scope, which is the number that
was missing. The table this replaces was taken before `DEFAULT_SEED_FACTS`
existed, so it measured an almost empty scope: its 8.4ms query was 2x faster
than the same query has any right to be, which is the mistake `benchmark.py`
warns about in its own docstring and had already made once in print.

| operation | min | median | max |
|---|---|---|---|
| `write_episode` | 19.8ms | 24.3ms | 27.3ms |
| `query_memory` | 14.9ms | 16.8ms | 53.0ms |
| query digest | 3.1ms | 4.2ms | 5.4ms |
| **cold start** (first write in a process) | | **5174ms** | |

**Cold start is reported separately on purpose.** It is the `LocalEmbedder` lazy-loading
sentence-transformers, and since every session starts its own MCP server process, it is
paid once per session rather than once per machine. Averaging it into the rest would
hide a real user-visible latency inside a max column while overstating steady-state cost.

The cost line is the one number here that isn't an estimate: **0 LLM calls per episode,
0 per query, $0.00 inference cost**, by construction rather than optimisation. Timings
are a rough local measurement and move with hardware, cache warmth and graph size —
which is also why nothing in CI asserts on them. The test asserts that a measurement was
produced and that the call count is zero, both deterministic.

## Projects

Every fact records the project it was written from, alongside the agent that
wrote it (`agent_id`) and the session (`provenance.session_id`). The project is
resolved server-side from the process's working directory - the repo root's
name, falling back to the directory's name - and is **never** passed in by the
calling agent, for the same reason `group_id` isn't: a value an agent types is
a value an agent types inconsistently, and "eigen" / "Eigon" / "eigen-backend"
across three calls would defeat the grouping entirely.

This works with no per-project setup because each session starts its own MCP
server process whose cwd is the project directory. `ECHO_MEMORY_PROJECT`
overrides it for callers where cwd means nothing: the direct Python client
inside a long-running service, a container whose cwd is `/app`, a chatbot
serving many tenants from one process.

**Project is not part of `group_id`.** `group_id` is the tenancy boundary;
making it per-project would partition memory per repo and destroy the
cross-project recall the system exists for. Project is an attribute *within* a
scope, and it lives on the edge rather than the node, because a fact is
authored in one project at one moment while a node like `Postgres` can be
referenced from several. A node's projects are derived from its edges.

Facts written before migration `0003` are marked `unknown`. Session ids are
per-install, so the migration doesn't guess:

```bash
echo-memory --scope shared reattribute --list                        # see what's unattributed
echo-memory --scope shared reattribute --session <id> --project eigen
```

## First run: importing work that already exists

A fresh store is empty; the machine it runs on usually isn't. By the time
anyone installs this there are already months of recorded decisions sitting in
per-project memory files, gstack learnings and CLAUDE.md files. Starting from
zero throws all of that away and makes you re-explain what you already wrote
down once.

So the first `query_memory` on a fresh store sweeps for it automatically, and
`echo-memory install` does the same. To run it by hand:

```bash
echo-memory bootstrap --dry-run          # list what would be queued
echo-memory bootstrap                    # queue it
echo-memory bootstrap --force            # sweep again after new work lands
echo-memory bootstrap --only claude-memory --only gstack-learnings
```

| source | where it looks |
|---|---|
| `claude-memory` | `~/.claude/projects/<project>/memory/*.md` |
| `gstack-learnings` | `~/.gstack/projects/<project>/learnings.jsonl` |
| `project-instructions` | `CLAUDE.md` in each discovered project |

Discovery never has to guess where you keep your work: the reference stores
name the paths, so the projects they point at are the projects that get swept.

**Deliberately not swept:** raw session transcripts
(`~/.claude/projects/*/*.jsonl`). They're enormous, mostly tool output, and
their signal has already been distilled into the memory files that *are*
swept. Queuing thousands of transcripts would bury the documents actually
worth reading.

It runs **once**, guarded by `bootstrap_state`, and it never fails a query:
recall is what the caller actually asked for, and a discovery problem has no
business breaking it. `--force` re-sweeps, and re-queues only what genuinely
changed.

Attribution falls back honestly: a project directory that no longer exists
can't be resolved from its encoded name, so it keeps the last path segment.
Fix any of it with `echo-memory reattribute` after the facts land.

Like the capture hook, **this queues, it does not extract** - see below.

## Automatic capture

`write_episode` fires when a model decides to call it. Measured over the first
two days of the v1a trial that was 4 times, while the agent's own file-based
memory wrote 7 files in the same window because a hook fired deterministically.
The capture path already existed; it just wasn't wired to Echo Memory.

### Install every hook at once

```bash
echo-memory install-hooks          # writes ~/.claude/settings.json
echo-memory install-hooks --dry-run
```

Five hooks, one command, safe to re-run: entries this tool wrote are replaced
rather than stacked, and hooks anything else registered are left alone.

**Why one command and not five copy-paste blocks.** This section used to
document each hook separately. That shape half-completes silently — the hooks
are independent, nothing checks that all of them landed, and a machine missing
one is indistinguishable from a machine missing none until months of memory
have quietly not been captured. They are not really independent anyway: they
are one mechanism in five places (notice a write, sweep for drift, recall on
the prompt, save before compaction, gate the stop), and a set where any member
can be absent is a set that will be.

The hooks go in the **user-level** settings file, which is what makes this work
in projects that don't exist yet. A per-project registration would have to be
remembered for every new repo — the exact failure this is fixing.

The individual blocks below are what the command writes, kept for anyone
merging them by hand.

### PostToolUse: queue a memory file the moment it is written

`scripts/capture-memory-hook.sh`. Register it as a `PostToolUse`
hook on `Write|Edit` in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "ECHO_MEMORY_USER_ID=you ECHO_MEMORY_AGENT_ID=claude-code ECHO_MEMORY_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/echo_memory ECHO_MEMORY_BIN=/path/to/echo-mem/.venv/bin/echo-memory /path/to/echo-mem/scripts/capture-memory-hook.sh"
          }
        ]
      }
    ]
  }
}
```

### UserPromptSubmit: retrieve against the prompt itself

Every other surface asks the agent to *remember* to call `query_memory`. On
2026-08-25 a dugout session received the session-start briefing in context, then
made 31 tool calls without a single memory call. The plumbing was fine;
remembering was the problem.

`UserPromptSubmit` fires on every prompt and knows what was asked, so memory is
retrieved against the prompt and injected before the agent acts. There is no
decision to forget.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [
        { "type": "command", "timeout": 10,
          "command": "ECHO_MEMORY_USER_ID=you ECHO_MEMORY_AGENT_ID=claude-code ECHO_MEMORY_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/echo_memory ECHO_MEMORY_BIN=/path/to/echo-mem/.venv/bin/echo-memory /path/to/echo-mem/scripts/user-prompt-hook.sh" }
      ]}
    ]
  }
}
```

Try it directly: `echo-memory recall "is chat-module-api dev or prod"`.

**Lexical-only, and that is a deliberate trade.** The hook runs in a fresh
process per prompt, and loading the embedding model costs **6.2 seconds** of
cold start (measured — see the baseline above). Postgres full-text search needs
no model, so this path is ~0.16s end to end. Recall is genuinely worse than a
full `query_memory` call, because keyword matching finds facts that share words
and misses ones that only share meaning. That is the difference between *some*
relevant memory arriving automatically and *none* arriving at all — not between
good and bad retrieval. The injected block says so, so the agent knows to call
`query_memory` for the fuller picture.

**ANY-term matching, not ALL.** `websearch_to_tsquery` ANDs every term, which is
right for a deliberate search and wrong for a typed sentence: *"is
chat-module-api dev or prod"* requires `chat`, `modul`, `api`, `dev` and `prod`
all to appear in one fact, and a fact written to answer exactly that question
still misses because the hostname tokenises as a single token. Measured, not
assumed. The prompt path ORs per-term `plainto_tsquery` calls instead — each
term still sanitised, never pasted into a `to_tsquery` string, per the design
doc's security review. The main `query_memory` path keeps AND semantics, which
is what v1a's retrieval was tested on.

**Silent when nothing matches**, because this injects into every prompt and a
hook that always speaks becomes noise the model learns to skim. Prompts under 12
characters are skipped: "yes", "go on", "fix it" match everything and mean
nothing.

### SessionStart: say memory exists, before anything else happens

This is the one that closes the circle. `write_episode` is discretionary, and
the "work is queued" nudge used to be delivered inside `query_memory`'s
response — so an agent only learned memory existed **if it had already used
memory**. A session that called neither tool heard nothing, silently. Two days
of real use across multiple projects produced **zero** organic writes.

`SessionStart` is the one moment guaranteed to happen in every session in every
project, and Claude Code injects `hookSpecificOutput.additionalContext`
deterministically rather than leaving it for the model to notice — unlike
instructions in a file, which it can skim past.

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "", "hooks": [
        { "type": "command",
          "command": "ECHO_MEMORY_USER_ID=you ECHO_MEMORY_AGENT_ID=claude-code ECHO_MEMORY_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/echo_memory ECHO_MEMORY_BIN=/path/to/echo-mem/.venv/bin/echo-memory /path/to/echo-mem/scripts/session-start-hook.sh" }
      ]}
    ]
  }
}
```

It injects, scoped to the project the session started in. **The instruction
comes first and the facts back it up** — the first version led with ~640
characters of fact text before ever saying what to do, and on 2026-08-25 it
fired alongside three other SessionStart hooks and was ignored. See it with
`echo-memory session-brief`.

Time-boxed to 5 seconds (`ECHO_MEMORY_BRIEF_TIMEOUT`). Session start is latency
you wait through before typing: a missing briefing costs one session's recall, a
hung hook costs the session.

### First run in an existing project

A first session in a project that is already months old shouldn't start with a
blank memory. When the session-start briefing finds a project with **no facts
and no recorded comprehension pass**, it asks the agent to do one, and names the
sources worth reading — graphify's `GRAPH_REPORT.md` first when present (it is
already a synthesis), then README, ARCHITECTURE, DESIGN, CLAUDE.md, docs, and
recent git history.

```bash
echo-memory analyse          # see the instruction for this project
echo-memory analyse --done   # record that the pass ran
```

The pass asks for **15-60 facts, not an inventory**: what the project is, its
architecture and boundaries, how it builds and deploys, conventions someone
would otherwise violate, and the gotchas. "deploys from master, never merge dev"
is worth keeping; "there is a file called utils.py" is not.

**Structure questions belong to graphify, not here.** That's a deliberate split,
not an oversight. graphify's graph for eigen is 9,321 nodes against this store's
115 facts — importing it would bury every recorded decision under file names,
and the two graphs answer different questions ("where is this code and what
calls it" versus "why is it like this and what did we decide").

The prompt stops as soon as the project has **any** fact, so an agent that
writes something and forgets `--done` can't pile up duplicate passes. `--done`
records it properly for reporting.

### PreCompact: catch it before the context goes

`PostToolUse` catches memory files as they're written. It cannot catch what a
session worked out but never wrote down, and compaction is where that
disappears. `scripts/precompact-hook.sh` fires immediately before compaction and
prints its reminder as **plain text on stdout**, which Claude Code passes as
`newCustomInstructions` — the instructions steering the summarisation itself, so
the reminder shapes what survives rather than being one more message the
summariser may drop:

```json
{
  "hooks": {
    "PreCompact": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "/path/to/echo-mem/scripts/precompact-hook.sh" }
      ]}
    ]
  }
}
```

**Do not give this hook a JSON body.** `hookSpecificOutput` is validated per
event, and PreCompact does not define one — the events that do are `PreToolUse`,
`PostToolUse`, `PostToolBatch`, `UserPromptSubmit`, `SessionStart`, `Stop` and
`SubagentStop`. This hook originally returned
`hookSpecificOutput.additionalContext`, copied from the SessionStart hook where
that shape is right, and every compaction from the day it landed until
2026-09-07 answered:

```
PreCompact [...] failed: Hook JSON output validation failed — (root): Invalid input
```

The hook still exited 0, so nothing looked broken, and the reminder never
reached a model once. `tests/unit/test_hook_output_contract.py` now runs each
hook script and rejects a `hookSpecificOutput` naming an event that cannot
accept it.

It deliberately does **not** touch the database. A hook on the compaction path
is latency the user waits through, and a reminder that always fires beats a
richer one that sometimes hangs on a connection — it also keeps working when the
database is down, which is exactly when you wouldn't want compaction to stall.
Needs no environment variables for the same reason.

The hook **queues; it does not extract**. Turning prose into entities and facts
needs a model, and the server never calls one (design doc, MCP tool contract,
architecture pivot). So `query_memory` returns a `pending_ingest` field at
session start listing what the graph hasn't heard about, the agent reads those
files and calls `write_episode`, and then:

```bash
echo-memory pending                    # what's outstanding
echo-memory pending --done <path>...   # close them once they're in the graph
```

The queue stores a digest, not the file's content: it tracks what still needs
reading, and a second copy of the memory would be one more place for the same
fact to disagree with itself. Editing a memory file reopens its entry, because
the file now says something the graph hasn't heard.

The hook never blocks or fails a tool call - a memory-capture side effect has
no business breaking the edit that triggered it - so every path exits 0.

### Stop: hold the session open until what it learned is stored

Everything above *asks*. Between 2026-08-23 and 2026-09-02 the session-start
briefing asked every session in every project to drain the capture queue, in
these words:

> N memory file(s) are queued but not yet recorded as facts. Run
> `echo-memory pending`, read each, and write_episode what it states.

In that window 16 memory files were written across eigen and dugout. The
capture hook noticed all 16. **None** were turned into facts by the session
that wrote them; they were ingested by hand ten days later, from a different
project. The instruction was correct, specific, and ignored 100% of the time.

The lesson is not that it needed better wording. Claude Code's own file-based
memory is a permanent system-prompt section, present on every turn; this was
one reminder at session start competing with several hundred registered tools.
Instructions lose to whatever is structurally louder. The things that worked in
that same window were all hooks.

So `scripts/session-stop-hook.sh` doesn't remind - it returns
`{"decision": "block", "reason": ...}`, which hands control back to the agent
with the reason as its next instruction. `Stop` fires when the agent is about
to finish, which is exactly when it still holds the context to say what it
learned and is no longer mid-task. A memory file written this session becomes a
fact in this session.

Three bounds keep it from being the hook people disable:

- **It fires once per session, recorded not inferred.** The first version
  trusted Claude Code's `stop_hook_active`, which is set only while a session is
  *continuing* from a stop hook — once the agent answers and stops again, it is a
  fresh stop with the flag clear. An eigen session hit the gate **five times** on
  2026-09-02 and spent 27 minutes unable to satisfy any of them. Gated sessions
  are now written to `stop_gate_fired` and never gated twice.
- **It states how to give up.** The gate cannot see the session's tool list, so
  it can block a session that has no way to comply — `write_episode` absent, the
  CLI not on `PATH`. The reason now tells that session to say so in one line and
  stop, and the command it prints is the hook's own absolute `ECHO_MEMORY_BIN`
  rather than a bare `echo-memory` that a virtualenv install does not put on
  `PATH`. An unsatisfiable demand with no stated way out is worse than no gate.
- **It is scoped to the current project.** Blocking an echo-mem session because
  eigen has a backlog would train everyone to switch it off.
- **It is silent when the queue is clean**, which is the steady state.

The reason names three outcomes, not one. A session that follows the MCP tool
description correctly - "call write_episode in the same turn" - and then writes
a Claude Code memory file leaves the queue holding a file whose content is
already in the graph. The first version of the reason offered only "write the
facts" or "it holds nothing durable", which told that session to write its facts
a second time. Already-stored is now an explicit outcome, because a gate that
fires on correct behaviour is a gate people switch off.

```bash
echo-memory stop-check              # what this project still owes
echo-memory stop-check --hook-json  # what the hook returns
```

### Reconcile: heal a hook that didn't fire

A queue only knows what it was told. On 2026-09-02 three eigen memory files
were found holding content the queue had never seen - the hook wasn't installed
when they were written, or the edit came through a path it didn't match.
Nothing detected it, and nothing would have.

`echo-memory reconcile` reads the filesystem instead of trusting the hook:
every `*/memory/*.md` under `~/.claude/projects/`, digest-compared against the
queue, with anything that moved reopened. It runs from the SessionStart hook
(silently, best-effort) and again inside `stop-check`, so a missed capture
self-heals instead of being permanent.

Deliberately the same scope as the capture hook. If the two disagreed about
what counts as a memory, the sweep would either keep reopening files the hook
ignores or keep missing files the hook queues.

## Releasing

**Pushing to git does not publish anything.** `pip install echo-mem` serves
whatever was last uploaded to PyPI, which is a separate act.

That gap bit this project once already. 0.1.0 was uploaded by hand on
2026-08-25 and never republished; by 2026-09-07 the published package was 30
commits and five migrations behind `main` — 0007 through 0011 — so a
pip-installed user could not reach the current schema at all. `pyproject.toml`
still said `0.1.0` throughout, so one version string named two materially
different codebases and nothing in the artifact revealed which one you had.

Releases now go out from a tag:

```bash
# 1. bump the version in pyproject.toml, commit it on a branch, merge it
# 2. tag the merge commit on main
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/release.yml` then builds an sdist and a wheel, refuses the
tag if it disagrees with `pyproject.toml`, checks the wheel actually carries
every migration script in `src/echo_memory/migrations/versions/`, and publishes.

The migration check is not ceremony. The migrations are package data and
`alembic.ini` deliberately is not shipped, so `echo-memory init-db` drives
Alembic through its Python API against the scripts inside the installed package
(see `cli/initdb.py`). A wheel missing them installs cleanly and then cannot
create its own database — the exact failure that command exists to prevent.

Auth is PyPI Trusted Publishing (OIDC), so no API token is stored in repository
secrets. It needs a **one-time setup on PyPI** before the first tagged release
will succeed: project settings → publishing → add a GitHub publisher for this
repository, workflow `release.yml`, environment `pypi`. Until that exists the
build steps pass and the publish step fails.

To verify a build locally before tagging:

```bash
python -m build
python -m venv /tmp/probe && /tmp/probe/bin/pip install dist/*.whl
ECHO_MEMORY_USER_ID=x ECHO_MEMORY_AGENT_ID=x \
  ECHO_MEMORY_DATABASE_URL=postgresql://... /tmp/probe/bin/echo-memory init-db
```

Run it from outside the repository. Inside a clone the package resolves from
`src/` and a wheel that ships nothing would still appear to work.

## Dashboard

```bash
echo-memory dashboard --serve --open   # http://127.0.0.1:8787, opens your browser
echo-memory dashboard --out memory.html --open
```

### Always on

A dashboard is only useful if looking at it is free. Remembering a command, an
env block and a venv path is enough friction that you stop glancing at it, and a
graph you don't glance at may as well not exist.

```bash
./scripts/install-dashboard-service.sh      # macOS LaunchAgent, starts on login
```

The page then just lives at **http://127.0.0.1:8787** — bookmark it. Restarts on
crash but not on a clean exit, so a down Postgres doesn't spin a relaunch loop.
Logs to `~/Library/Logs/com.echomem.dashboard.log`.

```bash
launchctl bootout gui/$(id -u)/com.echomem.dashboard    # uninstall
rm ~/Library/LaunchAgents/com.echomem.dashboard.plist
```

Localhost only, like the server itself.

One page over both scopes and every project. Nodes are coloured by the project
that talks about them most, filtered by the project chips and the scope
segment, searchable by fact text.

### Clusters: separating unrelated memory

Nodes are coloured by **detected cluster**, not by project. Project is metadata
— it says where a fact was written, not what it belongs with. Two facts in one
repo can be about entirely different things, and one idea can span repos.

Clusters come from the edges, via label propagation over the whole store. On
the real graph that separates `Trade-Ush` from `dugout-be` without being told
they're unrelated, and splits eigen into the product and its self-healing loop —
something a project label cannot do, because both are "eigen".

Three levels of separation, weakest to strongest:

| | |
|---|---|
| **cluster** | densely connected facts — related memory |
| **component** | no path at all between them — genuinely unrelated memory |
| **project** | where it was written; available as a colour toggle |

The sidebar lists clusters largest-first, named after each one's most-connected
node, with counts. Click to toggle one off. Layout anchors each cluster to its
own position and pushes harder between clusters than within one, so islands
read as gaps rather than as thinner regions of the same blob.

**Hubs don't spread their label.** A node connected far above the graph's norm
(at least 8 edges, and at least 4x the mean) keeps its place but stops donating
its community, so it can't pull unrelated clusters into one. Measured on the
real store: an `Ayush` node created during a portfolio backfill had degree 19 and
welded sixteen unrelated projects into a single 27-node blob. Dropping hubs from
the graph entirely over-corrects — 22 communities became 79, mostly debris,
because hubs also hold together things that genuinely belong together. Letting
them receive a label but not spread one un-welds the blob: 27 becomes 15 and the
largest clusters balance out at 15/13/12/12.

Known limit, stated rather than hidden: the rule is about **degree**. A node
joining two groups by one edge each still merges them. That's a bridge, and
separating on bridges needs articulation-point detection, a different algorithm.

Label propagation rather than Louvain, hand-written rather than `networkx`: the
algorithm is thirty lines, this graph is hundreds of nodes rather than millions,
and `networkx` is a v1b dependency (PR-B2) the v1a gate hasn't cleared.
Deterministic by construction — a graph that reshuffled its colours on every
render would be unreadable.

Clicking a **node** shows the facts it takes part in, active and superseded,
plus how it resolved during ingestion. Clicking a **link** shows what that one
fact carries:

| | |
|---|---|
| **what** | the fact text, its `relation_type`, and the confidence the agent stated |
| **when** | `t_valid`, and `t_invalid` if it has since been superseded |
| **who** | the agent that wrote it and the session it wrote it in |
| **where** | the project |
| **why** | the audit trail: created, superseded from what to what, and the entity-resolution rationale for the nodes at either end |

Only active facts draw a link, since a superseded fact is a relationship the
graph no longer asserts; it stays reachable from its node's list and from its
own history. `--serve` regenerates on every request, because a snapshot is
stale the moment the next episode lands. It binds to localhost only: the page
is every fact you have ever recorded, across every project.
