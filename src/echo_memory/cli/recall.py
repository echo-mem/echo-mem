"""echo-memory recall: retrieval for the UserPromptSubmit hook.

Every other surface asks the agent to remember to call `query_memory`. The
dugout transcript on 2026-08-25 showed what that is worth: the session-start
briefing landed in context, the agent then made 31 tool calls, and not one of
them was a memory call. The plumbing was fine; remembering was the problem.

`UserPromptSubmit` is the one hook that fires on every prompt and knows what
was asked, so memory can be retrieved *against the prompt* and injected before
the agent does anything. No decision to forget.

Two constraints shape this:

**No embedding model.** The hook runs in a fresh process per prompt and the
embedder costs 6.2 seconds of cold start (measured, see cli/benchmark.py).
So retrieval here is lexical-only - Postgres full-text search, no model.
Recall is genuinely worse than a full query_memory call, and that is the
correct trade: some relevant memory arriving on every prompt beats better
memory arriving when somebody remembers to ask.

**Silence when there is nothing to say.** This injects into every prompt the
user types. A hook that always speaks becomes noise the model learns to skim,
which is how the session briefing lost to three competing hooks. A prompt too
short to retrieve against is skipped, and a prompt that matches nothing emits
nothing.

**There is no relevance floor on this path, and this docstring claimed there
was one until 2026-10-21.** The claim was not idle: it is the stated reason
the hook is safe to fire on every prompt. What actually happens is that the
lexical channel keeps whatever scored above zero - `TS_RANK_FLOOR` is 0.0 and
BM25 applies no floor at all - so a weak match is injected exactly like a
strong one. Only the vector channel is floored, and this path is
`lexical_only=True` because the hook is a fresh process per prompt and the
embedder costs 6.2s cold.

Left unfixed rather than patched, deliberately. A floor here changes which
facts reach the model on every prompt, and the bar for that in this codebase
is a measurement whose interval clears zero, not a plausible constant. The
lexical scores are ts_rank and BM25 values, which are not comparable to the
cosine quantile the vector floor is calibrated from, so the number would have
to be derived rather than borrowed. Until then the honest statement is the
one above."""

import json
import re
from datetime import UTC, datetime

from echo_memory.ingestion import capture
from echo_memory.retrieval.query_memory import query_memory
from echo_memory.trial import reads

# Injected on every prompt, so this stays small. Three facts is enough to
# answer "we already know this" without crowding out the user's actual words.
DEFAULT_TOP_K = 3
FACT_PREVIEW_CHARS = 220

# Below this many characters a prompt is "yes", "go on", "fix it" - words that
# match everything lexically and mean nothing. Retrieving against them returns
# noise dressed as relevance.
MIN_PROMPT_CHARS = 12

# Blocks the harness wraps around machine-generated text before it reaches this
# hook. They are not questions and nobody typed them.
#
# 40% of this hook's firings were one of these - background task completions,
# mostly - and on those the retrieval was worse than useless. `prompt_terms`
# takes the first MAX_TERMS = 12 salient words of a prompt, and a
# task-notification's XML envelope alone exceeds that: the tag names, a task
# id, a tool-use id and an absolute path consume the entire budget before the
# summary is reached. 99% of real notifications hit the cap. So every one of
# them issued nearly the same query - the generic vocabulary of tags and paths
# - and a deterministic ranker answered it with the same three facts forever.
# Those three took 50% of all facts delivered to notifications, against 10% on
# prompts a human actually typed, and only 42 distinct facts were reachable at
# all.
#
# Stripped rather than detected, because a typed prompt often ARRIVES with one
# of these appended - a system reminder rides along with the user's words - and
# skipping those would lose the real question. What is left after stripping is
# what somebody wrote. If that is nothing, there is nothing to retrieve against.
_MACHINE_BLOCKS = re.compile(
    r"<(task-notification|system-reminder|cross-session-message|teammate-message"
    r"|local-command-stdout|local-command-stderr|command-name|command-message"
    r"|command-args)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def human_part(prompt: str) -> str:
    """What is left of a prompt once the harness's own blocks are removed."""
    return _MACHINE_BLOCKS.sub(" ", prompt or "").strip()

# Days without a write before the hook says so. Three is long enough that a
# quiet weekend does not trigger it and short enough that a habit which has
# stopped is caught in the same week rather than at the trial's end.
QUIET_DAYS = 3


def unwritten_work(conn, config) -> dict:
    """What this session has queued but not recorded, and whether it has
    recorded anything at all.

    Both signals existed and neither reached the agent. `pending_ingest` is
    populated by the PostToolUse hook and the nudge to drain it lived inside
    `query_memory`'s response (server.py:157) - so an agent learned work was
    queued only by calling a tool it does not spontaneously call, which is the
    exact reason this hook exists. And nothing anywhere said "you have written
    nothing today".

    Measured, not theorised: across 2026-08-28 to 08-31 an agent did eight
    merged PRs on this repository and called write_episode zero times, while
    three noticed files sat unprocessed."""
    queued = capture.pending(conn)
    row = conn.execute(
        """SELECT max(timestamp) FROM public.audit_entry
           WHERE mutation_type = 'created' AND group_id = ANY(%s)""",
        ([config.group_id(s) for s in ("solo", "shared")],),
    ).fetchone()
    last = row[0] if row and row[0] else None
    days = (datetime.now(UTC) - last).days if last else None
    return {"pending": len(queued), "days_since_write": days}


def recall_for_prompt(conn, config, prompt: str, top_k: int = DEFAULT_TOP_K) -> dict:
    """Facts worth showing for this prompt, across both scopes."""
    prompt = (prompt or "").strip()
    typed = human_part(prompt)
    if not typed and prompt:
        return {
            "prompt": prompt,
            "skipped": "nothing here was typed by a person",
            "facts": [], **unwritten_work(conn, config),
        }
    # Retrieve against what a person wrote, not against the envelope it
    # arrived in.
    prompt = typed
    if len(prompt) < MIN_PROMPT_CHARS:
        return {
            "prompt": prompt, "skipped": "prompt too short to retrieve against",
            "facts": [], **unwritten_work(conn, config),
        }

    facts = []
    for scope in ("shared", "solo"):
        result = query_memory(
            conn, config.group_id(scope), prompt, top_k, embedder=None, lexical_only=True
        )
        if "error" in result:
            return {"prompt": prompt, "skipped": result["error"], "facts": []}
        for fact in result["facts"]:
            fact["scope"] = scope
            facts.append(fact)

    seen, unique = set(), []
    for fact in facts:
        if fact["fact"] in seen:
            continue
        seen.add(fact["fact"])
        unique.append(fact)
    return {
        "prompt": prompt, "skipped": None, "facts": unique[:top_k],
        **unwritten_work(conn, config),
    }


def render_context(result: dict) -> str:
    """Empty string means inject nothing. Callers must treat that as 'stay
    quiet', not as an error."""
    write_side = _write_side_lines(result)
    if not result["facts"]:
        # Silence is the default, but a store nobody is writing to is worth
        # saying out loud even when nothing matched the prompt. Both conditions
        # here are self-limiting: writing clears them.
        return "\n".join(write_side) + "\n" if write_side else ""
    lines = [
        (
            "Echo Memory already knows this, retrieved for the prompt above. "
            "Use it instead of re-deriving or asking the user again:"
        )
    ]
    for fact in result["facts"]:
        text = fact["fact"]
        if len(text) > FACT_PREVIEW_CHARS:
            text = text[:FACT_PREVIEW_CHARS].rstrip() + "..."
        # Attribution has to travel with the fact. This hook fires on every
        # prompt and is where a save becomes recognisable at all, yet it used to
        # emit bare text while instructing the agent to report who wrote it -
        # asking for a value the payload never carried.
        prov = fact.get("provenance") or {}
        author, fact_id = prov.get("agent_id"), fact.get("fact_id")
        tag = f" [written by {author}, fact_id {fact_id}]" if author and fact_id else ""
        lines.append(f"- {text}{tag}")
    lines.append(
        "If one of these saved the user re-explaining something a different tool told "
        "them, call record_recall_save with that fact's fact_id. Call query_memory for "
        "the fuller picture - this is a keyword match, not a semantic one."
    )
    lines += write_side
    return "\n".join(lines)


def _write_side_lines(result: dict) -> list[str]:
    """The write half. Reads were made automatic by this hook; writes stayed a
    polite request in a tool description, and the observed write rate was zero.

    Deliberately terse and deliberately conditional. This fires on every prompt,
    and a hook that always speaks becomes noise the model learns to skim - the
    failure that lost the session briefing to three competing hooks."""
    lines = []
    queued = result.get("pending") or 0
    days = result.get("days_since_write")
    if queued:
        lines.append(
            f"{queued} memory file(s) noticed but not in the graph yet. Run "
            "`echo-memory pending` to see them, read them, and write_episode what "
            "matters; then `echo-memory pending --done <path>`."
        )
    if days is not None and days >= QUIET_DAYS:
        lines.append(
            f"Nothing has been written to memory in {days} days. If this session has "
            "produced a decision, a correction, or a hard-won finding, call "
            "write_episode now rather than at the end."
        )
    return lines


def record_read(conn, config, result: dict, context: str, session_id: str | None = None) -> None:
    """What this prompt cost in injected characters, recorded after rendering
    because that is the only point where the real figure is known.

    Counted even when the context is empty: a read that returned nothing still
    happened, and reads-that-found-nothing over reads-total is the ratio that
    says whether retrieval is working.

    **One row per scope that answered.** This queries shared AND solo and used
    to record the lot against shared, so every solo fact it delivered was filed
    under a scope it did not come from. The store showed 227 active solo facts
    and not one of them ever returned in thirty days, which is not a fact about
    retrieval - it is the read log pointing at the wrong scope.

    That matters beyond tidiness. `returned_fact_ids` is what corroborates a
    cross-tool recall save against the read that delivered the fact, and a save
    citing a solo fact could never be matched to a read recorded under shared.

    The character count follows the facts. Charging both rows the full figure
    would double the injected-token total; the facts are what differ between
    the scopes, so the cost is split in their proportion, and a read that found
    nothing anywhere is still recorded once against shared."""
    facts = result.get("facts") or []
    by_scope: dict[str, list] = {}
    for fact in facts:
        by_scope.setdefault(fact.get("scope") or "shared", []).append(fact)

    if not facts:
        reads.record(
            conn, config.group_id("shared"), reads.HOOK,
            n_facts=0, injected_chars=len(context),
            project=config.project, session_id=session_id,
            agent_id=config.agent_id, fact_ids=[],
        )
        return

    # Allocated so the parts sum to the whole. Rounding each share on its own
    # loses a character to the halfway case, and injected_chars is summed into
    # a token total that should not drift because a read was split in two.
    remaining, left = len(context), len(facts)
    for scope, scoped in by_scope.items():
        share = round(remaining * len(scoped) / left) if left else 0
        remaining -= share
        left -= len(scoped)
        reads.record(
            conn, config.group_id(scope), reads.HOOK,
            n_facts=len(scoped),
            injected_chars=share,
            project=config.project, session_id=session_id,
            # agent_id was available here and never passed, which is why 434 of
            # this store's 458 reads carry no tool: the hook is the busiest read
            # surface by an order of magnitude, and it was the anonymous one. It
            # also decides whether a recall save can be corroborated against the
            # tool that made the read rather than only against the scope.
            agent_id=config.agent_id,
            fact_ids=[f.get("fact_id") for f in scoped],
        )


def render_hook_output(context: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
    )
