"""All six capabilities in one scenario, and the negatives that matter more.

Each stream tests itself. What no stream can test is whether the six compose:
whether an outcome reported by a machine becomes a lesson that changes the
order of a cross scope answer, whether the budget that trims that answer can
be made to admit a scope nobody authorised, and whether a bundle from another
organisation stays out of all of it until somebody says otherwise.

The positive path is one test so that a break anywhere in the chain is one
failure rather than six. The negatives are separate, because each is a distinct
promise and the reason to have them is to know WHICH one broke.

Nothing here uses the hosted service. Every capability is exercised through the
engine alone, which is the claim §8 of the brief makes: a self-hosted
deployment gets all six.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from echo_memory.events import envelope as events
from echo_memory.events import inbox
from echo_memory.exchange import bundle as bundle_format
from echo_memory.exchange import importer, redact, sign
from echo_memory.infra.db import connect
from echo_memory.ingestion.embeddings import LocalEmbedder
from echo_memory.ingestion.write_episode import write_episode
from echo_memory.intelligence import lessons, outcomes
from echo_memory.retrieval import routing
from echo_memory.retrieval.across import query_across
from echo_memory.retrieval.context import assemble
from echo_memory.trust import evidence, states, verify

ACCOUNT = "acct-platform"
DEV = f"acct:{ACCOUNT}:memory:dev:shared"
PROD = f"acct:{ACCOUNT}:memory:prod:shared"
SECRETS = f"acct:{ACCOUNT}:memory:secrets:shared"

DECISION = "migrate-payments-to-razorpay"
SECRET = "the production signing key rotates on the first of every month"

# A deterministic signing key, so the bundle half does not depend on key
# material in the environment. 32 bytes of hex: ed25519 when `cryptography` is
# installed, HMAC otherwise, and the test does not care which.
EXCHANGE_KEY_ID = "e2e"
EXCHANGE_KEY = "11" * 32


def _write(conn, embedder, group_id, source, target, fact, *, session="e2e"):
    out = write_episode(
        conn, group_id, session,
        [{"name": source, "type": "tool"}, {"name": target, "type": "tool"}],
        [{"source": source, "target": target, "relation_type": "uses",
          "fact": fact, "confidence": "extracted"}],
        {}, embedder,
    )
    created = out.get("edges_created") or []
    return str(created[0]) if created else None


def _edges_applied(conn, event_ids) -> list[str]:
    """The graph edges the drain recorded writing for these events.

    The inbox stores them so that a replay can be reconciled against what
    actually landed, which makes it also the only honest way for a test to name
    the edge an event produced.
    """
    rows = conn.execute(
        """SELECT event_id, applied_edge_ids FROM public.event_inbox
            WHERE event_id = ANY(%s) AND state = 'applied'
            ORDER BY event_id""",
        (list(event_ids),),
    ).fetchall()
    return [str(e) for _, edges in rows for e in (edges or [])]


def _envelope(event_type, event_id, **payload):
    return events.Envelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        actor=events.Actor(agent="ci-reporter", run_id="run-1",
                           credential_id="key-ci"),
        scope=events.Scope(memory="prod", scope="shared"),
        payload=payload,
    )


#: The exporter's signing environment, passed explicitly rather than set on the
#: process. `sign.verify` takes its anchors as an argument for the reason its
#: docstring gives - a verifier reading trust anchors from the same place the
#: bundle came from is not verifying anything - and a test that leaned on
#: ambient environment would be quietly testing the thing that is not allowed.
EXPORTER_ENV = {
    sign.KEYS_ENV: f"{EXCHANGE_KEY_ID}:{EXCHANGE_KEY}",
    sign.KEY_ID_ENV: EXCHANGE_KEY_ID,
}


@pytest.fixture
def exchange_keys():
    """The importer's trust anchor for the exporter above, and nothing else."""
    return {EXCHANGE_KEY_ID: sign.trust_anchor(EXCHANGE_KEY_ID, env=EXPORTER_ENV)}


def _export(conn, group_id, *, memory, account=ACCOUNT, deployment="deploy-a"):
    """Build a bundle and sign it, which are two steps on purpose: redaction
    happens inside build and signing happens after, so a signature covers what
    was actually sent rather than something redacted afterwards."""
    manifest, blob = bundle_format.build(
        conn, group_id, deployment_id=deployment, account=account,
        memory=memory, policy=redact.POLICIES["minimal"],
    )
    signed = manifest.signed(sign.sign(manifest.unsigned(), env=EXPORTER_ENV))
    return signed, blob


# --------------------------------------------------------------- the positive

def test_six_capabilities_compose(migrated_db, exchange_keys):
    """One scenario, in the order a real one happens.

    A decision is recorded. A machine reports its outcome twice with one
    idempotency key. A reviewer verifies it. A question that names no memory
    is routed, fused across the two the caller may read, trimmed to a budget,
    and the lesson derived from the verified outcome is available to rank it.
    An export of one memory is imported by somebody else and sits where no
    read reaches.
    """
    conn = connect(migrated_db)
    embedder = LocalEmbedder()

    # --- the scopes, including one the caller will never be given
    _write(conn, embedder, DEV, "Deploy", "Staging",
           "dev deploys run against staging before anything else")
    _write(conn, embedder, PROD, "Deploy", "Release",
           "prod deploys run only from the release branch")
    _write(conn, embedder, SECRETS, "Key", "Rotation", SECRET)

    # --- A: a decision, then its outcome reported by a machine, twice
    first = inbox.enqueue(conn, PROD, _envelope(
        events.DECISION_RECORDED, "decide-razorpay-001", decision=DECISION,
        detail="card success rate was 71 percent on the old provider",
    ))
    assert first["accepted"] and not first["duplicate"]

    for event_id, subject in (("observe-errors-001", "checkout-error-rate-2026-11"),
                              ("observe-settle-001", "settlement-lag-2026-11")):
        inbox.enqueue(conn, PROD, _envelope(
            events.OUTCOME_OBSERVED, event_id, decision=DECISION,
            outcome=subject, sign="success",
            detail="measured over the first full billing period",
        ))
    # The retry a CI system cannot avoid: same key, no second effect.
    again = inbox.enqueue(conn, PROD, _envelope(
        events.OUTCOME_OBSERVED, "observe-errors-001", decision=DECISION,
        outcome="checkout-error-rate-2026-11", sign="success",
    ))
    assert again["duplicate"] is True

    drained = inbox.drain(
        conn, apply=lambda c, g, env: outcomes.apply(c, g, env, embedder)
    )
    assert drained["applied"], drained
    assert not drained["rejected"], drained["rejected"]
    assert len(drained["applied"]) == 3, "one decision and two outcomes, once each"

    # --- D: evidence, then a human verifier
    # Read the edges the drain recorded writing rather than searching for them
    # by text. A text search would make this test depend on the ranker, which
    # is a different thing being tested two sections further down, and it was
    # how the first version of this test silently verified the decision edge
    # instead of the outcome edges and then found no lesson.
    outcome_ids = _edges_applied(
        conn, ("observe-errors-001", "observe-settle-001"))
    assert len(outcome_ids) == 2, outcome_ids

    artefact = evidence.record(
        conn, PROD, "deployment", uri="https://ci.example/run/8821",
        reporter_agent="ci-reporter", reporter_run="run-1",
    )
    evidence.link(conn, PROD, outcome_ids[0], artefact)
    assert evidence.for_fact(conn, PROD, outcome_ids[0])

    for fact_id in outcome_ids:
        verify.record(
            conn, PROD, fact_id, states.VERIFIED,
            verifier_kind=states.HUMAN, verifier_id="reviewer@example.com",
            method="read the billing period report", policy_id="payments-v1",
        )
        assert verify.state_of(conn, PROD, fact_id) == states.VERIFIED

    # --- B + E: a question naming no memory, routed and fused under a budget
    authorised = [DEV, PROD]
    answer = assemble(
        conn, authorised, "how do production deploys work", 10, embedder,
        token_budget=400, max_scopes=3,
    )
    assert answer["facts"], answer
    assert all(f["source"] in authorised for f in answer["facts"])
    assert SECRET not in [f["fact"] for f in answer["facts"]]
    block = answer["routing"]
    assert "chosen" in block and "rejected" in block
    assert block["budget"]["estimated_tokens"] <= 400

    # --- A again: the lesson the verified outcomes support
    derived = lessons.candidates(conn, PROD)
    assert derived, "two verified successes on one decision must support a lesson"
    row = next(r for r in derived if DECISION in r["decision"])
    assert row["kind"] == "inferred", "a derived conclusion is never verified"
    assert row["basis"] == "verified_causal"
    assert row["support"] >= 2

    # The lesson channel reorders, and only over what the query already found.
    found = [f["fact_id"] for f in
             query_across(conn, [PROD], "razorpay migration outcome", 10, embedder)["facts"]]
    reordered = lessons.for_query(conn, PROD, found)
    assert set(reordered) <= set(found), "a channel may reorder, never introduce"

    # --- F: export dev, import it elsewhere, and it lands out of reach
    manifest, blob = _export(conn, DEV, memory="dev")
    parsed_manifest, parsed_facts = bundle_format.parse(
        bundle_format.canonical(manifest.to_dict()), blob
    )
    assert parsed_facts, "the bundle must carry dev's facts"

    landed = importer.import_bundle(
        conn, manifest=parsed_manifest, facts=parsed_facts,
        account="acct-other", memory="shared-in", embedder=embedder,
        trusted_keys=exchange_keys, deployment_id="deploy-b",
    )
    quarantine = landed["group_id"]
    assert ":import:" in quarantine

    assert importer.readable_scopes(conn, "acct-other", "shared-in") == [], (
        "a quarantined bundle is readable by nobody until a verifier promotes it"
    )
    importer.promote(conn, parsed_manifest.bundle_id, verifier_id="reviewer@example.com")
    assert importer.readable_scopes(conn, "acct-other", "shared-in") == [quarantine]


# --------------------------------------------------------------- the negatives

def test_an_unauthorised_memory_never_appears_at_any_budget(migrated_db):
    """The question is the secret's own words, which is the strongest pull a
    search can get, and the budget is swept because a smaller budget must mean
    fewer facts and never other facts."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, SECRETS, "Key", "Rotation", SECRET)
    _write(conn, embedder, DEV, "Deploy", "Staging", "dev deploys run against staging")

    for budget in (None, 1, 20, 200, 5000):
        answer = assemble(conn, [DEV], SECRET, 10, embedder, token_budget=budget)
        texts = [f["fact"] for f in answer["facts"]]
        assert SECRET not in texts, f"leaked at budget {budget}"
        assert all("signing key" not in t for t in texts)


def test_a_reporter_cannot_verify_its_own_claim(migrated_db):
    """events.envelope.REQUIRES_VERIFY names the event type a reporter may not
    send, and outcomes.apply refuses it unless the caller passes allow_verify.
    A reporter credential never reaches the path that sets it."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    edge = _write(conn, embedder, PROD, "Deploy", "Release", "prod deploys from release")

    claim = _envelope(
        events.CLAIM_VERIFIED, "verify-self-001", fact_id=edge,
        state=states.VERIFIED, method="I checked it myself",
    )
    with pytest.raises(Exception) as caught:
        outcomes.apply(conn, PROD, claim, embedder)
    assert "allow_verify" in str(caught.value) or "capab" in str(caught.value).lower()

    assert verify.state_of(conn, PROD, edge) == states.UNVERIFIED


def test_a_rejected_fact_is_absent_from_a_cross_scope_answer(migrated_db):
    """Suppression is authoritative, so it must survive the fan out and the
    fusion rather than only applying to a single scope read."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    bad = _write(conn, embedder, PROD, "Deploy", "Main", "prod deploys from main")
    good = _write(conn, embedder, DEV, "Deploy", "Staging", "dev deploys from staging")

    verify.record(
        conn, PROD, bad, states.REJECTED,
        verifier_kind=states.HUMAN, verifier_id="reviewer", method="manual review",
    )

    answer = query_across(conn, [DEV, PROD], "where do deploys come from", 10,
                          embedder, trust=True)
    returned = {f["fact_id"] for f in answer["facts"]}
    assert bad not in returned
    assert good in returned


def test_an_unverified_outcome_supports_no_lesson(migrated_db):
    """A pattern across claims nobody checked is a pattern in what got
    reported, not in what happened."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    inbox.enqueue(conn, PROD, _envelope(
        events.DECISION_RECORDED, "d-unver-001", decision=DECISION))
    for i, subject in enumerate(("a-metric", "b-metric")):
        inbox.enqueue(conn, PROD, _envelope(
            events.OUTCOME_OBSERVED, f"observe-unverified-{i:03d}", decision=DECISION,
            outcome=subject, sign="success"))
    inbox.drain(conn, apply=lambda c, g, env: outcomes.apply(c, g, env, embedder))

    assert lessons.candidates(conn, PROD) == []


def test_superseding_a_supporting_outcome_weakens_the_lesson(migrated_db):
    """Derived rather than stored is what makes this free. Nothing recomputes
    anything: the supporting edge gains a t_invalid and the next read sees
    less."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    inbox.enqueue(conn, PROD, _envelope(
        events.DECISION_RECORDED, "d-sup-001", decision=DECISION))
    for i, subject in enumerate(("m-one", "m-two")):
        inbox.enqueue(conn, PROD, _envelope(
            events.OUTCOME_OBSERVED, f"observe-superseded-{i:03d}", decision=DECISION,
            outcome=subject, sign="success"))
    drained = inbox.drain(
        conn, apply=lambda c, g, env: outcomes.apply(c, g, env, embedder))
    assert len(drained["applied"]) == 3

    observed = _edges_applied(
        conn, ("observe-superseded-000", "observe-superseded-001"))
    assert len(observed) == 2, observed
    for fact_id in observed:
        verify.record(
            conn, PROD, fact_id, states.VERIFIED, verifier_kind=states.HUMAN,
            verifier_id="reviewer", method="manual review")

    before = lessons.candidates(conn, PROD)
    assert before, "precondition: a lesson exists before anything is superseded"
    support_before = max(r["support"] for r in before)

    # Supersede one supporting outcome the way the store always does it.
    conn.execute(
        """SELECT * FROM cypher('echo_memory', $$
               MATCH ()-[e:FACT]->() WHERE id(e) = $eid
               SET e.t_invalid = 1
               RETURN id(e)
           $$, %s) AS (id agtype)""",
        ('{"eid": %d}' % int(observed[0]),),
    ).fetchone()

    after = lessons.candidates(conn, PROD)
    support_after = max((r["support"] for r in after), default=0)
    assert support_after < support_before, (
        "a lesson whose support was superseded must weaken with no job run"
    )


def test_a_bundle_with_a_bad_signature_writes_nothing(migrated_db, exchange_keys):
    """Refused before the first row, so a rejection cannot leave facts behind
    that nothing will clean up."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, DEV, "Deploy", "Staging", "dev deploys run against staging")

    manifest, blob = _export(conn, DEV, memory="dev")
    _, facts = bundle_format.parse(
        bundle_format.canonical(manifest.to_dict()), blob)

    before = conn.execute(
        "SELECT count(*) FROM public.fact_embedding").fetchone()[0]

    with pytest.raises(Exception):
        importer.import_bundle(
            conn, manifest=manifest, facts=facts, account="acct-other",
            memory="shared-in", embedder=embedder,
            trusted_keys={
                "somebody-else": sign.trust_anchor("somebody-else", env={
                    sign.KEYS_ENV: "somebody-else:" + "22" * 32,
                    sign.KEY_ID_ENV: "somebody-else",
                }),
            },
            deployment_id="deploy-b",
        )

    after = conn.execute(
        "SELECT count(*) FROM public.fact_embedding").fetchone()[0]
    assert after == before, "a refused bundle must write nothing at all"


def test_routing_never_widens_past_what_it_was_given(migrated_db):
    """Routing chooses among the scopes it was handed. It cannot discover one,
    which is the property that makes it safe to run before authorization has
    been re-checked."""
    conn = connect(migrated_db)
    embedder = LocalEmbedder()
    _write(conn, embedder, SECRETS, "Key", "Rotation", SECRET)
    _write(conn, embedder, DEV, "Deploy", "Staging", "dev deploys run against staging")

    scores = routing.score_scopes(conn, [DEV], SECRET)
    assert {s.group_id for s in scores} == {DEV}
    chosen, _ = routing.select(conn, [DEV], SECRET)
    assert SECRETS not in chosen
