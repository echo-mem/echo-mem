"""Lessons derived from real outcomes in a real graph.

The arithmetic is the feature: two agreeing outcomes, unanimous, each checked.
These tests push on the three edges of it, because each one is a different way
of being wrong and only one of them looks like a bug.

    below support     one outcome is an anecdote. The store already returns it
    below agreement   outcomes that disagree are a dispute, not a conclusion
    below trust       a pattern across unchecked reports is a pattern in what
                      got reported

And the fourth, which is the one this store's bitemporal model exists for:
retiring a supporting outcome has to weaken the lesson without deleting
anything.

## What is stubbed, and what that means

`echo_memory.trust.verify` is Stream 4's and lands separately. When it is
absent the fixture below installs a stand-in that `lessons` reaches through its
lazy import, so what these tests exercise is lessons' own grouping, counting
and basis logic over a graph written by the real write path. When the real
module is present the fixture uses it instead, and these become end to end.

The stand-in is deliberately not a stub of `lessons`. Nothing here replaces a
query, a threshold or a decision - only the answer to "has anybody checked
this claim", which is the one input that genuinely lives in another stream.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from datetime import UTC, datetime

import pytest
from hash_embedder import HashEmbedder

from echo_memory.events import envelope as envelope_module
from echo_memory.infra.db import GRAPH_NAME as GRAPH
from echo_memory.infra.db import connect
from echo_memory.intelligence import lessons, outcomes, vocabulary
from echo_memory.retrieval.causality import trace_cause
from echo_memory.trust import states

GROUP = "user:ayush:shared"

DECISION = "migrate-payments-to-razorpay"
OTHER_DECISION = "switch-to-postgres"

VERIFY_MODULE = "echo_memory.trust.verify"


@pytest.fixture
def embedder():
    return HashEmbedder()


@pytest.fixture
def verify_state():
    """A way to say "a verifier checked this fact", through Stream 4 or a
    stand-in for it.

    Yields `mark(conn, group_id, fact_id, state)`. See the module docstring for
    why a stand-in is acceptable here and what it does not cover.
    """
    if importlib.util.find_spec(VERIFY_MODULE) is not None:
        verify = importlib.import_module(VERIFY_MODULE)

        def mark_real(conn, group_id, fact_id, state):
            verify.record(
                conn, group_id, fact_id, state=state,
                method="integration-test", verifier="tests",
                verifier_kind=states.SERVICE,
            )

        yield mark_real
        return

    recorded: dict[tuple[str, str], str] = {}
    module = types.ModuleType(VERIFY_MODULE)
    # Only `state_of` - the one thing lessons asks for. A stand-in that also
    # answered questions nothing asks would be a second implementation of
    # Stream 4 rather than a seam in this test.
    module.state_of = (  # type: ignore[attr-defined]
        lambda conn, group_id, edge_id: recorded.get(
            (group_id, str(edge_id)), states.UNVERIFIED
        )
    )
    sys.modules[VERIFY_MODULE] = module

    def mark_stub(conn, group_id, fact_id, state):
        recorded[(group_id, str(fact_id))] = state

    try:
        yield mark_stub
    finally:
        del sys.modules[VERIFY_MODULE]


def _event(event_type: str, event_id: str, **payload) -> envelope_module.Envelope:
    return envelope_module.parse({
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": {"agent": "ci.github", "run_id": "4812"},
        "scope": {"memory": "default", "scope": "shared"},
        "payload": payload,
    })


def _decide(conn, embedder, name: str, event_id: str) -> dict:
    return outcomes.apply(
        conn, GROUP, _event(envelope_module.DECISION_RECORDED, event_id, decision=name),
        embedder,
    )


def _observe(conn, embedder, decision, outcome, sign, event_id, detail="") -> str:
    """Report an outcome and return the fact id of the edge it wrote."""
    payload = {"decision": decision, "outcome": outcome, "sign": sign}
    if detail:
        payload["detail"] = detail
    result = outcomes.apply(
        conn, GROUP,
        _event(envelope_module.OUTCOME_OBSERVED, event_id, **payload),
        embedder,
    )
    assert len(result["edge_ids"]) == 1, result
    return result["edge_ids"][0]


def _two_verified_successes(conn, embedder, verify_state) -> list[str]:
    _decide(conn, embedder, DECISION, "decide-payments-razorpay")
    first = _observe(
        conn, embedder, DECISION, "checkout-error-rate-2026-11", "success",
        "observe-checkout-errors",
    )
    second = _observe(
        conn, embedder, DECISION, "settlement-lag-2026-11", "success",
        "observe-settlement-lag",
    )
    for fact_id in (first, second):
        verify_state(conn, GROUP, fact_id, states.VERIFIED)
    return [first, second]


def test_two_verified_successes_make_one_verified_causal_lesson(
    migrated_db, embedder, verify_state
):
    with connect(migrated_db) as conn:
        supporters = _two_verified_successes(conn, embedder, verify_state)

        rows = lessons.candidates(conn, GROUP)

        assert len(rows) == 1
        row = rows[0]
        assert row["decision"] == DECISION
        assert row["sign"] == vocabulary.SUCCESS
        assert row["support"] == 2
        assert row["agreement"] == 1.0
        assert row["basis"] == vocabulary.BASIS_VERIFIED_CAUSAL
        # `inferred` whatever the basis. This is the string that keeps a derived
        # conclusion out of the verified vocabulary however strong its support.
        assert row["kind"] == vocabulary.KIND_INFERRED
        assert {o["fact_id"] for o in row["outcomes"]} == set(supporters)
        # The outcome's own name, not the composed node name the sign rides in.
        assert {o["outcome"] for o in row["outcomes"]} == {
            "checkout-error-rate-2026-11", "settlement-lag-2026-11",
        }


def test_one_outcome_is_an_anecdote_and_yields_no_lesson(
    migrated_db, embedder, verify_state
):
    """MIN_SUPPORT is 2 and the store already answers the one-outcome case:
    asking about the decision finds the outcome. A lesson claims a pattern."""
    with connect(migrated_db) as conn:
        _decide(conn, embedder, DECISION, "decide-payments-razorpay")
        only = _observe(
            conn, embedder, DECISION, "checkout-error-rate-2026-11", "success",
            "observe-checkout-errors",
        )
        verify_state(conn, GROUP, only, states.VERIFIED)

        assert lessons.candidates(conn, GROUP) == []


def test_outcomes_that_disagree_yield_no_lesson(migrated_db, embedder, verify_state):
    """REQUIRED_AGREEMENT is 1.0. A decision whose outcomes disagree is a case
    a reader should see as disputed, and a "mostly worked" conclusion would
    present it as settled. Both outcomes are verified here, so the only thing
    stopping the lesson is the disagreement."""
    with connect(migrated_db) as conn:
        _decide(conn, embedder, DECISION, "decide-payments-razorpay")
        good = _observe(
            conn, embedder, DECISION, "checkout-error-rate-2026-11", "success",
            "observe-checkout-errors",
        )
        bad = _observe(
            conn, embedder, DECISION, "settlement-lag-2026-11", "failure",
            "observe-settlement-lag",
        )
        third = _observe(
            conn, embedder, DECISION, "refund-latency-2026-11", "success",
            "observe-refund-latency",
        )
        for fact_id in (good, bad, third):
            verify_state(conn, GROUP, fact_id, states.VERIFIED)

        # Two successes against one failure clears MIN_SUPPORT and does not
        # clear unanimity, which is the case the threshold exists for.
        assert lessons.candidates(conn, GROUP) == []


def test_mixed_outcomes_do_not_dilute_and_do_not_support(
    migrated_db, embedder, verify_state
):
    """SIGN_VALUE scores mixed and unknown at zero, so they are evidence the
    decision was taken and say nothing about how it went. Ten of them must not
    add up to a weak lesson, and one of them must not break a real one."""
    with connect(migrated_db) as conn:
        supporters = _two_verified_successes(conn, embedder, verify_state)
        muddy = _observe(
            conn, embedder, DECISION, "refund-latency-2026-11", "mixed",
            "observe-refund-latency",
        )
        verify_state(conn, GROUP, muddy, states.VERIFIED)

        rows = lessons.candidates(conn, GROUP)
        assert len(rows) == 1
        assert rows[0]["support"] == 2
        assert rows[0]["agreement"] == 1.0
        assert {o["fact_id"] for o in rows[0]["outcomes"]} == set(supporters)


def test_an_unverified_supporter_is_not_counted_at_all(migrated_db, embedder, verify_state):
    """Unchecked outcomes do not support a lesson AND do not dispute one: they
    are dropped before any counting. So one verified success plus one
    unverified success is support of 1, not 2, and no lesson."""
    with connect(migrated_db) as conn:
        _decide(conn, embedder, DECISION, "decide-payments-razorpay")
        checked = _observe(
            conn, embedder, DECISION, "checkout-error-rate-2026-11", "success",
            "observe-checkout-errors",
        )
        _observe(
            conn, embedder, DECISION, "settlement-lag-2026-11", "success",
            "observe-settlement-lag",
        )
        verify_state(conn, GROUP, checked, states.VERIFIED)

        assert lessons.candidates(conn, GROUP) == []


def test_an_evidence_backed_supporter_keeps_the_basis_correlational(
    migrated_db, embedder, verify_state
):
    """Evidence is not proof. A deployment log saying the deploy happened does
    not establish that the decision behind it was right, so one merely
    evidence-backed supporter makes the whole row correlational. Promoting on a
    majority would mean the strongest label in this system could be reached
    without anybody having verified anything."""
    with connect(migrated_db) as conn:
        _decide(conn, embedder, DECISION, "decide-payments-razorpay")
        strong = _observe(
            conn, embedder, DECISION, "checkout-error-rate-2026-11", "success",
            "observe-checkout-errors",
        )
        weaker = _observe(
            conn, embedder, DECISION, "settlement-lag-2026-11", "success",
            "observe-settlement-lag",
        )
        verify_state(conn, GROUP, strong, states.VERIFIED)
        verify_state(conn, GROUP, weaker, states.EVIDENCE_BACKED)

        rows = lessons.candidates(conn, GROUP)
        assert len(rows) == 1
        assert rows[0]["support"] == 2
        assert rows[0]["basis"] == vocabulary.BASIS_CORRELATIONAL
        assert "correlation" in rows[0]["lesson"]


def test_superseding_a_supporter_weakens_the_lesson_and_deletes_nothing(
    migrated_db, embedder, verify_state
):
    """The bitemporal model's whole purpose, read through a derived
    conclusion.

    A corrected report for one of the two outcomes supersedes the first: the
    original edge gets a t_invalid rather than a delete, and a new edge takes
    its place. The lesson weakens immediately, because `candidates` only reads
    live edges and the replacement has not been verified.

    A stored lesson could not behave this way. It would sit there asserting a
    pattern over an outcome that is now history, and nothing in it would say
    so.
    """
    with connect(migrated_db) as conn:
        supporters = _two_verified_successes(conn, embedder, verify_state)
        assert lessons.candidates(conn, GROUP)[0]["support"] == 2

        replacement = _observe(
            conn, embedder, DECISION, "settlement-lag-2026-11", "success",
            "observe-settlement-lag-corrected",
            detail="Re-measured against the settlement report rather than the log.",
        )
        assert replacement not in supporters, "the correction reused the old edge"

        assert lessons.candidates(conn, GROUP) == []

        # Nothing was deleted. The retired edge is still there, still readable
        # as history, carrying the end of its validity.
        rows = conn.execute(
            f"""SELECT (properties ->> '"t_invalid"'::agtype)
                  FROM {GRAPH}."FACT" WHERE id = %s::text::graphid""",
            (supporters[1],),
        ).fetchall()
        assert len(rows) == 1, "the superseded edge was removed from the graph"
        assert rows[0][0] is not None, "the superseded edge is still live"

        # And verifying the replacement brings the lesson back, which is what
        # makes this a weakening rather than a contradiction.
        verify_state(conn, GROUP, replacement, states.VERIFIED)
        restored = lessons.candidates(conn, GROUP)
        assert len(restored) == 1
        assert restored[0]["support"] == 2


def test_repeated_failures_finds_a_decision_that_failed_twice(migrated_db, embedder):
    """Not filtered by trust state, and that asymmetry with `candidates` is
    deliberate. This claims no causality and carries no basis; it says "go and
    look". Requiring verification first would hide exactly the two cheap
    occurrences where the pattern was still cheap to catch."""
    with connect(migrated_db) as conn:
        _decide(conn, embedder, OTHER_DECISION, "decide-postgres-switch")
        _observe(
            conn, embedder, OTHER_DECISION, "write-latency-2026-10", "failure",
            "observe-write-latency",
        )
        _observe(
            conn, embedder, OTHER_DECISION, "connection-exhaustion-2026-11", "failure",
            "observe-connection-exhaustion",
        )
        _decide(conn, embedder, DECISION, "decide-payments-razorpay")
        _observe(
            conn, embedder, DECISION, "checkout-error-rate-2026-11", "failure",
            "observe-checkout-errors",
        )

        found = lessons.repeated_failures(conn, GROUP)

        assert [row["decision"] for row in found] == [OTHER_DECISION]
        assert found[0]["occurrences"] == 2
        # One failure is not a pattern, so the other decision is absent even
        # though it has a failure of its own.
        assert lessons.repeated_failures(conn, GROUP, min_occurrences=1) != found


def test_for_query_reorders_and_never_introduces_an_id(
    migrated_db, embedder, verify_state
):
    """A reordering, not a filter and not an expansion. The graph hop in this
    project's history could add facts to an answer and cost -0.142 MRR on the
    shape it was meant to help least; a lesson channel is the more tempting
    version of the same mistake, because it knows something true about facts
    the query did not ask for."""
    with connect(migrated_db) as conn:
        supporters = _two_verified_successes(conn, embedder, verify_state)
        action = outcomes.apply(
            conn, GROUP,
            _event(
                envelope_module.ACTION_EXECUTED, "act-razorpay-keys",
                action="rotate-razorpay-keys", decision=DECISION,
            ),
            embedder,
        )
        unrelated = action["edge_ids"][0]

        given = [unrelated, *supporters]
        ranked = lessons.for_query(conn, GROUP, given)

        assert set(ranked) == set(given), "for_query changed which ids came back"
        assert len(ranked) == len(given)
        assert set(ranked[:2]) == set(supporters)
        assert ranked[-1] == unrelated

        # An empty input is an empty answer, not an excuse to go looking.
        assert lessons.for_query(conn, GROUP, []) == []


def test_a_decision_and_its_outcome_are_walkable_by_trace_cause(migrated_db, embedder):
    """The reason an outcome is a node and an edge rather than a table: the
    traversal that answers "why" already exists and already normalises the
    direction. Nothing in the learning layer reimplements it."""
    with connect(migrated_db) as conn:
        _decide(conn, embedder, DECISION, "decide-payments-razorpay")
        _observe(
            conn, embedder, DECISION, "checkout-error-rate-2026-11", "success",
            "observe-checkout-errors",
        )

        traced = trace_cause(conn, GROUP, DECISION, embedder)

        assert [a["name"] for a in traced["anchors"]] == [DECISION]
        effects = [link["fact"] for chain in traced["effects"] for link in chain]
        assert any("checkout-error-rate-2026-11" in fact for fact in effects)
        # The sign is in the fact text as well as in the node name, so a reader
        # of the chain sees how it went without having to parse a name.
        assert any("success" in fact for fact in effects)


def test_a_scope_with_no_decisions_yields_nothing_and_asks_nothing(migrated_db):
    """The common case for every existing customer, and it has to cost one
    indexed query rather than a scan."""
    with connect(migrated_db) as conn:
        assert lessons.candidates(conn, GROUP) == []
        assert lessons.repeated_failures(conn, GROUP) == []
