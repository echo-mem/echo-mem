"""Which scopes are worth asking, decided without a model call.

`query_across` fans out over up to eight scopes and each one is a full
`query_memory`: two indexed candidate queries, an HNSW probe and a fusion. The
cost is linear in the number of scopes, so an account with eight memories pays
eight times over for an answer that lives in one of them. That is the whole
problem this module exists for, and `across.py`'s own MAX_GROUPS comment says
where to look: "a customer who genuinely needs more should be routing".

## The technique is already in this codebase and already measured

`needs_expansion` in query_memory.py is a query classifier with no model in it.
It tokenises the query with `prompt_terms`, finds nodes whose name contains one
of those terms, and asks whether an edge already joins them. It decides whether
to run a graph hop, and the eval endorsed the decision it makes: routed
expansion cut the always-on hop's -0.142 MRR damage on entity_pair by about
80%.

This generalises exactly that one step. Tokenise the query, find the nodes in
each scope whose name contains a term, and count the live facts hanging off
them. A scope that names none of the query's terms, or names them and holds no
live fact touching them, almost certainly does not hold the answer.

One indexed query per scope, and nothing embeds the query per scope. Per-scope
embedding is the mistake worth naming because it is the obvious next idea: a
vector probe per scope IS a query_memory, and if a scope is worth probing it is
worth keeping the answer from. Routing has to be cheaper than the thing it is
deciding about or it is not routing.

## What this cannot see

Lexical containment over node names cannot find the scope whose relevant fact
is phrased in words the query does not use. `query_memory` has a vector channel
for precisely that case and this scorer has nothing, so a scope rejected here
can genuinely hold a semantic match.

That failure direction is data loss, and it is silent, so two things lean
against it. Containment rather than exact or near match, so a scope errs toward
being kept: `_about_edge_ids` refuses substring matching for the opposite
reason, that it must not admit the wrong entity, and here admitting a scope
that turns out to hold nothing costs one query while rejecting one that held
the answer costs the answer. And `select` never drops a scope the caller named
as its default, however it scored.

## Nothing here is tuned

The score's shape below is a proposal. `fusion.py` states the rule this module
inherits: nothing but the eval decides a weight, and the two previous attempts
to reason a retrieval constant into place in this project both lost MRR. Every
constant in this file is marked with what it is a guess about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.retrieval.query_memory import ValidationError, prompt_terms

#: How many name-matching nodes one scope's fact count is measured over.
#:
#: `needs_expansion` stops at eight because it only needs to know whether two
#: exist. Here the count is the signal rather than a yes or no, so the cap has
#: to leave a scope that is genuinely about the query distinguishable from one
#: that mentions it once. 32 is a guess at where that stops mattering, and it
#: bounds what the fact count can cost; the number it caps only ever breaks
#: ties between scopes that matched the same share of terms.
#:
#: It caps NODES and deliberately not the term count, which is measured over
#: the whole scope. A cap on the term count would understate coverage, and
#: understated coverage rejects a scope that held the answer.
MAX_MATCHED_NODES = 32

#: Live facts at which the depth term is half its maximum.
#:
#: Depth saturates rather than scaling, because otherwise the largest scope
#: wins every question regardless of what it is about, which is the opposite of
#: routing. Ten is a guess. Nothing has measured where a scope stops being
#: better for holding more facts about the query's subject.
DEPTH_HALF_FACTS = 10

#: How much of a scope's score depth is allowed to move.
#:
#: Term coverage leads and depth breaks ties, because coverage answers "is this
#: scope about the question" and depth only answers "how much does it say". A
#: guess, and the sensitivity worth measuring first: at 0.0 this is pure
#: coverage and at 1.0 the biggest scope wins.
DEPTH_WEIGHT = 0.25

#: The smallest weight `weights_for` will propose for a scope that was asked.
#:
#: Not zero. A scope kept in the fan out and then given weight zero contributes
#: nothing to the fusion, so the query was paid for and discarded, and the
#: scope most likely to land there is the default one `select` keeps on purpose.
#:
#: The only weight this codebase has ever measured is `expansion_weight`, where
#: 0.2 was the point at which a secondary ranked list stopped costing MRR
#: (BENCHMARKS.md, 2026-11-07). That is a different list in a different
#: fusion and does not transfer; it is only why the floor sits near there
#: rather than being picked out of the air.
MIN_WEIGHT = 0.1

#: Characters per token.
#:
#: The same 4 that `eval/retrieval.py` uses for every token figure in
#: BENCHMARKS.md. Duplicated rather than imported because retrieval must not
#: depend on the eval package, and the two must not drift: a budget measured
#: against a different constant is not comparable with any number already
#: published.
CHARS_PER_TOKEN = 4

_logger = get_logger("routing")

_NODE_GROUP = """(n.properties ->> '"group_id"'::agtype)"""
_EDGE_GROUP = """(e.properties ->> '"group_id"'::agtype)"""


@dataclass(frozen=True)
class ScopeScore:
    """What one scope looks like to the scorer, and why.

    `reason` is carried rather than derived at the call site so that the
    sentence explaining a rejection is written once, next to the arithmetic
    that produced it. A routing decision a customer cannot read is a routing
    decision they have to take on trust, and the thing being trusted is a
    heuristic with no eval behind it yet.
    """

    group_id: str
    matched_terms: int
    candidate_facts: int
    score: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "matched_terms": self.matched_terms,
            "candidate_facts": self.candidate_facts,
            "score": round(self.score, 4),
            "reason": self.reason,
        }


def _score(matched: int, n_terms: int, facts: int) -> float:
    """Coverage of the query's terms, discounted by how little the scope holds.

    Zero when the scope names none of the terms, and zero when it names them
    but holds no live fact touching them. The second case is reachable rather
    than a tidy edge: `write_episode` writes an episode's new entities and then
    returns `not_stored: no_facts` when it asserted none, so a scope can answer
    to the word and have nothing current to say about it.
    """
    if not n_terms or not matched or not facts:
        return 0.0
    coverage = matched / n_terms
    depth = facts / (facts + DEPTH_HALF_FACTS)
    return coverage * ((1.0 - DEPTH_WEIGHT) + DEPTH_WEIGHT * depth)


def _reason(matched: int, terms: list[str], facts: int) -> str:
    shown = ", ".join(terms[:4]) + ("..." if len(terms) > 4 else "")
    if not terms:
        return "the query has no searchable terms, so nothing here can be scored"
    if not matched:
        return f"names none of the {len(terms)} query terms ({shown})"
    if not facts:
        return (
            f"names {matched} of {len(terms)} query terms but holds no live "
            f"fact touching them"
        )
    return f"names {matched} of {len(terms)} query terms over {facts} live facts"


def score_scopes(
    conn, group_ids: list[str], query: str, *, limit: int | None = None
) -> list[ScopeScore]:
    """How well each scope's entity names answer to the query's words.

    One indexed query per scope, best score first. `limit` truncates the
    returned list to that many scopes and exists for a caller that wants the
    scores for display rather than for a decision; it does not reduce the
    number of queries, because the scores being compared have to all exist
    before any of them can be called the best.

    A query with no searchable terms costs no queries at all and scores every
    scope zero. That is not "ask nothing": `choose` will keep nothing, and the
    caller decides what an unrouteable query means. `assemble` treats it as a
    reason to fall back rather than to return empty.
    """
    if not isinstance(group_ids, (list, tuple)) or not group_ids:
        raise ValidationError("score_scopes needs at least one group_id")
    terms = prompt_terms(query or "")
    if not terms:
        return [
            ScopeScore(gid, 0, 0, 0.0, _reason(0, [], 0)) for gid in group_ids
        ]

    start = time.perf_counter()
    scores: list[ScopeScore] = []
    for gid in group_ids:
        row = conn.execute(
            f"""
            WITH named AS (
                -- Containment, not equality, and the asymmetry is deliberate:
                -- see the module docstring. node_group_idx covers the scope
                -- filter, and the cap stops a scope that names everything from
                -- handing an unbounded id list to the count below.
                SELECT n.id
                  FROM {GRAPH}."Node" n
                 WHERE {_NODE_GROUP} = %(gid)s
                   AND EXISTS (
                       SELECT 1 FROM unnest(%(terms)s::text[]) AS w
                        WHERE position(
                            w IN lower(n.properties ->> '"name"'::agtype)
                        ) > 0
                   )
                 LIMIT %(cap)s
            )
            SELECT
              -- Counted per term rather than over the capped node set, and the
              -- EXISTS is what makes that affordable: it stops at the first
              -- node that answers to the term, so this is at most MAX_TERMS
              -- early-exiting index scans.
              --
              -- An earlier version counted distinct terms inside the capped
              -- join, where the cap applied to (node, term) PAIRS. One node
              -- matching five terms spent five of them, so an uncapped
              -- arbitrary 32 rows could miss a term entirely and understate
              -- coverage, and understated coverage rejects a scope that held
              -- the answer.
              (SELECT count(*) FROM unnest(%(terms)s::text[]) AS w
                WHERE EXISTS (
                    SELECT 1 FROM {GRAPH}."Node" n
                     WHERE {_NODE_GROUP} = %(gid)s
                       AND position(
                           w IN lower(n.properties ->> '"name"'::agtype)
                       ) > 0
                )),
              (SELECT count(*)
                 FROM {GRAPH}."FACT" e
                WHERE {_EDGE_GROUP} = %(gid)s
                  AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
                  AND (e.start_id IN (SELECT id FROM named)
                    OR e.end_id IN (SELECT id FROM named)))
            """,
            {"gid": gid, "terms": terms, "cap": MAX_MATCHED_NODES},
        ).fetchone()
        matched, facts = (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
        scores.append(
            ScopeScore(
                group_id=gid,
                matched_terms=matched,
                candidate_facts=facts,
                score=_score(matched, len(terms), facts),
                reason=_reason(matched, terms, facts),
            )
        )

    _logger.info(
        "score_scopes",
        extra={"fields": {
            "n_scopes": len(group_ids),
            "n_terms": len(terms),
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        }},
    )
    ranked = sorted(scores, key=lambda s: (-s.score, s.group_id))
    return ranked[:limit] if limit else ranked


def choose(
    scores: list[ScopeScore],
    *,
    max_scopes: int = 3,
    min_score: float = 0.0,
    default: str | None = None,
) -> tuple[list[str], list[dict]]:
    """The selection, over scores already in hand.

    Pure, and public, because `assemble` scores the scopes once and then needs
    both the decision and the weights from the same numbers. Going through
    `select` there would run the per-scope query twice.

    `min_score` is an exclusive floor, so the 0.0 default rejects a scope that
    matched nothing rather than keeping it. May return no scopes at all, which
    says "nothing here names the question" and is the honest answer to an
    unrouteable query; it is the caller that decides whether to abstain or to
    fall back, and `assemble` falls back loudly.
    """
    if not isinstance(max_scopes, int) or isinstance(max_scopes, bool) or max_scopes < 1:
        raise ValidationError(f"max_scopes must be a positive integer, got {max_scopes!r}")
    known = {s.group_id for s in scores}
    if default is not None and default not in known:
        # Refused rather than appended. `group_ids` is already the set the
        # caller is authorised for (see query_across), so honouring a default
        # from outside it would let routing widen authorisation, which is the
        # one thing in this pipeline that must only ever narrow.
        raise ValidationError(
            f"default={default!r} is not among the scopes being scored "
            f"({sorted(known)}). A default outside the authorised scopes would "
            f"make routing widen what the caller may read."
        )

    keep: set[str] = {default} if default is not None else set()
    cut_rank: dict[str, int] = {}
    for position, s in enumerate(scores, start=1):
        if s.group_id in keep:
            continue
        if len(keep) >= max_scopes:
            cut_rank[s.group_id] = position
            continue
        if s.score > min_score:
            keep.add(s.group_id)

    chosen = [s.group_id for s in scores if s.group_id in keep]
    rejected = []
    for position, s in enumerate(scores, start=1):
        if s.group_id in keep:
            continue
        if s.group_id in cut_rank:
            why = (
                f"{s.reason}; scored {s.score:.3f} but ranked "
                f"{cut_rank[s.group_id]} of {len(scores)} and max_scopes is "
                f"{max_scopes}"
            )
        else:
            why = f"{s.reason}; score {s.score:.3f} is not above the {min_score:.3f} floor"
        rejected.append({"group_id": s.group_id, "reason": why})
    return chosen, rejected


def select(
    conn,
    group_ids: list[str],
    query: str,
    *,
    max_scopes: int = 3,
    min_score: float = 0.0,
    default: str | None = None,
) -> tuple[list[str], list[dict]]:
    """The scopes worth asking, and why the others were not.

    `default` is the scope the caller configured, and it is kept whatever it
    scored. Routing that can exclude the scope a caller explicitly named is
    routing that loses data, and the loss is silent: the answer comes back
    shaped exactly like a correct one, from a scope the caller never meant to
    be optional.
    """
    scores = score_scopes(conn, list(group_ids), query)
    return choose(scores, max_scopes=max_scopes, min_score=min_score, default=default)


def weights_for(scores: list[ScopeScore]) -> dict[str, float]:
    """RRF weights proposed from the scores, best scope at 1.0.

    **These are proposals for the eval to score, not tuned values.** `fusion.py`
    states the rule: a weight asks what one list's top slot is worth against
    another's, and nothing but the eval decides that number. The two previous
    attempts in this project to settle such a number by argument rather than by
    measurement cost -0.142 MRR and then roughly a fifth of that.

    So what this returns is a shape to measure, and the shape is the claim: a
    scope that answers to twice as much of the query is worth twice as much in
    the fusion. That claim is untested.

    Every scope floors at MIN_WEIGHT rather than zero, because a scope that was
    asked and then weighted out of existence cost a full query_memory for
    nothing. All ones when nothing scored, which is plain RRF and what every
    caller got before this existed.
    """
    best = max((s.score for s in scores), default=0.0)
    if best <= 0.0:
        return {s.group_id: 1.0 for s in scores}
    return {s.group_id: max(MIN_WEIGHT, s.score / best) for s in scores}


def estimate_tokens(facts: list[dict]) -> int:
    """Roughly what these facts cost to put in a prompt.

    Characters over four, which is the accepted approximation for English prose
    and is the same constant `eval/retrieval.py` computes every token figure in
    BENCHMARKS.md with, so a budget here is comparable with those.

    **It is an approximation, and it is wrong in one direction.** It undercounts
    code and identifiers, where a BPE tokeniser splits `fact_embedding_hnsw_idx`
    into pieces far shorter than four characters, and it undercounts non-English
    badly: CJK and Devanagari often run near one token per character, so this
    can be off by a factor of three or four on a store that is not in English.
    It also counts the fact text alone, not the subject name `scratchpad.render`
    prepends to each line, nor any JSON framing, so the prompt a caller actually
    sends costs more than this says.

    Undercounting means a budget can be exceeded in real tokens while this
    function says it was honoured. It is kept anyway because the alternative is
    loading a tokeniser on the read path, and the budget's job is to compare two
    configurations at the same spend, which it does as long as both are measured
    the same way.
    """
    return sum(len(f.get("fact") or "") for f in facts) // CHARS_PER_TOKEN


def fit_to_budget(facts: list[dict], budget: int | None) -> tuple[list[dict], dict]:
    """The ranked list cut to the budget, and what the cut cost.

    **Truncated from the tail, never from the middle.** The list arrives ranked,
    so a gap in it is a reordering the caller did not ask for: dropping a long
    fact at rank 3 to fit a short one at rank 4 silently promotes the short one.
    A fact that does not fit therefore ends the list, and whatever followed it
    goes too.

    A budget of 0 or None means no budget. The shape does not change in that
    case: the report is still there and still says what the answer cost, because
    a caller that reads `estimated_tokens` should not have to know whether a
    budget was set to find out.

    The report is the only thing that distinguishes an empty answer from an
    exhausted budget, which is a real case: a budget smaller than the top fact
    returns nothing, and nothing looks exactly like "the store holds no answer".
    """
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
        raise ValidationError(f"budget must be an integer number of tokens, got {budget!r}")
    if budget is not None and budget < 0:
        raise ValidationError(f"budget must not be negative, got {budget}")

    kept = list(facts)
    if not budget:
        return kept, {
            "budget": budget,
            "estimated_tokens": estimate_tokens(kept),
            "dropped": 0,
        }

    spent = 0
    kept = []
    for i, fact in enumerate(facts):
        cost = estimate_tokens([fact])
        if spent + cost > budget:
            kept = list(facts[:i])
            break
        spent += cost
    else:
        kept = list(facts)
    return kept, {
        "budget": budget,
        "estimated_tokens": estimate_tokens(kept),
        "dropped": len(facts) - len(kept),
    }
