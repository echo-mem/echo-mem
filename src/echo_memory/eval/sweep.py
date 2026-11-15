"""What a recall costs as the store grows, measured rather than projected.

`eval --context` reports one number: on this store, today, a recall returned
this fraction of what injecting everything would have cost. That number is a
property of one corpus size, and read alone it invites the wrong conclusion in
both directions. Someone with a small store sees a saving smaller than the
headline and assumes the headline was inflated. Someone with a large one has no
way to tell whether the saving keeps rising or flattens out.

The shape is the claim, so the shape is what this measures. Retrieval is run
against real sub-scopes built from a prefix of the store's own facts, at
several sizes, so the numerator is measured at each size instead of assumed
constant. That matters: top_k bounds how many facts come back, but not how long
they are, and the adaptive floor can return fewer than top_k when nothing
scores well. Both are reasons the recall cost could drift with corpus size, and
neither is something to wave away in a footnote.

The cost of honesty here is wall clock. Building the sub-scopes rewrites about
twice the store's fact count through the normal write path, embeddings and
entity resolution included, because a scope assembled any other way would not
be the thing retrieval runs against in production.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from echo_memory.eval.retrieval import (
    CHARS_PER_TOKEN,
    SHAPE_ENTITY_SINGLE,
    build_cases,
    corpus_tokens,
    run,
)
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.ingestion.write_episode import write_episode

# Halving down from the full store rather than fixed absolute sizes: the
# question is how the saving behaves across THIS store's range, and a ladder of
# constants would either stop short of a large store or run past a small one.
STEPS = 5
MIN_FACTS = 24


@dataclass
class Point:
    facts: int
    inject_tokens: int
    recall_tokens: int
    hit_at_10: float
    cases: int

    @property
    def saving(self) -> float:
        if not self.inject_tokens:
            return 0.0
        return 1 - self.recall_tokens / self.inject_tokens


def sizes_for(total: int, steps: int = STEPS) -> list[int]:
    """Halve down from the whole store, stopping where a scope is too small to
    say anything. Ascending, so the table reads in the direction it grows."""
    out: list[int] = []
    size = total
    for _ in range(steps):
        if size < MIN_FACTS:
            break
        out.append(size)
        size //= 2
    return sorted(set(out))


def read_corpus(conn, group_id: str) -> list[tuple[str, str, str, str]]:
    """Every live fact as (source, target, relation_type, fact), oldest first.

    Oldest first is what makes a prefix meaningful: it is the store as it
    actually was at that size, not a random sample of the store as it is now.
    """
    rows = conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (a:Node)-[e:FACT]->(b:Node)
            WHERE e.group_id = $gid AND e.t_invalid IS NULL
            RETURN a.name, b.name, e.relation_type, e.fact, e.t_valid
        $$, %s) AS (a agtype, b agtype, rel agtype, fact agtype, t agtype)""",
        (json.dumps({"gid": group_id}),),
    ).fetchall()
    unpacked = [
        (
            str(a).strip('"'),
            str(b).strip('"'),
            str(rel).strip('"'),
            str(fact).strip('"'),
            int(str(t)) if str(t).isdigit() else 0,
        )
        for a, b, rel, fact, t in rows
    ]
    unpacked.sort(key=lambda r: r[4])
    return [(a, b, rel, fact) for a, b, rel, fact, _ in unpacked]


def materialise(conn, scope: str, corpus, embedder) -> None:
    """Write a prefix of the corpus into a scratch scope through write_episode.

    Deliberately the ordinary write path. A scope built by copying edge rows
    would skip entity resolution, and resolution is what decides how many
    distinct nodes the retrieval then has to rank against.

    Both endpoints are asserted as new, and that is what makes the copy
    faithful rather than what makes it cheat. These names already coexist as
    distinct entities in the source scope, so re-deriving that from embedding
    similarity can only lose facts: a pair that lands between the low and high
    thresholds is reported ambiguous and every fact touching it is DEFERRED,
    not written. Left to resolve itself, a 680 fact prefix silently became 336.
    An exact name match still overrides the assertion, so the second mention of
    a name resolves onto the first one's node exactly as it did originally.
    """
    for i, (source, target, relation_type, fact) in enumerate(corpus):
        write_episode(
            conn, scope, f"sweep-{i}",
            [{"name": source, "type": "entity"}, {"name": target, "type": "entity"}],
            [{"source": source, "target": target, "relation_type": relation_type,
              "fact": fact, "confidence": "extracted"}],
            {source: {"resolved_to": "new"}, target: {"resolved_to": "new"}},
            embedder,
            project="sweep",
            agent_id="sweep",
        )


#: `eval_judgement` and `eval_run` hold rows for a scope without carrying a
#: `group_id` of their own: they hang off `eval_question`. They go first, by
#: subquery, because the foreign key points the other way.
SCOPE_CHILD_DELETES = (
    ("eval_judgement", "question_id"),
    ("eval_run", "question_id"),
)

#: Every table that holds a scope's rows, keyed on `group_id`, children before
#: parents for the three foreign keys among them: `fact_evidence` -> `evidence`
#: and `trial_observation` -> `audit_entry`, with `eval_question` after the two
#: above.
#:
#: Listed rather than discovered, and then checked against the catalogue by
#: `test_drop_scope_covers_every_scoped_table`, which fails when a migration
#: adds a table this does not name. The list drifted once already: it held five
#: tables while the schema had nineteen, so a dropped benchmark scope left
#: behind its attestation chain, its BM25 statistics and its inbox. 634
#: orphaned `fact_attestation` rows were found that way, and `report` would
#: have called every one of them a vanished fact with no erasure to explain it.
SCOPE_TABLES = (
    "fact_evidence",
    "trial_observation",
    "eval_question",
    "evidence",
    "audit_entry",
    "bundle_registry",
    "causal_hint_scan",
    "erasure_event",
    "event_inbox",
    "fact_attestation",
    "fact_embedding",
    "fact_verification",
    "group_state",
    "lexical_scope",
    "lexical_term",
    "node_embedding",
    "read_event",
    "retention_policy",
    "verification_attestation",
)


def drop_scope(conn, scope: str) -> None:
    """A scratch scope that outlived the measurement would show up in status,
    health and every future eval as though someone had written it.

    Dropped outright rather than redacted, which is what separates this from
    `erasure`: there is no audit trail worth preserving for a scope the harness
    invented, so everything keyed on it goes.
    """
    for table, column in SCOPE_CHILD_DELETES:
        conn.execute(
            f"DELETE FROM public.{table} WHERE {column} IN "
            f"(SELECT id FROM public.eval_question WHERE group_id = %s)",
            (scope,),
        )
    # The graph before the tables that describe it, so a failure part way
    # through cannot leave rows pointing at nodes that are already gone.
    conn.execute(
        f"""SELECT * FROM cypher('{GRAPH}', $$
            MATCH (n:Node) WHERE n.group_id = $gid
            DETACH DELETE n RETURN 1
        $$, %s) AS (x agtype)""",
        (json.dumps({"gid": scope}),),
    ).fetchall()
    for table in SCOPE_TABLES:
        conn.execute(f"DELETE FROM public.{table} WHERE group_id = %s", (scope,))


def measure(conn, group_id: str, embedder, steps: int = STEPS, progress=None) -> list[Point]:
    corpus = read_corpus(conn, group_id)
    points: list[Point] = []
    run_id = uuid.uuid4().hex[:8]
    for size in sizes_for(len(corpus), steps):
        scope = f"sweep:{run_id}:{size}"
        if progress:
            progress(size, len(corpus))
        try:
            materialise(conn, scope, corpus[:size], embedder)
            # entity_single, not the default pair shape: the pair shape puts
            # both entity names in the query, and both names are embedded in
            # the fact it is meant to find. A leaky shape would make the hit
            # rate rise with corpus size for reasons that have nothing to do
            # with corpus size.
            cases = build_cases(conn, scope, None, shape=SHAPE_ENTITY_SINGLE)
            if not cases:
                continue
            result = run(conn, scope, embedder, cases, "shipping")
            facts, inject = corpus_tokens(conn, scope)
            points.append(
                Point(
                    facts=facts,
                    inject_tokens=inject,
                    recall_tokens=result.mean_tokens,
                    hit_at_10=result.recall(10),
                    cases=result.cases,
                )
            )
        finally:
            drop_scope(conn, scope)
    return points


def render(points: list[Point]) -> str:
    if len(points) < 2:
        return (
            "not enough corpus to sweep: this scope needs at least "
            f"{MIN_FACTS * 2} live facts for two points, and a single point is "
            "the number `--context` already prints."
        )

    lines = [
        "",
        f"  {'facts':>7}{'inject':>10}{'recall':>9}{'hit@10':>9}{'saving':>9}",
        "  " + "-" * 44,
    ]
    for p in points:
        lines.append(
            f"  {p.facts:>7,}{p.inject_tokens:>10,}{p.recall_tokens:>9,}"
            f"{p.hit_at_10:>9.3f}{p.saving:>9.1%}"
        )

    first, last = points[0], points[-1]
    growth = last.facts / first.facts if first.facts else 0
    drift = (
        (last.recall_tokens - first.recall_tokens) / first.recall_tokens
        if first.recall_tokens else 0.0
    )
    lines += [
        "  " + "-" * 44,
        "",
        (
            f"Across {growth:.0f}x of corpus growth the cost of injecting "
            f"everything rose {last.inject_tokens / first.inject_tokens:.0f}x, "
            f"while what a recall returned moved {drift:+.0%}. That is the claim: "
            "the saving is not a constant, it is what happens when a bounded "
            "number grows against an unbounded one."
        ),
        "",
        (
            f"A token is {CHARS_PER_TOKEN} characters. hit@10 is printed in every "
            "row because a saving is only worth having if the answer is still in "
            "what came back, and a scope that returned nothing would score 100%."
        ),
    ]
    return "\n".join(lines)
