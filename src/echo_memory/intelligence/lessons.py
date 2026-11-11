"""Conclusions derived from outcomes, at read time, and never stored.

A lesson is "deciding X tends to go badly". That is a claim about a pattern
across several observations, and this module refuses to write one down. The
refusal is the design, for three reasons that each cost something real:

**A stored conclusion outlives its evidence.** Supersede one of the outcomes a
lesson rests on and the outcome correctly becomes history; the lesson, if it
were a fact, would sit there unchanged, asserting a pattern over data that no
longer says it. Derived here, it simply stops being produced - weakened by the
supersession rather than contradicted by it, with nothing deleted and nothing
to reconcile.

**A stored conclusion is indistinguishable from an observation.** Every fact
in this store is believed because somebody wrote it, and `confidence` says how
the writer came by it, not whether it is true. A lesson written as a FACT edge
would be retrieved, ranked, embedded and quoted exactly like a deploy log,
with no marking that says a machine generalised it. `kind="inferred"` on a row
that only ever exists in a response cannot be laundered that way.

**A stored conclusion compounds.** Lessons derived from lessons is how a
memory fills with confident claims nobody made. Reading from the graph each
time keeps the inputs to every conclusion observations.

So: this module imports nothing from the write path, writes no FACT edge, and
there is a test that reads its source text and asserts as much. The constraint
is worth a test rather than a comment because the pressure to relax it is
entirely reasonable-sounding - deriving on every read looks wasteful, caching
the answer looks obvious, and a cache of conclusions in the graph is the thing
this module exists not to be.

## The bar a lesson has to clear

    support     >= MIN_SUPPORT (2) agreeing outcomes. One is an anecdote, and
                the store already returns it: asking about the decision finds
                the outcome. A lesson claims a pattern, and two points is the
                smallest number that can show one.
    agreement   >= REQUIRED_AGREEMENT (1.0), so any outcome pointing the other
                way kills it. A decision whose outcomes disagree is exactly the
                case a reader should see as disputed rather than resolved.
    trust       every supporting outcome must be `verified` or
                `evidence_backed`. An unverified outcome is a claim nobody
                checked, and a pattern across unchecked claims is a pattern in
                what got reported.

`mixed` and `unknown` outcomes are excluded from both counts on purpose, via
SIGN_VALUE. They are evidence that the decision was taken and say nothing
about how it went, so a decision with ten mixed outcomes yields no lesson
rather than a weak one.
"""

from __future__ import annotations

import importlib
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.logging import get_logger
from echo_memory.intelligence import vocabulary
from echo_memory.intelligence.naming import outcome_subject, sign_of_node_name
from echo_memory.trust import states

_logger = get_logger("lessons")

#: The trust states an outcome must be in to support a lesson. `unverified` is
#: absent, which is the whole filter: a pattern across claims nobody checked is
#: a pattern in what got reported, not in what happened.
SUPPORTING_STATES = frozenset({states.VERIFIED, states.EVIDENCE_BACKED})

#: The hint that makes a decision-to-outcome edge part of a chain. One value,
#: not the whole causal vocabulary: `led_to` is what the outcome path writes
#: and it is in CAUSE_IS_SOURCE, so the decision is the cause end. Walking
#: `contradicts` or `blocked_by` here would count edges that say the opposite
#: of what a lesson is about.
LED_TO = "led_to"


def _decisions(conn, group_id: str) -> list[tuple[str, str]]:
    """Every decision node in this scope, as (node_id, name).

    Migration 0034's partial index on (group_id) WHERE type = 'decision' is
    what makes this cheap. Without it this is a sequential scan of every node
    the scope has ever held, and the node table grows with the customer.
    """
    rows = conn.execute(
        f"""SELECT id::text, (properties ->> '"name"'::agtype)
              FROM {GRAPH}."Node"
             WHERE (properties ->> '"group_id"'::agtype) = %s
               AND (properties ->> '"type"'::agtype) = %s
             ORDER BY id""",
        (group_id, vocabulary.DECISION),
    ).fetchall()
    return [(str(node_id), name) for node_id, name in rows]


def _observations(conn, group_id: str, decision_ids: list[str]) -> list[dict]:
    """The live outcome edges hanging off these decisions.

    `t_invalid IS NULL` is the load-bearing predicate and the reason a
    supersession weakens a lesson instead of contradicting one. An outcome that
    turned out wrong gets an end to its validity rather than a delete, so it
    stays readable as history and stops counting here, in the same statement.

    The sign comes off the END node's name rather than out of the fact text.
    See naming.py: the name is where the sign structurally lives, because node
    names are unique per scope and edge identity is (source, target,
    relation_type) - so a success and a failure of the same named thing have to
    be two nodes or they are one edge, and the later report would retire the
    earlier one.
    """
    if not decision_ids:
        return []
    rows = conn.execute(
        f"""SELECT e.id::text,
                   e.start_id::text,
                   n.id::text,
                   (n.properties ->> '"name"'::agtype),
                   (e.properties ->> '"fact"'::agtype)
              FROM {GRAPH}."FACT" e
              JOIN {GRAPH}."Node" n ON n.id = e.end_id
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
               AND (e.properties ->> '"causal_hint"'::agtype) = %s
               AND (n.properties ->> '"type"'::agtype) = %s
               AND e.start_id::text = ANY(%s)
             ORDER BY e.id""",
        (group_id, LED_TO, vocabulary.OUTCOME, list(decision_ids)),
    ).fetchall()
    return [
        {
            "fact_id": str(edge_id),
            "decision_node_id": str(start_id),
            "outcome_node_id": str(node_id),
            "outcome": outcome_subject(node_name),
            "sign": sign_of_node_name(node_name),
            "fact": fact,
        }
        for edge_id, start_id, node_id, node_name, fact in rows
    ]


def _trust_states(conn, group_id: str, fact_ids: list[str]) -> dict[str, str]:
    """The trust state of each outcome edge, or nothing at all.

    `trust.verify` is imported here rather than at module scope because the
    trust and learning halves of this land separately, and a module-level
    import would make this file unimportable on a build that has one and not
    the other.

    When it is absent every fact is treated as `unverified`, which produces NO
    lessons. That is the safe direction and it is worth being explicit about:
    the alternative default - treat the unknown as verified - would mean a
    build missing its verification layer confidently generalising from
    unchecked reports, and nothing in the output would say which build it came
    from.

    `trust.states` IS imported at module scope, because that is the state
    vocabulary rather than the implementation, and the two have different
    lifetimes: the words were agreed before anything used them.
    """
    if not fact_ids:
        return {}
    try:
        verify = importlib.import_module("echo_memory.trust.verify")
    except ImportError:
        _logger.debug(
            "trust_verify_absent",
            extra={"group_id": group_id, "detail": "every fact read as unverified"},
        )
        return {}
    batch = getattr(verify, "state_of_many", None)
    if batch is not None:
        return {str(k): v for k, v in batch(conn, group_id, list(fact_ids)).items()}
    return {str(f): verify.state_of(conn, group_id, f) for f in fact_ids}


def candidates(
    conn,
    group_id: str,
    *,
    min_support: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The lessons this scope's outcomes currently support.

    One row per decision that clears the bar, carrying the supporting
    observations it was derived from. Every row is `kind="inferred"`, whatever
    its basis, and that string is what keeps a derived conclusion out of the
    verified vocabulary no matter how strong the support behind it.

    `basis` is `verified_causal` only when EVERY supporting outcome is
    `verified` - an authorised verifier asserted it, naming a method. One
    merely `evidence_backed` outcome in the set makes the whole row
    `correlational`, because evidence is not proof: a deployment log saying the
    deploy happened does not establish that the decision behind it was right.
    Promoting on a majority would mean the strongest label in this system could
    be reached without anybody having verified anything.

    `now` is recorded on each row as `as_of` rather than used in the
    arithmetic. There is no recency weighting here and that is deliberate: a
    decay constant fitted to the handful of outcomes any scope currently holds
    would be this codebase repeating a mistake it has already paid for. What
    the timestamp is for is that a lesson is a snapshot - the same query
    tomorrow can legitimately return less - and a caller holding a row needs to
    know which moment it described.
    """
    min_support = vocabulary.MIN_SUPPORT if min_support is None else min_support
    as_of = now or datetime.now(UTC)

    decisions = _decisions(conn, group_id)
    if not decisions:
        return []
    names = dict(decisions)
    observations = _observations(conn, group_id, [node_id for node_id, _ in decisions])
    if not observations:
        return []

    trust = _trust_states(conn, group_id, [o["fact_id"] for o in observations])

    by_decision: dict[str, list[dict]] = defaultdict(list)
    for observation in observations:
        state = trust.get(observation["fact_id"], states.UNVERIFIED)
        if state not in SUPPORTING_STATES:
            continue
        # Signs scoring zero - mixed, unknown - are dropped before any counting
        # so they cannot dilute agreement either way. See SIGN_VALUE.
        if vocabulary.SIGN_VALUE.get(observation["sign"], 0) == 0:
            continue
        by_decision[observation["decision_node_id"]].append({**observation, "trust": state})

    rows: list[dict[str, Any]] = []
    for node_id, supported in by_decision.items():
        counts: dict[str, int] = defaultdict(int)
        for observation in supported:
            counts[observation["sign"]] += 1
        # Sorted rather than Counter.most_common, so a tie resolves on the sign
        # name instead of on dict insertion order. A tie cannot clear
        # REQUIRED_AGREEMENT at 1.0 anyway; this is so that the row a future
        # lower threshold would produce is reproducible.
        sign, support = min(counts.items(), key=lambda item: (-item[1], item[0]))
        agreement = support / len(supported)
        if support < min_support or agreement < vocabulary.REQUIRED_AGREEMENT:
            continue
        supporters = [o for o in supported if o["sign"] == sign]
        basis = (
            vocabulary.BASIS_VERIFIED_CAUSAL
            if all(o["trust"] == states.VERIFIED for o in supporters)
            else vocabulary.BASIS_CORRELATIONAL
        )
        rows.append({
            "kind": vocabulary.KIND_INFERRED,
            "basis": basis,
            "decision": names.get(node_id, "?"),
            "decision_node_id": node_id,
            "sign": sign,
            "support": support,
            "agreement": round(agreement, 4),
            "as_of": as_of,
            "outcomes": supporters,
            "lesson": _sentence(names.get(node_id, "?"), sign, support, basis),
        })

    # Strongest support first, then by decision id so the order is fixed.
    rows.sort(key=lambda row: (-row["support"], row["decision_node_id"]))
    _logger.info(
        "lesson_candidates",
        extra={"group_id": group_id, "decisions": len(decisions),
               "observations": len(observations), "lessons": len(rows)},
    )
    return rows


def _sentence(decision: str, sign: str, support: int, basis: str) -> str:
    """The lesson in words, with its basis in the sentence rather than beside
    it. A caller that renders the text and drops the metadata would otherwise
    present a correlation as a finding, which is the exact confusion the three
    bases exist to prevent."""
    plural = "outcome" if support == 1 else "outcomes"
    if basis == vocabulary.BASIS_VERIFIED_CAUSAL:
        standing = "verified, with a method recorded on each"
    else:
        standing = "evidence-backed but not verified, so this is correlation"
    return (
        f"{decision} has {support} agreeing {sign} {plural} and none against "
        f"({standing})."
    )


def _outcome_edges_among(conn, group_id: str, edge_ids: list[str]) -> set[str]:
    """Which of these edges are live decision-to-outcome observations.

    One cheap query so that `for_query` can return immediately on the common
    case: a scope with no outcomes at all, or a result set that happens to
    contain none, does no lesson work.
    """
    rows = conn.execute(
        f"""SELECT e.id::text
              FROM {GRAPH}."FACT" e
              JOIN {GRAPH}."Node" n ON n.id = e.end_id
             WHERE (e.properties ->> '"group_id"'::agtype) = %s
               AND (e.properties ->> '"t_invalid"'::agtype) IS NULL
               AND (e.properties ->> '"causal_hint"'::agtype) = %s
               AND (n.properties ->> '"type"'::agtype) = %s
               AND e.id::text = ANY(%s)""",
        (group_id, LED_TO, vocabulary.OUTCOME, list(edge_ids)),
    ).fetchall()
    return {str(edge_id) for (edge_id,) in rows}


def for_query(conn, group_id: str, edge_ids: list[str]) -> list[str]:
    """`edge_ids` reordered so outcomes behind a lesson come first.

    Only the ids passed in, and every one of them comes back. This is a
    reordering, not a filter, and it never introduces a candidate - the same
    restraint `salience.rank` keeps, for the same measured reason. The graph
    hop in this project's history could add facts to an answer, and it cost
    -0.142 MRR on the shape it was meant to help least, because anything that
    can add facts can add wrong ones.

    A lesson channel would be the more tempting version of that mistake: it
    knows something genuinely useful and the obvious move is to surface the
    decision's other outcomes alongside whatever was asked. Those outcomes are
    not answers to the question. The worst this can do is move a fact the query
    already matched.
    """
    if not edge_ids:
        return []
    relevant = _outcome_edges_among(conn, group_id, edge_ids)
    if not relevant:
        return list(edge_ids)

    weight: dict[str, float] = {}
    for row in candidates(conn, group_id):
        # A verified causal basis outranks a correlational one at equal
        # support, which is the only place the basis affects an ordering.
        bonus = 0.5 if row["basis"] == vocabulary.BASIS_VERIFIED_CAUSAL else 0.0
        for observation in row["outcomes"]:
            fact_id = observation["fact_id"]
            if fact_id in relevant:
                weight[fact_id] = max(weight.get(fact_id, 0.0), row["support"] + bonus)
    if not weight:
        return list(edge_ids)
    # Stable on the incoming order, so every fact with no lesson behind it -
    # which is most of them - keeps the ranking the content channels gave it.
    return sorted(edge_ids, key=lambda edge_id: -weight.get(str(edge_id), 0.0))


def repeated_failures(conn, group_id: str, *, min_occurrences: int = 2) -> list[dict]:
    """Decisions with at least N live `failure` outcomes.

    Deliberately NOT filtered by trust state, which is the one place this
    module is laxer than `candidates`, and the asymmetry is on purpose. A
    lesson is a conclusion and has to be earned. This is a warning: it claims
    no causality, carries no basis, and says only "this decision has failed
    more than once, go and look". Requiring verification first would hide
    exactly the two cheap occurrences where the pattern was still cheap to
    catch - which is the reason MIN_SUPPORT is 2 and not 3.

    So a caller must not render this as a finding. It is a pointer at
    something to check, and the fields say what it rests on.
    """
    decisions = _decisions(conn, group_id)
    if not decisions:
        return []
    names = dict(decisions)
    observations = _observations(conn, group_id, [node_id for node_id, _ in decisions])

    failures: dict[str, list[dict]] = defaultdict(list)
    for observation in observations:
        if observation["sign"] == vocabulary.FAILURE:
            failures[observation["decision_node_id"]].append(observation)

    rows: list[dict[str, Any]] = [
        {
            "decision": names.get(node_id, "?"),
            "decision_node_id": node_id,
            "occurrences": len(found),
            "outcomes": found,
        }
        for node_id, found in failures.items()
        if len(found) >= min_occurrences
    ]
    rows.sort(key=lambda row: (-row["occurrences"], row["decision_node_id"]))
    return rows
