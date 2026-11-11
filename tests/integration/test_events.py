"""The machine door and the drain behind it, against a real graph.

What these assert is the queue's contract rather than what any one event
means. Four properties, each of which is a thing a reporter in the field will
rely on without being told:

    a retry is free              the whole reason event_id exists
    an applied event says what it wrote     so a replay can be reconciled
    a bad event does not take the batch     one customer's malformed payload
                                 must not stop the queue for everyone else
    an empty drain does nothing   it runs on a timer, mostly over nothing
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial

import pytest
from hash_embedder import HashEmbedder

from echo_memory.events import envelope as envelope_module
from echo_memory.events import inbox
from echo_memory.infra.db import connect
from echo_memory.intelligence import outcomes

GROUP = "user:ayush:shared"


@pytest.fixture
def embedder():
    return HashEmbedder()


def _event(event_type: str, event_id: str, **payload) -> envelope_module.Envelope:
    return envelope_module.parse({
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": {"agent": "ci.github", "run_id": "4812"},
        "scope": {"memory": "default", "scope": "shared"},
        "payload": payload,
    })


def _applier(embedder):
    """What the drain is handed: `apply(conn, group_id, envelope)`.

    The embedder is bound here rather than threaded through the inbox,
    deliberately. The inbox knows about rows and locks; it has no business
    holding a model, and keeping its applier signature to three arguments is
    what stops every future dependency of the write side from becoming a
    parameter of the queue.
    """
    return partial(outcomes.apply, embedder=embedder)


def _row(conn, event_id: str) -> dict:
    row = conn.execute(
        """SELECT state, attempts, error, applied_edge_ids, processed_at
             FROM public.event_inbox WHERE event_id = %s""",
        (event_id,),
    ).fetchone()
    assert row is not None, f"no inbox row for {event_id!r}"
    return {"state": row[0], "attempts": row[1], "error": row[2],
            "edge_ids": row[3], "processed_at": row[4]}


def test_the_same_event_id_twice_is_one_row_and_not_an_error(migrated_db):
    """A CI system that times out cannot know whether the first call landed.
    Its only safe behaviour is to retry, so the retry has to be free - and it
    has to come back saying the event is held, not saying something failed."""
    with connect(migrated_db) as conn:
        envelope = _event(
            envelope_module.DEPLOYMENT_COMPLETED, "deploy-ab12cd34-prod",
            decision="migrate-payments", outcome="checkout-errors", sign="success",
        )
        first = inbox.enqueue(conn, GROUP, envelope)
        second = inbox.enqueue(conn, GROUP, envelope)

        assert first == {"event_id": envelope.event_id, "accepted": True,
                         "duplicate": False}
        assert second == {"event_id": envelope.event_id, "accepted": False,
                          "duplicate": True}
        count = conn.execute(
            "SELECT count(*) FROM public.event_inbox WHERE event_id = %s",
            (envelope.event_id,),
        ).fetchone()[0]
        assert count == 1


def test_a_replay_does_not_reopen_an_event_that_already_applied(migrated_db, embedder):
    """The dangerous half of idempotency. If the second INSERT reset the state
    to pending, a captured request replayed after the drain had run would be
    applied a second time - and `ON CONFLICT DO NOTHING` is what stops that,
    not just the duplicate count."""
    with connect(migrated_db) as conn:
        envelope = _event(
            envelope_module.DECISION_RECORDED, "decide-payments-razorpay",
            decision="migrate-payments-to-razorpay",
        )
        inbox.enqueue(conn, GROUP, envelope)
        inbox.drain(conn, apply=_applier(embedder))
        assert _row(conn, envelope.event_id)["state"] == "applied"

        inbox.enqueue(conn, GROUP, envelope)
        assert _row(conn, envelope.event_id)["state"] == "applied"
        assert inbox.pending(conn) == []


def test_drain_applies_what_is_pending_and_records_the_edges(migrated_db, embedder):
    with connect(migrated_db) as conn:
        decision = _event(
            envelope_module.DECISION_RECORDED, "decide-payments-razorpay",
            decision="migrate-payments-to-razorpay",
        )
        outcome = _event(
            envelope_module.DEPLOYMENT_COMPLETED, "deploy-ab12cd34-prod",
            decision="migrate-payments-to-razorpay",
            outcome="checkout-error-rate-2026-11", sign="success",
        )
        inbox.enqueue(conn, GROUP, decision)
        inbox.enqueue(conn, GROUP, outcome)
        assert len(inbox.pending(conn)) == 2

        result = inbox.drain(conn, apply=_applier(embedder))

        assert result["applied"] == [decision.event_id, outcome.event_id]
        assert result["rejected"] == []
        assert inbox.pending(conn) == []

        # The decision event writes a node and deliberately no fact, so it has
        # no edges. The outcome event writes the edge, and the row has to say
        # which one: without it a replay cannot be reconciled against what the
        # first attempt did.
        assert _row(conn, decision.event_id)["edge_ids"] == []
        edges = _row(conn, outcome.event_id)["edge_ids"]
        assert len(edges) == 1
        stored = conn.execute(
            """SELECT count(*) FROM public.fact_embedding
                WHERE edge_id = %s::text::graphid""",
            (edges[0],),
        ).fetchone()[0]
        assert stored == 1, "the edge the row names is not in the graph"


def test_a_failing_apply_is_rejected_with_its_error_and_counted(migrated_db, embedder):
    """One reporter's inapplicable event must not stop the queue, and the
    reason has to survive in the row. A rejection with no error text leaves the
    reporter's author with nothing to act on but silence, which is how a
    broken integration keeps sending the same broken event."""
    with connect(migrated_db) as conn:
        # Well formed at the door - `parse` accepts it - and inapplicable at
        # the write, because an outcome event needs a decision to hang off.
        # That is exactly the class of failure the `rejected` state is for.
        bad = _event(
            envelope_module.TEST_COMPLETED, "tests-run-91ab77ff",
            outcome="suite-green", sign="success",
        )
        good = _event(
            envelope_module.DECISION_RECORDED, "decide-postgres-switch",
            decision="switch-to-postgres",
        )
        inbox.enqueue(conn, GROUP, bad)
        inbox.enqueue(conn, GROUP, good)

        result = inbox.drain(conn, apply=_applier(embedder))

        assert result["applied"] == [good.event_id]
        assert [r["event_id"] for r in result["rejected"]] == [bad.event_id]

        row = _row(conn, bad.event_id)
        assert row["state"] == "rejected"
        assert row["attempts"] == 1
        assert "decision" in row["error"]
        assert row["processed_at"] is not None

        # The savepoint did its job: the event after the failure was applied,
        # rather than being lost to a transaction the first one aborted.
        assert _row(conn, good.event_id)["state"] == "applied"


def test_an_unknown_payload_shape_names_the_missing_field(migrated_db, embedder):
    """The reporter is a program. It will keep sending what it sent until
    somebody reads a message saying which field was wrong."""
    with connect(migrated_db) as conn:
        envelope = _event(
            envelope_module.OUTCOME_OBSERVED, "observe-missing-sign",
            decision="switch-to-postgres", outcome="write-latency",
        )
        inbox.enqueue(conn, GROUP, envelope)
        inbox.drain(conn, apply=_applier(embedder))
        assert "payload.sign" in _row(conn, envelope.event_id)["error"]


def test_drain_is_a_no_op_on_an_empty_inbox(migrated_db, embedder):
    """It runs on a timer, so most of the time it runs over nothing. Doing
    nothing has to be free and has to be silent."""
    with connect(migrated_db) as conn:
        assert inbox.drain(conn, apply=_applier(embedder)) == {
            "applied": [], "rejected": [], "processed": 0,
        }


def test_drain_without_an_applier_is_refused_rather_than_silently_empty(migrated_db):
    """A drain that quietly did nothing because nobody passed it an applier
    would look exactly like a drain over an empty inbox, and the backlog would
    grow with every run reporting success."""
    with connect(migrated_db) as conn, pytest.raises(inbox.InboxError, match="apply"):
        inbox.drain(conn)


def test_verification_events_are_refused_without_the_capability(migrated_db, embedder):
    """A reporter credential may attach evidence and may not say the evidence
    was sufficient. `verified` is the only state the lesson layer counts as
    causal, so a CI token able to reach this could mark its own deploys as
    verified causes of their own success."""
    with connect(migrated_db) as conn:
        envelope = _event(
            envelope_module.CLAIM_VERIFIED, "verify-claim-7f2a91bb",
            fact_id="1", state="verified", method="manual-review",
        )
        inbox.enqueue(conn, GROUP, envelope)
        inbox.drain(conn, apply=_applier(embedder))
        error = _row(conn, envelope.event_id)["error"]
        assert "allow_verify" in error


def test_a_verifier_may_not_assert_an_advisory_state(migrated_db, embedder):
    """Even with the capability granted. The advisory states are computed from
    what the store can see, and an event channel able to assert them would be a
    way to mark a thousand facts `stale` - a denial of service on your own
    memory, dressed as a verification. Checked before anything reaches the
    trust package, so the refusal does not depend on which build this is."""
    with connect(migrated_db) as conn:
        envelope = _event(
            envelope_module.CLAIM_VERIFIED, "verify-claim-stale-7f2a",
            fact_id="1", state="stale", method="manual-review",
        )
        with pytest.raises(outcomes.OutcomeError, match="advisory"):
            outcomes.apply(conn, GROUP, envelope, embedder, allow_verify=True)


def test_an_unknown_verifier_kind_is_refused(migrated_db, embedder):
    """Human, service or rule. A fourth value would be a verifier nothing
    downstream can weigh, recorded as though it had been weighed."""
    with connect(migrated_db) as conn:
        envelope = _event(
            envelope_module.CLAIM_VERIFIED, "verify-claim-bykind-7f2a",
            fact_id="1", state="verified", method="manual-review",
            verifier_kind="committee",
        )
        with pytest.raises(outcomes.OutcomeError, match="verifier_kind"):
            outcomes.apply(conn, GROUP, envelope, embedder, allow_verify=True)


def test_an_unknown_state_is_refused_by_mark(migrated_db):
    """The CHECK constraint would catch this too. Catching it in Python keeps
    the message about the four states rather than about a constraint name."""
    with connect(migrated_db) as conn, pytest.raises(
        inbox.InboxError, match="unknown inbox state"
    ):
        inbox.mark(conn, "deploy-ab12cd34-prod", "half-applied")
