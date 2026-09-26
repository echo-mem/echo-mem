"""echo-memory infer-causal-hints: type the edges whose own text states a cause.

`causal_hint` and `trace_cause` shipped in 0.5.0. Production holds 38,479 edges
and 2 hints, both from a smoke test, because a hint is only written by a caller
that read one in what a session said and no session before 0.5.0 was asked. The
feature is correct and inert. Prompting at write time fixes the next fact;
nothing fixes the ones already stored.

This does, and the line it stays on the right side of is the whole design.

**What this is.** Extraction, done late. The design doc's standing refusal is on
statistical causal discovery: never infer causation from co-occurrence, from
graph structure, or from correlation. None of those is consulted here. The only
input is the fact's own sentence, and the only question asked of it is the one a
calling agent answers at write time - does this sentence state that one of these
two things brought about the other. "The pool was sized 5 so checkout returned
502s" says so. Two facts that happen to share an entity do not, and are never
looked at.

Three guards hold that line, in order of how much work they do:

1. **A causal connective must be present.** A sentence with no "so", "because",
   "led to", "blocked" or similar is never sent to a model at all. This is
   cheap, and it is also the honest filter: a sentence with no connective is not
   stating a relation, whatever a model might make of it.
2. **The model must quote the words.** Every proposal carries the span of the
   fact that states the relation, and a proposal whose quote is not literally
   present in the text is discarded before the graph sees it. A model that
   reasoned its way to a plausible cause from world knowledge cannot get one
   stored: it has to point at the sentence.
3. **The hint must name the edge's own two ends.** A fact can state a cause
   between things that are not its endpoints, and typing that edge would file a
   real claim against the wrong pair.

**Opt in, and offline.** This is a command a person types. It is not wired into
any migration, any hook, or any server path; nothing under ingestion/ imports
it. The engine's WRITE path remains model free, which is the claim the product
rests on, and this does not weaken it: the memory being read here was written
without a model, an operator chose to run this once, and `--clear` puts it back.

**Dry run by default.** A run with no `--write` touches no edge. It does record
what it found in `causal_hint_scan`, which is bookkeeping rather than memory:
without it, `--write` would have to ask the model again and could get a
different answer than the one the operator read. The same record makes the pass
resumable, which matters at 38,479 facts - an interrupted run has already paid
for every sentence it examined.

**Reversible.** An applied hint carries `causal_hint_origin` on the edge.
`--clear` removes only hints wearing that marker, so a hint a caller asserted at
write time is never touched by it, and each removal is audited like each write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from echo_memory.cli.llm import ModelUnavailable, Provider, complete, provider_from_env
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.ingestion.write_episode import VALID_CAUSAL_HINTS

_logger = get_logger("infer_causal_hints")

# What a hint applied by this command wears, and what `--clear` looks for. Read
# as "this hint was extracted from the fact's own text after the fact was
# written", as against a hint a caller asserted in the episode that wrote it -
# which carries no such key, like every hint written before this existed.
ORIGIN = "extracted_late"

# Every mutation records the session that made it. A batch command has no
# session, so it says what it is instead; `echo-memory why` prints this next to
# the hint, and an operator reading a fact's history should be able to tell at a
# glance that a person ran a tool rather than an agent writing what it was told.
SESSION_ID = "cli:infer-causal-hints"

# Facts read from the store per run. A full pass over a large scope is several
# runs, which is deliberate: the operator sees the bill and the proposals in
# instalments rather than discovering both at the end.
DEFAULT_LIMIT = 500

# Facts per model call. Small enough that one bad response costs little and that
# the model is not asked to hold thirty sentences in mind at once; large enough
# that the per-call overhead is not most of the cost.
DEFAULT_BATCH = 8

# The connectives that make a sentence a candidate. Word-boundary matched, so
# "sonar" is not "so" and "causeway" is not "cause".
#
# This list is the tool's first and most important refusal, and it is short on
# purpose. Every entry is a phrase that ASSERTS a relation between two things in
# the sentence that contains it. Words that merely order events ("then",
# "after", "later", "while") are absent and must stay absent: a sequence is not
# a cause, and admitting them would turn this pass into exactly the co-occurrence
# inference the design doc refuses.
CAUSAL_CUES = (
    "so that", "so", "because", "because of", "since", "as a result",
    "resulted in", "results in", "resulting in", "led to", "leads to",
    "leading to", "caused", "causes", "causing", "due to", "thanks to",
    "cause", "therefore", "thus", "hence", "consequently", "meant that", "means that",
    "in order to", "enabled", "enables", "enabling", "allowed", "allows",
    "made it possible", "blocked", "blocks", "blocking", "prevented",
    "prevents", "preventing", "stopped", "broke", "broken by", "triggered",
    "triggers", "forced", "forces", "why", "reason", "contradicts",
    "contradicted", "which is why",
)
_CUE_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(cue) for cue in sorted(CAUSAL_CUES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

NO_CUE = "no_cue"
NO_CAUSE = "no_cause"
HINT = "hint"
CLEARED = "cleared"

SYSTEM = (
    "You read one sentence at a time and report whether the sentence itself "
    "states that one of two named things brought about the other. You are not "
    "reasoning about the world; you are reading. If the sentence does not say "
    "it, the answer is null, and null is the common answer."
)

_RULES = """For each numbered item you are given a SOURCE, a TARGET and the TEXT of a
recorded fact about them. Answer with a JSON array, one object per item:

  {"n": 1, "hint": "led_to", "quote": "sized 5 so checkout returned 502s"}
  {"n": 2, "hint": null, "quote": null}

hint is one of these, chosen from the SOURCE's point of view, or null:

  led_to      the text says SOURCE brought about TARGET
  caused_by   the text says TARGET brought about SOURCE
  enabled_by  the text says TARGET made SOURCE possible, without causing it
  blocked_by  the text says TARGET stopped, prevented or broke SOURCE
  contradicts the text says the two are in conflict with each other

quote is the words, copied EXACTLY from TEXT, that state the relation. It must
appear in TEXT character for character. If you cannot point at the words, the
hint is null.

Rules, in order:
1. Only what the text states. Your own knowledge about these things is not
   evidence, however sure you are.
2. Sequence and correlation are not causation. "X, then Y" and "X and Y both
   happened" are null.
3. The cause and the effect must be SOURCE and TARGET. If the text states a
   cause between other things, the answer is null.
4. If the direction is unclear, answer null. A chain assembled from a guessed
   direction reads backwards and nothing downstream can tell.
5. Output the JSON array and nothing else."""


class InferenceError(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    edge_id: str
    fact: str
    source: str
    target: str


@dataclass(frozen=True)
class Proposal:
    edge_id: str
    hint: str
    quote: str


def states_a_cue(text: str) -> bool:
    """Whether the sentence contains a connective that asserts a relation.

    The gate before any model call. It is a keyword test and it is meant to be:
    the expensive, fallible judgement is what the sentence MEANS, and there is
    no point paying for that on a sentence that does not even claim a relation.
    """
    return bool(_CUE_PATTERN.search(text or ""))


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def quotes_the_text(quote: str, fact: str) -> bool:
    """Whether a proposal's quote is really in the fact.

    Whitespace is normalised and case is ignored, because a model re-typing a
    span gets those wrong without changing what it pointed at. Nothing else is
    forgiven: a quote that is a paraphrase, a summary or an invention fails, and
    failing means the proposal is dropped.
    """
    if not quote or not fact:
        return False
    return _normalised(quote) in _normalised(fact)


# --- reading the store --------------------------------------------------------
#
# Straight SQL over the label tables rather than Cypher. The filter is "active,
# in this scope, no hint yet, not already examined", and the last of those is a
# join against a plain Postgres table that Cypher cannot see. FACT.id has an
# index (0013) and Node.id has one (0024), so the endpoint lookup is two index
# probes per row.

_CANDIDATE_SELECT = f"""
    SELECT e.id::text,
           (e.properties ->> '"fact"'::agtype),
           (s.properties ->> '"name"'::agtype),
           (t.properties ->> '"name"'::agtype)
      FROM {GRAPH}."FACT" e
      JOIN {GRAPH}."Node" s ON s.id = e.start_id
      JOIN {GRAPH}."Node" t ON t.id = e.end_id
      LEFT JOIN public.causal_hint_scan cs ON cs.edge_id = e.id
     WHERE (e.properties ->> '"group_id"'::agtype) = %s
       AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
       AND (e.properties ->> '"causal_hint"'::agtype) IS NULL
       AND cs.edge_id IS NULL
     ORDER BY e.id
     LIMIT %s
"""


def candidates(conn, group_id: str, limit: int) -> list[Candidate]:
    """The next window of facts nobody has examined.

    Retired facts are excluded: nothing retrieves them and `trace_cause` filters
    them out, so a hint on one would be a model call spent on a chain no reader
    can reach.

    Ordered by edge id, which is monotonic, so successive runs walk the store
    once rather than re-reading the front of it.
    """
    rows = conn.execute(_CANDIDATE_SELECT, (group_id, limit)).fetchall()
    return [
        Candidate(edge_id=str(edge_id), fact=fact or "", source=source or "", target=target or "")
        for edge_id, fact, source, target in rows
    ]


def unexamined(conn, group_id: str) -> int:
    """How much of this scope is still unread, so a run can say what is left."""
    row = conn.execute(
        f"""SELECT count(*)
              FROM {GRAPH}."FACT" e
              LEFT JOIN public.causal_hint_scan cs ON cs.edge_id = e.id
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
               AND (e.properties ->> '"causal_hint"'::agtype) IS NULL
               AND cs.edge_id IS NULL""",
        (group_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def record(conn, group_id: str, edge_id: str, verdict: str, *,
           hint: str | None = None, quote: str | None = None,
           model: str | None = None) -> None:
    """One verdict per fact examined, so no fact is ever examined twice.

    ON CONFLICT DO UPDATE rather than DO NOTHING: `--rescan` deletes rows, but a
    re-examination that reaches here with a row already present means two runs
    overlapped, and the later verdict is the one that was actually reached.
    """
    conn.execute(
        """INSERT INTO public.causal_hint_scan
               (edge_id, group_id, verdict, hint, quote, model)
           VALUES (%s::text::graphid, %s, %s, %s, %s, %s)
           ON CONFLICT (edge_id) DO UPDATE
               SET group_id = EXCLUDED.group_id, verdict = EXCLUDED.verdict,
                   hint = EXCLUDED.hint, quote = EXCLUDED.quote,
                   model = EXCLUDED.model, scanned_at = now()""",
        (edge_id, group_id, verdict, hint, quote, model),
    )


def pending(conn, group_id: str) -> list[Proposal]:
    """Proposals recorded and not yet applied - what `--write` would write.

    This is what makes "review, then apply" possible without a second model
    call. A dry run leaves its proposals here; `--write` reads them back.
    """
    rows = conn.execute(
        """SELECT edge_id::text, hint, quote
             FROM public.causal_hint_scan
            WHERE group_id = %s AND verdict = %s AND applied_at IS NULL
            ORDER BY edge_id""",
        (group_id, HINT),
    ).fetchall()
    return [Proposal(edge_id=str(e), hint=h, quote=q or "") for e, h, q in rows]


def applied_hints(conn, group_id: str) -> list[tuple[str, str]]:
    """(edge_id, hint) for every hint this command has applied and not cleared.

    Read from the graph, not from the scan table: the marker on the edge is the
    authority on what is out there, and a scan row that disagreed with it would
    be the bug this read is meant to survive.
    """
    rows = conn.execute(
        f"""SELECT e.id::text, (e.properties ->> '"causal_hint"'::agtype)
              FROM {GRAPH}."FACT" e
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND (e.properties ->> '"causal_hint_origin"'::agtype) = %s
             ORDER BY e.id""",
        (group_id, ORIGIN),
    ).fetchall()
    return [(str(edge_id), hint) for edge_id, hint in rows]


# --- asking a model -----------------------------------------------------------


def render_prompt(batch: list[Candidate]) -> str:
    lines = []
    for i, c in enumerate(batch, start=1):
        lines.append(f"{i}. SOURCE: {c.source}\n   TARGET: {c.target}\n   TEXT: {c.fact}")
    return f"{_RULES}\n\n{chr(10).join(lines)}"


def parse_response(text: str, batch: list[Candidate]) -> dict[str, dict]:
    """The model's answer as {edge_id: {"hint", "quote"}}, shape-checked.

    Tolerant about the envelope and strict about the content. Models wrap JSON
    in prose or a code fence often enough that refusing the whole batch over it
    would waste the call, so the first array in the response is what gets read.
    An unparseable response raises, because recording "no cause" for eight facts
    because a response was malformed would be a permanent wrong answer written
    from a transport-level problem.

    Anything else out of shape is dropped silently for that one item: an
    unknown hint, a missing quote, an item number that was not asked about. A
    hint outside VALID_CAUSAL_HINTS is unwalkable, so storing it would make a
    fact read as causally typed while answering nothing.
    """
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise InferenceError(f"no JSON array in the model's response: {text[:200]!r}")
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise InferenceError(f"unparseable JSON in the model's response: {e}") from e
    if not isinstance(items, list):
        raise InferenceError("the model's response was not a list")

    found: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(batch):
            continue
        hint, quote = item.get("hint"), item.get("quote")
        if hint is None:
            continue
        if not isinstance(hint, str) or hint not in VALID_CAUSAL_HINTS:
            continue
        if not isinstance(quote, str) or not quote.strip():
            continue
        found[batch[n - 1].edge_id] = {"hint": hint, "quote": quote.strip()}
    return found


class ModelExtractor:
    """The default extractor: one provider call per batch.

    A class rather than a closure so `name` can travel with it into the scan
    record. Which model produced a verdict is the first thing anyone will want
    when a proposal looks wrong.
    """

    def __init__(self, provider: Provider):
        self._provider = provider
        self.name = provider.model

    def __call__(self, batch: list[Candidate]) -> dict[str, dict]:
        return parse_response(
            complete(self._provider, SYSTEM, render_prompt(batch)), batch
        )


# --- the pass ----------------------------------------------------------------


def scan(conn, group_id: str, extractor, limit: int = DEFAULT_LIMIT,
         batch_size: int = DEFAULT_BATCH) -> dict:
    """Read the next window of facts and record a verdict for each.

    Touches no edge. What it writes is its own bookkeeping, and the separation
    is the point: this half can be re-run, interrupted and reviewed without any
    of it reaching memory.

    A batch whose model call fails aborts the run rather than being recorded as
    "no cause". A verdict is permanent until `--rescan`, and a timeout is not a
    reading of a sentence.
    """
    window = candidates(conn, group_id, limit)
    no_cue = [c for c in window if not states_a_cue(c.fact)]
    cued = [c for c in window if states_a_cue(c.fact)]

    for c in no_cue:
        record(conn, group_id, c.edge_id, NO_CUE)

    proposals: list[Proposal] = []
    quote_failures: list[Candidate] = []
    examined = 0
    for i in range(0, len(cued), batch_size):
        batch = cued[i : i + batch_size]
        found = extractor(batch)
        examined += len(batch)
        for c in batch:
            proposal = found.get(c.edge_id)
            if proposal and not quotes_the_text(proposal["quote"], c.fact):
                # The guard that does the real work. A quote that is not in the
                # sentence means the relation was reasoned, not read, and this
                # tool has no licence to store that.
                #
                # The quote itself is not logged. It is a span of a user's own
                # memory, and operational telemetry in this codebase carries
                # counts and ids, never fact text.
                _logger.info(
                    "causal_hint_quote_rejected",
                    extra={"fields": {"group_id": group_id, "edge_id": c.edge_id}},
                )
                quote_failures.append(c)
                proposal = None
            if proposal:
                record(conn, group_id, c.edge_id, HINT, hint=proposal["hint"],
                       quote=proposal["quote"], model=getattr(extractor, "name", None))
                proposals.append(
                    Proposal(c.edge_id, proposal["hint"], proposal["quote"])
                )
            else:
                record(conn, group_id, c.edge_id, NO_CAUSE,
                       model=getattr(extractor, "name", None))

    return {
        "read": len(window),
        "no_cue": len(no_cue),
        "examined": examined,
        "proposed": len(proposals),
        "quote_rejected": len(quote_failures),
        "proposals": proposals,
        # The window itself, so the caller can print a proposal next to the
        # sentence it came from without reading the store a second time.
        "facts": {c.edge_id: c for c in window},
    }


def _audit(conn, group_id: str, mutation_type: str, edge_id: str, summary: str,
           detail: str | None = None) -> None:
    """One audit entry per edge changed, stamped with the version that changed it.

    writer_version is not optional here. `health` treats an audit entry written
    after migration 0019 with no version as proof that the writer predates the
    column - that is what the check is for - so a batch command that left it
    null would make every store it touched report a stale writer.
    """
    from echo_memory import __version__

    conn.execute(
        """INSERT INTO public.audit_entry
               (group_id, session_id, writer_version, mutation_type,
                affected_edge_ids, summary, resolution_detail)
           VALUES (%s, %s, %s, %s::public.audit_mutation_type,
                   %s::text[]::graphid[], %s, %s)""",
        (group_id, SESSION_ID, __version__, mutation_type, [edge_id], summary, detail),
    )


def apply(conn, group_id: str, proposals: list[Proposal]) -> dict:
    """Write the recorded proposals onto their edges, audited, marked, reversible.

    One Cypher call per distinct hint rather than per edge: five calls at most,
    against 38,479 potential edges.

    Every call reads back the ids it changed and only those get an audit entry
    and an `applied_at`. AGE does not make a statement's effect and a script's
    assumptions match on its own - a rollback here has failed to undo a commit
    before - so what the graph says it did is what gets recorded. The
    `causal_hint IS NULL` guard means an edge a caller typed between the scan
    and the write is skipped rather than overwritten, and shows up in the
    difference between what was proposed and what was applied.
    """
    by_hint: dict[str, list[str]] = {}
    for p in proposals:
        by_hint.setdefault(p.hint, []).append(p.edge_id)
    quotes = {p.edge_id: p.quote for p in proposals}

    applied: list[str] = []
    for hint, edge_ids in sorted(by_hint.items()):
        rows = conn.execute(
            f"""SELECT * FROM cypher('{GRAPH}', $$
                MATCH ()-[e:FACT]->()
                WHERE id(e) IN $ids AND e.group_id = $gid AND e.causal_hint IS NULL
                SET e.causal_hint = $hint, e.causal_hint_origin = $origin
                RETURN id(e)
            $$, %s) AS (edge_id agtype)""",
            (json.dumps({"ids": [int(e) for e in edge_ids], "gid": group_id,
                         "hint": hint, "origin": ORIGIN}),),
        ).fetchall()
        changed = [str(edge_id) for (edge_id,) in rows]
        for edge_id in changed:
            _audit(
                conn, group_id, "causal_hint_set", edge_id,
                f"causal_hint {hint!r} extracted from the fact's own text",
                f"states it: {quotes.get(edge_id, '')!r}",
            )
            conn.execute(
                """UPDATE public.causal_hint_scan SET applied_at = now()
                    WHERE edge_id = %s::text::graphid""",
                (edge_id,),
            )
        applied += changed

    skipped = [p.edge_id for p in proposals if p.edge_id not in set(applied)]
    _logger.info(
        "infer_causal_hints_applied",
        extra={"fields": {"group_id": group_id, "applied": len(applied),
                          "skipped": len(skipped)}},
    )
    return {"applied": applied, "skipped": skipped}


def clear(conn, group_id: str) -> dict:
    """Remove every hint this command applied, and nothing else.

    Keyed on `causal_hint_origin`, so a hint a caller asserted at write time is
    invisible to this: it carries no such key, like every hint written before
    this command existed.

    The hints are read before they are removed, because the audit entry has to
    say what was taken away and afterwards there is nothing left to ask. The
    scan rows survive as 'cleared' rather than being deleted, so a later run
    does not re-propose what an operator has just rejected.
    """
    before = applied_hints(conn, group_id)
    if not before:
        return {"cleared": []}

    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH ()-[e:FACT]->()
            WHERE e.group_id = $gid AND e.causal_hint_origin = $origin
            SET e.causal_hint = null, e.causal_hint_origin = null
            RETURN id(e)
        $$, %s) AS (edge_id agtype)""",
        (json.dumps({"gid": group_id, "origin": ORIGIN}),),
    ).fetchall()
    removed = {str(edge_id) for (edge_id,) in rows}

    cleared = []
    for edge_id, hint in before:
        if edge_id not in removed:
            continue
        _audit(
            conn, group_id, "causal_hint_cleared", edge_id,
            f"cleared causal_hint {hint!r}, extracted late by {SESSION_ID}",
        )
        conn.execute(
            """UPDATE public.causal_hint_scan
                  SET verdict = %s, applied_at = NULL
                WHERE edge_id = %s::text::graphid""",
            (CLEARED, edge_id),
        )
        cleared.append((edge_id, hint))
    return {"cleared": cleared}


def rescan(conn, group_id: str) -> int:
    """Forget every verdict for this scope, so the next run reads the text again.

    Only the verdicts. Hints already applied stay applied and stay marked, so
    `--clear` still finds them; this is "ask again", not "undo".
    """
    rows = conn.execute(
        "DELETE FROM public.causal_hint_scan WHERE group_id = %s RETURNING edge_id",
        (group_id,),
    ).fetchall()
    return len(rows)


# --- output ------------------------------------------------------------------


def render_scan(scope: str, result: dict, remaining: int, write: bool) -> str:
    """What the pass found, sentence by sentence.

    The quote is printed next to every proposal on purpose. It is the whole
    justification for the hint, and an operator deciding whether to pass --write
    is deciding whether those words really say what the hint claims.
    """
    facts: dict[str, Candidate] = result.get("facts") or {}
    lines = [
        (
            f"{scope}: read {result['read']} fact(s) not yet examined, "
            f"{result['no_cue']} with no causal connective, "
            f"{result['examined']} read by the model."
        ),
    ]
    if result["quote_rejected"]:
        lines.append(
            f"  {result['quote_rejected']} proposal(s) dropped: the quote was not "
            f"in the fact's own text."
        )
    if not result["proposals"]:
        lines.append("Nothing in this window states a cause.")
    else:
        lines.append("Setting:" if write else "Would set:")
        for p in result["proposals"]:
            c = facts.get(p.edge_id)
            where = f"{c.source} -> {c.target}" if c else "?"
            lines.append(f"  {p.hint:<12} {p.edge_id}  {where}")
            if c:
                lines.append(f"  {'':<12} {c.fact}")
            lines.append(f"  {'':<12} states it: {p.quote!r}")
    if remaining:
        lines.append(f"{remaining} fact(s) in this scope still unexamined; re-run to continue.")
    return "\n".join(lines) + "\n"


def render_pending(scope: str, proposals: list[Proposal]) -> str:
    if not proposals:
        return ""
    return (
        f"{len(proposals)} proposal(s) already recorded for {scope} and not yet "
        f"applied.\n"
    )


def render_applied(result: dict) -> str:
    if not result["applied"] and not result["skipped"]:
        return "Nothing to apply.\n"
    lines = [f"Applied {len(result['applied'])} hint(s), each audited."]
    if result["skipped"]:
        lines.append(
            f"{len(result['skipped'])} skipped: a hint was written by a caller "
            f"after this was proposed, and a caller's hint is never overwritten."
        )
    return "\n".join(lines) + "\n"


def render_dry_run() -> str:
    return (
        "Nothing was written. The proposals above are recorded, so --write "
        "applies them without asking the model again.\n"
    )


def render_clear(scope: str, cleared: list[tuple[str, str]], dry_run: bool) -> str:
    if not cleared:
        return f"No hints extracted by {SESSION_ID} in {scope}.\n"
    verb = "Would clear" if dry_run else "Cleared"
    lines = [f"{verb} {len(cleared)} hint(s) in {scope}:"]
    lines += [f"  {hint:<12} {edge_id}" for edge_id, hint in cleared]
    if dry_run:
        lines.append("Nothing was written. Re-run with --write to clear them.")
    return "\n".join(lines) + "\n"


def run(args, config, conn, extractor=None) -> int:
    """One scope, one window, dry run unless told otherwise.

    `extractor` is injectable so the whole command can be exercised without a
    provider. The suite has to stay offline and deterministic, and a test that
    called a real model would measure that model rather than this code.
    """
    group_id = config.group_id(args.scope)
    scope, write = args.scope, bool(args.write)

    if args.clear:
        if not write:
            return _print(render_clear(scope, applied_hints(conn, group_id), dry_run=True))
        return _print(render_clear(scope, clear(conn, group_id)["cleared"], dry_run=False))

    if args.rescan:
        forgotten = rescan(conn, group_id)
        print(f"{scope}: forgot {forgotten} recorded verdict(s); the text will be read again.")

    waiting = pending(conn, group_id)
    left = unexamined(conn, group_id)
    scanned = None

    if left:
        if extractor is None:
            try:
                extractor = ModelExtractor(provider_from_env())
            except ModelUnavailable as e:
                # Not necessarily an error. Applying proposals an earlier run
                # already paid for needs no provider at all, and refusing to do
                # it because a key is absent would strand reviewed work.
                if not waiting:
                    print(f"error: {e}")
                    return 1
                print(f"{e}\n")
        if extractor is not None:
            try:
                scanned = scan(conn, group_id, extractor, args.limit, args.batch_size)
            except (ModelUnavailable, InferenceError) as e:
                # Whatever earlier batches reached is recorded, which is why
                # verdicts are written per batch rather than at the end.
                print(f"error: {e}")
                print("Verdicts reached before this are recorded; re-run to continue.")
                return 1
            print(
                render_scan(scope, scanned, unexamined(conn, group_id), write),
                end="",
            )

    if scanned is None and not waiting:
        print(f"{scope}: nothing left to examine, and nothing waiting to apply.")
        return 0

    if not write:
        print(render_pending(scope, waiting), end="")
        return _print(render_dry_run())

    return _print(render_applied(apply(conn, group_id, pending(conn, group_id))))


def _print(text: str) -> int:
    print(text, end="")
    return 0
